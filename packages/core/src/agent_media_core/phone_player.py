"""The phone's own player — Sasonica's ExoPlayer — as the book channel's first choice.

Since 2026-09-09 the phone is the primary book player and mpv the fallback.
Sasonica serves a control endpoint on the phone's loopback (``SasonicaControl``,
port 8772: ``/state /play /pause /resume /seek /jump /speed /stop``), so a book
started from red5 gets the app's own lock-screen controls, focus handling and
exact speed, and the app can pause or move it like anything it plays.

Two transports, chosen by config:

* **ssh** (today): one hop to the phone's Termux, which thaws the app with a
  media-button broadcast (Android freezes a background app, and a frozen
  process answers nothing) and then curls loopback. Needs sshd, ``am`` and
  ``curl`` on the phone — i.e. Termux.
* **http** (``MEDIA_PHONE_PLAYER_URL``): straight to the app over the tailnet,
  for when Sasonica grows an always-on remote mode that listens off-loopback
  with a token. No Termux. Nothing here changes when that lands; only config.

Everything answers ``None`` for "the phone did not take it", and the caller
falls back to mpv. A refused connection means the player service is not
running (the app was killed); a timeout means it is frozen and the thaw did
not land. Both are the fallback signal, not errors.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from .types import Target

log = logging.getLogger(__name__)

PORT_DEFAULT = 8772
#: The app's exported media-button receiver; a broadcast to it thaws a frozen
#: process for a few seconds without bringing anything to the screen.
THAW_CMD = ("am broadcast -n com.sasonica.app/androidx.media.session.MediaButtonReceiver "
            "-a android.intent.action.MEDIA_BUTTON >/dev/null 2>&1")


def enabled() -> bool:
    return os.environ.get("MEDIA_PHONE_PLAYER", "1") not in ("", "0", "false", "no")


def _env_key(base: str, target: str) -> str:
    return f"{base}_{target.upper().replace('-', '_')}"


def ssh_host(target: Target) -> str:
    """The phone's ssh alias for this target — the same one the book cache uses."""
    return (os.environ.get(_env_key("MEDIA_PHONE_PLAYER_SSH", target.name))
            or os.environ.get(_env_key("MEDIA_BOOK_CACHE_SSH", target.name))
            or os.environ.get(_env_key("MEDIA_SPEECH_CLIP_SSH", target.name))
            # The `app` target has no ssh key of its own (its speech goes over
            # tcp to the companion), but the phone is the music host's phone.
            or os.environ.get("MEDIA_MUSIC_LOCAL_SSH", ""))


def direct_url(target: Target) -> str:
    return (os.environ.get(_env_key("MEDIA_PHONE_PLAYER_URL", target.name))
            or os.environ.get("MEDIA_PHONE_PLAYER_URL") or "").rstrip("/")


def reachable(target: Target) -> bool:
    """Is there any route to a player on this target? Local never has one."""
    if not enabled() or target.name in ("", "local"):
        return False
    return bool(direct_url(target) or ssh_host(target))


# --- the app's Audiobookshelf ------------------------------------------------

def app_server() -> tuple[str, str]:
    """``(url, token)`` of the Audiobookshelf the app is logged into.

    Item ids are per server, so the lookup has to hit the app's own — which on
    this fleet is the second instance (13379), not the one the bridge syncs.
    ``MEDIA_PHONE_PLAYER_ABS=url|token`` names it; failing that, the first
    entry of ``ABS_SERVERS`` in abs-bridge.env, which is that instance already.
    """
    raw = os.environ.get("MEDIA_PHONE_PLAYER_ABS", "")
    if not raw:
        env = Path(os.environ.get("MEDIA_ABS_BRIDGE_ENV",
                                  "~/.config/agent-media/abs-bridge.env")).expanduser()
        try:
            for line in env.read_text().splitlines():
                if line.startswith("ABS_SERVERS="):
                    raw = line.split("=", 1)[1].strip().split(",")[0]
                    break
        except OSError:
            return "", ""
    if "|" not in raw:
        return "", ""
    url, token = raw.split("|", 1)
    return url.strip().rstrip("/"), token.strip().strip('"').strip("'")


