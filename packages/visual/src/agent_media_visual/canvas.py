"""The canvas: a tiny SSE-driven full-bleed image page any screen can show.

Stdlib-only HTTP server. Endpoints:

  GET  /          the canvas page (cross-fade + Ken Burns, SSE client,
                  audio controller — Tab / tap walk the focus ring)
  GET  /events    Server-Sent Events stream of `show` events
  GET  /img/<f>   serve a generated image from the spool dir
  POST /show      {"image": "<spool filename | absolute URL>",
                   "caption": "...", "prompt": "..."}  → broadcast
  GET  /status?channel=speech|music|book   controller state (shells to
                  the `media` CLI, same one-code-path as the tmux popup)
  POST /ctl       {"channel": ..., "action": ..., "arg": ...} → run a
                  whitelisted `media` transport command
  GET  /conversation?item=<abs item id>   + an Audiobookshelf bearer →
                  the session behind that item and whether it is still live
  GET  /item?id=<abs item id>   + an Audiobookshelf bearer →
                  that library item carrying only what the app reads, gzipped
                  (1267 KB → 25 KB on a long conversation); see item.py
  POST /reply     {"item": "<abs item id>", "text": "...", "quote": "...",
                   "mode": "continue"|"branch"} + an Audiobookshelf bearer →
                  type into the session behind that conversation, reviving it
                  in a background tmux window if it has ended
  GET  /conversation?session=<uuid>   + an Audiobookshelf bearer →
                  a session the phone started: its item id once the library
                  has one, and whether it is live
  POST /ask       {"text", "target"?, "player_item"?, "sticky"?, "parse"?}
                  + an Audiobookshelf bearer → the assistant button's words,
                  routed: a picked session, a session named in the words
                  ("reply to drones, …"), the player's conversation, the one
                  last spoken to, else a FRESH session in the scratch tmux
                  session. 300 + candidates when a spoken name is ambiguous.
  GET  /conversations  + an Audiobookshelf bearer → live sessions and
                  recent conversations, by title (the picker)
  POST /session/resume {"session"} → bring that session back in a tmux
                  window (a reply's revive, without the reply)
  POST /session/close  {"session"} → close the pane it runs in
  POST /focus     {"pane": "%23"} → bring the attached tmux client to a pane
  GET  /healthz   liveness

Config (env):
  MEDIA_VISUAL_PORT   listen port (default 8781 — clip server is 8780)
  MEDIA_VISUAL_BIND   bind address (default 0.0.0.0; the shipped systemd
                      unit passes the Tailscale IP for a tailnet-only bind)
  MEDIA_VISUAL_DEBUG  "1" to log requests
  MEDIA_VISUAL_TRUST_TAILNET  "1" drops the amux token even on /input — trust
                      every caller of the tailnet-bound server. Default off; the
                      token guards /input (keystroke injection) against a site
                      your browser visits POSTing into your agents. The read-
                      only /agents + /sessions are open regardless.

Point the phone / TV browser at http://<host>:8781/ and leave it open.
A screen that is off just misses the show — nothing depends on it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shutil
import socket as _socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from .state import spool_dir

DEFAULT_PORT = 8781
MAX_SSE_CLIENTS = 64        # held-open /events streams before we shed load (#137)


class Hub:
    """Fan-out of show events to connected SSE clients; remembers the last
    image event (and the latest speech state) so a screen that (re)connects
    immediately shows something."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self.last: dict | None = None        # last *image* event only
        self.last_state: dict | None = None  # latest speech-state event
        self.last_video: dict | None = None  # latest video-sync event

    def attach(self) -> queue.Queue | None:
        # Cap held-open /events streams: an unbounded fan-out lets thousands of
        # half-open clients (mobile backgrounding, days-long walls) exhaust
        # ThreadingHTTPServer's threads/fds (#137). Reject past the cap; the
        # SSE handler turns None into a 503 and the browser retries.
        with self._lock:
            if len(self._clients) >= MAX_SSE_CLIENTS:
                return None
            q: queue.Queue = queue.Queue(maxsize=16)
            self._clients.append(q)
            return q

    def detach(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    def watchers(self) -> int:
        with self._lock:
            return len(self._clients)

    def publish(self, event: dict, *, remember: bool = True) -> None:
        with self._lock:
            if remember:
                if event.get("kind") == "state":
                    self.last_state = event
                elif event.get("kind") == "video":
                    self.last_video = event
                else:
                    self.last = event
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # a stalled screen skips frames, never blocks the rest


HUB = Hub()


# --- viewer activity: which screen most recently had eyes on it ---------------
# A canvas names itself once via ?screen=<name> (persisted in localStorage) and
# then beacons /seen on interaction/focus. Every show event is stamped with the
# freshest screen ("wake"), so a per-host wake agent can turn THAT display back
# on — a browser page can only *prevent* sleep, never end it, and the server
# deliberately never reaches out to screens itself (no ssh/adb creds here).

_VIEWERS: dict[str, dict] = {}   # name -> {ts, focused, blur_ts}
_VIEWERS_LOCK = threading.Lock()

# A blur this close before the host's screen-blank is *caused by* the blank
# (GNOME takes window focus when it blanks) and must not count against the
# canvas — the screen went dark on it, nobody switched away from it.
_BLANK_BLAME_S = 6.0


def _viewer_seen(name: str, focused: "bool | None" = True,
                 blank: "bool | None" = None) -> None:
    """`focused` is the page's own blur/focus report of being the active
    window — the only focus signal that works everywhere (GNOME Wayland
    offers services no way to ask, see canvas-wake-watch.py). `blank` comes
    from the host's wake agent watching the screensaver: blank=True right
    after a blur means the blur was the blank's doing, so the canvas was
    still up front when the lights went out — restore its eligibility."""
    name = re.sub(r"[^A-Za-z0-9._-]", "", name or "")[:32]
    if not name:
        return
    now = time.time()
    with _VIEWERS_LOCK:
        v = _VIEWERS.setdefault(name, {"ts": 0.0, "focused": False,
                                       "blur_ts": 0.0})
        if blank is not None:
            if blank and not v["focused"] and \
                    now - v["blur_ts"] <= _BLANK_BLAME_S:
                v["focused"] = True
            return   # agent housekeeping, not viewer activity — ts untouched
        v["ts"] = now
        if focused is not None:
            if not focused and v["focused"]:
                v["blur_ts"] = now
            v["focused"] = bool(focused)


_WHOIS_CACHE: dict[str, tuple[str, float]] = {}


def _screen_from_ip(ip: str) -> str:
    """Tailnet machine name for a client IP via `tailscale whois` (cached 1h —
    the mapping only changes when David re-homes a device). The server binds
    the tailnet IP, so every viewer arrives with a resolvable source address;
    "" when it isn't one (subnet-routed guest, tailscaled hiccup)."""
    hit = _WHOIS_CACHE.get(ip)
    if hit and time.time() - hit[1] < 3600:
        return hit[0]
    name = ""
    try:
        r = subprocess.run(["tailscale", "whois", "--json", ip],
                           capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            name = str((json.loads(r.stdout).get("Node") or {})
                       .get("ComputedName") or "").split(".")[0]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    _WHOIS_CACHE[ip] = (name, time.time())
    return name


def _wake_ignored() -> set[str]:
    """Screens that may view and control but never claim wake targeting
    (MEDIA_VISUAL_WAKE_IGNORE, comma/space separated — e.g. the phone: a
    glance at its canvas shouldn't keep the big screens dark afterwards)."""
    raw = os.environ.get("MEDIA_VISUAL_WAKE_IGNORE") or ""
    return {n for n in re.split(r"[,\s]+", raw.strip().lower()) if n}


def _wake_target() -> "str | None":
    """The most recently active screen that is neither ignored nor blurred
    (its canvas must still be the active window there), if fresh enough that
    David is plausibly still near it (MEDIA_VISUAL_WAKE_WINDOW seconds,
    default 12h) — else None and nobody's display gets poked."""
    try:
        window = float(os.environ.get("MEDIA_VISUAL_WAKE_WINDOW") or 43200)
    except ValueError:
        window = 43200.0
    ignored = _wake_ignored()
    with _VIEWERS_LOCK:
        live = {n: v for n, v in _VIEWERS.items()
                if n.lower() not in ignored and v["focused"]}
        if not live:
            return None
        name, v = max(live.items(), key=lambda kv: kv[1]["ts"])
    return name if time.time() - v["ts"] <= window else None


# --- audio controller backend: shell to the `media` CLI ----------------------
# One code path with the tmux popup: every button runs the same CLI verb the
# popup's hotkey runs, on this host — where `media` already resolves the
# remote speech target (the phone), Mopidy, and the book socket.

def _media_bin() -> str:
    exe = shutil.which("media")
    if exe:
        return exe
    # Installed alongside us in the same venv even when PATH is bare (systemd).
    return str(Path(sys.executable).parent / "media")


def _run(argv: list[str], timeout: int = 10) -> str:
    """stdout of `argv`, stripped; "" on any failure or timeout."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
        return (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _media(args: list[str], timeout: int = 10) -> str:
    return _run([_media_bin(), *args], timeout)


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _book_title() -> str:
    """The book channel's media-title straight off its mpv IPC socket (the
    popup does the same via socat) — `media book now` is a bare URI."""
    sock = (os.environ.get("MEDIA_BOOK_SOCKET")
            or str(Path.home() / ".local/state/agent-media/sink-book.sock"))
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(sock)
            s.sendall(b'{"command":["get_property","media-title"]}\n')
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            for line in buf.decode("utf-8", "replace").splitlines():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "data" in d:
                    return " ".join(str(d["data"]).split())
    except OSError:
        pass
    return ""


def channel_status(channel: str) -> dict:
    """Controller snapshot for one channel: marquee label + progress line +
    indicator flags. Mirrors the popup's fetch()."""
    muted = False
    if channel == "music":
        status = _media(["music", "status", "--show-idle", "--no-bar"])
        label = " ".join(_media(["music", "now"]).split()) or "(no music)"
    elif channel == "book":
        status = _media(["book", "status", "--show-idle", "--no-bar"])
        label = _book_title() or "(audiobook)"
    else:
        out = _media(["popup-status", "--show-idle", "--no-bar"])
        lines = (out.splitlines() + ["", "", ""])[:3]
        status, label, _mutecount = (ln.strip() for ln in lines)
        label = label or "agent-media"
    if "[M]" in status:
        muted = True
        status = status.replace(" [M]", "").replace("[M]", "").strip()
    return {"channel": channel, "label": label, "status": status or "○",
            "muted": muted}


# --- the input box backend: reply to whoever just spoke ----------------------
# POST /input types text into a Claude session: the *last speaker's* tmux pane
# by default (the same source_pane the popup's go-to-source uses), or a named
# amux session via `amux send`. This is remote keystroke injection, so unlike
# the transport controls it requires amux's own auth token — one credential
# for both dashboards (~/.amux/auth_token / AMUX_AUTH_TOKEN).

def _amux_token() -> str:
    tok = os.environ.get("AMUX_AUTH_TOKEN", "")
    if tok:
        return "" if tok.lower() == "none" else tok
    try:
        return (Path.home() / ".amux" / "auth_token").read_text().strip()
    except OSError:
        return ""


def _authorized(handler: "Handler") -> bool:
    # Opt-in: on the tailnet-bound server, trust every caller — drops the token
    # even for /input. Default off, so the token stays as CSRF protection
    # against a site your browser visits POSTing keystrokes into your agents.
    if (os.environ.get("MEDIA_VISUAL_TRUST_TAILNET") or "").strip() == "1":
        return True
    token = _amux_token()
    if not token:
        return False  # no token configured → the input surface stays closed
    got = (handler.headers.get("X-Auth-Token")
           or (handler.headers.get("Authorization") or "").removeprefix("Bearer").strip())
    return got == token


# --- one-time pairing: install the token into a device's localStorage ----------
# Typing a 40-char token on a phone keyboard is miserable, so `/pair?c=<code>`
# does it: a ONE-TIME code minted host-side (written to the state dir by
# whoever has shell access — no HTTP path can create one) unlocks a page that
# stores the amux token in localStorage and redirects to the canvas. The code
# file is deleted on first use and expires after PAIR_TTL_S regardless.

PAIR_TTL_S = int(os.environ.get("MEDIA_VISUAL_PAIR_TTL") or 1800)  # 30 min; tune per host


def _pair_code_path() -> Path:
    return spool_dir() / "pair-code"


def _persona_dir() -> Path:
    """SillyTavern persona portraits, served at /persona/<slug>/<file>. Layout:
    <slug>/neutral.<ext> (+ optional happy/sad/angry/surprised). <slug> is the
    persona's TTS voice, slugified. The tts-shim references these URLs when a
    persona speaks (see packages/tts-shim/.../personas.py)."""
    base = os.environ.get("MEDIA_PERSONA_DIR") or str(
        Path.home() / ".config" / "agent-media" / "personas")
    return Path(base)


def _pair_consume(code: str) -> bool:
    """True (and burn the code) when `code` matches a fresh minted one."""
    p = _pair_code_path()
    try:
        minted = p.read_text().strip()
        fresh = (time.time() - p.stat().st_mtime) <= PAIR_TTL_S
    except OSError:
        return False
    if not code or not minted or not fresh or code != minted:
        return False
    try:
        p.unlink()
    except OSError:
        pass
    return True


_PAIR_PAGE = """<!doctype html><meta charset="utf-8">
<body style="background:#000;color:#eee;font:16px system-ui">
<script>
  localStorage.setItem('amux_token', %s);
  location.replace('/');
</script>
paired — loading the canvas…
</body>"""


def _qr(url: str) -> str:
    """A COMPACT terminal QR for `url` — half-block rows (▀▄) at low error
    correction and a 1-module quiet zone, so it stays short. A tall QR scrolls
    off (or gets collapsed into scrollback) before you can scan it, which is the
    real failure mode here, not the glyphs. Falls back to the URL alone if
    `qrcode` isn't importable. QR is a nicety, never fatal."""
    try:
        import io
        import qrcode
        qr = qrcode.QRCode(border=1,
                           error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(url)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)   # invert → scannable on a dark terminal
        return buf.getvalue().rstrip("\n")
    except Exception:  # noqa: BLE001
        return "  (pip install qrcode for a scannable QR — or open the URL below)"


def _cmd_pair(argv: list[str]) -> int:
    """`media-visual-canvas pair` — mint a one-time link (+ QR) that installs
    this host's amux token into a device's browser, so no secret is typed by
    hand. The code is one-time and expires after PAIR_TTL_S (see _pair_consume)."""
    import argparse
    import secrets
    ap = argparse.ArgumentParser(
        prog="media-visual-canvas pair",
        description="Mint a one-time pairing link (and QR) for a device.")
    ap.add_argument("--host", default=(os.environ.get("MEDIA_VISUAL_PAIR_HOST")
                                       or _socket.gethostname()),
                    help="host used in the URL (default: this machine's hostname)")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("MEDIA_VISUAL_PORT") or DEFAULT_PORT))
    args = ap.parse_args(argv)

    if not _amux_token():
        print("no amux token on this host (~/.amux/auth_token) — nothing to pair.",
              file=sys.stderr)
        return 1

    code = secrets.token_hex(4)   # 8 hex chars — shorter URL, smaller/less-scrolly QR
    path = _pair_code_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code)
    url = f"http://{args.host}:{args.port}/pair?c={code}"

    print(f"\n  Scan to pair this device (valid {PAIR_TTL_S // 60} min, one-time):\n")
    print(_qr(url))
    print(f"\n  {url}\n")
    return 0


