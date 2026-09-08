"""Reply from the player: an Audiobookshelf listener types back into a session.

You are listening to a past conversation in Sasonica (the ABS app fork). A box
under the player takes a line of text and puts it into the Claude Code session
that produced what you are hearing — reviving it in a tmux window if it has
since ended.

The design, and the arguments for it, are in
`docs/proposals/2026-09-04-reply-from-the-player.md`. Three things it settles
that this module implements:

* **The ABS login is the credential.** The phone sends the bearer it already
  holds; we hand that bearer straight back to ABS's own `POST /api/authorize`
  and believe what it says. No secret is provisioned to the phone, and the
  amux token stays exactly as it was for the browser and the OWUI pipe. Using
  the *caller's* token (not the service token) to look the item up is
  deliberate: ABS then enforces its own library permissions for us.
* **Typing is a capability, not a role.** ABS has no permission meaning "may
  type into an agent", so the allow-list lives here — root by default, anyone
  else by name in `MEDIA_REPLY_USERS`.
* **A dead session is revived, not refused.** In a background window, in the
  attached tmux client, because Claude Code's TUI will not start without one.

Config (env):
  MEDIA_REPLY_USERS   comma-separated ABS usernames allowed to type (in
                      addition to root)
  MEDIA_REPLY_ROOT    "0" to stop treating the ABS root account as allowed
  MEDIA_REPLY_TMUX    tmux session to open revived windows in (default: the
                      one with an attached client)
  MEDIA_ASK_SESSION   amux session name whose registration (directory, flags)
                      a fresh session started from the phone copies
                      (default: scratch); MEDIA_ASK_TMUX / MEDIA_ASK_CWD /
                      MEDIA_ASK_FLAGS override its parts
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("agent-media.visual.reply")

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# How long to wait for a revived pane's TUI to accept input, and how often to
# look. Claude Code takes a few seconds to paint; typing before it is up drops
# the text on the floor, so this is a readiness probe, not a sleep.
READY_TIMEOUT_S = float(os.environ.get("MEDIA_REPLY_READY_TIMEOUT") or 45.0)
READY_POLL_S = 0.5


# --- who is asking ------------------------------------------------------------

# ABS is asked once per token per minute, not once per keystroke — /authorize
# is a database read on their side and this sits in the path of a POST a human
# made by hand.
_IDENT_TTL_S = 60.0
_IDENT_LOCK = threading.Lock()
_IDENT: dict[str, tuple[float, dict | None]] = {}


def _abs_url() -> str:
    from agent_media_core import library

    url, _token, _lib = library._abs_cfg()
    return url


def abs_urls() -> list[str]:
    """Every Audiobookshelf this canvas will speak to, likeliest first.

    One host can run more than one server — a second one to try a new client
    against, say — and the app sends the bearer of whichever it is signed in
    to. A bearer means nothing to the server that did not issue it, so "who is
    this?" has to be asked of each in turn rather than only of the one we
    publish to.

    This is an allow-list, and deliberately not built from anything the caller
    says: the caller's own token is forwarded to whatever is on it, so a
    caller-named address would be a way to have us post their login to a host
    of their choosing.

    Extra servers go in `ABS_URLS` in ~/.config/agent-media/abs-bridge.env
    (comma-separated), beside the ABS config that is already there, or in
    MEDIA_ABS_URLS for a one-off. With neither set this is exactly the single
    configured server it always was.
    """
    extra = os.environ.get("MEDIA_ABS_URLS") or ""
    try:
        for line in (Path.home() / ".config" / "agent-media"
                     / "abs-bridge.env").read_text().splitlines():
            line = line.strip()
            if line.startswith("ABS_URLS=") and not extra:
                extra = line.split("=", 1)[1].strip().strip('"\'')
    except OSError:
        pass
    out, seen = [], set()
    for u in [_abs_url()] + extra.split(","):
        u = (u or "").strip().rstrip("/")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def abs_home(bearer: str) -> str:
    """Which Audiobookshelf this bearer belongs to, if we have found out.

    Only meaningful after `abs_identity`, which is what does the finding; on
    its own it answers with the server we publish to, which is the right guess
    and the only one worth making.
    """
    with _IDENT_LOCK:
        hit = _IDENT.get((bearer or "").strip())
    if hit and time.monotonic() - hit[0] < _IDENT_TTL_S and len(hit) > 2:
        return hit[2]
    return _abs_url()


def _abs_get(url: str, bearer: str, path: str,
             method: str = "GET") -> tuple[dict | None, int]:
    """`(body, status)`. Status 0 means Audiobookshelf could not be reached.

    The status matters: "your token is no good" and "the server did not
    answer" look identical from here otherwise, and they need opposite
    responses — one should send the app off to refresh its token, the other
    must not, because a failed refresh logs the user out.
    """
    req = urllib.request.Request(
        url + path, method=method,
        headers={"Authorization": f"Bearer {bearer}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except (urllib.error.URLError, OSError, ValueError):
        return None, 0


def abs_identity(bearer: str) -> tuple[dict | None, int]:
    """`(user, status)` for this bearer — who ABS says it belongs to.

    Asks ABS the same question the app asks at startup. Only *successes* are
    cached: caching a refusal meant one transient failure refused every reply
    for the next minute, which is exactly how this first went wrong.
    """
    bearer = (bearer or "").strip()
    if not bearer:
        return None, 401
    now = time.monotonic()
    with _IDENT_LOCK:
        hit = _IDENT.get(bearer)
        if hit and now - hit[0] < _IDENT_TTL_S:
            return hit[1], 200
    urls = abs_urls()
    if not urls:
        return None, 0
    # Asked of each server until one recognises the token. A refusal from a
    # server that did not issue it is not news, so a 401 is only the answer
    # once every one of them has said it.
    worst = 0
    for url in urls:
        body, status = _abs_get(url, bearer, "/api/authorize", method="POST")
        user = (body or {}).get("user") if isinstance(body, dict) else None
        user = user if isinstance(user, dict) and user.get("username") else None
        if user:
            with _IDENT_LOCK:
                _IDENT[bearer] = (now, user, url)
            return user, 200
        if status:
            worst = status if worst in (0, 401) or status == 401 else worst
    return None, worst


def _identity_error(status: int) -> dict:
    """Turn "ABS would not tell us who this is" into an answer the app can act
    on.

    401 is the only status that should reach the app as 401, because the app
    answers a 401 by refreshing its token and retrying — and if that refresh
    fails it logs the user out. An Audiobookshelf that is merely down must
    therefore never come back as 401: it would end the session over an outage.
    """
    if status == 401:
        return {"error": "Audiobookshelf rejected that login", "status": 401}
    if status in (0, 502, 503, 504):
        return {"error": "Audiobookshelf did not answer", "status": 503}
    return {"error": f"Audiobookshelf answered {status}", "status": 502}


def may_reply(user: dict | None) -> tuple[bool, str]:
    """Whether this ABS user may type into a session, and why not if not.

    Root is allowed by default: on a single-user server root is the owner, and
    making the sole admin edit a config file to talk to their own agents is
    friction that buys nothing. Everyone else is named explicitly — by
    username, not by type, because `admin` is a library-management role and
    someone trusted with metadata is not thereby trusted with a keyboard.
    """
    if not user:
        return False, "not signed in to Audiobookshelf"
    name = str(user.get("username") or "")
    if user.get("type") == "root" and (os.environ.get("MEDIA_REPLY_ROOT") or "1") != "0":
        return True, name
    allowed = {u.strip() for u in (os.environ.get("MEDIA_REPLY_USERS") or "").split(",") if u.strip()}
    if name in allowed:
        return True, name
    return False, f"{name} is not allowed to reply"


# --- which conversation, and so which session ---------------------------------


def _manifest_dir() -> Path:
    from agent_media_core._paths import state_dir

    return state_dir() / "book-tracks"


def _tail(path: str) -> str:
    """The `<author>/<title>` tail two ABS and this host agree on.

    ABS reports the path inside its own mount; we know the host's, and nothing
    tells either how the other maps. `_find_item` in book_tracks matches on the
    same tail going the other way.
    """
    parts = [p for p in str(path).replace("\\", "/").split("/") if p]
    return "/".join(parts[-2:])


def session_for_item(item_id: str, bearer: str) -> tuple[str | None, str]:
    """The session uuid behind an ABS library item, or (None, why not).

    Looked up with the *caller's* bearer, so an item they cannot see is an item
    they cannot reply to, decided by ABS.
    """
    item_id = (item_id or "").strip()
    if not item_id:
        return None, "no item id"
    url = abs_home(bearer)
    if not url:
        return None, "no Audiobookshelf configured on this host"
    item, _status = _abs_get(url, bearer, f"/api/items/{item_id}")
    if not item:
        return None, "no such item"
    return session_for_path(item.get("path") or "")


def session_for_path(path: str) -> tuple[str | None, str]:
    """The session uuid behind an item's folder, or (None, why not).

    The half of `session_for_item` that needs no server: a caller already
    holding the item (the `/item` route does) can ask this directly.
    """
    tail = _tail(path or "")
    if not tail:
        return None, "item has no path"
    for f in sorted(_manifest_dir().glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if _tail(data.get("folder") or "") == tail:
            return str(data.get("session") or f.stem), ""
    return None, "not a conversation (no session behind it)"


# --- is that session live, and where ------------------------------------------


def live_sessions() -> dict[str, str]:
    """`{session uuid: pane}` for every Claude Code process in a tmux pane.

    Same detection as `claude-resume`, and for the same reason: a session's
    uuid is in argv when it was resumed, and only in the SessionStart registry
    when it was started fresh. Driven off live processes either way, so a stale
    registry entry cannot resurrect a dead session on a recycled pane.
    """
    reg = Path.home() / ".claude" / "tmux-sessions"
    live: dict[str, str] = {}
    for d in glob.glob("/proc/[0-9]*"):
        try:
            cmd = Path(d, "cmdline").read_bytes().split(b"\0")
            if not cmd or not cmd[0] or os.path.basename(cmd[0].decode(errors="replace")) != "claude":
                continue
            env = Path(d, "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        pane = next((e[len(b"TMUX_PANE="):].decode(errors="replace")
                     for e in env if e.startswith(b"TMUX_PANE=")), "")
        if not pane:
            continue
        m = _UUID.search(b" ".join(cmd).decode(errors="replace"))
        sid = m.group(0) if m else ""
        if not sid:
            try:
                parts = (reg / pane.lstrip("%")).read_text().split()
            except OSError:
                parts = []
            # Trust the entry only if it names THIS claude's pid (or no pid at
            # all — legacy rows, kept so long-running sessions aren't dropped).
            if parts and (len(parts) < 2 or parts[1] == os.path.basename(d)):
                sid = parts[0]
        if sid:
            live[sid] = pane
    return live


def transcript_cwd(session: str) -> str:
    """The working directory a session ran in, from its own transcript."""
    for f in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session}.jsonl")):
        try:
            with open(f) as fh:
                for line in fh:
                    if '"cwd"' not in line:
                        continue
                    cwd = json.loads(line).get("cwd") or ""
                    if cwd:
                        return cwd
        except (OSError, ValueError):
            continue
    return ""


def session_exists(session: str) -> bool:
    return bool(glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session}.jsonl")))


# --- the ghost prompt -----------------------------------------------------------

_PROMPT_GLYPH = "\u276f"   # ❯ — the input line starts with it
_SGR = re.compile(r"\x1b\[([0-9;]*)m")


def _capture_pane(pane: str) -> str:
    """The bottom of `pane` with its colours, which is where the ghost lives."""
    return _tmux(["capture-pane", "-t", pane, "-p", "-e", "-S", "-40"])


def _dim_runs(line: str) -> list[tuple[str, bool]]:
    """`(text, dim)` runs of one captured line, following SGR state along it."""
    out: list[tuple[str, bool]] = []
    dim = False
    pos = 0
    for m in _SGR.finditer(line):
        if m.start() > pos:
            out.append((line[pos:m.start()], dim))
        params = [x for x in m.group(1).split(";") if x] or ["0"]
        i = 0
        while i < len(params):
            p = params[i]
            if p in ("38", "48", "58") and i + 1 < len(params):
                # A colour, whose arguments may well be "2" (truecolor)
                i += 5 if params[i + 1] == "2" else 3
                continue
            if p == "0":
                dim = False
            elif p == "2":
                dim = True
            elif p == "22":
                dim = False
            i += 1
        pos = m.end()
    if pos < len(line):
        out.append((line[pos:], dim))
    return out


def ghost_prompt(pane: str) -> str:
    """Claude Code's suggested next prompt, read off the screen. "" if none.

    When a turn ends the TUI writes a follow-up it thinks you might type next
    onto the input line — dim, accepted with Tab, gone the moment you type.
    It exists nowhere but on that screen: the transcript records that a
    suggestion was accepted, never its words. So it is scraped. The input
    line is the one starting with the prompt glyph; the suggestion is the dim
    run after it, continued onto the lines below while those are dim too
    (a long one wraps). Typed text is not dim, so a half-typed reply reads as
    no suggestion — right, since the ghost has already left the screen.
    """
    cap = _capture_pane(pane)
    if not cap:
        return ""
    lines = cap.split("\n")
    start = next((i for i in range(len(lines) - 1, -1, -1)
                  if _SGR.sub("", lines[i]).lstrip().startswith(_PROMPT_GLYPH)), None)
    if start is None:
        return ""
    words: list[str] = []
    first = True
    for line in lines[start:]:
        runs = _dim_runs(line)
        on_prompt_line = first
        if first:
            # Drop everything up to and including the glyph itself.
            text = "".join(t for t, _ in runs)
            cut = text.index(_PROMPT_GLYPH) + 1
            trimmed: list[tuple[str, bool]] = []
            seen = 0
            for t, d in runs:
                if seen + len(t) <= cut:
                    seen += len(t)
                    continue
                trimmed.append((t[max(0, cut - seen):], d))
                seen += len(t)
            runs = trimmed
            first = False
        plain = "".join(t for t, d in runs if not d).strip("\u00a0 ")
        dim = "".join(t for t, d in runs if d).strip("\u00a0 ")
        if plain and on_prompt_line:
            return ""            # something typed: the ghost has already gone
        if plain or not dim:
            break                # the box's bottom rule, or a bare line
        words.append(dim)
    return " ".join(" ".join(words).split())


# --- reviving a session that has ended -----------------------------------------


def _tmux(argv: list[str], timeout: int = 10) -> str:
    try:
        out = subprocess.run(["tmux", *argv], capture_output=True, text=True,
                             timeout=timeout, check=False)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def attached_session() -> str:
    """A tmux session with a client attached to it, or "".

    Claude Code's TUI will not start without one — a detached `new-session`
    gives you a pane that dies on startup, which cost a previous session an
    afternoon. So a revived window goes into a session someone is looking at.
    """
    want = (os.environ.get("MEDIA_REPLY_TMUX") or "").strip()
    if want:
        return want
    clients = _tmux(["list-clients", "-F", "#{client_session}"])
    return clients.splitlines()[0] if clients else ""


# Resuming a long session opens a modal before the TUI: "Resume from summary
# (recommended) / Resume full session as-is / Don't ask me again". A revived
# window sits on it forever otherwise. Enter takes the highlighted recommended
# option, which is also the right one here — a reply does not need the whole
# transcript re-read, and the full resume is what blows through usage limits.
_RESUME_PROMPT = re.compile(r"Resume from summary|Resume full session")


def pane_ready(pane: str) -> bool:
    """Whether a Claude Code TUI in `pane` is painted and taking input.

    Answers the resume-choice modal if it is up. Pressing Enter into a pane is
    exactly what `send_input` refuses to do blind — the difference is that this
    window is one we opened seconds ago and the text on it is matched first.
    """
    from . import canvas

    cap = canvas._strip_ansi(canvas._run(["tmux", "capture-pane", "-t", pane, "-p", "-S", "-40"]))
    if canvas._classify_cc(cap) is not None:
        return True
    if _RESUME_PROMPT.search(cap):
        _tmux(["send-keys", "-t", pane, "Enter"])
    return False


# A tmux session with nobody attached cannot host a Claude Code TUI (see
# `attached_session`), and the scratch session a phone-started chat goes into
# is exactly the kind nobody is looking at. So a client is held on it:
# `script` gives `tmux` the tty it insists on, and `new-session -A` creates
# the session attached from its first moment — which matters twice over. A
# session born detached loses its only window to the after-new-session hook
# (tmux-claude-resume respawns an unattended window as a `claude --resume`,
# which then dies for want of a client, and the empty session goes with it);
# the hook skips sessions that have a client. And the holder lives in its own
# transient systemd unit, not this process: the canvas restarts on every
# deploy, and a chat started from the phone must not die with it.
_HOLD_UNIT = "agent-media-tmux-hold-{host}"
_HOLDERS: dict[str, subprocess.Popen] = {}
_HOLDER_LOCK = threading.Lock()


def _has_client(host: str) -> bool:
    return bool(_tmux(["list-clients", "-t", f"={host}", "-F", "#{client_tty}"]))


def _holder_argv(host: str, cwd: str) -> list[str]:
    # $SHELL runs `script -c`, and under the user manager that is zsh, whose
    # `=word` expansion eats a `-t =name` target — so the shell is pinned and
    # the target is `-s name`, which needs no exact-match prefix.
    return ["script", "-qfc", f"tmux new-session -A -s {shlex.quote(host)} -c {shlex.quote(cwd)}",
            "/dev/null"]


def hold_client(host: str, cwd: str = "") -> bool:
    """Keep a client attached to tmux session `host`, creating it if need be.

    Whether one is attached by the time this returns.
    """
    if _has_client(host):
        return True
    cwd = cwd or os.path.expanduser("~")
    env = {"TERM": "xterm-256color", "SHELL": "/bin/sh"}
    with _HOLDER_LOCK:
        held = _HOLDERS.get(host)
        if held is None or held.poll() is not None:
            unit = _HOLD_UNIT.format(host=re.sub(r"[^A-Za-z0-9_.-]", "-", host))
            argv = _holder_argv(host, cwd)
            spawned = False
            if shutil.which("systemd-run"):
                # A stopped unit of that name may linger failed; clear it.
                subprocess.run(["systemctl", "--user", "reset-failed", f"{unit}.service"],
                               capture_output=True, check=False)
                r = subprocess.run(["systemd-run", "--user", "--collect", f"--unit={unit}",
                                    *[f"--setenv={k}={v}" for k, v in env.items()], *argv],
                                   capture_output=True, text=True, check=False)
                spawned = r.returncode == 0
                if not spawned:
                    log.warning("reply: systemd-run holder failed (%s); holding in-process",
                                r.stderr.strip()[:200])
            if not spawned:
                try:
                    _HOLDERS[host] = subprocess.Popen(
                        argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True,
                        env={**os.environ, **env})
                except OSError as e:
                    log.warning("reply: could not hold a client on %s (%s)", host, e)
                    return False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _has_client(host):
            return True
        time.sleep(0.2)
    return False


def ensure_host(host: str, cwd: str) -> bool:
    """Make tmux session `host` exist, with a client on it. Whether it does."""
    return hold_client(host, cwd)


def _claude_bin() -> str:
    """Where `claude` is, by absolute path.

    A window's command runs with whatever PATH the tmux session was born
    with, and a session the canvas made from under systemd has the user
    manager's — no ~/.local/bin, no bun, no npm — so `claude` was "not found"
    in a window that looked exactly like a working one.
    """
    home = Path.home()
    extra = [home / ".local" / "bin", home / ".bun" / "bin", home / ".npm-global" / "bin",
             home / ".claude" / "local", Path("/usr/local/bin"),
             # fnm puts the live node on PATH through a per-shell symlink dir
             # under /run; its `default` alias is the stable name for it.
             home / ".local" / "share" / "fnm" / "aliases" / "default" / "bin"]
    path = os.pathsep.join([os.environ.get("PATH") or "", *map(str, extra)])
    return shutil.which("claude", path=path) or "claude"


def open_window(session: str, cwd: str, *, resume: bool, host: str = "",
                flags: tuple[str, ...] | list[str] = ()) -> tuple[str, str]:
    """Open a background tmux window running Claude Code. `(pane, error)`.

    `host` names the tmux session to open it in; by default the one someone is
    attached to. `flags` go to `claude` (a fresh session may want
    `--dangerously-skip-permissions`; a revived one wants nothing).

    Background (`-d`) on purpose: this is triggered from the phone, and a
    window that steals the desk's focus mid-task is a worse answer than one
    that waits to be found. The app is handed the pane id so it can offer a
    link back to it.

    Two things observed doing this for real, neither a fault:

    * The window does not stay where we put it — a SessionStart hook moves it
      into the session named for its project. The pane id survives the move, so
      everything downstream still works; do not "fix" the target.
    * Answering the resume modal means resuming from a summary, and that runs a
      compaction first. On a large transcript the reply sits in Claude Code's
      own queue for a minute or two before it is read. That is why the caller
      is told `opened: true` — "opening" is a truer thing to show than "sent".
    """
    cwd = cwd or os.path.expanduser("~")
    if host:
        if not ensure_host(host, cwd):
            return "", f"could not get a client onto tmux session {host!r}"
    else:
        host = attached_session()
        if not host:
            return "", "no attached tmux session to open a window in"
    cmd = f"exec env -u ANTHROPIC_API_KEY {shlex.quote(_claude_bin())}"
    if resume:
        cmd += f" --resume {session}"
    if flags:
        cmd += " " + shlex.join(list(flags))
    # "host:" not "host": a bare name is a target *window*, and tmux happily
    # resolves it into some other session's window (verified the hard way).
    pane = _tmux(["new-window", "-d", "-t", f"{host}:", "-c", cwd,
                  "-P", "-F", "#{pane_id}", cmd])
    if not pane:
        return "", "tmux could not open a window"
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        time.sleep(READY_POLL_S)
        if pane_ready(pane):
            return pane, ""
    return pane, f"{pane} did not come up within {READY_TIMEOUT_S:.0f}s"


def focus(pane: str) -> tuple[bool, str]:
    """Bring the attached client to `pane` — the app's "opened in %23" link.

    Single user, so pulling the desk's screen somewhere from the phone is a
    feature. Only panes hosting Claude Code are eligible, so this cannot be
    used to go rummaging through someone's shells.
    """
    from . import canvas

    if pane not in {p["pane"] for p in canvas._tmux_cc_panes()}:
        return False, f"not a live claude pane: {pane!r}"
    sess = _tmux(["display", "-pt", pane, "#{session_name}"])
    win = _tmux(["display", "-pt", pane, "#{window_id}"])
    if not sess or not win:
        return False, f"pane {pane} is gone"
    _tmux(["switch-client", "-t", sess])
    _tmux(["select-window", "-t", win])
    _tmux(["select-pane", "-t", pane])
    return True, pane


def _registry_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("MEDIA_PANE_REGISTRY_DIR")
                                   or "~/.claude/tmux-sessions"))


def session_of_pane(pane: str, timeout: float = 10.0) -> str:
    """The uuid of the Claude Code session running in `pane`, or "".

    A fresh session's uuid is in nobody's argv; the SessionStart hook writes it
    to the pane registry a moment after the TUI is up, so this waits for the
    row and believes it only when the pid it names is a live `claude` — a
    recycled pane id can otherwise hand back the previous tenant.
    """
    row = _registry_dir() / pane.lstrip("%")
    deadline = time.monotonic() + timeout
    while True:
        try:
            parts = row.read_text().split()
        except OSError:
            parts = []
        if parts:
            pid = parts[1] if len(parts) > 1 else ""
            if pid:
                try:
                    cmd0 = Path("/proc", pid, "cmdline").read_bytes().split(b"\0")[0]
                    alive = os.path.basename(cmd0.decode(errors="replace")) == "claude"
                except OSError:
                    alive = False
            else:
                alive = True             # a legacy row without a pid: trust it
            if alive:
                return parts[0]
        if time.monotonic() >= deadline:
            return ""
        time.sleep(0.25)


# --- a fresh session, from the phone -------------------------------------------

def ask_target() -> tuple[str, str, list[str]]:
    """`(tmux session, cwd, claude flags)` for a session started from the phone.

    Copied from the amux registration named by MEDIA_ASK_SESSION (default
    `scratch`) — the same directory and flags `amux start scratch` would use,
    in the tmux session amux would put it in — so where a phone-started chat
    lands is set in one place, with amux. Each part can be overridden.
    """
    name = (os.environ.get("MEDIA_ASK_SESSION") or "scratch").strip()
    cwd, flags = "", ""
    env = Path(os.path.expanduser(os.environ.get("CC_HOME") or "~/.amux")) / "sessions" / f"{name}.env"
    try:
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k == "CC_DIR":
                cwd = v
            elif k == "CC_FLAGS":
                flags = v
    except OSError:
        pass
    host = (os.environ.get("MEDIA_ASK_TMUX") or "").strip() or f"amux-{name}"
    cwd = os.path.expanduser((os.environ.get("MEDIA_ASK_CWD") or "").strip() or cwd or "~")
    flags_s = os.environ.get("MEDIA_ASK_FLAGS")
    argv = shlex.split(flags_s if flags_s is not None else flags)
    return host, cwd, argv


def _settle(pane: str, timeout: float = 5.0) -> None:
    """Wait until the screen in `pane` stops changing (or `timeout` passes).

    `pane_ready` says yes at the first sight of Claude Code's chrome, and the
    TUI is still painting for a moment after that: a message typed then is
    taken but its Enter is not, and it sits in the box unsent. Two identical
    captures a beat apart is the TUI at rest.
    """
    last = _capture_pane(pane)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.7)
        now = _capture_pane(pane)
        if now == last:
            return
        last = now


def _ensure_submitted(pane: str, text: str, timeout: float = 3.0) -> None:
    """Press Enter again if `text` is still sitting in the input box.

    Looked at rather than assumed: the composer shows what it holds, so
    "still in the box" is the first words of the message after the prompt
    glyph with no sign of a turn in progress.
    """
    from . import canvas

    head = " ".join(text.split())[:24]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)
        cap = canvas._strip_ansi(_capture_pane(pane))
        if canvas._classify_cc(cap) == "working":
            return
        flat = " ".join(cap.split())
        i = flat.rfind(_PROMPT_GLYPH)
        if i < 0 or head not in flat[i:]:
            return
    _tmux(["send-keys", "-t", pane, "Enter"])


def ask(text: str, bearer: str, *, quote: str = "") -> tuple[bool, dict]:
    """Start a fresh Claude Code session with `text` as its first message.

    What the phone's assistant button does. Nothing to resume and no item yet:
    the window opens in the scratch session, the words are typed in, and the
    listener's turn is shelved against the new session's uuid so the
    conversation appears in the library once its first reply is spoken. The
    app is told the uuid and polls `/conversation?session=` for the item.
    """
    from . import canvas

    text = " ".join((text or "").split())
    if not text:
        return False, {"error": "empty message"}
    user, status = abs_identity(bearer)
    if not user:
        return False, _identity_error(status)
    ok, why = may_reply(user)
    if not ok:
        return False, {"error": why, "status": 403}
    host, cwd, flags = ask_target()
    pane, err = open_window("", cwd, resume=False, host=host, flags=flags)
    if err:
        return False, {"error": err, "pane": pane or None}
    session = session_of_pane(pane)
    _settle(pane)
    body = compose(text, quote)
    send_err = canvas._send_to_pane(pane, body)
    if send_err:
        return False, {"error": send_err, "session": session or None, "pane": pane}
    _ensure_submitted(pane, body)
    if session:
        _record_turn(session, text)
    return True, {"session": session or None, "pane": pane, "opened": True,
                  "fresh": True, "tmux": host}


def _folder_for_session(session: str) -> str:
    for f in sorted(_manifest_dir().glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if str(data.get("session") or f.stem) == session:
            return str(data.get("folder") or "")
    return ""


def item_for_session(session: str) -> str | None:
    """The ABS library item id behind `session`, or None until it has one.

    None means "not yet" as often as "never": the item exists once a turn has
    been exported (a minute's debounce) and ABS has scanned the folder.
    """
    folder = _folder_for_session(session)
    if not folder:
        return None
    try:
        from agent_media_core import book_tracks

        ready = book_tracks._abs_ready()
        if not ready:
            return None
        url, token, libs = ready
        item = book_tracks._find_item(url, token, libs, Path(folder))
    except Exception as e:  # noqa: BLE001 — "not yet" is the honest answer
        log.warning("reply: item lookup for %s failed (%s)", session[:8], e)
        return None
    return str(item.get("id")) if item and item.get("id") else None


def conversation_for_session(session: str, bearer: str) -> tuple[bool, dict]:
    """`/conversation?session=`: where a session started from the phone got to.

    Same gates as `conversation`. The answer is `ok` from the first poll —
    the session is real — and `item` fills in when the library has it.
    """
    session = (session or "").strip()
    if not _UUID.fullmatch(session):
        return False, {"error": "not a session id", "status": 400}
    user, status = abs_identity(bearer)
    if not user:
        return False, _identity_error(status)
    ok, why = may_reply(user)
    if not ok:
        return False, {"error": why, "status": 403}
    pane = live_sessions().get(session, "")
    return True, {"session": session, "item": item_for_session(session),
                  "live": bool(pane), "pane": pane or None,
                  "resumable": session_exists(session)}


# --- the whole move -----------------------------------------------------------

# Quote and reply go in on ONE line. `send-keys` types literally and then
# presses Enter, so an embedded newline would submit half a message; a quoted
# turn is context, not a document, and one line carries it.
_QUOTE_LIMIT = 160


def compose(text: str, quote: str = "") -> str:
    # The reply is flattened too, not just the quote: the box grows to several
    # rows now, and shift+enter puts a real newline in it. `send-keys` types
    # literally and then presses Enter, so a newline mid-message would submit
    # the first half and leave the rest sitting in the composer.
    text = " ".join((text or "").split())
    quote = " ".join((quote or "").split())
    if not quote:
        return text
    if len(quote) > _QUOTE_LIMIT:
        quote = quote[:_QUOTE_LIMIT - 1] + "…"
    return f'Re: "{quote}" — {text}'


def _record_turn(session: str, text: str) -> None:
    """Put the listener's own words into the conversation, in the background.

    Rendering takes a second or two and the reply has already been delivered,
    so this must not sit in front of the response — the box would look stuck
    for no reason the user could see. Failures are logged and dropped: the
    words reached the session either way.
    """
    def run() -> None:
        try:
            from agent_media_core import book_tracks

            book_tracks.record_listener_turn(session, text)
        except Exception as e:  # noqa: BLE001 — the reply already landed
            print(f"reply: could not shelve the listener's turn ({e})",
                  file=sys.stderr)

    threading.Thread(target=run, daemon=True).start()


def reply(item: str, text: str, bearer: str, *, quote: str = "",
          mode: str = "continue") -> tuple[bool, dict]:
    """Put `text` into the session behind ABS item `item`.

    `continue` types into the live pane, reviving the session in a background
    window if it has ended. `branch` always opens a fresh session in the same
    working directory, seeded with the quoted line — the cheap version of
    forking a conversation, which Claude Code cannot really do (see the
    proposal: a true fork means truncating an undocumented transcript format).
    """
    from . import canvas

    text = (text or "").strip()
    if not text:
        return False, {"error": "empty reply"}
    user, status = abs_identity(bearer)
    if not user:
        return False, _identity_error(status)
    ok, why = may_reply(user)
    if not ok:
        return False, {"error": why, "status": 403}
    session, err = session_for_item(item, bearer)
    if not session:
        return False, {"error": err, "status": 404}
    text = " ".join(text.split())
    body = compose(text, quote)

    if mode == "branch":
        pane, err = open_window(session, transcript_cwd(session), resume=False)
        if err:
            return False, {"error": err, "pane": pane or None}
        send_err = canvas._send_to_pane(pane, body)
        if not send_err:
            _record_turn(session, text)
        return (not send_err), {"session": session, "pane": pane,
                                "opened": True, "branched": True,
                                **({"error": send_err} if send_err else {})}

    pane = live_sessions().get(session, "")
    opened = False
    if pane and canvas._pane_alive(pane):
        pass
    elif not session_exists(session):
        # No transcript: nothing to revive, and reviving into a fresh session
        # would silently answer as someone else.
        return False, {"error": f"session {session[:8]} has no transcript to resume"}
    else:
        pane, err = open_window(session, transcript_cwd(session), resume=True)
        if err:
            return False, {"error": err, "pane": pane or None}
        opened = True
    send_err = canvas._send_to_pane(pane, body)
    if send_err:
        return False, {"error": send_err, "session": session, "pane": pane}
    _record_turn(session, text)
    return True, {"session": session, "pane": pane, "opened": opened}


def conversation(item: str, bearer: str) -> tuple[bool, dict]:
    """Whether `item` is a conversation this caller may reply to.

    The app asks this before drawing the reply box, so it never has to know
    what a conversation is or which library holds them — it shows the box when
    the answer here is yes. Same two gates as `reply`, in the same order, so
    the box cannot appear where the send would be refused.
    """
    user, status = abs_identity(bearer)
    if not user:
        return False, _identity_error(status)
    ok, why = may_reply(user)
    if not ok:
        return False, {"error": why, "status": 403}
    session, err = session_for_item(item, bearer)
    if not session:
        return False, {"error": err, "status": 404}
    pane = live_sessions().get(session, "")
    return True, {"session": session, "live": bool(pane), "pane": pane or None,
                  "resumable": session_exists(session),
                  "suggestion": ghost_prompt(pane) if pane else ""}


def attach_pictures(lines: list) -> None:
    """Give each log line the picture(s) the canvas drew for that reply.

    The visual channel remembers what it pushed for a reply under the reply's
    dedup key (state.save_push), and the speech row carries the same key, so
    the join is a lookup. Each line gains `images`: canvas-relative or absolute
    URLs the app can put straight into an <img>, and `figure`: whether the
    picture was drawn to be read (a [[visual:]] figure) rather than ambient
    artwork. Only pictures still in the spool are offered — a swept file would
    be a broken image, and a transcript with holes in it is worse than one
    with no pictures.
    """
    from .state import load_push, spool_dir

    spool = spool_dir()
    for line in lines:
        key = line.get("key")
        if not key:
            continue
        payload = load_push(key)
        if not payload:
            continue
        names = ([payload.get("image")] if payload.get("image")
                 else [b.get("image") for b in payload.get("sequence") or []])
        images = []
        for name in names:
            name = str(name or "").strip()
            if not name:
                continue
            if "/" in name:
                images.append(name)          # absolute: another host's spool
            elif (spool / name).is_file():
                images.append("/img/" + name)
        if images:
            line["images"] = images
            line["figure"] = payload.get("purpose") == "figure"


def log_for_item(item: str, bearer: str) -> tuple[bool, dict]:
    """The conversation behind `item`, as readable lines. Same gates as a reply.

    Gated identically on purpose: the log is the words of the conversation, so
    anyone who can read it could have read them by listening — but an account
    that may not reply has no business being handed a transcript either.
    """
    user, status = abs_identity(bearer)
    if not user:
        return False, _identity_error(status)
    ok, why = may_reply(user)
    if not ok:
        return False, {"error": why, "status": 403}
    session, err = session_for_item(item, bearer)
    if not session:
        return False, {"error": err, "status": 404}
    try:
        from agent_media_core import book_tracks

        for f in sorted(_manifest_dir().glob("*.json")):
            try:
                data = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if str(data.get("session") or f.stem) == session:
                lines = book_tracks.conversation_log(
                    session, Path(str(data.get("folder") or "")),
                    target="conversations")
                # `pending` is true while the last thing said was the listener's:
                # a reply is in, no answer has landed yet. The app shows a
                # "thinking" line and polls faster until it clears, rather than
                # waiting out a whole idle poll with nothing on screen.
                pending = bool(lines) and lines[-1].get("who") == "you"
                attach_pictures(lines)
                # The ghost prompt rides along with every poll: it appears a
                # few seconds after the turn it follows, so a one-off read at
                # page-open would mostly find it not there yet.
                pane = live_sessions().get(session, "")
                return True, {"session": session, "lines": lines,
                              "pending": pending,
                              "suggestion": ghost_prompt(pane) if pane else ""}
    except Exception as e:  # noqa: BLE001
        # Say so in the journal as well as to the caller: the app folds every
        # failed fetch into an empty transcript, so this line is the only
        # record on this side of what actually went wrong.
        log.exception("conversation log for %s (session %s) failed: %s",
                      item, session, e)
        return False, {"error": f"could not read the conversation ({e})",
                       "status": 500}
    return False, {"error": "no manifest for that conversation", "status": 404}