def _abs_get(url: str, token: str, path: str, timeout: float = 8.0):
    req = urllib.request.Request(url + path)
    req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def match_item(results: list, filename: str) -> Optional[str]:
    """The id of the search hit whose audio includes `filename`, else None.

    Titles collide (every conversation is "a reply", every part is "Part 1")
    but the file on disk is the file the app streams, so the basename decides.
    """
    for hit in results:
        item = hit.get("libraryItem") or hit
        media = item.get("media") or {}
        for af in media.get("audioFiles") or []:
            meta = af.get("metadata") or {}
            if meta.get("filename") == filename or os.path.basename(meta.get("path") or "") == filename:
                return item.get("id")
    return None


def item_for(path: Path) -> Optional[str]:
    """The app-server item id for a local file, or None when it is not one.

    ``abs:<id>`` URIs skip the lookup. Anything the app's library does not
    hold — a YouTube fetch not yet imported, a file elsewhere on disk — stays
    on mpv.
    """
    url, token = app_server()
    if not url or not token:
        return None
    try:
        libs = _abs_get(url, token, "/api/libraries").get("libraries") or []
        # Search matches titles, not filenames. A one-file book is titled like
        # its file; a many-file item (a conversation, one clip per turn) is
        # titled like its folder. Try both, then let the basename decide.
        for q in dict.fromkeys([path.stem, path.parent.name]):
            for lib in libs:
                res = _abs_get(url, token,
                               f"/api/libraries/{lib['id']}/search?q={urllib.parse.quote(q)}&limit=10")
                found = match_item(list(res.get("book") or []) + list(res.get("podcast") or []), path.name)
                if found:
                    return found
    except Exception as e:  # noqa: BLE001 — a lookup failure is "not on the phone"
        log.debug("phone player: item lookup failed for %s: %r", path, e)
    return None


# --- talking to the app ------------------------------------------------------

def request(target: Target, route: str, params: Optional[dict] = None,
            timeout: float = 50.0) -> Optional[dict]:
    """One call to the app's endpoint; the state it answers, or None."""
    qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    path = route + (f"?{qs}" if qs else "")
    url = direct_url(target)
    try:
        if url:
            req = urllib.request.Request(url + path)
            tok = os.environ.get("MEDIA_PHONE_PLAYER_TOKEN", "")
            if tok:
                req.add_header("Authorization", "Bearer " + tok)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
        else:
            host = ssh_host(target)
            if not host:
                return None
            port = int(os.environ.get("MEDIA_PHONE_PLAYER_PORT", str(PORT_DEFAULT)))
            # Thaw, then ask, in the same shell: the window is a few seconds.
            remote = (f"{THAW_CMD}; sleep 1; "
                      f"curl -s -m {int(timeout)} {shlex.quote(f'http://127.0.0.1:{port}{path}')}")
            p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, remote],
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=timeout + 15, check=False)
            body = p.stdout.strip()
            if p.returncode != 0 or not body:
                return None
        data = json.loads(body)
    except Exception as e:  # noqa: BLE001 — every failure here means "use mpv"
        log.debug("phone player: %s failed: %r", route, e)
        return None
    if not isinstance(data, dict) or "error" in data:
        log.info("phone player: %s answered %s", route, data)
        return None
    return data


def state(target: Target) -> Optional[dict]:
    return request(target, "/state", timeout=8.0) if reachable(target) else None


def has_item(target: Target) -> bool:
    """Is the app holding a session? Then transport commands belong to it."""
    s = state(target)
    return bool(s and s.get("item"))


def play(path: Path, target: Target, start_ms: int = 0, rate: Optional[float] = None) -> Optional[dict]:
    """Start `path` in the app at `start_ms`. None = not taken, use mpv."""
    if not reachable(target):
        return None
    item = item_for(path)
    if not item:
        return None
    params = {"item": item, "t": max(0, start_ms) / 1000.0}
    if rate:
        params["rate"] = rate
    s = request(target, "/play", params)
    if s is None:
        return None
    s["item_id"] = item
    return s