def _amux_bin() -> str:
    """amux lives in ~/.local/bin, which a systemd user service's default
    PATH doesn't include."""
    return (shutil.which("amux")
            or str(Path.home() / ".local" / "bin" / "amux"))


def _amux_sessions() -> list[dict]:
    """Sessions from `amux ls --json`: [{name, state, dir, flags, preview}]
    where state is working / input / approval / stopped. Empty list if amux is
    old (no --json) or absent, so callers degrade gracefully."""
    out = _run([_amux_bin(), "ls", "--json"])
    try:
        data = json.loads(out) if out else []
    except (ValueError, TypeError):
        return []
    return [s for s in data if isinstance(s, dict) and s.get("name")]


def _classify_cc(pane: str) -> "str | None":
    """Classify an ANSI-stripped capture of a Claude Code TUI → working / input
    / approval, or None if it doesn't look like Claude Code (so plain shells,
    vim, etc. are ignored). Mirrors amux's detector: require CC chrome, check a
    permission dialog BEFORE the working signal (CC shows "esc to interrupt"
    even while a dialog blocks), and match the width-truncated "esc…" too."""
    if not re.search(r"\? for shortcuts|bypass permissions|esc to interrupt|"
                     r"esc…|⏵⏵", pane):
        return None
    if re.search(r"❯ *[0-9]+\.|Do you want to |Yes, (and|allow|proceed)", pane):
        return "approval"
    if re.search(r"· [↑↓] [0-9.]+k? tokens|esc to interrupt|esc…", pane):
        return "working"
    return "input"


