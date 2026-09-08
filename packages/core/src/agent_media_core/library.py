"""Audiobook library: resolve YouTube URIs to locally-cached files.

A datacenter IP is what makes this necessary: YouTube blocks them
(SABR/PO-token), so a hub hosted in one can neither stream nor download
YouTube directly. (This began on an IONOS host and is equally true of the
current Hetzner one.) The book channel's
mpv therefore cannot play `yt:`/youtube URLs at all. Instead we acquire the
audio on the phone (residential IP) via `audiobook-fetch`, sync it into a
local library, and the book channel plays the *local file*.

This module is the glue: detect a YouTube URI, map it to its cached file (by
the video id yt-dlp embeds in the filename, ``... [<id>].<ext>``), and — on a
miss — kick off `audiobook-fetch` to acquire it.

See memory: project_youtube_acquisition_phone.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Sequence
import urllib.request


# Bare 11-char YouTube id from the common URL shapes (after any `yt:` strip).
_YT_ID = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/)|youtu\.be/)"
    r"([0-9A-Za-z_-]{11})"
)
_YT_HOST = re.compile(r"^https?://(?:[\w-]+\.)?(?:youtube\.com|youtu\.be)/", re.I)
_YT_LIST = re.compile(r"[?&]list=([0-9A-Za-z_-]+)")


def _suffix(target=None) -> str:
    """Env-var suffix for a target (accepts a Target, a name str, or None)."""
    name = getattr(target, "name", target) or ""
    return str(name).upper().replace("-", "_")


def _tenv(base: str, target=None) -> Optional[str]:
    """Per-target override of an env var, falling back to the global.

    e.g. MEDIA_AUDIOBOOK_ABS_DIR_ALICE -> MEDIA_AUDIOBOOK_ABS_DIR. Mirrors the
    existing MEDIA_SPEECH_PLAYOUT_MS_<TARGET> convention in submit.py.
    """
    if target is not None:
        sfx = _suffix(target)
        if sfx:
            v = os.environ.get(f"{base}_{sfx}")
            if v:
                return v
    return os.environ.get(base)


def library_dir(target=None) -> Path:
    """Where synced audiobook files live (override: MEDIA_AUDIOBOOK_LIB).

    Per-target override: MEDIA_AUDIOBOOK_LIB_<TARGET>.
    """
    override = _tenv("MEDIA_AUDIOBOOK_LIB", target)
    if override:
        return Path(override).expanduser()
    return Path.home() / "media" / "audiobooks"


def abs_import_dir(target=None) -> Path:
    """Host directory Audiobookshelf scans for books.

    Override with MEDIA_AUDIOBOOK_ABS_DIR / ABS_AUDIOBOOK_DIR (or their
    per-target _<TARGET> forms). The current container setup mounts
    ~/audiobooks as /audiobooks, so prefer that when it exists; fall back to
    the historical agent-media library.
    """
    override = _tenv("MEDIA_AUDIOBOOK_ABS_DIR", target) or _tenv("ABS_AUDIOBOOK_DIR", target)
    if override:
        return Path(override).expanduser()
    p = Path.home() / "audiobooks"
    return p if p.exists() else library_dir(target)


def _abs_cfg(target=None) -> tuple[str, str, str]:
    url = _tenv("MEDIA_AUDIOBOOKSHELF_URL", target) or _tenv("ABS_URL", target) or ""
    token = _tenv("MEDIA_AUDIOBOOKSHELF_TOKEN", target) or _tenv("ABS_TOKEN", target) or ""
    lib = _tenv("ABS_LIBRARY", target) or ""
    try:
        for line in (Path.home() / ".config" / "agent-media" / "abs-bridge.env").read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"\'')
            if k == "ABS_URL" and not url:
                url = v
            elif k == "ABS_TOKEN" and not token:
                token = v
            elif k == "ABS_LIBRARY" and not lib:
                lib = v
    except OSError:
        pass
    return url.rstrip("/"), token, lib


def _abs_extra_servers(target=None) -> list[tuple[str, str]]:
    """`(url, token)` for the *additional* Audiobookshelf servers that should
    receive the same conversation metadata and rescans as the primary one.

    Each has its own token, because an API key is minted per server: a bearer
    from one is rejected by another. The format is therefore ``url|token``,
    comma-separated, in ``ABS_SERVERS`` (env ``MEDIA_ABS_SERVERS`` for a
    one-off) beside the primary ``ABS_URL``/``ABS_TOKEN`` in abs-bridge.env.
    Blank — the default — means the single primary server it always was.

    Distinct from ``ABS_URLS``, which is a bare-URL allow-list the reply/cast
    side forwards the *caller's own* bearer to; those never carry a token and
    must not, so metadata fan-out gets its own key rather than overloading it.
    """
    raw = _tenv("MEDIA_ABS_SERVERS", target) or ""
    if not raw:
        try:
            for line in (Path.home() / ".config" / "agent-media"
                         / "abs-bridge.env").read_text().splitlines():
                line = line.strip()
                if line.startswith("ABS_SERVERS=") and "=" in line:
                    raw = line.split("=", 1)[1].strip().strip('"\'')
                    break
        except OSError:
            pass
    out: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "|" not in entry:
            continue
        url, token = entry.split("|", 1)
        url, token = url.strip().rstrip("/"), token.strip()
        if url and token:
            out.append((url, token))
    return out


def _scan_one(url: str, token: str, want: str) -> bool:
    """Trigger a rescan of one server's book library. True on success."""
    try:
        req = urllib.request.Request(f"{url}/api/libraries", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            libs = __import__("json").loads(r.read()).get("libraries", [])
        lib = next((l for l in libs if l.get("id") == want or l.get("name") == want), None) if want else None
        lib = lib or next((l for l in libs if l.get("mediaType") == "book"), None)
        if not lib:
            return False
        req = urllib.request.Request(f"{url}/api/libraries/{lib['id']}/scan", method="POST",
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception:
        return False


def trigger_abs_scan(target=None) -> bool:
    """Ask Audiobookshelf to rescan its book library after an import.

    Fans out to every configured server — the primary plus any in
    ``ABS_SERVERS`` (see `_abs_extra_servers`) — so a second instance with its
    file watcher off still picks the import up. True if the primary scanned.

    Per-target library selection via ABS_LIBRARY_<TARGET> (falls back to
    ABS_LIBRARY, then the first book-type library).
    """
    url, token, want = _abs_cfg(target)
    if not url or not token:
        return False
    ok = _scan_one(url, token, want)
    for xurl, xtoken in _abs_extra_servers(target):
        _scan_one(xurl, xtoken, want)
    return ok


def is_youtube(uri: str) -> bool:
    u = uri.strip()
    for prefix in ("yt:", "youtube:"):
        if u.startswith(prefix):
            u = u[len(prefix):]
            break
    return bool(_YT_HOST.match(u))


def youtube_playlist_url(uri: str) -> Optional[str]:
    """Canonical YouTube playlist URL, or None for a single video/non-YouTube.

    A watch URL with both ``v=`` and ``list=`` is treated as the single shared
    video, matching the music sink's behavior.
    """
    u = uri.strip()
    for prefix in ("yt:", "youtube:"):
        if u.startswith(prefix):
            u = u[len(prefix):]
            break
    if u.startswith("playlist:"):
        pid = u[len("playlist:"):]
        return f"https://www.youtube.com/playlist?list={pid}" if pid else None
    if not _YT_HOST.match(u) or "/playlist" not in u.lower():
        return None
    m = _YT_LIST.search(u)
    return f"https://www.youtube.com/playlist?list={m.group(1)}" if m else None


def video_id(uri: str) -> Optional[str]:
    m = _YT_ID.search(uri)
    return m.group(1) if m else None


def expand_youtube_playlist(uri: str, *, limit: Optional[int] = None) -> Optional[list[str]]:
    """Expand a YouTube playlist into watch URLs using the active book profile.

    ``yt-profile book use ...`` controls ``active-book.txt``; yt-dlp reads it
    through the user's normal config/cookies, so private account playlists work
    without a separate account selector here.
    """
    purl = youtube_playlist_url(uri)
    if not purl:
        return None
    cap = int(limit or os.environ.get("MEDIA_AUDIOBOOK_PLAYLIST_MAX", "200"))
    ytdlp = os.environ.get("MEDIA_YTDLP_BIN", "yt-dlp")
    try:
        proc = subprocess.run(
            [ytdlp, "--flat-playlist", "--no-warnings", "--ignore-errors",
             "--print", "%(id)s", "--playlist-end", str(cap), purl],
            capture_output=True, text=True, timeout=300, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    ids = [ln.strip() for ln in proc.stdout.splitlines()
           if ln.strip() and ln.strip() != "NA"]
    return [f"https://www.youtube.com/watch?v={vid}" for vid in ids] or None


def cached_path(vid: str, target=None) -> Optional[Path]:
    """The library file for video id `vid` (yt-dlp names it ``... [<vid>].ext``).

    Searches the per-target library dir when `target` is given.
    """
    d = library_dir(target)
    if not d.is_dir():
        return None
    # The bracketed id is unambiguous; match it literally to avoid title clashes.
    hits = sorted(d.glob(f"*[[]{vid}[]]*"))
    return hits[0] if hits else None


def fetch_cmd() -> Optional[str]:
    """Path to the `audiobook-fetch` acquisition helper, if installed."""
    return os.environ.get("MEDIA_AUDIOBOOK_FETCH") or shutil.which("audiobook-fetch")


def start_fetch_many(urls: Sequence[str], *, play: bool = False, target=None) -> bool:
    """Kick off a detached `audiobook-fetch` for one or more URLs.

    Returns False if the helper isn't installed. With `play=True` the helper
    plays the last fetched file on the book channel when the phone download +
    sync finishes.

    When `target` is given, the helper syncs into that target's ABS library
    dir via the AUDIOBOOK_LIB env var it already honors, and receives
    `--target <name>` so its internal `media book play` / `media abs-scan`
    calls hit that target's book channel + library too.
    """
    fetch = fetch_cmd()
    if not fetch:
        return False
    argv = [fetch]
    if play:
        argv.append("--play")
    env = None
    name = getattr(target, "name", target) or ""
    if target is not None and name:
        env = dict(os.environ)
        env["AUDIOBOOK_LIB"] = str(abs_import_dir(target))
        argv += ["--target", str(name)]
    argv.extend(str(u) for u in urls)
    subprocess.Popen(
        argv, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env=env,
    )
    return True


def start_fetch(url: str, *, play: bool = False, target=None) -> bool:
    return start_fetch_many([url], play=play, target=target)