def _tmux_cc_panes() -> list[dict]:
    """Auto-discover Claude Code across ALL tmux panes (not just each session's
    active one — a session can hold several agents in different windows),
    EXCLUDING amux's own `amux-*` sessions (those come from `amux ls`). One agent
    per CC pane, replyable by its pane id. Display name is the session, with the
    window appended when a session holds more than one CC pane."""
    out = _run(["tmux", "list-panes", "-a", "-F",
                      "#{pane_id}\t#{pane_current_command}\t#{session_name}\t"
                      "#{window_name}\t#{pane_current_path}"])
    agents: list[dict] = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) < 5:
            continue
        pane_id, cmd, sess, win, cwd = f[:5]
        # Claude Code panes report `claude` as their command — a cheap, exact
        # filter (no need to capture shells/editors). Skip amux-managed ones.
        if not pane_id or cmd != "claude" or sess.startswith("amux-"):
            continue
        cap = _strip_ansi(_run(["tmux", "capture-pane", "-t", pane_id,
                                "-p", "-S", "-40"]))
        preview = next((ln.strip()[:60] for ln in reversed(cap.splitlines())
                        if ln.strip()), "")
        agents.append({"name": (win if win and win != sess else sess),
                       "session": sess,
                       "state": _classify_cc(cap) or "input",  # cmd=claude ⇒ CC
                       "dir": cwd, "preview": preview,
                       "source": "tmux", "pane": pane_id})
    return agents


# /agents fan-out is expensive — `tmux list-panes` plus a `capture-pane` per
# claude pane, per request. Every connected canvas polls it, so N screens × M
# panes forked subprocesses on a short timer (#141). Memoize the whole payload
# for a couple of seconds so a burst of client polls collapses to one sweep.
_AGENTS_TTL = 2.0
_AGENTS_LOCK = threading.Lock()
_AGENTS_CACHE: dict = {"t": 0.0, "data": None}


def _agents_payload() -> list[dict]:
    now = time.monotonic()
    with _AGENTS_LOCK:
        if (_AGENTS_CACHE["data"] is not None
                and now - _AGENTS_CACHE["t"] < _AGENTS_TTL):
            return _AGENTS_CACHE["data"]
        amux = [{**a, "session": a.get("name")} for a in _amux_sessions()]
        data = amux + _tmux_cc_panes()
        _AGENTS_CACHE["t"] = now
        _AGENTS_CACHE["data"] = data
        return data


def _pane_alive(pane: str) -> bool:
    return bool(_run(["tmux", "display-message", "-pt", pane,
                            "#{pane_id}"]))


def _last_speaker() -> dict | None:
    """{pane, session, tmux_session} of the most recent speech message whose
    source pane still exists — live now_playing extras first, else a walk
    back through recent history (idle). Panes die and ids get recycled, so
    every candidate is probed before it wins."""
    try:
        import sqlite3

        from agent_media_core.state.store import StateStore
        st = StateStore()
        candidates = [((st.get_now_playing("speech") or {}).get("extras")) or {}]
        db = sqlite3.connect(str(st.path))
        for (raw,) in db.execute(
                "SELECT extras FROM history WHERE sink='speech' AND "
                "extras IS NOT NULL ORDER BY rowid DESC LIMIT 10"):
            try:
                candidates.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        for ex in candidates:
            pane = ex.get("source_pane")
            if pane and _pane_alive(pane):
                return {"pane": pane,
                        "session": (ex.get("source_session")
                                    or ex.get("session") or ""),
                        "tmux_session": ex.get("source_tmux_session") or ""}
        return None
    except Exception:  # noqa: BLE001
        return None


def _peek_pane(pane: str, lines: int = 60) -> list[str]:
    """The last N non-blank, ANSI-stripped lines of a pane — for the peek panel."""
    if not pane:
        return []
    cap = _strip_ansi(_run(["tmux", "capture-pane", "-t", pane, "-p",
                            "-S", f"-{lines * 3}"]))
    out = [ln.rstrip() for ln in cap.splitlines() if ln.strip()]
    return out[-lines:]


def _pane_session(pane: str) -> str:
    """The Claude Code session uuid for a pane — walk the pane process's whole
    descendant tree for a process carrying CLAUDE_CODE_SESSION_ID (claude may be
    a grandchild via a wrapper, not a direct child)."""
    ppid = _run(["tmux", "display-message", "-t", pane, "-p", "#{pane_pid}"])
    if not ppid.isdigit():
        return ""
    stack, seen = [ppid], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            for kv in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
                if kv.startswith(b"CLAUDE_CODE_SESSION_ID="):
                    return kv.split(b"=", 1)[1].decode().strip()
        except OSError:
            pass
        stack += _run(["pgrep", "-P", pid]).split()
    return ""


def _pane_turns(pane: str, limit: int = 12) -> list[str]:
    """A pane's Claude Code session as assistant turns (oldest→newest), read from
    its transcript (~/.claude/projects/<cwd-slug>/<session>.jsonl). Falls back to
    one block of the raw pane capture when no transcript is found."""
    if not pane:
        return []
    session = _pane_session(pane)
    if session:
        cwd = _run(["tmux", "display-message", "-t", pane, "-p",
                          "#{pane_current_path}"])
        path = (Path.home() / ".claude" / "projects"
                / cwd.replace("/", "-") / f"{session}.jsonl")
        try:
            turns: list[str] = []
            for line in path.read_text(errors="replace").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") != "assistant":
                    continue
                c = (r.get("message") or {}).get("content")
                if isinstance(c, list):
                    text = "\n".join(b.get("text", "") for b in c
                                     if isinstance(b, dict) and b.get("type") == "text")
                else:
                    text = c if isinstance(c, str) else ""
                if text.strip():
                    turns.append(text.strip())
            if turns:
                return turns[-limit:]
        except OSError:
            pass
    lines = _peek_pane(pane)
    return ["\n".join(lines)] if lines else []


def _child_env() -> dict:
    """Environment for shell-outs that RENDER speech in-process (`media say`,
    replay). `media say` submits and renders in the calling process, so the TTS
    renderer (`edge-tts`) is looked up on PATH — and this systemd user service
    ships a bare PATH (unlike sink-speech, which pins the venv bin). Prepend the
    venv bin + ~/.local/bin so the renderer resolves, same gap the _media_bin /
    _amux_bin fallbacks paper over for their own executables."""
    env = dict(os.environ)
    extra = [str(Path(sys.executable).parent), str(Path.home() / ".local" / "bin")]
    env["PATH"] = os.pathsep.join(extra + ([env["PATH"]] if env.get("PATH") else []))
    return env


# `media say` / replay block until the utterance finishes PLAYING (not merely
# rendering), so a per-turn play of a long assistant turn legitimately runs for
# minutes. This subprocess timeout is only a hung-process backstop, NOT a length
# limit — a low cap kills `media say` mid-sentence and cuts the audio off. Keep
# it well past any real turn; override via MEDIA_VISUAL_SAY_TIMEOUT if needed.
_SAY_TIMEOUT_S = int(os.environ.get("MEDIA_VISUAL_SAY_TIMEOUT") or 900)


def _say(text: str) -> bool:
    """Speak arbitrary text through the speech channel — per-turn 'play'."""
    text = (text or "").strip()
    if not text:
        return False
    try:
        subprocess.run([_media_bin(), "say", text], env=_child_env(),
                       timeout=_SAY_TIMEOUT_S, check=False)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _play_pane(pane: str) -> bool:
    """Replay a pane's last spoken clip through the speech channel — 'play the
    output' (b). Reuses `replay-at-cursor`, which resolves the pane's latest clip
    from the speech history via TTS_POPUP_PANE."""
    # The pane arrives RAW in the /play JSON body — no unquote here: a tmux id
    # with a two-digit number ("%12") would percent-decode into a control char.
    if not pane:
        return False
    env = {**_child_env(), "TTS_POPUP_PANE": pane}
    try:
        subprocess.run([_media_bin(), "replay-at-cursor"], env=env,
                       timeout=_SAY_TIMEOUT_S, check=False)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _send_to_pane(pane: str, text: str) -> str:
    """Type `text` + Enter into a tmux pane (amux's literal-then-Enter timing,
    which Claude Code's input buffering needs). Returns "" or an error."""
    probe = _run(["tmux", "display-message", "-pt", pane, "#{pane_id}"])
    if not probe:
        return f"pane {pane} is gone"
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text],
                       timeout=5, check=True)
        time.sleep(0.05)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                       timeout=5, check=True)
        return ""
    except (OSError, subprocess.SubprocessError) as e:
        return f"send-keys: {e}"


def send_input(text: str, target: str) -> tuple[bool, str]:
    """Deliver `text` to `target`: "speaker" or "amux:<name>". Returns
    (ok, detail) where detail is the resolved destination or the error."""
    text = (text or "").strip()
    if not text:
        return False, "empty text"
    if target.startswith("amux:"):
        name = target[len("amux:"):]
        if name not in {s["name"] for s in _amux_sessions()}:
            return False, f"unknown amux session {name!r}"
        out = _run([_amux_bin(), "send", name, text])
        return (True, f"amux:{name}") if out else (False, "amux send failed")
    if target.startswith("tmux:"):
        # An auto-discovered (non-amux) Claude Code pane — type into it
        # directly, same literal-then-Enter path as `amux send`. The target is
        # a pane id; only genuine `claude` panes are valid — typing text+Enter
        # into a bare shell pane would be host command execution, so validate
        # against _tmux_cc_panes() (which already filters cmd=="claude").
        pane = target[len("tmux:"):]
        if pane not in {p["pane"] for p in _tmux_cc_panes()}:
            return False, f"not a live claude pane: {pane!r}"
        err = _send_to_pane(pane, text)
        return (False, err) if err else (True, f"tmux:{pane}")
    speaker = _last_speaker()
    if not speaker:
        return False, "no speaker on record yet"
    err = _send_to_pane(speaker["pane"], text)
    if err:
        return False, err
    return True, speaker.get("tmux_session") or speaker["pane"]


# --- speech-state poller: the canvas reacts to the voice ---------------------
# While at least one screen is connected, poll the speech channel (~1 Hz, one
# `media popup-status` spawn — the popup's own cadence) and broadcast
# {"kind": "state", "speaking": bool, "pos": s, "dur": s} over the SSE stream.
# The page uses it to drive motion (faster Ken Burns + a breathing vignette
# while the voice is talking) and the start/stop sound cues. The local mpv
# socket can't be the source: speech usually plays on the *phone*, and only
# the `media` CLI sees the remote target's state.

_TIME_PAIR = re.compile(r"(?:(\d+):)?(\d+):(\d{2})")


def _parse_clock(text: str) -> list[int]:
    """All H:MM:SS / MM:SS times in `text`, as seconds."""
    out = []
    for h, m, s in _TIME_PAIR.findall(text):
        out.append((int(h or 0)) * 3600 + int(m) * 60 + int(s))
    return out


def _speech_extras() -> dict:
    """The speech channel's now_playing extras (fresh StateStore per read —
    cheap WAL hit, thread-safe by construction). Empty dict on any problem."""
    try:
        from agent_media_core.state.store import StateStore
        np = StateStore().get_now_playing("speech")
        return (np or {}).get("extras") or {}
    except Exception:  # noqa: BLE001 — subtitles are garnish, never a fault
        return {}


# --- the words live where they are produced ----------------------------------
# A canvas on a render-only host (the phone's local control surface) has no
# speech state to read: now_playing for speech is written where the reply is
# PRODUCED, and this host only plays the audio. So its subtitle, its band, its
# seam and its transcript were all empty, always — not broken, just asking a
# store that was never going to have the answer. From the outside that is
# indistinguishable from a canvas that has not been deployed, which cost six
# rounds of looking at deploys.
#
# The origin's canvas has already computed exactly the snapshot we want, so ask
# it rather than reaching into its store. Cached, because the poller runs at
# 1 Hz and this hop crosses to Falkenstein on a link that drops packets — a
# stale sentence is much better than a canvas that stutters, and the transcript
# is read after the fact anyway.
# The last clip's sentences, kept because the store does not keep them — and
# kept ON DISK, because keeping them only in memory means every canvas restart
# empties the transcript until something next speaks. Restarts are exactly when
# somebody is told to reload and go and look, so an in-memory cache is empty
# at the one moment it is asked for.
# Keyed by the pane that is speaking, with a timestamp, because a reply is not
# one clip. The intake splits it — the first sentence goes out on its own so
# the voice starts sooner, and the rest follows — so clip_sentences is only
# ever the CURRENT clip. A transcript built from it shows one sentence for most
# of a reply and the whole thing only once the last clip is playing, which is
# exactly "on the app it doesn't show until the end".
_LAST_CLIP: dict = {"lines": [], "idx": 0, "key": "", "t": 0.0}
# A new clip from the same pane within this many seconds continues the same
# reply rather than starting a new one.
_REPLY_GAP_S = 180.0


def _sublist_index(hay: list, needle: list) -> int:
    """Where `needle` already sits inside `hay`, or -1. Empty needle: -1."""
    if not needle or len(needle) > len(hay):
        return -1
    for i in range(len(hay) - len(needle) + 1):
        if hay[i:i + len(needle)] == needle:
            return i
    return -1


def _last_clip_path() -> Path:
    return spool_dir() / "last-clip.json"


def _last_clip_load() -> None:
    try:
        data = json.loads(_last_clip_path().read_text())
        lines = [str(t) for t in (data.get("lines") or []) if str(t).strip()]
    except (OSError, ValueError, AttributeError):
        return
    if lines:
        _LAST_CLIP["lines"] = lines[:120]
        try:
            _LAST_CLIP["idx"] = int(data.get("idx") or 0)
        except (TypeError, ValueError):
            _LAST_CLIP["idx"] = 0
        _LAST_CLIP["key"] = str(data.get("key") or "")
        try:
            _LAST_CLIP["t"] = float(data.get("t") or 0)
        except (TypeError, ValueError):
            _LAST_CLIP["t"] = 0.0


def _last_clip_save() -> None:
    try:
        _last_clip_path().write_text(json.dumps(_LAST_CLIP))
    except OSError:      # a transcript is garnish; never a fault
        pass
_ORIGIN_STATE: dict = {"t": 0.0, "data": None}
_ORIGIN_TTL = 2.0


def _origin_host() -> str | None:
    """The host producing the speech, or None to answer locally."""
    try:
        from agent_media_core import config
        roles = config.host_roles()
        if roles is None or "origin" in roles:
            return None
        found = config.peer("origin")
        return found.host if found else None
    except Exception:  # noqa: BLE001 — a surface renders what it got
        return None


def _origin_speech() -> dict | None:
    """The origin canvas's own speech snapshot, cached. None if unreachable."""
    host = _origin_host()
    if not host:
        return None
    now = time.time()
    if _ORIGIN_STATE["data"] is not None and now - _ORIGIN_STATE["t"] < _ORIGIN_TTL:
        return _ORIGIN_STATE["data"]
    try:
        import urllib.request
        port = int(os.environ.get("MEDIA_VISUAL_PORT") or DEFAULT_PORT)
        with urllib.request.urlopen(
                f"http://{host}:{port}/speech", timeout=4) as r:
            data = json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        # Keep serving the last good answer rather than blinking the band out
        # of existence on one dropped packet.
        return _ORIGIN_STATE["data"]
    data.pop("events", None)
    data.pop("local_audio", None)
    _ORIGIN_STATE["t"] = now
    _ORIGIN_STATE["data"] = data
    return data


def speech_state() -> dict:
    """One SSE-shaped speech-state snapshot off `media popup-status`, enriched
    with the current sentence (the same per-clip marker that drives the tmux
    copy-mode highlight — this is highlight mode for the canvas) and the
    figure flag for the ▣ badge."""
    remote = _origin_speech()
    if remote is not None:
        # Whether a voice is audible is still a local fact — this host is the
        # one playing it — but the WORDS are the origin's.
        return remote
    line = (_media(["popup-status", "--no-bar", "--show-idle"], timeout=5)
            .splitlines() or [""])[0].strip()
    times = _parse_clock(line)
    state: dict = {"kind": "state", "speaking": line.startswith("▶")}
    if len(times) >= 2:
        state["pos"], state["dur"] = times[0], times[1]
    # Read the extras whether or not a voice is live. The clip's sentences are
    # the LAST reply's until a new one replaces them, and they are what the
    # canvas transcript is made of — so a screen that connects after the voice
    # has stopped can still show what was just said, and read back through it.
    # Serving them only while speaking meant the transcript existed solely
    # during the seconds you were being read to, and was empty every time
    # anybody went looking for it afterwards, which is the entire complaint.
    ex = _speech_extras()
    if state["speaking"]:
        sentence = " ".join(str(ex.get("current_sentence") or "").split())
        if sentence:
            state["sentence"] = sentence[:220]
        # The whole clip, not just the live line. The extras already carry it
        # — clip_sentences is what the splitter produced and
        # current_sentence_idx is where the voice is in that list — so the
        # canvas can show a transcript without asking for anything new, and
        # without accumulating what it happened to be awake for. A page
        # reloaded mid-reply gets the same transcript as one that watched it
        # arrive, which an accumulate-as-you-go model could never manage.
        if ex.get("visual"):
            state["visual"] = ex["visual"]
        if ex.get("source_session"):
            # Who's talking — the page uses this to dim a figure that belongs
            # to a different session than the current voice.
            state["session"] = str(ex["source_session"])[:80]
    lines = [" ".join(str(t).split()) for t in (ex.get("clip_sentences") or [])]
    lines = [t for t in lines if t]
    if lines:
        # A reply is not one clip. The intake sends the first sentence on its
        # own so the voice starts sooner and the rest follows, so
        # clip_sentences is only ever the CURRENT clip — a transcript built
        # straight from it shows one sentence for most of a reply and the whole
        # thing only once the last clip plays. So clips from the same pane,
        # close together, accumulate into one reply.
        try:
            within = int(ex.get("current_sentence_idx") or 0)
        except (TypeError, ValueError):
            within = 0
        key = str(ex.get("source_pane") or ex.get("source_session") or "")
        now = time.time()
        same_reply = bool(key) and key == _LAST_CLIP.get("key") and (
            now - float(_LAST_CLIP.get("t") or 0) < _REPLY_GAP_S)
        kept = list(_LAST_CLIP["lines"]) if same_reply else []
        # Where this clip sits in the reply. Matched on the sentences
        # themselves because a clip carries no id — which also means a replay
        # lands on itself instead of piling up a second copy.
        at = _sublist_index(kept, lines)
        if at < 0:
            at = len(kept)
            kept = kept + lines
        changed = (kept != _LAST_CLIP["lines"] or key != _LAST_CLIP.get("key")
                   or at + within != _LAST_CLIP["idx"])
        _LAST_CLIP.update({"lines": kept[:120], "idx": min(at + within, 119),
                           "key": key, "t": now})
        if changed:
            _last_clip_save()

    # Published from the remembered reply in BOTH cases — speaking or not.
    #
    # now_playing holds what is PLAYING and the row is cleared when the clip
    # ends, so once the voice stops there is nothing left to read and the
    # transcript, which is wanted precisely then, would be empty. And while a
    # voice IS live, publishing the raw clip instead of the reply is what made
    # the transcript arrive only at the end.
    #
    # Capped for the wire: this rides a 1 Hz broadcast to every screen, and one
    # pathological reply should not put a megabyte on it every second.
    if _LAST_CLIP["lines"]:
        state["lines"] = [t[:220] for t in _LAST_CLIP["lines"]]
        state["lidx"] = _LAST_CLIP["idx"]
    return state


def _speech_events(limit: int = 20) -> list:
    """The newest `limit` start/end breadcrumbs from the speech event log
    (written by agent_media_core.intake.submit at the moment playback begins
    and in its closing finally). Oldest first, so a reader replays them in
    the order they happened. Empty list when the log is missing or garbled —
    a peek endpoint must degrade to less data, never to an error."""
    state = Path(os.environ.get("XDG_STATE_HOME")
                 or (Path.home() / ".local" / "state"))
    path = state / "agent-media" / "speech-events.jsonl"
    try:
        lines = path.read_text().splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def _local_audio_playing() -> bool:
    """True if the *local* speech mpv is actively playing (core-idle False).
    Same probe the :8675 ducker endpoint uses: a 0.5s-capped unix-socket
    property read, fail-open — unreadable/absent socket reads as False, so a
    consumer never sticks 'ducked'. Blind to phone playback by design."""
    sock = (Path(os.environ.get("XDG_STATE_HOME")
                 or (Path.home() / ".local" / "state"))
            / "agent-media" / "sink-speech.sock")
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(str(sock))
        s.sendall(b'{"command":["get_property","core-idle"]}\n')
        data = b""
        while True:
            try:
                chunk = s.recv(4096)
            except _socket.timeout:
                break
            if not chunk:
                break
            data += chunk
            for line in data.decode(errors="ignore").splitlines():
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("request_id") == 0 and o.get("error") == "success":
                    s.close()
                    return not bool(o.get("data"))
        s.close()
    except Exception:  # noqa: BLE001 — fail open, exactly like the ducker
        pass
    return False


def _state_poller() -> None:
    last_key = None
    was_speaking = False
    while True:
        if HUB.watchers() == 0:
            time.sleep(2)
            continue
        try:
            st = speech_state()
        except Exception:  # noqa: BLE001 — the poller must outlive any hiccup
            time.sleep(2)
            continue
        # Voice starting (fresh clip OR unpause) is a wake-worthy moment: stamp
        # the transition — and only the transition, so wake agents see one
        # event per resume, not the ~1 Hz progress ticks.
        if st["speaking"] and not was_speaking:
            wake = _wake_target()
            if wake:
                st["wake"] = wake
        was_speaking = st["speaking"]
        key = (st["speaking"], st.get("pos"), st.get("sentence"))
        # Broadcast on any change; while speaking, pos ticks every poll, so
        # watchers get a ~1 Hz progress signal without idle-time chatter.
        if key != last_key:
            HUB.publish(st)
            last_key = key
        time.sleep(1)


# --- video sync: mirror the phone's YouTube audio as muted video --------------
# When the music channel plays on the phone-local backend (a YouTube track
# downloaded to <video-id>.mka and played in the phone's mpv), the canvas can
# show the matching video: the page embeds a muted YouTube IFrame player (the
# browser device sits on the home network, so it streams from YouTube directly —
# red5 itself can't, datacenter IPs get 403) and this poller broadcasts the
# phone's position/pause/speed so the page keeps the video within ~1.5s of the
# audio. Poll only while screens are connected; one batched IPC round-trip.

_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Same URL shapes sinks/book.py recognises — a book that fell back to raw-URL
# streaming still identifies its video.
_YT_URL = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})")


def _probe_video(endpoint) -> dict | None:
    """{"vid", "t", "paused", "rate"} for one player, or None when it's idle,
    unreachable, or not on YouTube-identifiable content."""
    from agent_media_core.sinks import _mpv_ipc as ipc
    try:
        props = ipc.get_properties(
            endpoint, ["idle-active", "pause", "time-pos", "speed", "path"],
            timeout=1.5)
    except Exception:  # noqa: BLE001 — down ⇒ no video, never a fault
        return None
    if props.get("idle-active") is not False:
        return None
    path = str(props.get("path") or "")
    stem = Path(path).stem
    # Three shapes carry a video id: the phone music cache (`<id>.mka`), the
    # audiobook library (yt-dlp's `Title [<id>].mka`), and a raw YouTube URL.
    vid = stem if _YT_ID.fullmatch(stem) else None
    if not vid:
        m = re.search(r"\[([A-Za-z0-9_-]{11})\]$", stem)
        vid = m.group(1) if m else None
    if not vid:
        m = _YT_URL.search(path)
        vid = m.group(1) if m else None
    if not vid:
        return None
    return {"vid": vid,
            "t": round(float(props.get("time-pos") or 0.0), 2),
            "paused": bool(props.get("pause")),
            "rate": float(props.get("speed") or 1.0)}


def _selected_channel() -> str:
    """The channel the user last selected (popup Tab / canvas controller both
    persist it via `media popup-channel --set`). The canvas video layer and
    controller follow this, so the two surfaces stay in sync."""
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    try:
        v = (state / "agent-media" / "popup-channel").read_text().strip()
    except OSError:
        return "speech"
    return v if v in ("speech", "music", "book") else "speech"


def _video_state() -> dict:
    """One video-sync snapshot across the channels that can carry a YouTube
    video: the book (red5's sink-book mpv — phone-fetched .mka cache files or
    raw-URL streams) and the music channel's phone mpv.

    The SELECTED channel's player wins when it has a video (even paused — the
    user is looking at that channel); otherwise prefer whichever player is
    actively playing, with a paused one only when nothing else is live. The
    event always carries "chan" so the page's controller follows the popup."""
    sel = _selected_channel()
    probes: dict = {}
    try:
        book_sock = (os.environ.get("MEDIA_BOOK_SOCKET")
                     or str(Path.home()
                            / ".local/state/agent-media/sink-book.sock"))
        if Path(book_sock).exists():
            probes["book"] = _probe_video(book_sock)
        from agent_media_core.sinks import music_local
        if music_local.configured():
            probes["music"] = _probe_video(music_local.endpoint())
    except Exception:  # noqa: BLE001
        pass
    pick = probes.get(sel)
    if pick is None:
        # Not on a video channel (or its player has nothing): only an
        # ACTIVELY PLAYING player may claim the screen. A paused player's
        # frozen video sitting over the speech canvas is noise — it returns
        # when its channel is selected or it resumes.
        candidates = [p for p in (probes.get("book"), probes.get("music")) if p]
        pick = next((c for c in candidates if not c["paused"]), None)
    if pick is None:
        return {"kind": "video", "vid": None, "chan": sel}
    return {"kind": "video", "ts": time.time(), "chan": sel, **pick}


def _video_poller() -> None:
    last_key = None
    while True:
        if HUB.watchers() == 0:
            time.sleep(3)
            continue
        ev = _video_state()
        # While a video is live, publish every poll (the position heartbeat the
        # page drift-corrects against); otherwise only on a change — the hide
        # transition, or a channel switch the controller must follow.
        key = (ev["vid"], ev.get("chan"))
        if ev["vid"] is not None or key != last_key:
            HUB.publish(ev)
        last_key = key
        time.sleep(5 if ev["vid"] else 3)


def ctl_argv(channel: str, action: str, arg: int,
             sarg: str = "") -> list[str] | None:
    """Whitelisted button/key → `media` argv. None = unknown/unsupported combo.
    The maps mirror the popup's handle_key dispatch. `sarg` carries free text
    for the popup's typed-seek (`s`) and open-URL (`o`) keys."""
    if action == "select" and channel in ("speech", "music", "book"):
        # Persist the channel choice — the popup opens on it, and the video
        # poller broadcasts it back so every canvas follows.
        return ["popup-channel", "--set", channel]
    if channel == "speech":
        table = {
            "bookmark": ["bookmark", "--channel", "speech"],
            "bookmark-end": ["bookmark", "--channel", "speech", "--range-end"],
            "toggle": ["toggle"],
            "prev": ["replay-prev", "--idx", str(arg)],
            "replay": ["replay", str(arg)],
            "jump-end": ["jump", "end"],
            "vol-": ["volume", "-5"],
            "vol+": ["volume", "5"],
            "mute": ["mute"],
            # M — durable "keep muted" of the popup subject (distinct from m).
            "mute-keep": ["mute-pane", "--subject", "toggle"],
            # v — toggle the copy-mode auto-highlight follow-along.
            "highlight": ["highlight-toggle"],
            # p — play the clip at the caller pane's copy-mode cursor.
            "clip-cursor": ["replay-at-cursor"],
            # g — focus the speaking pane in tmux.
            "goto": ["goto-pane"],
            # w — open the visual canvas.
            "web": ["speech-web"],
            "speed-": ["speed", "down"],
            "speed+": ["speed", "up"],
            "speed0": ["speed", "reset"],
            # Double-tap a line in the canvas transcript: play from there.
            # The index rides in `sarg` rather than `arg` because arg is
            # clamped to 1..999 for the repeat-count actions, and sentence
            # zero is the first thing anybody will double-tap.
            # The pane comes from the remembered reply rather than the
            # caller: the transcript on screen belongs to whoever spoke it, and
            # that is the reply a tap means — not whatever spoke most recently.
            "goto-sentence": ["skip", "--unit", "sentence",
                              "--to", sarg if sarg.isdigit() else "0"]
                             + (["--pane", _LAST_CLIP["key"]]
                                if _LAST_CLIP.get("key") else []),
            # h/l/H/L — the popup's sentence/paragraph steps.
            "skip-": ["skip", "--unit", "sentence", "--dir", "-1",
                      "--seek-fallback", "-5"],
            "skip+": ["skip", "--unit", "sentence", "--dir", "1",
                      "--seek-fallback", "5"],
            "para-": ["skip", "--unit", "paragraph", "--dir", "-1",
                      "--seek-fallback", "-30"],
            "para+": ["skip", "--unit", "paragraph", "--dir", "1",
                      "--seek-fallback", "30"],
        }
        return table.get(action)
    if channel in ("music", "book"):
        table = {
            "prev": [channel, "prev", "--restart-first"],
            "next": [channel, "next"],
            "vol-": [channel, "volume", "-5"],
            "vol+": [channel, "volume", "5"],
            "skip-": [channel, "seek", "-5"],
            "skip+": [channel, "seek", "+5"],
            "para-": [channel, "seek", "-30"],
            "para+": [channel, "seek", "+30"],
            # g — focus the channel's pane/UI (ncmpcpp / mpvc).
            "goto": ["goto-track" if channel == "music" else "goto-book"],
            # w — print the channel's web-UI URL (the browser opens it).
            "web": [f"{channel}-web"],
            # b — capture the current channel position.
            "bookmark": [channel, "bookmark"],
            "bookmark-end": [channel, "bookmark", "--range-end"],
        }
        # s / o — typed seek and open-URL both carry a free-text arg.
        if action == "seek-to" and sarg:
            return [channel, "seek", "--", sarg]
        if action == "open-url" and sarg:
            return [channel, "play", sarg]
        if action == "search":
            return ["search", channel] + ([sarg] if sarg else [])
        if action == "toggle":
            if channel == "music":
                return ["music", "toggle"]
            # The book channel has no `toggle`; derive it like the popup does:
            # only an actively-playing status pauses — paused OR idle resumes
            # (pausing an already-stopped channel is a no-op that strands you).
            status = _media(["book", "status", "--no-bar"])
            return ["book", "pause"] if status.startswith("▶") else ["book", "resume"]
        argv = table.get(action)
        return argv or None
    return None


# The page ships as three real files (static/canvas.{html,css,js} — lintable,
# highlightable, no double-escaped regexes) and is assembled once at import
# into the same single self-contained response as before: one request, no
# asset-version skew on a wall page that reconnects across deploys.
def _page() -> str:
    from importlib import resources
    static = resources.files(__package__) / "static"

    def read(name: str) -> str:
        return (static / name).read_text(encoding="utf-8")

    return (read("canvas.html")
            .replace("/*__CANVAS_CSS__*/", read("canvas.css"))
            .replace("//__CANVAS_JS__", read("canvas.js")))


PAGE = _page()

# The picture on its own, for when one is tapped rather than watched: full
# screen, and landscape with it. Small enough to stay one file — it has no SSE,
# no state and no channel, and splitting three files off a single <img> and one
# button would be filing for its own sake.
def _view_page() -> str:
    from importlib import resources
    return ((resources.files(__package__) / "static" / "view.html")
            .read_text(encoding="utf-8"))


VIEW_PAGE = _view_page()

# What THIS page is, so a screen can tell it is holding an old one.
#
# The page is assembled once at import, which is the right trade for serving it
# — but it means a canvas restarted behind a screen serves something that
# screen will never ask for. Nothing in the client ever reloaded the document:
# the SSE watchdog reconnects the stream, and the stream is not the page. So a
# wall left open across a deploy, a phone browser tab, and the app's WebView
# all keep whatever they loaded, for as long as they stay open. Reported, twice
# and reasonably, as "the canvas keeps reverting to the old version" — it never
# reverted, it never moved.
#
# A digest of the page rather than a boot id or a timestamp: canvases get
# restarted for reasons that have nothing to do with the page (a bind address
# changing, a crash, a machine rebooting), and blanking every screen in the
# house for those would be a worse fault than the one being fixed. Same bytes,
# same id, nobody reloads.
PAGE_ID = hashlib.sha256(PAGE.encode()).hexdigest()[:12]


# The endpoints a browser on another origin may reach. Everything here
# carries its own credential — the caller's Audiobookshelf bearer, handed back
# to ABS to ask who they are — and none of it is reachable with the ambient
# authority a browser attaches by itself, so opening them to any origin gives
# a drive-by page nothing it did not already have. The token-guarded routes
# (/input, /show, /ctl, /say, /play) are deliberately NOT here: their
# credential is ours, not the caller's, and CORS is what keeps a page you
# happen to be visiting from spending it.
#
# Needed because the web client is served from a different port than the
# canvas (Audiobookshelf on :13379, this on :8781). The Capacitor app never
# needed it — a native HTTP client is not subject to the same-origin policy.
_CORS_PATHS = frozenset({
    "/conversation", "/conversation/log", "/conversations", "/item",
    "/reply", "/ask", "/focus", "/session/resume", "/session/close",
})

# Long enough that a chat page's polling is not preceded by a preflight every
# time; short enough that a change here is picked up the same day.
_CORS_MAX_AGE = "3600"


# Cap request bodies: an unbounded Content-Length (e.g. 5 GB) would force a
# multi-GB read/alloc — a trivial remote OOM on a RAM-tight host (#139).
_MAX_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if os.environ.get("MEDIA_VISUAL_DEBUG") == "1":
            super().log_message(fmt, *args)

    def _cors(self) -> None:
        """Allow a browser on another origin, on the bearer-authed routes only.

        `*` rather than the caller's origin, and no Allow-Credentials: the
        credential is the Authorization header the client sets by hand, so the
        browser never attaches anything of its own to these.
        """
        if self.path.split("?", 1)[0] not in _CORS_PATHS:
            return
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "Content-Encoding")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        """CORS preflight. Anything not on the list is simply not allowed."""
        path = self.path.split("?", 1)[0]
        if path not in _CORS_PATHS:
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", _CORS_MAX_AGE)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _json_z(self, code: int, obj: dict) -> None:
        """JSON, compressed if the caller said it could take it.

        Audiobookshelf itself does not compress — it ignores Accept-Encoding
        and sends its item JSON whole, which on a conversation is 1.27 MB of
        highly repetitive text. Ours is already a tenth of that; gzip takes it
        to a fortieth. Only worth the CPU on a body big enough to matter.
        """
        body = json.dumps(obj).encode()
        accepts = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        if accepts and len(body) > 4096:
            import gzip as _gzip

            packed = _gzip.compress(body, 6)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(packed)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(packed)
            return
        self._send(code, body, "application/json")

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        if path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/pageid":
            # Which page this canvas would serve, for a client that cannot ask
            # the page itself. The SSE hello tells a RUNNING page it has gone
            # stale, which is no use to a page loaded before that handler
            # existed — and none at all to the app, whose WebView is a
            # container the page's own JS cannot reload out from under. So the
            # digest is also readable from outside, cheaply, with no stream.
            self._send(200, (PAGE_ID + "\n").encode(), "text/plain")
        elif path == "/healthz":
            self._send(200, b"ok\n", "text/plain")
        elif path == "/seen":
            # Read-only registry dump (debugging which screen would wake) —
            # open like /sessions; the POST twin is what mutates.
            with _VIEWERS_LOCK:
                snap = {n: {"age_s": round(time.time() - v["ts"], 1),
                            "focused": v["focused"]}
                        for n, v in _VIEWERS.items()}
            self._json(200, {"viewers": snap, "target": _wake_target()})
        elif path == "/events":
            self._sse()
        elif path == "/last":
            # When the canvas last received something to show — the reveal
            # flow polls this to know the image is up before speech resumes.
            last = HUB.last or {}
            self._json(200, {"t": last.get("t") or 0,
                             "kind": "sequence" if last.get("sequence")
                                     else "image" if last.get("image") else None})
        elif path == "/sessions":
            # Read-only (session names + speaker) — open on the tailnet-bound
            # server; only /input (keystroke injection) stays gated.
            speaker = _last_speaker()
            self._json(200, {
                "speaker": ({"label": speaker.get("tmux_session")
                             or "last speaker"} if speaker else None),
                "amux": [s["name"] for s in _amux_sessions()],
            })
        elif path == "/agents":
            # Live session states for the agent strip ("who needs me") — read-
            # only, so open on the tailnet-bound server (only /input is gated).
            # amux-registered sessions + auto-discovered Claude Code tmux panes,
            # memoized ~2s so concurrent canvases don't each fork the sweep (#141).
            self._json(200, {"agents": _agents_payload()})
        elif path == "/peek":
            # A pane's Claude Code session as assistant turns (read-only, open
            # like /agents) — latest turn full, older ones collapsible snapshots.
            # parse_qs percent-decodes the client's encodeURIComponent exactly
            # once — downstream must treat the pane id as raw from here.
            pane = (parse_qs(query).get("pane") or [""])[0]
            self._json(200, {"pane": pane, "turns": _pane_turns(pane)})
        elif path == "/pair":
            code = (parse_qs(query).get("c") or [""])[0]
            token = _amux_token()
            if not token:
                self._send(503, b"no amux token configured on the host\n",
                           "text/plain")
            elif _pair_consume(code):
                self._send(200, (_PAIR_PAGE % json.dumps(token)).encode(),
                           "text/html; charset=utf-8")
            else:
                self._send(403, b"invalid or expired pairing code\n",
                           "text/plain")
        elif path == "/item":
            # The library item, carrying only what the app reads. Sasonica asks
            # here first and falls back to Audiobookshelf, so this is a way of
            # being quick rather than a thing to depend on. See item.py for the
            # measurements and for what is left out.
            from . import item as _item
            item_id = parse_qs(self.path.partition("?")[2]).get("id", [""])[0]
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer").strip()
            ok, detail = _item.item_for_app(item_id, bearer)
            if ok:
                self._json_z(200, detail)
            else:
                self._json(detail.pop("status", 404), {"ok": False, **detail})
        elif path == "/conversation":
            # "Is this item a conversation I can reply to?" — what the app asks
            # before it draws the reply box. Authed by the caller's own ABS
            # bearer, like /reply. `?session=` instead of `?item=` asks the
            # other way round: a session the phone just started (see /ask),
            # and whether the library has an item for it yet.
            from . import reply as _reply
            qs = parse_qs(self.path.partition("?")[2])
            item = qs.get("item", [""])[0]
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer").strip()
            if qs.get("session", [""])[0] and not item:
                ok, detail = _reply.conversation_for_session(qs["session"][0], bearer)
            else:
                ok, detail = _reply.conversation(item, bearer)
            self._json(200 if ok else detail.pop("status", 404),
                       {"ok": ok, **detail})
        elif path == "/conversation/log":
            # The same conversation, read rather than heard.
            from . import reply as _reply
            item = parse_qs(self.path.partition("?")[2]).get("item", [""])[0]
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer").strip()
            ok, detail = _reply.log_for_item(item, bearer)
            self._json(200 if ok else detail.pop("status", 404),
                       {"ok": ok, **detail})
        elif path == "/conversations":
            # What the assistant button can be pointed at: live sessions and
            # recent conversations, by title. Gated like /conversation.
            # (/sessions is taken: the amux list the popup reads.)
            from . import reply as _reply
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer").strip()
            user, status = _reply.abs_identity(bearer)
            allowed = bool(user) and _reply.may_reply(user)[0]
            if not allowed:
                self._json(403 if user else _reply._identity_error(status).get("status", 401),
                           {"ok": False, "error": "not allowed"})
            else:
                self._json(200, {"ok": True, "sessions": _reply.sessions_index()})
        elif path == "/speech":
            # One-shot speech-state peek for outside agents (a voice-mode
            # Claude asking "is the phone talking, and about what?" through
            # the tmux relay's read-only fast lane). Same snapshot the SSE
            # poller broadcasts, plus the recent start/end breadcrumbs.
            st = speech_state()
            st["events"] = _speech_events(20)
            st["local_audio"] = _local_audio_playing()
            self._json(200, st)
        elif path == "/status":
            channel = (parse_qs(query).get("channel") or [""])[0]
            if channel not in ("music", "book"):
                channel = "speech"
            self._json(200, channel_status(channel))
        elif path.startswith("/img/"):
            self._image(path[len("/img/"):], query)
        elif path.startswith("/persona/"):
            self._persona(path[len("/persona/"):])
        else:
            self._send(404, b"not found\n", "text/plain")

    def _persona(self, rel: str) -> None:
        # /persona/<slug>/<file> — a persona portrait sprite (no traversal).
        parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
        if len(parts) != 2:
            self._send(404, b"not found\n", "text/plain")
            return
        f = _persona_dir() / os.path.basename(parts[0]) / os.path.basename(parts[1])
        if not f.is_file():
            self._send(404, b"no such portrait\n", "text/plain")
            return
        data = f.read_bytes()
        ext = f.suffix.lstrip(".").lower()
        ctype = {"png": "image/png", "webp": "image/webp", "jpg": "image/jpeg",
                 "jpeg": "image/jpeg", "gif": "image/gif"}.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _wants_viewer(self, query: str) -> bool:
        """Is this a person navigating to the picture, or a page loading it?

        The same URL has to answer both. Sasonica shows the picture under a
        message as an ``<img>`` and opens the very same address in the browser
        when it is tapped — so the address that must return bytes to the chat
        must return a page to the tap, and neither end can be asked to know
        which is which.

        ``Sec-Fetch-Dest`` is exactly that question, asked by the browser and
        not forgeable by the page: ``document`` for a top-level navigation,
        ``image`` for an ``<img>``. Where it is absent (curl, an old client, a
        native HTTP plugin) the answer is bytes — the behaviour this route has
        always had. A viewer is an improvement on a tap, never a condition of
        the picture loading, so every ambiguous case falls to the raw file.

        ``?raw=1`` declines the viewer outright (it is how the viewer page asks
        for its own picture) and ``?view=1`` asks for it, so a link can be
        deliberate without relying on a header at all.
        """
        q = parse_qs(query)
        if q.get("raw"):
            return False
        if q.get("view"):
            return True
        return self.headers.get("Sec-Fetch-Dest", "").lower() == "document"

    def _image(self, name: str, query: str = "") -> None:
        name = os.path.basename(name)  # no traversal
        f = spool_dir() / name
        if not f.is_file():
            self._send(404, b"no such image\n", "text/plain")
            return
        if self._wants_viewer(query):
            # The viewer reads the picture off its own address, so there is
            # nothing to substitute into it and nothing to escape.
            self._send(200, VIEW_PAGE.encode(), "text/html; charset=utf-8")
            return
        data = f.read_bytes()
        ctype = "image/webp" if name.endswith(".webp") else \
                "image/svg+xml" if name.endswith(".svg") else \
                "image/png" if name.endswith(".png") else "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Spool names are unique per image, safe to cache hard.
        self.send_header("Cache-Control", "max-age=86400, immutable")
        self.end_headers()
        self.wfile.write(data)

    def _sse(self) -> None:
        q = HUB.attach()
        if q is None:                       # over the client cap → shed load (#137)
            self._send(503, b"too many canvas clients\n", "text/plain")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            # First frame on every connection, including every reconnect: this
            # is the page you should be running. A client holding another one
            # reloads itself. Reconnects are exactly when a restart has
            # happened, so this costs one small frame and needs no polling.
            self._event({"kind": "hello", "page": PAGE_ID})
            if HUB.last is not None:
                self._event(HUB.last)
            if HUB.last_state is not None:
                self._event(HUB.last_state)
            if HUB.last_video is not None:
                self._event(HUB.last_video)
            while True:
                try:
                    self._event(q.get(timeout=15))
                except queue.Empty:
                    # A real data frame, not an SSE `: comment` — EventSource
                    # ignores comments, so a comment heartbeat can't drive the
                    # client's stall watchdog. onmessage fires on this (#137).
                    self.wfile.write(b'data: {"kind":"ping"}\n\n')
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.detach(q)

    def _event(self, event: dict) -> None:
        self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
        self.wfile.flush()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        # Reject oversized bodies before reading a byte (#139).
        try:
            clen = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            clen = 0
        if clen > _MAX_BODY:
            self._send(413, b"request body too large\n", "text/plain")
            return
        # Every state-changing POST needs the same auth as /input (#138):
        # otherwise a drive-by page can speak, play audio, spoof screens, or
        # drive media (CSRF). Read-only GET endpoints stay open by design.
        if path in ("/show", "/ctl", "/say", "/play"):
            if not _authorized(self):
                print(f"ctl: 401 unauthorized for {path} "
                      f"from {self.client_address[0]}", file=sys.stderr)
                self._json(401, {"error": "unauthorized"})
                return
        if path == "/show":
            self._show()
        elif path == "/seen":
            # Screen-activity beacon. Identity comes from the tailnet source
            # IP — a caller can only name itself — so no pairing needed. An
            # EXPLICIT ?screen override still demands the token (it's the one
            # form that could redirect wakes elsewhere).
            body = self._read_json() or {}
            focused = body.get("focused")
            focused = True if focused is None else bool(focused)
            blank = body.get("blank")
            blank = None if blank is None else bool(blank)
            explicit = str(body.get("screen") or "")
            if explicit and _authorized(self):
                _viewer_seen(explicit, focused, blank)
            else:
                _viewer_seen(_screen_from_ip(self.client_address[0]),
                             focused, blank)
            self._json(200, {"ok": True})
        elif path == "/ctl":
            self._ctl()
        elif path == "/input":
            if not _authorized(self):
                self._json(401, {"error": "unauthorized"})
                return
            body = self._read_json() or {}
            ok, detail = send_input(str(body.get("text") or ""),
                                    str(body.get("target") or "speaker"))
            self._json(200 if ok else 400, {"ok": ok, "detail": detail})
        elif path == "/reply":
            # Reply to a conversation from inside the Audiobookshelf player.
            # Deliberately NOT gated by _authorized: the credential here is the
            # caller's own ABS bearer, verified with ABS, so the phone carries
            # no secret of ours. See reply.py and the proposal.
            from . import reply as _reply
            body = self._read_json() or {}
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer").strip()
            ok, detail = _reply.reply(
                str(body.get("item") or ""), str(body.get("text") or ""), bearer,
                quote=str(body.get("quote") or ""),
                mode=str(body.get("mode") or "continue"))
            status = detail.pop("status", 400)
            if not ok:
                # The item id too: a refusal that names only the reason
                # cannot be told apart from the next one, and "no such item"
                # is a question about WHICH item was asked for.
                print(f"reply: refused {status} ({detail.get('error')}) "
                      f"for item {str(body.get('item') or '')!r} "
                      f"from {self.client_address[0]}", file=sys.stderr)
            self._json(200 if ok else status, {"ok": ok, **detail})
        elif path == "/ask":
            # A fresh session from the phone: the assistant button, or "new
            # chat" in the app. Gated like /reply — the ABS bearer is the
            # credential — and it lands in the scratch tmux session.
            from . import reply as _reply
            body = self._read_json() or {}
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer").strip()
            ok, detail = _reply.ask_routed(
                str(body.get("text") or ""), bearer,
                target=str(body.get("target") or ""),
                player_item=str(body.get("player_item") or ""),
                sticky=str(body.get("sticky") or ""),
                parse=body.get("parse", True) is not False,
                dry=body.get("dry") is True)
            status = detail.pop("status", 400)
            if not ok:
                print(f"ask: refused {status} ({detail.get('error')}) "
                      f"from {self.client_address[0]}", file=sys.stderr)
            self._json(200 if ok else status, {"ok": ok, **detail})
        elif path in ("/session/resume", "/session/close"):
            # Managing the session behind a conversation from the app: bring
            # it back in a tmux window, or close the pane it runs in. Gated
            # like /reply.
            from . import reply as _reply
            body = self._read_json() or {}
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer").strip()
            fn = _reply.session_resume if path.endswith("resume") else _reply.session_close
            ok, detail = fn(str(body.get("session") or ""), bearer)
            self._json(200 if ok else detail.pop("status", 400), {"ok": ok, **detail})
        elif path == "/focus":
            # The "opened in %23" link: pull the attached tmux client to a pane.
            from . import reply as _reply
            body = self._read_json() or {}
            bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer").strip()
            allowed = _authorized(self) or _reply.may_reply(_reply.abs_identity(bearer)[0])[0]
            if not allowed:
                self._json(401, {"error": "unauthorized"})
                return
            ok, detail = _reply.focus(str(body.get("pane") or ""))
            self._json(200 if ok else 400, {"ok": ok, "detail": detail})
        elif path == "/play":
            # Replay a pane's last spoken clip — open like /agents (plays audio,
            # never injects keystrokes).
            body = self._read_json() or {}
            ok = _play_pane(str(body.get("pane") or ""))
            self._json(200 if ok else 400, {"ok": ok})
        elif path == "/say":
            # Speak arbitrary text (a peeked turn) — open, plays audio only.
            body = self._read_json() or {}
            ok = _say(str(body.get("text") or ""))
            self._json(200 if ok else 400, {"ok": ok})
        else:
            self._send(404, b"not found\n", "text/plain")

    def _read_json(self) -> dict | None:
        try:
            # Never read past the cap even if a caller reached here without the
            # do_POST guard (defence in depth for the #139 OOM).
            n = min(int(self.headers.get("Content-Length", "0")), _MAX_BODY)
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return None

    def _show(self) -> None:
        body = self._read_json()
        if body is None:
            self._send(400, b"bad json\n", "text/plain")
            return

        # A bare spool filename becomes a canvas-relative /img/ URL, so the
        # event works from any screen regardless of how it reached us.
        def ref(img: str) -> str:
            return img if "/" in img else "/img/" + img

        event: dict = {
            "caption": (body.get("caption") or None),
            "prompt": (body.get("prompt") or None),
            "purpose": (body.get("purpose")
                        if body.get("purpose") in ("figure", "portrait") else None),
            # Which session's reply this visual belongs to — the page dims a
            # figure while a DIFFERENT session is speaking (else a stale
            # diagram reads as belonging to whatever voice is talking).
            "session": (str(body.get("session"))[:80]
                        if body.get("session") else None),
            "t": int(time.time()),
        }
        seq = body.get("sequence")
        if isinstance(seq, list) and seq:
            beats = []
            for entry in seq:
                img = str((entry or {}).get("image") or "").strip()
                if not img:
                    continue
                try:
                    at = max(0.0, min(1.0, float(entry.get("at") or 0)))
                except (TypeError, ValueError):
                    at = 0.0
                beats.append({"image": ref(img), "at": at})
            if not beats:
                self._send(400, b"empty sequence\n", "text/plain")
                return
            event["sequence"] = beats
            for k in ("estdur", "gen_secs"):
                try:
                    event[k] = max(0.0, float(body.get(k) or 0))
                except (TypeError, ValueError):
                    pass
        else:
            image = str(body.get("image") or "").strip()
            if not image:
                self._send(400, b"missing image\n", "text/plain")
                return
            event["image"] = ref(image)
        # Stamp the screen worth waking (most recently active viewer) so each
        # host's wake agent can decide "is that me?" locally.
        wake = _wake_target()
        if wake:
            event["wake"] = wake
        HUB.publish(event)
        self._send(200, b"shown\n", "text/plain")

    def _ctl(self) -> None:
        body = self._read_json()
        if body is None:
            self._json(400, {"ok": False, "err": "bad json"})
            return
        channel = str(body.get("channel") or "")
        action = str(body.get("action") or "")
        sarg = str(body.get("sarg") or "")[:512]
        try:
            arg = max(1, min(999, int(body.get("arg") or 1)))
        except (TypeError, ValueError):
            arg = 1
        argv = ctl_argv(channel, action, arg, sarg)
        if argv is None:
            print(f"ctl: unknown action {channel}/{action}", file=sys.stderr)
            self._json(400, {"ok": False, "err": "unknown action"})
            return
        out = _media(argv)
        # Logged because a control that does nothing is indistinguishable from
        # a control that was never asked to do anything, and telling those two
        # apart has cost days. One line per action, with what `media` said.
        print(f"ctl: {channel}/{action} {argv} -> {out.strip()[:120]!r}",
              file=sys.stderr)
        self._json(200, {"ok": True, "out": out})


def main() -> None:
    from agent_media_core.intake._env import load_env_file
    load_env_file("visual-canvas")
    if sys.argv[1:2] == ["pair"]:            # `media-visual-canvas pair`
        raise SystemExit(_cmd_pair(sys.argv[2:]))
    ap = argparse.ArgumentParser(description="agent-media visual canvas")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("MEDIA_VISUAL_PORT") or DEFAULT_PORT))
    ap.add_argument("--bind", default=os.environ.get("MEDIA_VISUAL_BIND") or "0.0.0.0")
    args = ap.parse_args()
    _last_clip_load()      # a restart should not empty the transcript
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=_state_poller, daemon=True).start()
    if os.environ.get("MEDIA_VISUAL_VIDEO", "1") != "0":
        threading.Thread(target=_video_poller, daemon=True).start()
    print(f"canvas on http://{args.bind}:{args.port}/  spool={spool_dir()}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
