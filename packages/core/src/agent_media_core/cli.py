"""media — unified CLI control surface for agent-media.

Speech playback control (sink-speech), the now-speaking text + history
(state/), and music control (sink-music / Mopidy). The tmux popup, status
line, and keybind plugin all drive this CLI — it replaces the old aar
`tts-ctl` / `tts-popup` / `tts-status-line` shell bins (decision 5).

Speech control always targets the local sink-speech broker (the thing
producing audio on this host); routing of *new* clips to rooms/etc. is a
submit-time concern (see intake/submit).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

from ._paths import state_dir
from .sinks import _mpv_ipc as ipc
from .sinks.music import SinkMusic
from .sinks.music_router import SinkMusicRouter
from .sinks import speech as _speech_sink
from .sinks.speech import SinkSpeech, _broker_max_volume, _socket_for
from .state import StateStore
from .types import Event, Priority, Source, Target

POPUP_CHANNELS = ("speech", "music", "book")

# Load machine-local config (~/.config/agent-media.env) so the CLI — including
# the tmux status bar and the popup controls — resolves the same speech target
# the hook plays to. Without this the status/popup would read the *local* mpv
# even when speech is playing on a remote target (the phone, Grade B), and show
# nothing. Real env vars still win, matching the hooks' precedence.
try:
    from .intake._env import load_env_file as _load_env_file
    _load_env_file("cli")
except Exception:  # noqa: BLE001 — config is best-effort; CLI must still run
    pass

# The speech target the control surface reads/drives. For a remote target (the
# phone over a tcp:// bridge) media status/now/pause/skip/replay all talk to
# *that* mpv, so the popup follows phone-local playback (Grade B). Falls back to
# the local broker when unset.
SPEECH_TARGET = Target(os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET", "local"))


# --- pure helpers (unit-tested) -------------------------------------------

def fmt_mmss(secs: Optional[float]) -> str:
    if secs is None:
        return "--:--"
    secs = max(0, int(secs))
    return f"{secs // 60:02d}:{secs % 60:02d}"


def fmt_time(secs: Optional[float], *, hours: Optional[bool] = None) -> str:
    """Compact duration. `hours=True` → ``H:MM`` (audiobook scale — minutes,
    no seconds); `hours=False` → ``M:SS``; `None` auto-picks ``H:MM`` once the
    value reaches an hour. Pass an explicit `hours` (derived from the *total*)
    so a pos/total pair shares one format — otherwise a 45-min position into an
    11-hour book would render ``45:00`` next to ``11:05``.

    Keeps long content compact: an 11h book is ``11:05`` instead of fmt_mmss's
    overflowing ``665:37``.
    """
    if secs is None:
        return "--:--"
    secs = max(0, int(secs))
    if hours is None:
        hours = secs >= 3600
    if hours:
        h, rem = divmod(secs, 3600)
        return f"{h}:{rem // 60:02d}"
    # Sub-hour stays byte-identical to fmt_mmss (MM:SS) so speech/music status
    # is unchanged; only >= 1h content switches to the compact H:MM above.
    return f"{secs // 60:02d}:{secs % 60:02d}"


def progress_bar(frac: float, width: int = 12) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


def render_status(*, idle: Optional[bool], pos: Optional[float],
                  dur: Optional[float], paused: Optional[bool],
                  muted: Optional[bool], width: int = 12,
                  hide_idle: bool = True, bar: bool = True,
                  speed: Optional[float] = None) -> str:
    """Build the one-line status string (or '' / '○' when idle).

    With bar=False, the progress bar is dropped and only the times remain
    (`▶ 00:30 / 02:00`) — used by the popup, which shows just the clock.
    `speed` (when not ~1.0) appends a `⏩1.4×` readout so a listening-mode
    speed change is visible in the status bar.
    """
    if idle is None or idle:
        return "" if hide_idle else "○"
    icon = "⏸" if paused else "▶"
    # Format chosen by the total's magnitude (applied to both) so an 11h book
    # reads `1:55 / 11:05` rather than the overflowing `115:32 / 665:37`.
    hours = bool(dur is not None and dur >= 3600)
    if bar:
        frac = (pos / dur) if (pos and dur) else 0.0
        line = (f"{icon} {fmt_time(pos, hours=hours)} {progress_bar(frac, width)} "
                f"{fmt_time(dur, hours=hours)}")
    else:
        line = f"{icon} {fmt_time(pos, hours=hours)} / {fmt_time(dur, hours=hours)}"
    if muted:
        line += " [M]"
    if isinstance(speed, (int, float)) and abs(speed - 1.0) > 0.05:
        glyph = "⏩" if speed > 1.0 else "🐢"
        line += f" {glyph}{round(speed, 2):g}×"
    return line


# --- IPC plumbing ----------------------------------------------------------

def _active_speech_target() -> Target:
    """The target speech is *actually* playing on — the now-playing mirror's
    recorded target, falling back to the configured default when idle.

    The daemon that started playback resolves its target from its own env
    (`MEDIA_SPEECH_DEFAULT_TARGET`, e.g. the phone), but a popup keypress spawns
    a short-lived `media` in the user's shell, which usually lacks that var and
    so would default to `local`. Reading the wrong player makes the status show
    `○` and pause act on an empty local mpv. Follow the live player instead —
    the same precedence the nav/skip path already uses (now-playing target, then
    SPEECH_TARGET). When idle there's no row, so this is just SPEECH_TARGET.
    """
    name = (StateStore().get_now_playing("speech") or {}).get("target")
    return Target(name=name) if name else SPEECH_TARGET


def _sock():
    return _socket_for(_active_speech_target())


def _get(prop: str, critical: bool = False):
    try:
        return ipc.get_property(_sock(), prop, critical=critical)
    except ipc.MpvIpcError:
        return None


def _now_speaking() -> Optional[dict]:
    np = StateStore().get_now_playing("speech")
    if not np:
        return None
    ex = np.get("extras") or {}
    if isinstance(ex, str):
        try:
            ex = json.loads(ex)
        except json.JSONDecodeError:
            ex = {}
    np["extras"] = ex
    return np


def _live_history_row() -> Optional[dict]:
    """The turn that is speaking *right now*, shaped like a history row.

    History is written when a turn ENDS — every lane calls ``add_history`` on
    the way out — so for the whole time a reply is audible it is missing from
    the very list the popup traverses. The turn you are listening to is then
    not row 1; the *previous* one is. So `r` replayed the turn before the one
    playing, `<` stepped back one turn too far, and a conversation's first
    reply scoped to an empty history and every traversal key did nothing at
    all — a dead keybinding for exactly as long as it was talking.

    now_playing carries the same identity fields, so hand it back as the
    newest row. `id` is None: it is not a record yet, and `replay --id`
    addresses records.
    """
    np = _now_speaking()
    if not np or not _speech_in_flight():
        # The same zombie guard the display uses: a submit process that died
        # without cleanup leaves a row behind, and resurrecting it here would
        # put a turn nobody is hearing at the head of the traversal forever.
        return None
    ex = np.get("extras") or {}
    if ex.get("kind") == "notif":
        return None                     # alerts are not part of the traversal
    uri = np.get("uri")
    # A lane that renders on the far side records what it *ran* ("remote-say:
    # phone"), not a clip anyone here can play. Keep the row — the traversal
    # still needs it to count and to scope — but strip the pseudo-uri, so
    # _replay_row refuses it instead of handing mpv a path that is a command.
    if not ex.get("clip_uris") and not (uri and os.path.exists(uri)):
        uri = None
    return {"id": None, "sink": "speech", "uri": uri,
            "started_at": np.get("started_at"), "ended_at": None,
            "target": np.get("target"), "source": ex.get("source"),
            "content_type": None, "text": ex.get("text") or "", "extras": ex}


def _row_has_audio(row: dict) -> bool:
    """Did this history row leave anything a replay could play?

    Most rows did. The ones that did not are renders that FAILED — the org
    reminder whose remote renderer timed out (`curl` exit 28) is the live
    example, and there were three of them at the top of the history on
    2026-08-17. Such a row still carries its text and its pane, because the
    record of "this was said, or meant to be" is worth keeping, but its `uri`
    is the pseudo-uri the lane recorded for what it *ran* — `remote-say:phone`,
    a description of a command, not a clip.

    Left in the traversal they are worse than useless: `r` and `<` in the popup
    address rows by index, so the newest failure is what "replay the last clip"
    reaches, and it hands mpv a path that is a sentence. Nothing plays and
    nothing says why — reported as "I can't seem to play the last clip".

    So they are filtered out of the traversal for the same reason notif alerts
    are: it is a list of things you can hear again. `media recent` reads the
    store directly and still shows them.

    The test is what the uri IS, not whether the file is still there. A clip
    pruned from the cache is a different failure with its own handling (the
    prefetch re-pushes, and mpv complains if it cannot), and a row whose clips
    live on the far side records bare filenames that exist nowhere on this
    host — so asking the filesystem here would refuse the phone's whole
    history.
    """
    ex = row.get("extras") if isinstance(row.get("extras"), dict) else {}
    if ex.get("clip_uris"):
        return True
    uri = row.get("uri") or ""
    return bool(uri) and not uri.startswith("remote-say:")


def _adopt_pane_sessions(rows) -> None:
    """Give a clip with no conversation the one its pane was holding.

    Not every clip spoken into a conversation carries its id. `media say` — the
    lead-in prose before a question, a hook's aside, anything an agent speaks
    from its own shell — knows the pane it is standing in and nothing about the
    Claude session, because there is no session id in the environment to read.
    The Stop hook is handed one; a shell is not.

    The consequences were not cosmetic. A scoped traversal filters on the id,
    so those clips were invisible to `<` and `>` inside the very conversation
    that said them, and the phone's list, grouping on the same field, showed
    one conversation twice — the same window name, once for the replies and
    once for the prose. Two names for one thing is the bug this whole afternoon
    kept finding.

    So a pane's clips are read as belonging to that pane's conversation, taking
    the *nearest in time* of the ones that named it. Nearest, not newest: a pane
    that held conversation A this morning and B this afternoon should not hand
    every one of A's asides to B. A pane that has never named a conversation —
    the reminders cron speaks — keeps none, which is true of it.

    Mutates the rows it was handed, extras copied rather than scribbled on, so
    the store's own dicts are left alone.
    """
    marks: dict = {}
    for r in rows:
        ex = r.get("extras")
        if not isinstance(ex, dict):
            continue
        pane, sess = ex.get("source_pane"), ex.get("source_session")
        if pane and sess:
            marks.setdefault(pane, []).append(
                (float(r.get("started_at") or 0.0), sess))
    if not marks:
        return
    for r in rows:
        ex = r.get("extras")
        if not isinstance(ex, dict) or ex.get("source_session"):
            continue
        near = marks.get(ex.get("source_pane") or "")
        if not near:
            continue
        at = float(r.get("started_at") or 0.0)
        ex = dict(ex)
        ex["source_session"] = min(near, key=lambda m: abs(m[0] - at))[1]
        # Marked, because "we worked out where this belongs" and "it said so
        # itself" are different claims, and only one of them can be wrong.
        ex["session_adopted"] = True
        r["extras"] = ex


def _adopt_pane_places(rows) -> None:
    """Give a clip with no tmux session the one its conversation was said in.

    The sibling of `_adopt_pane_sessions`, for the other half of the identity: a
    clip knows which conversation it belongs to but not where that conversation
    was sitting. The write side loses this when a pane closes between the turn
    ending and the reply being spoken — which is exactly what a goodbye does, so
    the last clip of a conversation was the one most likely to arrive untagged,
    and the phone's list filed all of them together under "no session".

    That is now fixed at the source, but the rows already written are still
    there, and a conversation's other clips know the answer for them: the same
    Claude session is the strongest key (a conversation does not move between
    tmux sessions mid-flight), the pane is the fallback for clips that never had
    a session id. Nearest in time, for the same reason as the sibling — a pane
    outlives the conversations in it.

    Best-effort, and bounded by the window it was handed: a conversation whose
    every other clip is older than the fetch keeps its blank, which is honest —
    the answer is not in front of us — and rare, now that the blanks have
    stopped being written.

    Mutates the rows it was handed, extras copied rather than scribbled on.
    """
    marks: dict = {}
    for r in rows:
        ex = r.get("extras")
        if not isinstance(ex, dict):
            continue
        tmux = str(ex.get("source_tmux_session") or "").strip()
        if not tmux:
            continue
        mark = (float(r.get("started_at") or 0.0), tmux,
                str(ex.get("source_window") or "").strip())
        for key in (("session", ex.get("source_session")),
                    ("pane", ex.get("source_pane"))):
            if key[1]:
                marks.setdefault(key, []).append(mark)
    if not marks:
        return
    for r in rows:
        ex = r.get("extras")
        if not isinstance(ex, dict) or str(ex.get("source_tmux_session") or "").strip():
            continue
        own = marks.get(("session", ex.get("source_session")))
        near = own or marks.get(("pane", ex.get("source_pane")))
        if not near:
            continue
        at = float(r.get("started_at") or 0.0)
        _, tmux, window = min(near, key=lambda m: abs(m[0] - at))
        ex = dict(ex)
        ex["source_tmux_session"] = tmux
        # The window name travels only along the session key. A tmux session is
        # a property of the pane and every clip from that pane shares it; a
        # window *name* is a conversation's title, and the pane's other
        # conversations have their own. Borrowing one across would file the
        # reminders a cron job speaks under whatever was being worked on there.
        # And never over a name of its own: that is the title as it stood when
        # this clip spoke, which is a better answer than a later one.
        if own and window and not str(ex.get("source_window") or "").strip():
            ex["source_window"] = window
        # Same claim, same caveat as the sibling: worked out, not stated.
        ex["place_adopted"] = True
        r["extras"] = ex


def _speech_history(n: int = 20, session: Optional[str] = None,
                    include_live: bool = False):
    # `session`, when given, is a *Claude session id* (extras.source_session) —
    # the true "this conversation" boundary. It's preferred over the tmux
    # session because one tmux session can hold several distinct conversations
    # (so tmux-scoping bleeds between them) and one conversation can move panes
    # on resume (so pane-scoping splits it). The Claude id does neither.
    #
    # Exclude "Claude is waiting" notif clips: they're alerts, not responses,
    # and shouldn't appear when traversing past TTS (popup < / >, r, replay).
    # Over-fetch so filtering still leaves n real responses to step through;
    # over-fetch harder when scoping, since other conversations' clips
    # interleave and would otherwise crowd out the buffer.
    fetch = max(n * 4, n + 50)
    if session:
        fetch = max(fetch, 400)
    rows = StateStore().recent_history(sink="speech", limit=fetch)
    # Before anything is filtered, because the alerts about to be dropped are
    # some of the best evidence of which conversation a pane was holding.
    _adopt_pane_sessions(rows)
    _adopt_pane_places(rows)
    rows = [r for r in rows
            if not (isinstance(r.get("extras"), dict)
                    and r["extras"].get("kind") == "notif")]
    if include_live:
        live = _live_history_row()
        # started_at is the same float the lane will hand add_history, so it
        # dedupes exactly — no window where the turn is listed twice as it
        # finishes.
        #
        # A *replay* is a second start of something already in this list, and
        # it starts when it starts: its own started_at matches nothing, so
        # folding it in put the turn you were hearing in the list twice and
        # shifted every index below it by one. `<` then stepped from the
        # phantom onto the row it mirrored — the same turn, from the top —
        # which is what "pressing < twice just replays the same clip" was.
        # _replay_row stamps which row it is playing for exactly this reason:
        # `history_id` for a record, and for a live turn restarted mid-flight
        # (no record yet) the started_at it will be filed under.
        if live is not None:
            lex = live.get("extras") or {}
            hid = lex.get("history_id")
            starts = {live["started_at"], lex.get("replay_of_started_at")}
            starts.discard(None)
            if hid is not None or any(r.get("started_at") in starts
                                      for r in rows):
                live = None
        if live is not None:
            rows.insert(0, live)
    if session:
        # Scope traversal to one conversation's clips. Rows that predate the
        # source_session field (or came from a session-less source) carry no
        # tag and are excluded rather than leaking across conversations.
        rows = [r for r in rows
                if isinstance(r.get("extras"), dict)
                and r["extras"].get("source_session") == session]
    return rows[:n]


def _tmux_session_for_pane(pane: str) -> str:
    """Resolve a tmux pane id (e.g. ``%41``) to its session name, or ""."""
    if not pane or "#{" in pane:
        return ""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
            capture_output=True, text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _registered_session_for_pane(pane: str) -> Optional[str]:
    """Which conversation *currently owns* `pane`, or None.

    Read from the registry `claude-tmux-session-register` (agent-config's
    SessionStart/SessionEnd hook) already maintains at
    ~/.claude/tmux-sessions/<pane-number>, so nothing new has to be written to
    answer this. Format: ``<sessionId> <claudePid> <cwd>``, with a legacy bare
    ``<sessionId>``; keyed by pane, newest start wins.

    Why prefer it over the clip history: history can only answer "who spoke here
    last", and that degrades every time tmux recycles a pane id — one observed
    pane had carried twelve conversations plus fifteen untagged clips, so the
    honest answer from clips can be a conversation that ended days ago. The
    registry knows the live occupant even before it has said anything.

    The pid is what makes a stale entry *detectable* rather than merely old: a
    dead one means the registry is describing a session that has exited, which
    owns nothing. Fall back in that case rather than trust it.

    MEDIA_PANE_REGISTRY_DIR overrides the location (tests, and any host that
    keeps its Claude state elsewhere).
    """
    if not pane or "#{" in pane:
        return None
    root = os.environ.get("MEDIA_PANE_REGISTRY_DIR") or "~/.claude/tmux-sessions"
    path = os.path.join(os.path.expanduser(root), pane.lstrip("%"))
    try:
        with open(path, encoding="utf-8") as fh:
            fields = fh.read().strip().split()
    except OSError:
        return None
    if not fields:
        return None
    sess = fields[0]
    if len(fields) >= 2 and fields[1].isdigit():
        # Shared with the now_playing orphan guard rather than reimplemented:
        # "is this pid still here" has the same conservative-on-error behaviour
        # in both places, which is the behaviour that matters.
        from .state.store import _pid_alive

        # The pane's owner has exited; whatever is there now is not this session.
        if not _pid_alive(int(fields[1])):
            return None
    return sess or None


def _anchor_session() -> Optional[str]:
    """The *conversation* the popup's < / > traversal should stay within,
    as a Claude session id (extras.source_session).

    Follows what you're hearing: the now-playing clip's conversation if one is
    playing; otherwise the conversation that last spoke in the pane that opened
    the popup (TTS_POPUP_PANE). The Claude id is the right scope — it survives a
    session being resumed into another pane and doesn't bleed across sibling
    conversations sharing one tmux session. Returns None when neither resolves,
    so callers fall back to unscoped (all-conversation) history.
    """
    ex = (_now_speaking() or {}).get("extras") or {}
    sess = ex.get("source_session")
    if sess:
        # ...but only if the traversal can see anything under that scope. It
        # normally can — the turn being spoken is itself row 1 — but a stale
        # or half-written now_playing must not scope every key to an empty
        # set, which is what a dead keybinding is made of.
        if _speech_history(1, session=sess, include_live=True):
            return sess
    # Idle: a bare pane id carries no Claude id, so resolve it from that pane's
    # most recent clip — asked of the store directly, because the answer can be
    # arbitrarily far back. Scanning the 50 newest clips globally looked
    # equivalent and was not: this conversation went quiet for three days, 285
    # clips landed on top of it, its pane fell out of the window, and every
    # `--session` view silently widened to all conversations.
    pane = os.environ.get("TTS_POPUP_PANE", "")
    if not pane:
        return None
    # Ask who *owns* the pane before asking who last spoke in it. Ownership is
    # recorded when a session starts, so it is right for a conversation that has
    # not spoken yet, and it does not decay when tmux recycles a pane id.
    for sess in (_registered_session_for_pane(pane),
                 StateStore().session_for_pane(pane)):
        # Same guard as the now-playing branch above, and for the same reason:
        # both lookups can name a conversation whose rows the traversal filters
        # out (a clip whose audio never rendered), or one that has said nothing
        # at all, and anchoring to a scope with nothing in it is what a dead
        # keybinding is made of. Falling through to the last speaker then shows
        # a list that is at least this pane's own past.
        if sess and _speech_history(1, session=sess, include_live=True):
            return sess
    return None


def _caller_pane() -> str:
    """The pane the user is "at", for the different-pane (↪) comparison.

    Popup: TTS_POPUP_PANE (TMUX_PANE inside display-popup is the popup's own
    ephemeral pane). Status bar: MEDIA_STATUS_PANE, which the status-right
    config passes as `#{pane_id}` (the viewing client's active pane) — without
    it the status bar has no pane context, so the ↪ comparison can't tell which
    pane you're on. Falls back to TMUX_PANE. An unexpanded `#{...}` literal is
    resolved by asking tmux for the active pane."""
    pane = (os.environ.get("TTS_POPUP_PANE")
            or os.environ.get("MEDIA_STATUS_PANE")
            or os.environ.get("TMUX_PANE", ""))
    if "#{" in pane:
        try:
            r = subprocess.run(["tmux", "display-message", "-p", "#{pane_id}"],
                               capture_output=True, text=True)
            pane = r.stdout.strip() if r.returncode == 0 else ""
        except Exception:  # noqa: BLE001
            pane = ""
    return pane


# --- speech subcommands ----------------------------------------------------

def _remote_speech() -> bool:
    """Speech plays somewhere this host cannot observe.

    Two shapes. A tcp:// speech socket is the phone driven over a bridge — we
    still control that mpv, just remotely. MEDIA_REMOTE_SAY_CMD is stronger:
    the whole reply is handed to another host which renders and plays it, so
    there is no mpv here at all.

    The second case matters for what the popup shows. Falling through to the
    local broker would display whatever it last played — a finished clip, with
    a progress bar advancing through audio nobody is hearing. Stale readings
    are worse than blank ones: they make a working system look wrong and hide
    that speech has moved.
    """
    if os.environ.get("MEDIA_REMOTE_SAY_CMD"):
        return True
    return str(_sock()).startswith("tcp://")


_SNAP_CACHE: dict = {"at": 0.0, "value": None}


def _remote_snapshot():
    """One batched read of the remote player, or None if it can't be reached.

    idle/pause/time-pos/duration/mute/speed in a single round trip. The point is
    that the player is the source of truth: a mirror maintained here has to be
    patched at every control site, and whatever gets missed reads stale — which
    is how "resume" came to look like "restart".

    Cached briefly because a popup redraw is not a rare event and each call
    crosses a link that drops a quarter of its packets. MEDIA_REMOTE_SNAPSHOT_TTL
    tunes it; 0 disables the cache.

    Not `critical` — a redraw we skip costs nothing, and keypresses go through
    the control path, which is critical and bypasses the breaker. But it is
    exempt from the breaker's *latency* rule (`slow_s=0`): a 2s round trip to
    the phone is this link's normal, not a fault, and judging the read by the
    budget that keeps policy chatter from delaying speech left the breaker open
    almost always — a short utterance then played and finished without a single
    snapshot landing, and the popup showed nothing.

    Failure still trips it, so a phone that is simply gone doesn't make every
    redraw wait out the timeout — but gently, and only after a retry. This link
    loses a fifth of its packets, so single-shot reads fail regularly with
    nothing wrong at the far end, and the default 45s penalty for one lost
    packet blanked the popup most of the time. One retry absorbs the drop; a
    5s window keeps a genuine outage cheap to notice and cheap to leave.
    """
    try:
        ttl = float(os.environ.get("MEDIA_REMOTE_SNAPSHOT_TTL", "1.0"))
    except ValueError:
        ttl = 1.0
    now = time.monotonic()
    if ttl > 0 and _SNAP_CACHE["value"] is not None and now - _SNAP_CACHE["at"] < ttl:
        return _SNAP_CACHE["value"]
    try:
        snap = ipc.display_properties(
            _sock(), ["idle-active", "pause", "time-pos", "duration",
                      "mute", "speed", "playlist-pos"], timeout=2.0)
    except (ipc.MpvIpcError, OSError):
        return None
    if not snap:
        return None
    _SNAP_CACHE["at"], _SNAP_CACHE["value"] = now, snap
    return snap


def _speech_in_flight() -> bool:
    """Is a reply being spoken right now, judged without touching the network?

    now_playing is written and cleared on this host, so unlike a read across
    the bridge it cannot come back false because a packet was lost. That
    distinction matters wherever a *control* has to choose what to do: an
    observation that fails looks identical to one that says "nothing is
    playing", and acting on the second when you got the first is how pause
    turned into a no-op mid-utterance.

    Same zombie guard as the display: the row is only as alive as the process
    that wrote it, and a crash without cleanup would otherwise leave the answer
    stuck at True forever.
    """
    np = _now_speaking()
    if not np:
        return False
    wp = (np.get("extras") or {}).get("writer_pid")
    if wp:
        try:
            os.kill(int(wp), 0)
        except (OSError, ValueError):
            return False
    return True


def _speech_paused_mirror() -> bool:
    """Is the speech the far side is playing currently held paused?

    Read from the now-playing row, not the player: `stamp_speech_pause` writes
    every pause down here — ours, and the ones the report stream hears about —
    because the remote lane's follow-along runs on a clock and has to know when
    the clock stopped. That makes the row the one answer a control can get
    without a round trip that might not come back.
    """
    np = _now_speaking()
    if not np:
        return False
    ex = np.get("extras") or {}
    return bool(ex.get("paused_at") or ex.get("live_pause"))


def _announced_timeline():
    """Speech state extrapolated from what the submit process announced, or
    None when it announced no timeline to extrapolate from.

    None is a real answer, not an error: the `remote-say` path deliberately
    records no `total_duration_s`, because the audio is rendered and played on
    another device and never reports its length back. Anything we printed there
    would be invented — so callers that want a progress bar on that path have
    to ask the far side, however much the round trip costs.
    """
    np = _now_speaking()
    ex = (np or {}).get("extras") if np else None
    if not (ex and ex.get("total_duration_s")):
        return None
    # Zombie guard: the mirror is only as alive as the submit process that
    # writes it. If that process died without its cleanup (kill, crash, power
    # loss), the row would otherwise show a frozen "▶ 00:00 / N:NN" forever.
    # Both playback paths stamp their pid.
    wp = ex.get("writer_pid")
    if wp:
        try:
            os.kill(int(wp), 0)
        except (OSError, ValueError):
            return (True, None, None, False, False, None, False)
    ps = ex.get("play_started_at")
    lp = ex.get("live_pos_s")
    total = float(ex["total_duration_s"])
    speed = float(ex.get("live_speed") or 1.0)
    if lp is None and ps:
        # Clamped: an overrun means the utterance finished and cleanup has not
        # landed, and a bar past 100% reads as a fault.
        pos = min(max(time.time() - float(ps), 0.0), total)
    elif lp is not None:
        # A position is only true at the moment it was read, and on the phone
        # lane the poll that reads it lands every one to three seconds. Taking
        # the reading as current makes the bar stall for as long as the gap and
        # then jump — measured against the player: 3.3s held for three seconds
        # while the audio ran on to 6.1s. So carry the reading on with the
        # clock, as the play_started_at branch above does, and let the next
        # poll correct it; freeze it when the row says the player is paused.
        at = ex.get("live_pos_at")
        pos = float(lp)
        if at and not ex.get("live_pause"):
            pos = min(pos + max(time.time() - float(at), 0.0) * speed, total)
    else:
        pos = ex.get("clip_offset_s") or 0.0
    return (False, pos, ex.get("total_duration_s"),
            bool(ex.get("live_pause")), bool(ex.get("live_mute")),
            ex.get("live_speed") or 1.0, True)


def _as_response_timeline(pos, dur, playlist_pos):
    """Lift a player's per-clip reading onto the whole response's timeline.

    A reply is rendered one clip per sentence and queued as a playlist, so the
    player's `duration` is whichever sentence it happens to be reading and its
    `time-pos` starts again at every full stop. Neither is what a progress bar
    or an end time is asking about: those are questions about the reply. The
    clip lengths are on the now-playing row, so the offset is a sum rather
    than a guess.

    Returns `(pos, dur, False)` unchanged when there is no announced timeline
    to lift onto — the remote-render lane records none, and an end time
    invented there would be worse than the sentence's honest one.
    """
    np = _now_speaking()
    if not np:
        return pos, dur, False
    ex = np.get("extras") or {}
    total = ex.get("total_duration_s")
    if not total:
        return pos, dur, False
    clip_durs = ex.get("clip_durations_s")
    if clip_durs:
        ppos = max(0, int(playlist_pos or 0))
        offset = sum(clip_durs[:ppos])
    else:
        offset = ex.get("clip_offset_s") or 0.0
    return offset + (pos or 0.0), total, True


def _speech_display_state(allow_remote: bool = True,
                          prefer_local: bool = False):
    """`(idle, pos, dur, paused, muted, speed, playing)` for the speech channel.

    Remote target (the phone): one batched, briefly-cached snapshot off the
    remote player itself, falling back to now_playing's announced duration when
    there is no player to ask. Local target: one batched snapshot off the local
    mpv, enriched with the response timeline (offset+pos / total) from
    now_playing.

    `prefer_local=True` tries the announced timeline FIRST — a file read (~1ms)
    against a snapshot that costs ~2s on the phone link — and only asks the far
    side when there is no announced timeline to use. That is the right default
    for the tmux status line, which redraws every second in every pane.

    It is an optimisation, not a substitute, and on the `remote-say` path it
    buys nothing: that path records no duration, so the fallback is the only
    thing that knows the utterance is running at all. Defaulting the status bar
    to local-ONLY (an earlier version of this) therefore blanked the progress
    bar for every reply on the phone — which is every reply, by default.

    `allow_remote=False` forbids the round trip outright. It makes the call
    cheap and, on a remote-say target, blind; use it only where a missing bar
    is better than a slow one.
    """
    if _remote_speech():
        # Cheap path first when asked: if the announced timeline is there, it
        # says everything a status bar shows and costs a file read. When it
        # ISN'T there we must still ask the far side — see below.
        if prefer_local:
            local = _announced_timeline()
            if local is not None:
                return local

        snap = _remote_snapshot() if allow_remote else None
        if snap is not None:
            # Ground truth, in one round trip. Every field the popup shows comes
            # from the player itself, so a control nobody special-cased here
            # still displays correctly — which the hand-patched mirror below
            # could never manage.
            if snap.get("idle-active"):
                return (True, None, None, False, False, None, False)
            # The same lift the local lane does. Without it this lane answered
            # "how far through are we" with the sentence being read: the bar
            # restarted at every full stop and the end time was the sentence's,
            # while the tmux bar beside it — which takes the announced timeline
            # — said the reply's. Two surfaces disagreeing about where the end
            # is, on the lane that plays every reply by default.
            pos, dur, _lifted = _as_response_timeline(
                snap.get("time-pos"), snap.get("duration"),
                snap.get("playlist-pos"))
            return (False, pos, dur,
                    bool(snap.get("pause")), bool(snap.get("mute")),
                    snap.get("speed") or 1.0, True)

        # Fallback: the far side has no player we can query — a renderer that
        # just speaks (Android TTS, a bare `say`) — or the bridge is unreachable.
        # All we have is whatever it announced up front, so extrapolate.
        local = _announced_timeline()
        return local if local is not None else (
            True, None, None, False, False, None, False)
    try:
        snap = ipc.get_properties(
            _sock(), ["idle-active", "time-pos", "duration", "pause", "mute",
                      "speed", "playlist-pos"])
    except Exception:  # noqa: BLE001
        snap = {}
    idle = snap.get("idle-active")
    pos = snap.get("time-pos")
    dur = snap.get("duration")
    playing = False         # the response timeline (offset+pos / total) is known
    if not idle:
        pos, dur, playing = _as_response_timeline(
            pos, dur, snap.get("playlist-pos"))
    return (idle, pos, dur, snap.get("pause"), snap.get("mute"),
            snap.get("speed"), playing)


def _sticky_speech_speed(live: Optional[float]) -> Optional[float]:
    """The speech rate to *show*, including while nothing is playing.

    Speed is a property of the long-lived broker mpv, so a 1.5× set mid-reply
    still applies to the next one — but the readout used to vanish the moment
    playback stopped (the remote/phone path has no now_playing mirror when
    idle, so there's nothing to read). Keep a copy in the store: a live reading
    always wins and refreshes it (so a broker restart back to 1.0 self-heals on
    the next clip), and when there's none we fall back to what was last set.
    """
    st = StateStore()
    if isinstance(live, (int, float)):
        live = float(live)
        prev = st.get_speech_speed() or 1.0
        if abs(live - prev) > 0.005:
            st.set_speech_speed(live)
        return live
    return st.get_speech_speed()


def _speech_visual_flag() -> str:
    """"figure"/"reveal" while the now-playing speech message carries a
    purposeful visual ([[visual:]]/[[reveal:]] marker), else "". Drives the
    ▣ indicator in the status bar and popup — never load-bearing, so any
    lookup problem is just "no indicator"."""
    try:
        np = _now_speaking()
        return ((np or {}).get("extras") or {}).get("visual") or ""
    except Exception:  # noqa: BLE001
        return ""


def _with_visual_glyph(line: str) -> str:
    """Append the figure indicator to a rendered status line: the listener's
    cue that this spoken message has a picture worth looking at."""
    if line and not line.startswith("○") and _speech_visual_flag():
        return f"{line} ▣"
    return line


# Which repos the fleet is skew-checked against, as `name:path-below-$HOME`.
#
# Was a two-name tuple with `dotfiles` special-cased at three separate call
# sites, because agent-media lives at ~/projects/<name> and dotfiles does not.
# A second repo's location baked into a conditional is the shape of the problem:
# anyone who is not this author has no ~/dotfiles, so doctor reported a repo
# that cannot exist as permanently stale.
#
# Paths are relative to $HOME on purpose. They are used twice — locally, and
# inside shell that runs on a REMOTE host — and a relative path is the only form
# that means the same thing in both places without knowing the remote's user.
#
# The default is this checkout alone. Skew monitoring across several repos is a
# thing a particular fleet wants, not something the package should assume.
_DEFAULT_SKEW_REPOS = "agent-media:projects/agent-media"


def skew_repos() -> "list[tuple[str, str]]":
    """[(name, path-relative-to-HOME)], from MEDIA_SKEW_REPOS or the default.

    Entries are `name:relpath`; a bare `name` is shorthand for
    `name:projects/name`. Empty means skew monitoring is off, which is a
    legitimate configuration and not an error.
    """
    raw = os.environ.get("MEDIA_SKEW_REPOS", _DEFAULT_SKEW_REPOS)
    out: list[tuple[str, str]] = []
    for chunk in raw.replace(",", " ").split():
        name, _, rel = chunk.partition(":")
        name = name.strip()
        if not name:
            continue
        out.append((name, (rel.strip() or f"projects/{name}").lstrip("/")))
    return out


def _skew_repo_names() -> "list[str]":
    return [name for name, _ in skew_repos()]

# How often the ledger is refreshed in the background. A clean fleet is checked
# rarely; a fleet with a warning UP is rechecked far more often, because that is
# the state where being wrong is visible. A verdict goes stale the moment the
# host it names is fixed, and nothing tells us that happened — so the only way a
# corrected host clears promptly is to look again soon.
_SKEW_INTERVAL_CLEAN_S = 7200
_SKEW_INTERVAL_WARNING_S = 600


def _repo_head(name: str) -> str:
    """HEAD sha of a local repo, read straight out of `.git`.

    Deliberately not `git rev-parse`: this is called from the status bar, which
    tmux redraws every second, and two subprocesses per second to answer a
    question two file reads can answer is not a trade worth making.
    """
    from pathlib import Path
    rel = dict(skew_repos()).get(name) or f"projects/{name}"
    git = Path.home() / rel / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
    except OSError:
        return ""
    if not head.startswith("ref: "):
        return head                      # detached
    ref = head[5:].strip()
    try:
        return (git / ref).read_text().strip()
    except OSError:
        pass
    try:                                 # ref was packed
        for line in (git / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0]
    except OSError:
        pass
    return ""


def _local_head_sig() -> str:
    """The local revisions a verdict was formed against, e.g.
    `agent-media=981f27d dotfiles=8756927`. Stamped into the ledger by doctor
    and re-checked on read: if local HEAD has moved since, every host was
    compared against code this machine no longer runs, so the verdict describes
    a fleet that no longer exists and must not stay on screen."""
    return " ".join(f"{r}={_repo_head(r)[:7]}" for r in _skew_repo_names())


def _fleet_alert_entries() -> "list[str]":
    """The unhappy hosts from the ledger `media doctor` writes, and the place
    that decides when to go and look again.

    Split out from the line it used to build because the same verdict is now
    shown two ways — beside a reply that is playing, and on its own when none
    is — and both must read one ledger and trigger at most one background
    check between them.
    """
    try:
        from pathlib import Path
        d = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
        logdir = d / "agent-media"
        logdir.mkdir(parents=True, exist_ok=True)
        ledger = logdir / "version-skew.log"

        try:
            mtime = ledger.stat().st_mtime
        except FileNotFoundError:
            mtime = 0

        raw = ledger.read_text() if mtime else ""
        stamp = ""
        entries = []
        for line in raw.splitlines():
            if line.startswith("# judged "):
                stamp = line[len("# judged "):].strip()
            elif line.strip():
                entries.append(line.strip())

        # A verdict formed against code we have since moved off describes a
        # fleet that no longer exists. Drop it and re-check now rather than
        # showing a warning we know is answering an old question.
        if entries and stamp and stamp != _local_head_sig():
            entries = []
            mtime = 0

        interval = _SKEW_INTERVAL_WARNING_S if entries else _SKEW_INTERVAL_CLEAN_S
        if time.time() - mtime > interval:
            ledger.touch()  # prevent concurrent spawns
            import subprocess
            subprocess.Popen(
                [sys.argv[0], "doctor"],
                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

        return entries
    except OSError:
        return []


def _alert_glyph() -> str:
    """Flashing, on the epoch second: the status bar redraws about once a
    second, so alternating with a blank makes it blink."""
    return "⚠" if int(time.time()) % 2 else " "


def _skew_alert_line() -> str:
    """The fleet verdict as a line of its own, for when there is no reply to
    put it beside. "fleet", not "skew": the ledger also carries hosts whose
    install is broken or whose services are down (a trailing !)."""
    entries = _fleet_alert_entries()
    if not entries:
        return ""
    return f"{_alert_glyph()} fleet: {', '.join(entries)}"


def _miss_alert_line() -> str:
    """Flashing `⚠ <target> unreachable (N lost)` — shown INSTEAD of the
    progress bar while spoken replies are known lost and the target hasn't
    acknowledged (the miss ledger is pending). A frozen `▶ 00:00` bar reads
    as playback; a lost reply must read as a fault. The status bar redraws
    ~1/s, so alternating the glyph on the epoch second makes it blink."""
    try:
        from .sinks._miss_notify import pending_miss
        pm = pending_miss()
    except Exception:  # noqa: BLE001 — an alert lookup must never break status
        return ""
    if not pm:
        return ""
    count, _latest = pm
    glyph = "⚠" if int(time.time()) % 2 else " "
    return f"{glyph} {_active_speech_target().name} unreachable ({count} lost)"


def cmd_status(a) -> int:
    # A lost reply still takes the line outright. That alert is about the
    # speech itself: a frozen `▶ 00:00` bar reads as playback, so a bar beside
    # it would be actively misleading, which is the whole reason it replaces.
    miss = _miss_alert_line()
    if miss:
        print(miss)
        return 0
    # The fleet verdict is not about this reply — some other host is stale or
    # unhappy, while the words being spoken here are fine. It used to replace
    # the line all the same, so a complaint nobody could act on from where they
    # were standing (a phone away from a trusted wifi, and no shell without
    # one) blanked the bar speech is watched on, for days. So: a mark beside
    # the reply while one is playing, and the whole line, with the hosts named,
    # when there is nothing to sit beside.
    fleet = _fleet_alert_entries()
    # Prefer the local timeline, but do NOT refuse the remote one: this is the
    # status bar, so cost matters, and on a remote-say target the far side is
    # the only thing that knows an utterance is running. MEDIA_STATUS_NO_REMOTE
    # trades the bar away on that path for a guaranteed-fast render.
    idle, pos, dur, paused, muted, speed, playing = _speech_display_state(
        allow_remote=os.environ.get("MEDIA_STATUS_NO_REMOTE") != "1",
        prefer_local=True)
    # Optional title-overlay bar (EXPERIMENTAL): the whole `▶ pos title dur`
    # segment becomes one background-progress bar, times embedded in the fill.
    # `--title` carries the tmux client width; the title-field width is derived
    # from it (_title_window) so one config fits any screen. Only while playing.
    cw = getattr(a, "title", None)
    # `--now-playing` appends the music/book channel, so one process renders the
    # whole right-hand side of the bar. They answer different questions and are
    # never the same thing: this pane's SPEECH, then what the room is listening
    # to. Both collapse to '' when idle, so a quiet bar stays quiet.
    np_seg = ""
    if getattr(a, "now_playing", False):
        np_seg = _now_playing_segment(_now_playing_window(cw or a.width))

    if cw and playing:
        prefix, body = _subject_label()
        if prefix or body:
            line = _title_status_line(pos, dur, paused, muted, speed, prefix,
                                      body, _title_window(cw), key="status")
            if fleet:
                line = f"{line} {_alert_glyph()}"
            print(f"{line} {np_seg}" if np_seg else line)
            return 0
    speech = _with_visual_glyph(
        render_status(idle=idle, pos=pos, dur=dur, paused=paused, muted=muted,
                      width=a.width, hide_idle=not a.show_idle,
                      bar=not getattr(a, "no_bar", False), speed=speed))
    if fleet:
        # Nothing to sit beside: say which hosts, since the line is free.
        speech = (f"{speech} {_alert_glyph()}" if speech
                  else f"{_alert_glyph()} fleet: {', '.join(fleet)}")
    if np_seg:
        print(f"{speech} {np_seg}" if speech else np_seg)
    else:
        print(speech)
    return 0


def cmd_popup_status(a) -> int:
    """Aggregate the speech popup's whole redraw into ONE process: four lines —
    status / subject-pane label / durable-mute count / speed. The popup used to
    spawn `status` + `now-pane` + `mute-count` separately (~3× Python startup) on
    every refresh, which made it slow to open and slow to react. Emits exactly
    four newline-terminated fields (any may be empty) so the caller reads them
    with four `read -r`s. Readers that only want the status line (the canvas
    controller) take the leading fields and ignore the rest.

    With ``--act VERB [ARGS…]`` it first runs that media subcommand *in this
    process* (reusing main()'s parser/dispatch) and prepends its stdout,
    whitespace-collapsed, as a leading line — so a popup keypress costs ONE
    `media` spawn (action + redraw) instead of two. The popup keeps the
    key→verb map (one source of truth); this just fuses the two spawns. That
    leading line carries e.g. `replay-prev`'s resolved history cursor back to
    the popup; it's empty for actions that print nothing.
    """
    act = getattr(a, "act", None)
    if act:
        import contextlib
        import io
        buf = io.StringIO()
        try:
            ns = _build_parser().parse_args(_end_opts_before_time(act))
            with contextlib.redirect_stdout(buf):
                ns.func(ns)
        except SystemExit:
            pass          # a malformed/parse-failed action must not eat the redraw
        except Exception:  # noqa: BLE001 — nor may an action error blank the popup
            pass
        # Leading line = the action's own output (collapsed to one line), which
        # the caller reads before the three status fields.
        print(" ".join(buf.getvalue().split()))
    alert = _miss_alert_line()
    speed = None
    if alert:
        print(alert)
    else:
        idle, pos, dur, paused, muted, speed, _ = _speech_display_state()
        print(_with_visual_glyph(
            render_status(idle=idle, pos=pos, dur=dur, paused=paused,
                          muted=muted, width=a.width,
                          hide_idle=not a.show_idle,
                          bar=not getattr(a, "no_bar", False), speed=speed)))
    prefix, body = _subject_label()
    print(f"{prefix}{body}" if (prefix or body) else "")
    m = StateStore().list_mutes()
    n = sum(1 for v in m["panes"].values() if v) + \
        sum(1 for v in m["sessions"].values() if v)
    print(n if n else "")
    # Fourth field: the speech rate as its own indicator, shown by the popup
    # whether or not anything is playing (the status line above only carries it
    # mid-clip). Empty at 1.0× — there's nothing worth a column then.
    sp = _sticky_speech_speed(speed)
    # %g on the rounded value, not %.2g: two *significant* digits turns the
    # ladder's 1.25 rung into a wrong "1.2".
    print(f"{round(sp, 2):g}" if sp is not None and abs(sp - 1.0) > 0.05 else "")
    return 0


def cmd_now(a) -> int:
    np = _now_speaking()
    if np:
        print((np["extras"].get("text") or "").strip())
    return 0


def _spoken_extras() -> dict:
    """Extras of the current (or most recent) speech — source of pane/session.

    Actively playing: THIS clip's extras (or {} when paneless — a gateway/
    openclaw agent, `media say`, etc.). Don't fall back to history while
    playing, or paneless speech would borrow the last Claude pane.
    Idle: the most recent clip's extras, so we keep naming whoever last spoke.
    """
    np = _now_speaking()
    if np:
        return np.get("extras") or {}
    rows = _speech_history(1)
    if rows:
        ex = rows[0].get("extras") or {}
        if isinstance(ex, str):
            try:
                ex = json.loads(ex)
            except json.JSONDecodeError:
                ex = {}
        return ex
    return {}


def _spoken_pane() -> Optional[str]:
    """tmux pane id that produced the current (or most recent) speech."""
    return _spoken_extras().get("source_pane") or None


def _spoken_session() -> Optional[str]:
    """Claude Code session id behind the current (or most recent) speech.

    Captured at speech time by the hook, so it survives the source pane being
    closed — lets `goto-pane` offer to resume the conversation.
    """
    return _spoken_extras().get("source_session") or None


def _subject() -> tuple[str, str, bool]:
    """The single thing the popup acts on: ``(pane, tmux_session, following)``.

    "What you see is what every key acts on." The subject is whatever is
    *playing now* (the pane you're actually hearing), else the pane that opened
    the popup. The title, the `M` key, the 🔒 indicator and the `<`/`>` scope
    all resolve through this, so they never disagree. `following` is True when
    the subject is a *different* pane than the caller — i.e. you're hearing
    another conversation, not your own — which the popup flags with `↪`.

    Uses *now-playing* only (not last-history) as the active signal, so an idle
    popup is always "about your pane", never a stale background speaker.
    """
    np = StateStore().get_now_playing("speech")
    ex = (np or {}).get("extras") or {}
    if isinstance(ex, str):
        try:
            ex = json.loads(ex)
        except json.JSONDecodeError:
            ex = {}
    caller = _caller_pane()
    np_pane = ex.get("source_pane") or ""
    if np_pane:
        # "following" (↪) only when we actually have a caller pane to compare
        # AND the subject pane is a live pane *on this server*: the status bar
        # runs `media status` with no pane context (caller=""), where we can't
        # tell; and a pane that's dead here — renumbered by a tmux restore,
        # closed, or living on another host (rooms hub) — isn't a "different
        # live pane" we can honestly point at, so don't flag it with ↪.
        following = bool(caller) and _pane_alive(np_pane) and np_pane != caller
        return np_pane, ex.get("source_tmux_session") or "", following
    return caller, (_tmux_session_for_pane(caller) if caller else ""), False


def _focus_pane(pane: str) -> None:
    """Bring `pane` to the foreground for the calling client.

    Selects the pane within its window/session, then `switch-client`s the
    calling client to that pane's session — without the last step, focus
    never follows when the pane lives in a *different* session than the one
    the popup was opened from (select-window/select-pane only move the target
    session's active pane, not the attached client). Each step is best-effort
    so a missing client or a since-closed pane can't surface a traceback.
    """
    for args in (["select-window", "-t", pane], ["select-pane", "-t", pane]):
        try:
            subprocess.run(["tmux", *args], capture_output=True)
        except Exception:  # noqa: BLE001
            pass
    # Resolve the pane's session and switch the client there.
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
            capture_output=True, text=True)
        sess = (r.stdout or "").strip()
        if sess:
            subprocess.run(["tmux", "switch-client", "-t", sess],
                           capture_output=True)
    except Exception:  # noqa: BLE001
        pass


def _pane_alive(pane: str) -> bool:
    """True if `pane` is still an open tmux pane on this server."""
    if not pane:
        return False
    try:
        r = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
            capture_output=True, text=True)
    except Exception:  # noqa: BLE001
        return False
    if r.returncode != 0:
        return False
    return pane in r.stdout.split()


def _subject_label() -> "tuple[str, str]":
    """`(prefix, title)` for the subject pane — what every key acts on.

    Names the pane playing now, or (idle) the pane that opened the popup, via
    `_subject()`. `prefix` holds the leading indicators — `↪ ` when the subject
    is a *different* pane than the caller (you're hearing another conversation)
    and `🔒 ` when that subject is muted — returned *separately* from the title
    so a marquee can pin them (keep them fixed) while only the title scrolls.
    `('', '')` when no subject pane resolves.

    While something is PLAYING the title comes from the clip itself — the
    conversation title captured when it was queued (`source_window`) — not from
    a live lookup of the pane's window name. They diverge constantly: Claude
    renames the window as the conversation moves on, and the queue runs behind,
    so a live lookup labels the audio you're hearing with whatever that pane
    has got up to *since*. Live pane names are for the idle case, where the
    subject is your own pane and there's no clip to speak for itself.

    Resolving live, we prefer the *window name* (which tracks the stable Claude
    conversation title) over the *pane title* — the pane title is the transient
    tool-status, so it carries a leading spinner glyph and flips to whatever
    Claude is doing right now rather than naming what's actually being spoken.
    Falls back to a spinner-stripped pane title only when the window has no
    usable name.

    Shared by `now-pane` (popup marquee) and the optional status-bar marquee.
    """
    pane, tmux_sess, following = _subject()
    if not pane:
        return "", ""
    np = _now_speaking()
    label = ((np or {}).get("extras", {}).get("source_window") or "").strip()
    # Resolve the live pane name only when the pane is actually open on this
    # server. A pane that's dead here (renumbered by a tmux-resurrect restore,
    # closed since, or — for a rooms hub — living on another host) returns
    # success-with-empty-fields from `display-message`, which the old
    # `returncode != 0` guard sailed straight past, leaving a blank title.
    if not label and _pane_alive(pane):
        try:
            r = subprocess.run(
                ["tmux", "display-message", "-p", "-t", pane,
                 "#{window_name}\t#{pane_title}"],
                capture_output=True, text=True)
        except Exception:  # noqa: BLE001 — popup must never see a traceback
            r = None
        if r is not None and r.returncode == 0:
            window_name, _, pane_title = r.stdout.strip().partition("\t")
            # A default-named window (the shell/program name) is no better than
            # the pane title; only prefer it when it's a real conversation title.
            label = window_name.strip()
            if not label or label in {"zsh", "bash", "sh", "fish"}:
                # Strip a leading Claude spinner glyph (braille U+2800–U+28FF).
                label = re.sub(r"^[⠀-⣿]\s*", "", pane_title.strip())
    if not label:
        # Idle, with a pane that won't resolve here (renumbered/closed/remote):
        # fall back to the title the LAST clip carried, so the bar names whoever
        # spoke most recently instead of showing a bare ↪ with an empty title.
        label = (_spoken_extras().get("source_window") or "").strip()
    prefix = ""
    if following:
        prefix += "↪ "
    if StateStore().resolve_mute(pane, tmux_sess):
        prefix += "🔒 "
    return prefix, label


def _marquee(text: str, width: int, *, key: str = "status",
             gap: str = "   ") -> str:
    """A `width`-column window into `text`, scrolling at a steady rate.

    The offset comes from how long the CURRENT text has been showing, not from
    a counter bumped once per call. Per-call was wrong in a way that only
    appears once you have company: this runs as a fresh process for every pane
    on every redraw, and they all share the one state file, so the marquee
    sped up in proportion to how many panes were watching it — and drifted
    into a different phase in each.

    The file now records only *when* the text changed, so a new track still
    starts from column zero, but the write happens on that change rather than
    on every redraw. Rate via MEDIA_STATUS_MARQUEE_CPS (columns per second).
    Text that already fits is returned as-is.
    """
    text = " ".join(text.split())
    if not text or width <= 0:
        return ""
    if len(text) <= width:
        return text
    try:
        cps = float(os.environ.get("MEDIA_STATUS_MARQUEE_CPS", "1.0"))
    except ValueError:
        cps = 1.0
    cps = max(0.1, cps)

    now = time.time()
    p = state_dir() / f"marquee-{key}"
    try:
        saved = json.loads(p.read_text())
        last, since = saved.get("t"), float(saved.get("s", now))
    except Exception:  # noqa: BLE001
        last, since = None, now
    if last != text or since > now:
        # New subject (or a clock that went backwards): restart the crawl and
        # stamp it. This is the only path that writes.
        since = now
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"t": text, "s": since}))
        except OSError:
            pass

    loop = text + gap
    off = int((now - since) * cps) % len(loop)
    return (loop + loop)[off:off + width]


_NOW_PLAYING_CHANNELS = (
    # (icon, state-relative socket, marquee key). Book first: if a book is
    # running it is what the room is listening to, and music underneath it is
    # the bed, not the subject.
    ("📖", "sink-book.sock", "np-book"),
    ("♪", None, "np-music"),          # None -> the Mopidy-Mpv renderer socket
)


def _now_playing_segment(window: int) -> str:
    """`<icon> <scrolling title> [pos/dur]` for the music or book channel.

    Deliberately reads the mpv sockets DIRECTLY rather than going through the
    service layer. `book_now_playing()` is the natural-looking call and costs
    ~2.6s, because it reasons about remote targets; `_srv()` alone is ~0.6s.
    Both are fine for a keypress and hopeless for a status line that redraws
    every second in every pane. The sockets are local and answer in under a
    millisecond, and everything this renders is in the snapshot.

    Returns '' when neither channel is playing, so an idle bar stays empty.
    """
    from .sinks import _mpv_ipc as ipc
    from .sinks.music import _mpv_socket

    props = key = icon = None
    for ch_icon, sock_name, ch_key in _NOW_PLAYING_CHANNELS:
        sock = str(state_dir() / sock_name) if sock_name else _mpv_socket()
        if not sock or not os.path.exists(sock):
            continue
        try:
            snap = ipc.get_properties(
                sock, ["idle-active", "pause", "time-pos", "duration",
                       "media-title", "chapter-metadata/by-key/title"])
        except Exception:  # noqa: BLE001 — a dead sink is just "not playing"
            continue
        if not snap or snap.get("idle-active"):
            continue
        props, key, icon = snap, ch_key, ch_icon
        break

    if props is None:
        return ""

    label = _mpv_music_label(props)
    if not label:
        return ""
    pos, dur = props.get("time-pos"), props.get("duration")
    hours = bool(dur is not None and dur >= 3600)
    times = f"[{fmt_time(pos, hours=hours)}/{fmt_time(dur, hours=hours)}]"
    if props.get("pause"):
        icon = "⏸"
    # The marquee gets whatever is left after the icon, the times and their
    # separating spaces, so the segment as a whole honours `window`.
    body = max(4, window - len(times) - len(icon) - 2)
    return f"{icon} {_marquee(label, body, key=key)} {times}"


def _client_width(v) -> int:
    """argparse type for --title: a tmux client width, tolerant of a literal
    unexpanded `#{client_width}` (→ 80) so the status bar never errors out."""
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return 80


def _now_playing_window(client_width: int) -> int:
    """Columns the now-playing segment may occupy, from the tmux client width.

    Sized like `_title_window` so the two segments scale together and a narrow
    client shrinks both rather than letting one crowd the other off the line.
    Bounds via MEDIA_STATUS_NOW_PLAYING_{MIN,MAX}."""
    nmin = int(os.environ.get("MEDIA_STATUS_NOW_PLAYING_MIN", "18"))
    nmax = int(os.environ.get("MEDIA_STATUS_NOW_PLAYING_MAX", "46"))
    return max(nmin, min(nmax, client_width // 3))


def _title_window(client_width: int) -> int:
    """Title-field width derived from the tmux client width, so one status-bar
    config fits any screen (wide desktop → roomy, ~32-col phone → tight): a
    quarter of the client, clamped. Bounds via MEDIA_STATUS_TITLE_{MIN,MAX}."""
    tmin = int(os.environ.get("MEDIA_STATUS_TITLE_MIN", "8"))
    tmax = int(os.environ.get("MEDIA_STATUS_TITLE_MAX", "26"))
    return max(tmin, min(tmax, client_width // 4))


def _title_status_line(pos: Optional[float], dur: Optional[float],
                       paused: Optional[bool], muted: Optional[bool],
                       speed: Optional[float], prefix: str, body: str,
                       width: int, *, key: str = "status") -> str:
    """The whole speech status segment as ONE background-progress bar.

    `▶ {pos} {prefix}{scrolling title} {dur}` is rendered as a single field
    whose background colour-fills left→right by progress, so the numeric times
    on either side of the title are *part of* the bar rather than sitting
    outside it. `prefix` (the ↪/🔒 indicators) is pinned — only `body` scrolls
    within the remaining space — so the indicator stays visible. `width` is the
    title field (prefix + body window); the times/icon add a few cols either
    side.

    Emits tmux `#[...]` directives (honoured inside `#()` status output).
    Colours via MEDIA_STATUS_TITLE_{FILL,REST}; `#[default]` resets at the end
    (set the env vars to the theme's status-right colours if the handoff to
    whatever follows looks off). Mute/speed readouts ride after the bar.
    """
    icon = "⏸" if paused else "▶"
    hours = bool(dur is not None and dur >= 3600)
    bodywin = _marquee(body, max(1, width - len(prefix)), key=key)
    titlefield = f"{prefix}{bodywin}"
    inner = f"{icon} {fmt_time(pos, hours=hours)} {titlefield} {fmt_time(dur, hours=hours)}"
    frac = (pos / dur) if (pos and dur) else 0.0
    frac = max(0.0, min(1.0, frac))
    split = int(round(frac * len(inner)))
    fill = os.environ.get("MEDIA_STATUS_TITLE_FILL", "bg=colour24,fg=colour231")
    rest = os.environ.get("MEDIA_STATUS_TITLE_REST", "bg=colour236,fg=colour250")
    line = f"#[{fill}]{inner[:split]}#[{rest}]{inner[split:]}#[default]"
    if muted:
        line += " [M]"
    if isinstance(speed, (int, float)) and abs(speed - 1.0) > 0.05:
        line += f" {'⏩' if speed > 1.0 else '🐢'}{round(speed, 2):g}×"
    return line


def cmd_now_pane(a) -> int:
    """Print the popup's subject-pane title (see `_subject_label`).

    With `--width N` the body is windowed through `_marquee` (own state key,
    so it doesn't double-advance the status bar's crawl) — used by the control
    popup's border title, re-expanded by tmux once per status-interval.
    """
    if getattr(a, "session_only", False):
        pane, sess, _following = _subject()
        print(sess or (_tmux_session_for_pane(pane) if pane else ""))
        return 0
    prefix, body = _subject_label()
    width = getattr(a, "width", None)
    if width:
        body = _marquee(body, max(1, width - len(prefix)), key="popup-border")
    if prefix or body:
        print(f"{prefix}{body}")
    return 0


def cmd_goto_pane(a) -> int:
    """Focus the pane that produced the now-playing (or last) speech.

    Exit codes let the popup react instead of silently no-opping when the
    pane is gone:
      0  focused a live pane (nothing printed)
      3  pane is closed but a Claude session is resumable — its id is printed
         on stdout so the popup can offer `claude --resume <id>`
      2  pane is closed and there's nothing to resume
      1  no source pane was ever captured (paneless speech)
    """
    pane = _spoken_pane()
    if pane and _pane_alive(pane):
        _focus_pane(pane)
        return 0
    session = _spoken_session()
    if session:
        print(session)
        return 3
    if pane:
        return 2  # had a pane, it's closed, no session to fall back to
    return 1


def _session_cwd(sid: str) -> Optional[str]:
    """Working directory a Claude Code session was recorded under, or None.

    `claude --resume <id>` only finds a session when run from that session's
    project directory — transcripts live under ~/.claude/projects/<enc-cwd>/,
    keyed by the cwd they ran in. So a resume launched from the wrong pane's
    cwd fails with "No conversation found". Recover the real cwd from the
    transcript's first line that carries one.
    """
    import glob as _glob
    root = os.path.expanduser("~/.claude/projects")
    hits = _glob.glob(os.path.join(root, "*", f"{sid}.jsonl"))
    if not hits:
        return None
    try:
        with open(hits[0], encoding="utf8", errors="replace") as fh:
            for line in fh:
                if '"cwd"' not in line:
                    continue
                try:
                    cwd = json.loads(line).get("cwd") or ""
                except Exception:  # noqa: BLE001
                    continue
                if cwd:
                    return cwd
    except Exception:  # noqa: BLE001
        return None
    return None


def cmd_open_session(a) -> int:
    """Open a new tmux window resuming the given Claude Code session.

    The popup calls this after `goto-pane` reports a closed pane (rc 3) and
    the user confirms — it brings the conversation back as `claude --resume`.

    The new window MUST start in the session's own project cwd: `claude
    --resume` resolves the id per-project, so launching from the caller pane's
    directory (whatever it happened to be) would fail silently and the window
    would close instantly. Mirror the claude-resume CLI: `-c <cwd>` plus
    `env -u ANTHROPIC_API_KEY` so a stray key doesn't override the login.
    """
    sid = (getattr(a, "session", "") or "").strip()
    if not sid:
        return 1
    argv = ["tmux", "new-window"]
    cwd = _session_cwd(sid)
    if cwd:
        argv += ["-c", cwd]
    argv.append(f"env -u ANTHROPIC_API_KEY claude --resume {sid}")
    try:
        subprocess.run(argv, capture_output=True)
    except Exception:  # noqa: BLE001
        return 1
    return 0


def _ncmpcpp_pane() -> Optional[str]:
    """tmux pane id running ncmpcpp on this server, or None.

    Scans every pane (all sessions/windows) and matches the foreground
    command, so the music `g` lands on the player wherever it lives.
    """
    try:
        r = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{pane_current_command}"],
            capture_output=True, text=True)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        pane, _, cmd = line.partition("\t")
        if cmd.strip() == "ncmpcpp":
            return pane
    return None


def cmd_goto_track(a) -> int:
    """Focus the ncmpcpp pane and jump it to the now-playing song.

    Mirrors the speech side's goto-pane for the music channel: bring the
    player to the foreground, then send ncmpcpp's default JumpToPlayingSong
    key (`o`) so it centers on the track the music sink is playing. Returns
    1 (and stays quiet) when no ncmpcpp pane is running, so the popup can
    show a hint instead of silently doing nothing.
    """
    pane = _ncmpcpp_pane()
    if not pane:
        return 1
    _focus_pane(pane)
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "o"],
                       capture_output=True)
    except Exception:  # noqa: BLE001
        pass
    return 0


def cmd_open_ncmpcpp(a) -> int:
    """Open a new tmux window running ncmpcpp.

    The popup calls this when the music `g` found no ncmpcpp pane and the
    user confirms. ncmpcpp's config sets `jump_to_now_playing_song_at_start`,
    so it lands on the current track without us sending `o`. The launch
    command is overridable via MEDIA_NCMPCPP_CMD (e.g. a wrapper or a path).
    """
    cmd = os.environ.get("MEDIA_NCMPCPP_CMD", "ncmpcpp")
    try:
        subprocess.run(["tmux", "new-window", cmd], capture_output=True)
    except Exception:  # noqa: BLE001
        return 1
    return 0


# Window name we launch the book's mpvc-tui under, so goto-book can find it
# again regardless of what the foreground command reports (rlwrap/sh/mpvc-tui).
_MPVC_WINDOW = "agent-media-book"


def _mpvc_pane() -> Optional[str]:
    """tmux pane id showing the book's mpvc-tui, or None.

    The book channel's player is mpvc-tui — an IPC client of the headless
    sink-book broker (analogous to ncmpcpp for Mopidy). We launch it in a
    window named `_MPVC_WINDOW`, so match that first; also accept a pane whose
    foreground command is mpvc-tui in case one was started by hand.
    """
    try:
        r = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_id}\t#{window_name}\t#{pane_current_command}"],
            capture_output=True, text=True)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pane, wname, cmd = parts
        if wname == _MPVC_WINDOW or cmd.strip() == "mpvc-tui":
            return pane
    return None


def cmd_goto_book(a) -> int:
    """Focus the book's mpvc-tui player pane (the book channel's `g`).

    Mirrors goto-track for the book channel: bring an existing mpvc-tui to the
    foreground. Since mpvc-tui only drives the broker over IPC, the audiobook
    keeps playing on the multi-room stream. Returns 1 (quietly) when no
    mpvc-tui is running, so the popup can offer to open one.
    """
    pane = _mpvc_pane()
    if not pane:
        return 1
    _focus_pane(pane)
    return 0


def cmd_open_mpvc(a) -> int:
    """Open a new tmux window running mpvc-tui bound to the book socket.

    The popup calls this when the book `g` found no mpvc-tui pane and the user
    confirms. mpvc-tui's socket already defaults to sink-book.sock; the launch
    command/mode is overridable via MEDIA_MPVC_CMD (e.g. `mpvc-tui -tt` for the
    tiny TUI, or a wrapper that sets the socket explicitly).
    """
    cmd = os.environ.get("MEDIA_MPVC_CMD", "mpvc-tui -t")
    try:
        subprocess.run(["tmux", "new-window", "-n", _MPVC_WINDOW, cmd],
                       capture_output=True)
    except Exception:  # noqa: BLE001
        return 1
    return 0


def _ask_context(channel: str) -> str:
    """A one-line "what I'm listening to" blurb for the active channel.

    Recombines what the popup already surfaces per channel into a compact,
    paste-ready line so `media open-pi` can seed a fresh pi/nvim session with
    the user's current place in their listening. Never raises — a broken
    backend just yields an empty (or partial) blurb rather than a traceback in
    the popup's `a` path.
    """
    ch = (channel or "speech").strip()
    try:
        if ch == "music":
            m = SinkMusicRouter(SinkMusic())
            line, label, _ = _music_now_status(m, 20, hide_idle=True, bar=False)
            label = " ".join((label or "").split())
            clock = (line or "").lstrip("▶⏸○ ").strip()
            if not label:
                return ""
            return (f"I'm listening to music: {label}"
                    + (f" [{clock}]" if clock else ""))
        if ch == "book":
            srv = _srv()
            np = srv.book_now_playing(target="")
            if np.get("idle"):
                return ""
            title = (np.get("title") or np.get("media_title")
                     or np.get("uri") or "").strip()
            chap = (np.get("chapter_title") or "").strip()
            pos = float(np.get("position_ms") or 0) / 1000.0
            dur = float(np.get("duration_ms") or 0) / 1000.0
            clock = f"{_hms(pos)} / {_hms(dur)}" if dur else _hms(pos)
            what = " — ".join(p for p in (title, chap) if p)
            if not what:
                return ""
            return f"I'm listening to an audiobook: {what} [{clock}]"
        # speech (default): the clip I'm hearing right now.
        np = _now_speaking()
        if not np:
            return ""
        ex = np.get("extras") or {}
        ctx = ((ex.get("current_sentence") or "").strip()
               or (ex.get("text") or "").strip())
        ctx = " ".join(ctx.split())
        if not ctx:
            return ""
        return f'From the agent speech I\'m listening to: "{ctx}"'
    except Exception:  # noqa: BLE001 — the popup must never see a traceback
        return ""


def cmd_ask_context(a) -> int:
    """Print the listening-context blurb for CHANNEL (see `_ask_context`)."""
    print(_ask_context(getattr(a, "channel", "") or "speech"))
    return 0


def cmd_open_pi(a) -> int:
    """Open a fresh pi window seeded with my listening context + a question.

    The music/book/speech analogue of speech's `g`: the popup's `a` key reads a
    question, and this opens a new tmux window running the user's pi launcher
    with `"<listening context>\n\n<question>"` as the first message — a new
    conversation *about what I'm hearing*, which is what a listening-context ask
    almost always is (the player is the context's origin, not an ongoing chat).

    The launcher is `MEDIA_PI_CMD` (default `p` — the user's pi-in-nvim wrapper,
    a zsh function, so it's run through `zsh -ic`; set e.g. `p -c` to continue
    the last session, or `pi` for raw pi instead of the nvim wrapper).
    """
    question = (getattr(a, "question", "") or "").strip()
    context = _ask_context(getattr(a, "channel", "") or "speech")
    prompt = "\n\n".join(p for p in (context, question) if p)
    if not prompt:
        return 1
    pi_cmd = os.environ.get("MEDIA_PI_CMD", "p")
    # `p` is a zsh *function* (pi-in-nvim), so it only resolves in an
    # interactive zsh; `zsh -ic` gets us that. shlex-quote twice: once for the
    # inner `p '<prompt>'`, once to hand that whole line to zsh as one arg.
    inner = f"{pi_cmd} {shlex.quote(prompt)}"
    cmd = f"zsh -ic {shlex.quote(inner)}"
    try:
        subprocess.run(["tmux", "new-window", cmd], capture_output=True)
    except Exception:  # noqa: BLE001
        return 1
    return 0


def _ask_title(channel: str) -> str:
    """What is playing, as a name rather than a sentence.

    `_ask_context` produces prose for the question; this produces the subject,
    which is what a fresh conversation's window gets called. Speech has none —
    a spoken reply is not a thing with a title — and the empty string is the
    honest answer there rather than a made-up one.
    """
    ch = (channel or "speech").strip()
    try:
        if ch == "music":
            m = SinkMusicRouter(SinkMusic())
            _, label, _ = _music_now_status(m, 20, hide_idle=True, bar=False)
            return " ".join((label or "").split())
        if ch == "book":
            np = _srv().book_now_playing(target="")
            if np.get("idle"):
                return ""
            return " ".join(str(np.get("title") or np.get("media_title")
                                or "").split())
    except Exception:  # noqa: BLE001 — a name is not worth a traceback
        return ""
    return ""


def _ask_snapshot(session: str = "", channel: str = "speech") -> dict:
    """What `media ask` would do, without doing it.

    One shape for the terminal and the phone alike: which conversation is being
    addressed, whether it is still going, and the context line the question
    would be joined to. A surface that shows the answer before the question is
    typed is a surface where nobody asks the void.
    """
    from . import conversation as conv_mod

    conv = conv_mod.resolve(session)
    live = conv_mod.liveness(conv)
    reason = live.reason
    if conv is None and session:
        # Asked for by name and not found is not the same as "nobody has ever
        # spoken here", and a surface that says the second when it means the
        # first sends the user looking for a fault that is not there.
        reason = "that conversation has not spoken here"
    out = {"live": bool(live), "reason": reason,
           "session": "", "label": "", "window": "", "pane": "",
           "tmux": "", "age_s": None, "last": "",
           "context": _ask_context(channel),
           # What a fresh conversation would be called, so a surface can offer
           # to start one by name instead of only reporting that nobody is in.
           "subject": _ask_title(channel)}
    out["new_window"] = conv_mod.window_name(channel, out["subject"])
    if conv is not None:
        out.update(session=conv.session, label=conv.label, window=conv.window,
                   pane=conv.pane, tmux=conv.tmux, age_s=int(conv.age()),
                   last=conv.text[:200])
    return out


def cmd_ask(a) -> int:
    """Put a question to the conversation that has been talking to me.

    The thread is the speech history — every turn it holds is tagged with the
    session that spoke it — so this needs no state of its own. It resolves the
    conversation, checks it is still going, and types the question into its
    pane. The answer comes back the way every other reply does: spoken, tagged
    with the same session, which is what makes the next ask find this one.

    Exit 3, not 1, when there is no live conversation. It is not a failure —
    it is an answer, and the caller (the phone, mostly) wants to tell the two
    apart so it can say "that conversation has closed" rather than "error".
    """
    from . import conversation as conv_mod

    session = (getattr(a, "session", "") or "").strip()
    channel = getattr(a, "channel", "") or "speech"
    snap = _ask_snapshot(session, channel)
    if getattr(a, "status", False):
        if getattr(a, "json", False):
            print(json.dumps(snap))
        else:
            # The reason names the conversation itself, so the marker is all
            # that is added here; printing the label as well said it twice.
            print(f"{'▶' if snap['live'] else '○'} {snap['reason']}")
            if snap["last"]:
                print(f"  last said: {snap['last'][:110]}")
        return 0 if snap["live"] else 3

    question = " ".join((getattr(a, "question", "") or "").split())
    if not question:
        print("ask: nothing to ask", file=sys.stderr)
        return 1
    line_for_new = conv_mod.compose(
        question, "" if getattr(a, "no_context", False) else snap["context"],
        via=(getattr(a, "via", "") or "media ask"))
    if not snap["live"]:
        if getattr(a, "no_new", False):
            if getattr(a, "json", False):
                print(json.dumps({**snap, "asked": False, "started": ""}))
            else:
                print(f"ask: {snap['reason']}", file=sys.stderr)
            return 3
        # Nothing listening, so start something — and name the window for what
        # is being asked about. tmux's window name is what the speech hook
        # records as source_window, which is what a conversation's label is
        # read back from, so the moment this one answers it becomes findable
        # like any other and the next question lands in it rather than beside
        # it. That is the whole difference from `open-pi`, which starts a
        # window that nothing can ever address again.
        if getattr(a, "dry_run", False):
            print(f"{conv_mod.window_name(channel, snap['subject'])}: "
                  f"{line_for_new}")
            return 0
        name = conv_mod.start(line_for_new, channel=channel,
                              title=snap["subject"],
                              session=(snap["tmux"] or ""))
        if getattr(a, "json", False):
            print(json.dumps({**snap, "asked": bool(name),
                              "started": name or "",
                              "reason": (f"started {name}" if name else
                                         "could not start a conversation")}))
        elif name:
            print(f"started {name}")
        else:
            print("ask: could not start a conversation", file=sys.stderr)
        return 0 if name else 4

    conv = conv_mod.resolve(snap["session"])
    line = line_for_new
    if getattr(a, "dry_run", False):
        print(line)
        return 0
    ok = conv_mod.deliver(conv, line, verify=not getattr(a, "no_verify", False))
    if getattr(a, "json", False):
        print(json.dumps({**snap, "asked": bool(ok),
                          "reason": snap["reason"] if ok else
                          "typed into the pane but the session did not take it"}))
    elif ok:
        print(f"asked {snap['label']}")
    else:
        # Typed and not accepted is its own outcome and must not read as sent.
        # A still-initialising TUI swallows text and Enter without trace, and
        # claiming delivery on the strength of send-keys is how tmux-relay's
        # runner once reported work underway when there was none.
        print("ask: typed into the pane but the session did not take it",
              file=sys.stderr)
    return 0 if ok else 4


def _print_open_url(url: str) -> int:
    """Print a URL for client-side opening.

    The tmux popup consumes stdout and presents it to the attached client as an
    OSC 8 link. Avoid opening a browser on the media host in that path; it is
    usually a headless SSH/tmux server, not the device in the user's hand.
    """
    print(url)
    if (os.environ.get("MEDIA_WEB_PRINT_ONLY") or "").strip() == "1":
        return 0
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") \
            or os.environ.get("BROWSER"):
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    return 0


def _first_url(raw: str, fallback: str = "") -> str:
    return (raw.replace(",", " ").split() or [fallback])[0]


def _tailscale_magicdns_name() -> str:
    """This node's short MagicDNS name for URLs opened from another tailnet device."""
    try:
        r = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                           text=True, timeout=3)
        data = json.loads(r.stdout or "{}") if r.returncode == 0 else {}
        self_node = data.get("Self") or {}
        # MagicDNS adds the tailnet search domain, so the short name keeps the
        # popup link readable while still opening on phones/laptops in the tailnet.
        name = str(self_node.get("HostName") or "").strip()
        if name:
            return name
        name = str(self_node.get("DNSName") or "").strip().rstrip(".")
        if name:
            return name
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        pass
    return ""


def _canvas_web_url() -> str:
    if raw := os.environ.get("MEDIA_CANVAS_URL"):
        return _first_url(raw)
    try:
        port = int(os.environ.get("MEDIA_VISUAL_PORT") or "8781")
    except ValueError:
        port = 8781
    if host := (os.environ.get("MEDIA_VISUAL_MAGICDNS_HOST")
                or _tailscale_magicdns_name()):
        return f"http://{host}:{port}/"
    if raw := os.environ.get("MEDIA_VISUAL_URL"):
        return _first_url(raw)
    return f"http://127.0.0.1:{port}"


def cmd_speech_web(a) -> int:
    """Open the visual canvas for the speech channel."""
    return _print_open_url(_canvas_web_url())


def cmd_book_web(a) -> int:
    """Open the simple-mpv-webui browser control page for the book channel.

    Set MEDIA_BOOK_WEB_URL to the host running the webui. Prefer a bare tailnet
    IP over a MagicDNS name: it's short enough to show on one line in the popup
    (Termux's long-press URL detection has no OSC 8) and resolves without
    MagicDNS. Defaults to loopback.
    """
    return _print_open_url(os.environ.get(
        "MEDIA_BOOK_WEB_URL", "http://127.0.0.1:8889/"))


def cmd_music_web(a) -> int:
    """Open the Mopidy-Iris web UI for the music channel (the music analogue of
    book-web). Set MEDIA_MUSIC_WEB_URL for the same reasons. Defaults to loopback.
    """
    return _print_open_url(os.environ.get(
        "MEDIA_MUSIC_WEB_URL", "http://127.0.0.1:6680/iris/"))


def cmd_highlight_toggle(a) -> int:
    """Toggle auto-highlight on/off. Prints the new state.

    Turning it on jumps focus to the speaking pane (so the copy-mode
    follow-along is actually visible) and highlights the current sentence
    immediately for feedback.
    """
    from .intake.submit import (toggle_auto_highlight, _tmux_highlight_text,
                                ensure_follow_view, set_force_highlight,
                                _set_follow_rows, publish_follow_text)
    on = toggle_auto_highlight()
    # Prefer the pane that produced the speech; fall back to the popup's
    # caller pane if we never captured a source pane.
    pane = (_spoken_pane()
            or os.environ.get("TTS_POPUP_PANE")
            or os.environ.get("TMUX_PANE", ""))
    # The pane half of following along, opened/closed with the flag: it is the
    # half that keeps working when the app owns the screen, so leaving it on a
    # separate switch would mean the feature is "on" and invisible.
    ensure_follow_view(on, pane=pane, deliberate=True)
    if on:
        # Turning it on IS "I have stopped typing and I am attending now", so
        # it overrides the keystroke skip the same way `prefix V` does —
        # otherwise the reply already in flight, which you almost certainly
        # typed into a moment ago, stays unfollowed to its end.
        set_force_highlight()
        if pane:
            # Jump to the speaking pane so the follow-along is on screen.
            _focus_pane(pane)
            os.environ["TMUX_PANE"] = pane
            if not os.environ.get("TMUX"):
                os.environ["TMUX"] = "x"
            # If a sentence is playing right now, highlight it immediately —
            # and if it cannot be found on screen, hand it to the status rows,
            # which is the same choice the scheduler makes per sentence. The
            # reply in flight is the one you turned this on for.
            # The bar takes its one row now, whether or not anything is
            # playing: the switch is usually thrown between replies, and a
            # switch with no visible effect is indistinguishable from a broken
            # one. (The row says "follow-along on" when idle.)
            _set_follow_rows(False, pane)
            publish_follow_text(
                ((_now_speaking() or {}).get("extras") or {}).get("current_sentence"),
                pane)
            np = _now_speaking()
            sentence = (np.get("extras") or {}).get("current_sentence") if np else None
            if sentence and not _tmux_highlight_text(sentence, force=True):
                _set_follow_rows(True, pane)
        print("highlight: ON")
    else:
        # Kill any in-flight clear-timer (so it can't fire into the pane after
        # we're off), then force copy-mode shut and verify it actually left —
        # otherwise the pane stays in tmux copy-mode, eating the app's own
        # scroll/transcript keys even though highlighting is "off".
        if pane:
            from .intake.submit import (_kill_pending_clear,
                                        _force_cancel_copy_mode)
            _kill_pending_clear(pane)
            _force_cancel_copy_mode(pane)
        # And take the rows back now rather than at the end of the reply: they
        # render nothing once the feature is off, so leaving them would sit
        # four blank lines under you for however long the reply had left.
        publish_follow_text(None, pane)     # silent while it is off
        _set_follow_rows(False, pane)
        print("highlight: OFF")
    return 0


def cmd_highlight_now(a) -> int:
    """Force highlight follow-along for the upcoming turn(s), bypassing the
    keystroke-skip, until the user types again. Bound to tmux `prefix V`.

    The keystroke-skip suppresses the highlight for a turn when you've just
    typed (so it doesn't yank copy-mode out from under you). This says "I've
    stopped — follow along now" without waiting it out. If a sentence is
    already playing, it highlights it immediately for instant feedback.
    """
    from .intake.submit import (set_force_highlight, _is_auto_highlight_enabled,
                                _tmux_highlight_text)
    set_force_highlight()
    if not _is_auto_highlight_enabled():
        # Force only overrides the keystroke-skip, not the master opt-in.
        print("highlight: armed (note: auto-highlight is OFF — toggle it on)")
        return 0
    pane = (_spoken_pane()
            or os.environ.get("TTS_POPUP_PANE")
            or os.environ.get("TMUX_PANE", ""))
    if pane:
        os.environ["TMUX_PANE"] = pane
        if not os.environ.get("TMUX"):
            os.environ["TMUX"] = "x"
        np = _now_speaking()
        sentence = (np.get("extras") or {}).get("current_sentence") if np else None
        if sentence:
            _tmux_highlight_text(sentence, force=True)
    print("highlight: now (until you type again)")
    return 0


def cmd_current_sentence(a) -> int:
    """Print the currently-spoken sentence (one of many in a response).

    Designed for tmux status-line use: shows a karaoke-style indicator of
    what's being read aloud right now, without touching the source pane.
    Truncates to --width chars (default 80) with an ellipsis so it fits.

    This is the follow-along that costs no layout — a row of chrome rather than
    rows taken from the conversation — and it works inside a fullscreen TUI,
    where the copy-mode highlight has no scrollback to search.

    --follow claims the row for follow-along, which is one feature with one
    switch (the popup's `v`): silent while that is off, the spoken sentence
    while it is on, and — idle — whether it is on at all, since that is the
    question one asks between replies, when there is no sentence to show.
    Without it the row is unconditional, which is what a bare status-line
    karaoke indicator wants.
    """
    from .intake.submit import _is_auto_highlight_enabled
    follow = getattr(a, "follow", False)
    if follow and not _is_auto_highlight_enabled():
        return 0
    np = _now_speaking()
    ex = (np or {}).get("extras") or {}
    sentence = (ex.get("current_sentence") or "").strip() if np else ""
    if not sentence:
        if follow:
            # Dim: present enough to answer the question, quiet enough to
            # ignore. tmux styles rather than ANSI — this is a status line.
            print("#[fg=colour244]♪ follow-along on#[default]")
        return 0
    sentence = " ".join(sentence.split())  # collapse whitespace
    width = getattr(a, "width", 80) or 80
    row = getattr(a, "row", None)
    if row is None:
        if len(sentence) > width:
            sentence = sentence[: max(0, width - 1)].rstrip() + "…"
        print(f"♪ {sentence}")
        return 0
    # One status row per --row: a sentence longer than the bar gets wrapped
    # across as many as the caller has laid out, rather than truncated. The
    # last row it fits in is where the truncation goes, if any.
    import textwrap
    lines = textwrap.wrap(f"♪ {sentence}", width=max(8, width),
                          subsequent_indent="  ") or [""]
    if row >= len(lines):
        return 0
    if row == getattr(a, "rows", 0) - 1 and len(lines) > row + 1:
        line = lines[row]
        print(line[: max(0, width - 1)].rstrip() + "…")
        return 0
    print(lines[row])
    return 0


def _follow_size(a) -> tuple[int, int]:
    import shutil
    size = shutil.get_terminal_size(fallback=(80, 24))
    return (getattr(a, "width", None) or size.columns,
            getattr(a, "height", None) or size.lines)


def cmd_follow(a) -> int:
    """Follow-along pane: the reply being spoken, current sentence marked.

    A surface of our own, for the case the copy-mode highlight can't serve: a
    fullscreen TUI holds the alternate screen, so there is no scrollback to
    search and the app redraws over anything we paint. This pane reads the same
    state the highlight does and touches nobody else's terminal.

    Runs until interrupted. `--once` prints a single frame (scripting, tests).
    """
    from . import follow as F

    interval = max(0.05, getattr(a, "interval", 0.2))
    last_key = None
    last_sentences: list[str] = []
    quiet_since: Optional[float] = None
    out = sys.stdout
    if not getattr(a, "once", False):
        out.write("\x1b[?25l")              # hide the cursor while we own the pane
    try:
        while True:
            np = _now_speaking()
            ex = (np or {}).get("extras") or {}
            sentences = ex.get("clip_sentences") or []
            idx = ex.get("current_sentence_idx")
            if not sentences:
                # A lane that reports no sentence list still has the text; show
                # it whole rather than nothing. Mark it only once the audio has
                # actually started — the row exists from the moment the reply is
                # submitted, and marking the whole thing while it is still
                # rendering claims we are reading words nobody has heard yet.
                whole = (ex.get("text") or "").strip()
                sentences = [whole] if whole else []
                idx = 0 if whole and ex.get("play_started_at") else None
            width, height = _follow_size(a)
            if sentences:
                last_sentences = sentences
                quiet_since = None
                lines = F.frame(sentences, idx, width, height)
            else:
                # Hold the last reply on screen a moment before dimming it, so
                # the final sentence doesn't lose its mark the instant the audio
                # stops — that is exactly when you're still reading it.
                quiet_since = quiet_since or time.time()
                lines = F.idle_frame(" ".join(last_sentences), width, height)
            key = (tuple(sentences), idx, width, height, bool(quiet_since))
            if key != last_key:
                out.write(F.CLEAR + "\n".join(lines))
                out.flush()
                last_key = key
            if getattr(a, "once", False):
                out.write("\n")
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if not getattr(a, "once", False):
            out.write("\x1b[?25h\n")
            out.flush()


def cmd_text(a) -> int:
    """Return the currently-speaking text, or the latest history entry if idle."""
    np = _now_speaking()
    if np:
        txt = (np["extras"].get("text") or "").strip()
        if txt:
            print(txt)
            return 0
    rows = _speech_history(1)
    if rows:
        txt = (rows[0].get("text") or "").strip()
        if txt:
            print(txt)
    return 0


def _history_index_for_pane(pane: str, limit: int = 50) -> Optional[int]:
    """1-based index into recent speech history of the latest clip produced
    by `pane` (1 = most recent overall). None if the pane has no clip."""
    if not pane:
        return None
    for i, r in enumerate(_speech_history(limit), start=1):
        ex = r.get("extras") or {}
        if isinstance(ex, str):
            try:
                ex = json.loads(ex)
            except json.JSONDecodeError:
                ex = {}
        if ex.get("source_pane") == pane:
            return i
    return None


def _patch_speech_mirror(**live) -> None:
    """Optimistically patch the speech now_playing mirror (live_pause/speed/mute)
    so a control shows up in the popup on its very next redraw, instead of
    lagging ~1s until the intake monitor re-reads the remote player. The monitor
    overwrites these with ground truth on its next tick, so a stale patch is
    self-correcting."""
    store = StateStore()
    np = store.get_now_playing("speech")
    if not np:
        return
    ex = np.get("extras")
    if not isinstance(ex, dict):
        return
    ex.update(live)
    store.set_now_playing("speech", uri=np["uri"], started_at=np["started_at"],
                          target=np.get("target") or "local",
                          content_type=np.get("content_type"), extras=ex)


def _stamp_speech_pause(paused: Optional[bool] = None) -> None:
    """Record a pause we issued, so the clock-driven follow-along freezes with
    the audio. The doing lives in intake.submit, because the report stream from
    a remote renderer stamps the same row for pauses we did NOT issue."""
    from .intake.submit import stamp_speech_pause
    stamp_speech_pause(StateStore(), paused)


def cmd_toggle(a) -> int:
    # If nothing is loaded, "play" means replay a clip (matches the old
    # popup's Space = play/pause-or-replay). Prefer the most recent clip from
    # the *active* pane (the one that opened the popup), so Space-while-idle
    # replays "what this pane just said"; fall back to the latest overall.
    # Otherwise flip pause.
    if _remote_speech():
        # Decide from LOCAL state, act with ONE atomic command.
        #
        # This used to read the player to find out whether anything was playing
        # and what `pause` currently was, then write back the opposite. Both
        # halves were wrong over a bridge that loses a fifth of its packets.
        # The read fails often with nothing wrong at the far end, and a failed
        # read looks exactly like "nothing is playing" — so Space fell through
        # to the replay branch, which on this lane loads a pseudo-URI mpv
        # cannot open and does nothing at all. Pressing pause during speech
        # simply had no effect, intermittently, for no visible reason.
        #
        # So: whether a reply is in flight comes from now_playing, which is
        # written on this host and cannot be dropped in transit; and the toggle
        # is `cycle pause`, which flips it at the player. Read-then-write also
        # raced the renderer — say.sh clears pause before each clip — and cost
        # two round trips to do one thing.
        if not _speech_in_flight():
            pane = os.environ.get("TTS_POPUP_PANE") or os.environ.get("TMUX_PANE", "")
            return _do_replay(_history_index_for_pane(pane) or 1)
        # critical: a keypress is not policy chatter. Fire-and-forget, because
        # pausing suspends the phone's audio device (~0.6s) and the reply adds
        # nothing — the next redraw reads the player itself.
        #
        # The flip is written as a value rather than as `cycle pause`. `cycle`
        # is mpv's verb, and the phone lane no longer always ends at mpv: the
        # companion app answers the same socket with its own subset, which
        # rejects `cycle` — and a fire-and-forget command never hears the
        # refusal, so Space simply stopped pausing speech, silently, on the
        # lane it is pressed on most. What `cycle` would have flipped is
        # already written down here for the follow-along clock, so decide from
        # the row and set the value: still one command, still no read.
        want = not _speech_paused_mirror()
        try:
            ipc.send_nowait(_sock(), "set_property", "pause", want,
                            critical=True)
        except Exception:  # noqa: BLE001
            ipc.set_property(_sock(), "pause", want, critical=True)
        # Same decision, recorded: the follow-along on this lane runs on a
        # clock and would otherwise read on through the silence.
        _stamp_speech_pause(want)
        _SNAP_CACHE["value"] = None   # next redraw asks the player, not the cache
        return 0
    if _get("idle-active"):
        pane = os.environ.get("TTS_POPUP_PANE") or os.environ.get("TMUX_PANE", "")
        return _do_replay(_history_index_for_pane(pane) or 1)
    want = not bool(_get("pause", critical=True))
    ipc.set_property(_sock(), "pause", want, critical=True)
    _stamp_speech_pause(want)
    return 0


def cmd_pause(a) -> int:
    SinkSpeech().pause(_active_speech_target())
    return 0


def cmd_resume(a) -> int:
    SinkSpeech().resume(_active_speech_target())
    return 0


def cmd_stop(a) -> int:
    SinkSpeech().stop(_active_speech_target())
    return 0


def cmd_speech_flush(a) -> int:
    """Drop every queued/pending reply. The clip currently speaking is not
    cut (pause/stop/supersede handle that); flushed replies still land in
    history, marked flushed, so nothing the user browses later is lost."""
    from .intake.submit import request_speech_flush
    request_speech_flush()
    print("speech: pending queue flushed — the current clip, if any, plays out; "
          "flushed replies are archived unheard")
    return 0


def _hold_owner(explicit: str | None) -> str | None:
    """Who this hold belongs to.

    Explicit wins; then MEDIA_SPEECH_HOLD_OWNER; then the tmux pane, so that
    several sessions each hold their own without anybody having to invent a
    name. Falling back to the pane matters because the common case is now N
    Claude sessions in N panes: with one shared marker the second session to
    release would lift the first session's hold and talk over it.

    None means the unnamed marker — the pre-owner behaviour, kept for callers
    outside tmux that never asked for any of this.
    """
    if explicit:
        return explicit
    env = os.environ.get("MEDIA_SPEECH_HOLD_OWNER", "").strip()
    if env:
        return env
    pane = os.environ.get("TMUX_PANE", "").strip()
    if pane:
        # "%42" -> "pane42": the marker is a filename, and '%' is legal but
        # reads like an escape in half the places these names get printed.
        return "pane" + pane.lstrip("%")
    return None


def cmd_speech_hold(a) -> int:
    """Hold the start of new speech playback, with a mandatory expiry."""
    from .intake.submit import (release_speech_hold, set_speech_hold,
                                speech_holders)
    owner = _hold_owner(getattr(a, "owner", None))

    if a.release:
        if getattr(a, "all", False):
            release_speech_hold(everyone=True)
            print("speech: all holds released")
        else:
            release_speech_hold(owner)
            print(f"speech: hold released ({owner or 'unnamed'})")
        return 0

    if a.seconds is None:
        held = speech_holders()
        if not held:
            print("speech: no hold active")
            return 0
        now = time.time()
        parts = ", ".join(
            f"{name or 'unnamed'} ({until - now:.0f}s)"
            for name, until in sorted(held.items(), key=lambda kv: -kv[1])
        )
        print(f"speech: held by {parts}")
        return 0

    try:
        until = set_speech_hold(a.seconds, owner)
    except ValueError as exc:
        print(f"speech: {exc}", file=sys.stderr)
        return 2
    if not until:
        print("speech: could not write the hold marker", file=sys.stderr)
        return 1
    who = owner or "unnamed"
    print(f"speech: {who} holding new playback for {until - time.time():.0f}s "
          "(expires on its own; `media speech-hold --release` lifts it early). "
          "Speech stays held while any owner holds it.")
    return 0


def cmd_converse_reply(a) -> int:
    """Answer a waiting `converse` with typed text.

    The MCP `converse` tool speaks a question and blocks on a unix socket for
    the answer. Normally that answer is a transcript handed over by
    tmux-voice-bridge — but the socket does not care where the words came from,
    and an agent replying over the relay has no microphone on that path. This
    is that door.

    The caller is usually another agent, so the exit code carries the outcome
    and the three failures are kept apart: nothing armed is a different problem
    from armed-but-unacknowledged, and only the first is safe to retry blind.
    """
    from .capture.rendezvous import (offer, pending_question, socket_path,
                                     wait_for_question)

    if a.pending:
        q = (wait_for_question(a.wait) if a.wait else pending_question())
        if q is None:
            print("converse: nothing waiting", file=sys.stderr)
            return 3
        if a.json:
            print(json.dumps(q))
        else:
            print(q["text"])
        return 0

    if not a.text:
        print("converse: nothing to say — pass the reply text",
              file=sys.stderr)
        return 2
    # Checked before offering only to tell 3 from 4; offer() is authoritative
    # and races here resolve as a 4, which is the honest answer anyway.
    if not socket_path().exists():
        print("converse: nobody waiting", file=sys.stderr)
        return 3
    if not offer(a.text):
        print("converse: nobody took the reply — it was NOT delivered",
              file=sys.stderr)
        return 4
    print("converse: reply delivered")
    return 0


# Every speech control below sends `critical=True`.
#
# The breaker exists to stop *policy* chatter — "is anything playing, should I
# duck it" — from delaying speech, and those callers all treat a skip as
# "unknown, carry on". A keypress is not chatter: skipping it doesn't save a
# round trip anyone was waiting on, it makes the control silently do nothing.
# Pause was made critical when this bit it once (a paused clip parked at 100%);
# the rest were left behind, so one dropped display read on a lossy bridge shut
# jump, skip, volume, mute, speed and replay for the whole 45s cool-off while
# the player sat there answering probes in milliseconds. From a terminal that
# printed "endpoint slow"; from the popup or the canvas the button just did
# nothing, which is indistinguishable from a broken control.
#
# Critical calls still *report* failure — they only skip the pre-emptive
# refusal, and (slow_s=0) they never arm the breaker against the display read
# on latency alone, which is what kept the popup blank before.
def cmd_seek(a) -> int:
    ipc.command(_sock(), "seek", a.secs, "relative", critical=True)
    return 0


def cmd_volume(a) -> int:
    """Nudge the broker's software volume by ±delta (the popup's -/= keys).

    The ceiling is the broker's own --volume-max (MEDIA_SPEECH_VOLUME_MAX,
    default 200), not a hardcoded number: mpv refuses a volume above its max,
    and a clamp *below* the resting level made the first press of either key
    snap down to the clamp — so "louder" made speech quieter.
    """
    cur = _get("volume", critical=True) or 100
    ceiling = _broker_max_volume()
    ipc.set_property(_sock(), "volume",
                     max(0, min(int(ceiling), int(cur) + a.delta)),
                     critical=True)
    return 0


def cmd_mute(a) -> int:
    ipc.set_property(_sock(), "mute", not bool(_get("mute", critical=True)),
                     critical=True)
    return 0


# --- durable per-pane / per-session mute (Step 3/4) -------------------------

def _live_panes() -> list[str]:
    """Current tmux pane ids across all sessions, or [] if tmux is unreachable.

    [] means "couldn't determine" — callers must treat it as such and never
    use it to prune (see StateStore.prune_panes, which no-ops on an empty set).
    """
    try:
        r = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                           capture_output=True, text=True)
    except Exception:  # noqa: BLE001
        return []
    return r.stdout.split() if r.returncode == 0 else []


def _mute_target_pane(a) -> str:
    """Resolve which pane a mute command acts on.

    Precedence: explicit --pane → --subject (the popup's subject: what's
    playing now, else the caller pane) → --current (legacy: the speaking/last
    pane) → the calling shell's $TMUX_PANE → the speaking pane as a last resort.
    The popup uses --subject so `M` acts on the same thing the title shows.
    """
    if getattr(a, "pane", None):
        return a.pane
    if getattr(a, "subject", False):
        return _subject()[0]
    if getattr(a, "current", False):
        return _spoken_pane() or ""
    return os.environ.get("TMUX_PANE", "") or (_spoken_pane() or "")


def _silence_current_if_covered(scope: str, key: str) -> bool:
    """Stop the speech broker if it's *actively* playing a clip from a pane the
    mute now covers, so `M` feels immediate (like `m`) instead of only
    suppressing the next response. The in-flight response is already in history,
    so it stays replayable. Returns True if it stopped something.
    """
    np = _now_speaking()                 # active playback only (not history)
    if not np:
        return False
    ex = np.get("extras") or {}
    covered = (ex.get("source_pane") == key if scope == "pane"
               else ex.get("source_tmux_session") == key)
    if covered:
        try:
            SinkSpeech().stop(_active_speech_target())
        except Exception:  # noqa: BLE001 — a dead/absent broker mustn't fail the mute
            pass
        return True
    return False


def cmd_mute_pane(a) -> int:
    """Set/clear durable per-pane (or --session) speech mute. Default toggles.

    A muted pane still renders + records history (the popup can replay it) but
    is never played live and never ducks music — enforced at intake. Muting
    also stops the covered pane's currently-playing clip, if any.
    """
    state = StateStore()
    pane = _mute_target_pane(a)
    if not pane:
        print("media mute-pane: no target pane (not in tmux and nothing "
              "speaking) — pass --pane %ID", file=sys.stderr)
        return 1
    from .intake.submit import _tmux_session_for_pane
    session = _tmux_session_for_pane(pane)
    action = getattr(a, "state", None) or "toggle"

    if getattr(a, "session", False):
        if not session:
            print(f"media mute-pane: could not resolve a tmux session for "
                  f"{pane}", file=sys.stderr)
            return 1
        scope, key = "session", session
        if action == "on":
            new = True
        elif action == "off":
            new = False
        else:  # toggle this session's own override
            new = not bool(state.get_mute("session", key))
    else:
        scope, key = "pane", pane
        if action == "on":
            new = True
        elif action == "off":
            new = False
        else:  # toggle the *effective* state, so it flips what you actually hear
            new = not state.resolve_mute(pane, session)

    state.set_mute(scope, key, new)
    stopped = _silence_current_if_covered(scope, key) if new else False
    print(f"{scope} {key}: {'muted' if new else 'unmuted'}"
          f"{' (stopped current)' if stopped else ''}")
    return 0


def cmd_mute_status(a) -> int:
    """List per-pane / per-session mutes, pruning since-closed panes first."""
    state = StateStore()
    live = _live_panes()
    if live:
        state.prune_panes(live)   # only when tmux gave a reliable snapshot
    live_set = set(live)
    mutes = state.list_mutes()
    panes, sessions = mutes["panes"], mutes["sessions"]
    if not panes and not sessions:
        print("no per-pane or per-session mutes set")
        return 0
    for key, m in sorted(panes.items()):
        tag = "" if key in live_set else " (dead)"
        print(f"pane    {key}{tag}: {'muted' if m else 'unmuted'}")
    for key, m in sorted(sessions.items()):
        print(f"session {key}: {'muted' if m else 'unmuted'}")
    return 0


def cmd_mute_count(a) -> int:
    """Print the total number of muted panes + sessions (nothing when zero).

    Drives the popup's "you have N things muted" badge so a durable mute set
    on a pane you're not looking at doesn't silently stay forgotten.
    """
    m = StateStore().list_mutes()
    n = sum(1 for v in m["panes"].values() if v) + \
        sum(1 for v in m["sessions"].values() if v)
    if n:
        print(n)
    return 0


def cmd_pane_muted(a) -> int:
    """Print '1' when the popup's *subject* pane is effectively muted.

    Drives the popup's 🔒 indicator. Resolves the same subject as the title
    and `M` (`_subject()`: what's playing now, else the caller pane), so the
    glyph always reflects exactly what `M` would toggle. An explicit `--pane`
    overrides. Silent (prints nothing) when unmuted or unresolvable.
    """
    pane = getattr(a, "pane", None)
    sess = ""
    if not pane:
        pane, sess, _ = _subject()
    if not pane:
        return 0
    if not sess:
        sess = _tmux_session_for_pane(pane)
    if StateStore().resolve_mute(pane, sess):
        print("1")
    return 0


# Speed [ / ] ladder. At/above 1.0x, presses hop these rungs — the gaps widen
# (1.0→1.25→1.5→2.0→3.0) so a held key accelerates. Below 1.0x, fine
# flat 0.1 steps for precise control (no ladder). Symmetric for up/down. As a
# position ladder (snap off the live speed) it needs no cross-press accel state —
# each listening-mode [ / ] press is a separate `media speed` process.
# Keep in sync with SPEED_RUNGS in tmux/media-popup (book channel's shell copy).
_SPEED_MIN, _SPEED_MAX, _SPEED_FLAT = 0.3, 3.0, 0.1
_SPEED_RUNGS = (1.0, 1.25, 1.5, 2.0, 3.0)


def _speed_next(cur: float, direction: int) -> float:
    """Next speed for a [ / ] press: +1 faster / -1 slower. Hops _SPEED_RUNGS
    at/above 1.0x; flat _SPEED_FLAT steps below. Clamped to [_SPEED_MIN, _SPEED_MAX]."""
    eps = 1e-6
    if direction > 0:
        if cur < 1.0 - eps:
            return min(round(cur + _SPEED_FLAT, 2), 1.0)
        for r in _SPEED_RUNGS:
            if r > cur + eps:
                return r
        return _SPEED_MAX
    if cur > 1.0 + eps:
        for r in reversed(_SPEED_RUNGS):
            if r < cur - eps:
                return r
        return 1.0
    return max(round(cur - _SPEED_FLAT, 2), _SPEED_MIN)


def cmd_speed(a) -> int:
    """Set speech speed: absolute factor, 'reset' (→1.0), or relative 'up'/'down'
    (the listening-mode [ / ] keys) which snap the live sink along the speed ladder.
    The raw '+0.1' / '-0.1' forms still apply a literal delta. Clamped to range."""
    sock = _sock()
    f = a.factor

    def _cur() -> float:
        # For a remote target read the live speed off the local mirror rather
        # than paying a bridge round-trip (matches cmd_toggle).
        if _remote_speech():
            sp = _speech_display_state()[5]
            return float(sp) if isinstance(sp, (int, float)) else 1.0
        cur = _get("speed", critical=True)
        return float(cur) if isinstance(cur, (int, float)) else 1.0

    if f == "reset":
        target = 1.0
    elif f in ("up", "down"):
        target = _speed_next(_cur(), 1 if f == "up" else -1)
    elif f and f[0] in "+-":
        target = max(_SPEED_MIN, min(_SPEED_MAX, _cur() + float(f)))
    else:
        target = max(_SPEED_MIN, min(_SPEED_MAX, float(f)))
    target = round(target, 2)
    ipc.set_property(sock, "speed", target, critical=True)
    # Remember it: the rate sticks on the broker across clips, so the popup
    # keeps showing it while idle (see _sticky_speech_speed).
    StateStore().set_speech_speed(target)
    if _remote_speech():
        _patch_speech_mirror(live_speed=target)
    return 0


def _seek_to_end(sock) -> int:
    """Skip to the end of the (last) clip so the response finishes."""
    # A seek-to-end only plays out if the broker isn't paused/muted: a paused
    # clip just parks the playhead at 100% and never reaches EOF (so the popup
    # `>` looked like a no-op when the clip had been paused, e.g. via Space).
    # Clear those first so the clip actually finishes.
    for prop in ("pause", "mute"):
        try:
            ipc.set_property(sock, prop, False, critical=True)
        except ipc.MpvIpcError:
            pass
    # On a multi-clip replay the response's clips are queued as one mpv
    # playlist; seeking the *current* clip to 100% would only advance to the
    # next one. Jump to the final playlist entry first so we land on the
    # actual last clip before seeking it to the end — but only if we're not
    # already on it. Re-setting playlist-pos to the index that's already
    # current makes mpv *reload* that entry (restart it from 0): that was the
    # "`>` repeated the current clip instead of ending it" bug, hit whenever
    # the popup's `>` landed while the last clip was already playing.
    try:
        count = ipc.get_property(sock, "playlist-count", critical=True)
        pos = ipc.get_property(sock, "playlist-pos", critical=True)
        if isinstance(count, int) and count > 1 and pos != count - 1:
            ipc.set_property(sock, "playlist-pos", count - 1, critical=True)
    except ipc.MpvIpcError:
        pass
    ipc.command(sock, "seek", 100, "absolute-percent", critical=True)
    return 0


def cmd_jump(a) -> int:
    """Seek to the start or end of the current clip."""
    sock = _sock()
    if a.where == "start":
        ipc.command(sock, "seek", 0, "absolute", critical=True)
        return 0
    # End-of-response. On a *replay* the clips are queued as one mpv playlist,
    # so seeking the last entry to its end finishes the whole response. During
    # a *live* readout each sentence is a separate loadfile (playlist-count 1):
    # seeking the current clip to EOF would just let the reader loop advance to
    # the next sentence — making `>` behave like `l`. Hand the reader a
    # past-the-end jump so it stops after the current clip instead of
    # continuing, then seek the current clip out so playback ends promptly.
    np = StateStore().get_now_playing("speech")
    ex = (np or {}).get("extras") or {}
    if isinstance(ex, str):
        try:
            ex = json.loads(ex)
        except json.JSONDecodeError:
            ex = {}
    sentences = ex.get("clip_sentences") or []
    try:
        count = ipc.get_property(sock, "playlist-count", critical=True)
    except ipc.MpvIpcError:
        count = 1
    playlist = isinstance(count, int) and count > 1
    if len(sentences) > 1 and not playlist:
        _write_nav_request(len(sentences),
                           (np or {}).get("target") or SPEECH_TARGET.name)
    return _seek_to_end(sock)


def _nav_target(cur: int, n: int, para_idx: list, unit: str,
                direction: int) -> int:
    """Resolve the sentence index to jump to for `media skip`.

    A return >= n means "past the last section" → finish the response; a
    negative return is clamped to 0 by the caller (restart the first section).
    """
    if unit == "sentence":
        return cur + (1 if direction > 0 else -1)
    # paragraph
    if not para_idx or cur >= len(para_idx):
        return cur + (1 if direction > 0 else -1)
    cur_para = para_idx[cur]
    if direction > 0:
        nxt = [p for p in para_idx if p > cur_para]
        if not nxt:
            return n  # already in the last paragraph → finish
        tp = min(nxt)
        return next(j for j in range(n) if para_idx[j] == tp)
    # backward: to the start of the current paragraph, else the previous one's
    para_start = next(j for j in range(n) if para_idx[j] == cur_para)
    if cur > para_start:
        return para_start
    prev = [p for p in para_idx if p < cur_para]
    if not prev:
        return 0
    tp = max(prev)
    return next(j for j in range(n) if para_idx[j] == tp)


def _force_highlight_sentence(sentence: str) -> None:
    """Force the copy-mode highlight onto `sentence` (used for replay jumps)."""
    from .intake.submit import _tmux_highlight_text
    pane = os.environ.get("TTS_POPUP_PANE") or os.environ.get("TMUX_PANE", "")
    if "#{" in pane:
        pane = ""
    if not pane:
        return
    os.environ["TMUX_PANE"] = pane
    if not os.environ.get("TMUX"):
        os.environ["TMUX"] = "x"
    try:
        _tmux_highlight_text(sentence, force=True)
    except Exception:  # noqa: BLE001 — popup must never see a traceback
        pass


def _write_nav_request(idx: int, target_name: str = "local") -> None:
    """Drop a jump request the live reader loop reads after the current clip.

    Keyed by the *playing* target (e.g. "rooms" for the Snapcast feed) so the
    flag filename matches what the reader loop polls — the loop runs with
    MEDIA_SPEECH_DEFAULT_TARGET, which isn't necessarily "local".
    """
    from .intake.submit import _nav_flag_path
    try:
        _nav_flag_path(Target(name=target_name)).write_text(str(idx))
    except OSError:
        pass


# Rapid skip presses must chain: each press is one step from where the LAST
# press pointed, not from a re-read of "current sentence" — the playlist-pos /
# mirror reads can lag a quick second press (bridge latency, mirror tick), so
# re-deriving would compute the same target and merely replay the sentence the
# first press chose. The breadcrumb holds the last commanded index, honored
# while presses cluster within this window.
_SKIP_CHAIN_S = 3.0


def _skip_cursor_path() -> Path:
    return state_dir() / f"skip-cursor-{SPEECH_TARGET.name}"


def _read_skip_cursor() -> Optional[int]:
    try:
        p = _skip_cursor_path()
        if time.time() - p.stat().st_mtime > _SKIP_CHAIN_S:
            return None
        return int(p.read_text().strip())
    except (OSError, ValueError):
        return None


def _write_skip_cursor(idx: int) -> None:
    try:
        p = _skip_cursor_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(idx))
    except OSError:
        pass


def cmd_skip(a) -> int:
    """Step the speech reader forward/back by a sentence (h/l) or paragraph (H/L).

    Works both on a replay (clips queued as one mpv playlist → jump by
    playlist-pos) and during the live readout (the reader loop picks up a jump
    request even while paused). Falls back to a plain time-seek of
    --seek-fallback seconds when there's no multi-sentence sequence to step.
    """
    sock = _sock()
    direction = 1 if a.dir > 0 else -1

    def _time_seek() -> int:
        try:
            ipc.command(sock, "seek", float(a.seek_fallback), "relative",
                        critical=True)
            return 0
        except ipc.MpvIpcError:
            return 1

    np = StateStore().get_now_playing("speech")
    ex = (np or {}).get("extras") or {}
    if isinstance(ex, str):
        try:
            ex = json.loads(ex)
        except json.JSONDecodeError:
            ex = {}
    sentences = ex.get("clip_sentences") or []
    para_idx = ex.get("clip_paragraph_idx") or []
    n = len(sentences)

    try:
        idle = bool(ipc.get_property(sock, "idle-active", critical=True))
    except ipc.MpvIpcError:
        idle = True
    try:
        raw = ipc.get_property(sock, "playlist-count", critical=True)
        count = int(raw) if isinstance(raw, int) else 1
    except ipc.MpvIpcError:
        count = 1

    # "Play from this sentence" with nothing playing.
    #
    # Stepping assumes a reader to move; an absolute jump does not. The canvas
    # transcript is READ AFTER the reply has finished — that is what it is for
    # — so by the time a sentence is tapped the player is idle and
    # now_playing has been cleared, leaving nothing to seek and no sentence
    # list to seek within. Every tap ran, returned 0, and did nothing.
    #
    # So bring the reply back first, then jump into it. One retry only: if the
    # replay produced no sentence list either, fall through rather than loop.
    if getattr(a, "to", None) is not None and (idle or n <= 1)             and not getattr(a, "_replayed", False):
        a._replayed = True
        # WHICH reply, not just "the last one". The canvas transcript belongs
        # to a particular pane, and the newest clip in the history is very
        # often something else — an org reminder, another session's voice.
        # Replaying that and jumping to sentence four of it is worse than doing
        # nothing, which is what the first attempt did: something played, and
        # it was not what had been tapped.
        pane = getattr(a, "pane", None)
        if pane:
            os.environ["TTS_POPUP_PANE"] = pane
            replayed = cmd_replay_at_cursor(argparse.Namespace()) == 0
        else:
            replayed = _do_replay(1, session=_anchor_session()) == 0
        if replayed:
            for _ in range(20):
                time.sleep(0.15)
                ex2 = ((StateStore().get_now_playing("speech") or {})
                       .get("extras") or {})
                if ex2.get("clip_sentences"):
                    return cmd_skip(a)
    if n <= 1 or idle:
        return _time_seek()
    if len(para_idx) != n:
        para_idx = list(range(n))  # no paragraph map → one paragraph per line

    playlist = count > 1
    if playlist:
        try:
            cur = int(ipc.get_property(sock, "playlist-pos", critical=True) or 0)
        except ipc.MpvIpcError:
            cur = 0
    else:
        cur = ex.get("current_sentence_idx")
        if cur is None:
            return _time_seek()
        cur = int(cur)

    # A press within the chain window steps from the LAST press's target,
    # whatever the (possibly lagging) live read said.
    crumb = _read_skip_cursor()
    if crumb is not None and 0 <= crumb < n:
        cur = crumb

    # An absolute jump — "play from this sentence" — reuses everything below
    # unchanged. All three lanes (playlist, single-clip-with-offsets, live
    # readout) already take a resolved target index; only the way of choosing
    # it differed, and stepping was the only way offered. The canvas transcript
    # asks for this when a line is double-tapped.
    if getattr(a, "to", None) is not None:
        target = int(a.to)
    else:
        target = _nav_target(cur, n, para_idx, a.unit, direction)
    if target < 0:
        target = 0
    if target > n - 1:
        target = n - 1
    _write_skip_cursor(min(target, n - 1))

    if playlist:
        if target >= n:
            return _seek_to_end(sock)
        try:
            ipc.set_property(sock, "playlist-pos", target, critical=True)
        except ipc.MpvIpcError:
            return 1
        # Rapid presses race mpv's async entry loads: an earlier in-flight
        # jump can commit AFTER ours and clobber it (observed as a skip
        # "bouncing back" a moment later). Verify once, best-effort.
        try:
            time.sleep(0.15)
            if int(ipc.get_property(sock, "playlist-pos",
                                    critical=True) or -1) != target:
                ipc.set_property(sock, "playlist-pos", target, critical=True)
        except (ipc.MpvIpcError, TypeError, ValueError):
            pass
        _force_highlight_sentence(sentences[target])
        return 0
    # One clip holding every sentence, with the boundaries known: the far side
    # rendered the whole reply in one piece (the phone lane), so there is no
    # reader loop watching for a nav flag and no playlist to step — but there IS
    # a timeline. Seek the player to where the sentence starts.
    offsets = ex.get("clip_offsets_s") or []
    if len(offsets) == n:
        if target >= n:
            return _seek_to_end(sock)
        try:
            ipc.command(sock, "seek", float(offsets[target]), "absolute",
                        critical=True)
        except ipc.MpvIpcError:
            return 1
        # Move the timeline's origin with the playhead. The follower that writes
        # current_sentence for this lane runs on the clock, not on the player —
        # left alone it would drag the highlight back to wherever the reply had
        # got to, a beat after the jump.
        try:
            ex["play_started_at"] = time.time() - float(offsets[target])
            ex["current_sentence"] = sentences[target]
            ex["current_sentence_idx"] = target
            StateStore().set_now_playing(
                "speech", uri=(np or {}).get("uri") or "",
                started_at=(np or {}).get("started_at") or time.time(),
                target=(np or {}).get("target") or SPEECH_TARGET.name,
                extras=ex)
        except Exception:  # noqa: BLE001 — the seek already happened
            pass
        _force_highlight_sentence(sentences[target])
        return 0
    # Live readout: hand the jump to the reader loop (honored even while
    # paused). Key the flag by the target that's actually playing, falling
    # back to the CLI's resolved speech target — NOT "local", which orphans
    # the flag whenever now_playing lacks a target (the reader polls the
    # actual playout target's flag).
    _write_nav_request(target, (np or {}).get("target") or SPEECH_TARGET.name)
    return 0


def _replay_visual(extras: dict) -> None:
    """Re-show the visual that accompanied a replayed reply. A replay means
    "that again" — for a figure-bearing reply the picture IS part of it, and
    without this it plays under whatever newer artwork holds the canvas.
    Best-effort: needs the visual package's push memory and live spool files."""
    key = (extras or {}).get("dedup_key")
    if not key:
        return
    try:
        from agent_media_visual.state import load_push, spool_dir
    except ImportError:
        return
    payload = load_push(str(key))
    if not payload:
        return
    # Spool-relative names must still exist (GC keeps ~200); absolute /img/
    # URLs can't be checked from here — push and let the canvas 404 quietly.
    names = ([payload.get("image")] if payload.get("image")
             else [b.get("image") for b in payload.get("sequence") or []])
    for nm in names:
        if nm and "/" not in str(nm) and not (spool_dir() / str(nm)).is_file():
            return
    import urllib.request
    urls = (os.environ.get("MEDIA_VISUAL_URL") or "").replace(",", " ").split()
    for base in urls:
        try:
            req = urllib.request.Request(
                base.rstrip("/") + "/show",
                data=json.dumps(payload).encode(),
                method="POST", headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=2).read()
        except OSError:
            pass


#: How far past an addressed row to look for one that can actually be heard.
#: Bounded so a long run of failed renders cannot turn one keypress into a scan
#: of the whole history; eight is more consecutive failures than have ever been
#: seen, and past that the honest answer is "nothing to replay".
_REPLAY_LOOKAHEAD = 8


def _do_replay(index: int, session: Optional[str] = None) -> int:
    rows = _speech_history(max(1, index) + _REPLAY_LOOKAHEAD,
                           session=session, include_live=True)
    if len(rows) < index:
        print("media: no clip to replay", file=sys.stderr)
        return 1
    # Step past turns that never rendered. They stay in the history and keep
    # their place in it — the indices are shared with the pane lookup and the
    # < / > cursor, so dropping them would silently move every other row — but
    # they are not what "replay" means. On 2026-08-17 three org reminders whose
    # renderer had timed out sat at the top of the history, and `r` in the popup
    # kept addressing the newest of them: nothing played, and nothing said why.
    for offset, row in enumerate(rows[index - 1:]):
        if not _row_has_audio(row):
            continue
        if offset:
            print(f"media: skipped {offset} turn(s) that never rendered",
                  file=sys.stderr)
        return _replay_row(row)
    print("media: no clip to replay", file=sys.stderr)
    return 1


def _replay_row(row: dict) -> int:
    """Play one speech-history row: push its clips to the speech target and
    refresh now_playing (+ position follower). The traversal path addresses
    rows by index (`_do_replay`); the clip browser addresses them by history
    id (`replay --id`), which stays stable while a picker is open even if new
    clips land meanwhile."""
    uri = row.get("uri")
    if not uri:
        return 1
    if not _row_has_audio(row):
        # The traversal filters these out, so reaching one here means the clip
        # browser or a `--id` addressed it directly. Refuse rather than hand
        # mpv `remote-say:phone`, which it will treat as a path and fail on
        # silently — the failure this whole guard exists to make legible.
        print("media replay: that one never rendered — nothing to play",
              file=sys.stderr)
        return 1
    ex = row.get("extras") or {}
    clip_uris: list[str] = ex.get("clip_uris") or [uri]
    clip_durations: list[float] = ex.get("clip_durations_s") or []
    if not clip_durations and len(clip_uris) == 1 and ex.get("total_duration_s"):
        # A lane that renders the whole reply as one clip measures it once and
        # records that, never a per-clip list. Without this the replay has "no
        # durations", so no follower is spawned at all — no progress bar and no
        # follow-along on every phone-lane replay.
        clip_durations = [float(ex["total_duration_s"])]
    replay_text: str = row.get("text") or ""

    # Re-show the reply's visual concurrently with the (slow, bridge-bound)
    # playback push below; the thread outlives neither — the process waits.
    threading.Thread(target=_replay_visual, args=(ex,)).start()

    sink = SinkSpeech()
    # A remote target plays clips from its clips-relay dir, which only the
    # live intake path populates — and a replayed item's clips may never have
    # arrived (e.g. rendered while ssh to the phone was down) or may have been
    # cleaned since. Re-push them first; on failure the sink resolves clips to
    # the HTTP base URL instead. No-op for local/rooms, and cheap (one
    # multiplexed ssh hop) when the files are already there.
    #
    # Unless the far side rendered them itself: then the audio has never
    # existed on this host, and "re-pushing" would look up a path that isn't
    # here, fail, and drop the whole replay to the HTTP fallback for files that
    # were already sitting next to the player.
    if not ex.get("clips_remote"):
        getattr(sink, "prefetch", lambda *a, **k: True)(clip_uris, SPEECH_TARGET)
    # Push the whole turn in ONE batched round-trip (stop/clear/append-all/
    # unpause/jump-to-0) rather than 1 play + N queues + 2 state-sets — each a
    # ~600ms hop over the phone bridge. Traversing (< / >) or replaying a long
    # multi-clip turn otherwise drove every clip individually, blocking the
    # popup for seconds per press (a 14-clip reply ≈ 8s frozen); mashing back
    # through a few clips then looked like the popup had hung. Mirrors the live
    # intake path (play_playlist), which also clears any lingering pause/mute so
    # a "replay" ("I want to hear this now") is audible past a stale pause/mute.
    if len(clip_uris) > 1:
        sink.play_playlist(clip_uris, SPEECH_TARGET)
    else:
        # Single clip: one loadfile + explicit state reset. OSError too — a
        # missing/refused socket (mpv not up yet) must be a no-op, not a
        # traceback (_open raises raw FileNotFoundError/ConnectionRefused).
        sink.play(clip_uris[0], SPEECH_TARGET)
        try:
            ipc.set_property(_sock(), "pause", False, critical=True)
            ipc.set_property(_sock(), "mute", False, critical=True)
        except (ipc.MpvIpcError, OSError):
            pass
    # Say what is playing, the way live speech says it. Every display that
    # shows the spoken words reads the two properties the coordinator writes as
    # it speaks (`sinks/speech.py`: TITLE_PROPERTY, TEXT_PROPERTY) — the phone's
    # card, the shade's, `share_control._speech`. A replay pushed the audio and
    # left them alone, so tapping a clip in the history changed what you heard
    # and nothing on the card: the *previous* reply's words, under the previous
    # reply's conversation, for the length of the clip. Best-effort and after
    # the push, for the reason the coordinator keeps them off the critical path
    # — a label is never worth delaying a sentence for. A row with no
    # conversation to name leaves the last name standing rather than clearing
    # the title, because an empty `force-media-title` puts the clip's *filename*
    # on every display that reads `media-title`.
    _speech_sink.set_media_title(str(ex.get("source_window") or ""),
                                 SPEECH_TARGET)
    _speech_sink.set_reply_text(replay_text, SPEECH_TARGET)
    clip_sentences: list[str] = ex.get("clip_sentences") or []
    have_durations = (
        len(clip_durations) == len(clip_uris) and len(clip_durations) > 0
    )
    # Always refresh now_playing so cmd_status's progress bar reflects the
    # clip we just started, not a stale prior entry. Without this, replaying
    # a single-clip history item (the common `<` case) left the previous
    # response's total_duration_s in place and the bar never acknowledged
    # the jump. When we have per-clip durations, persist them so cmd_status
    # can compute a spanning bar; otherwise omit total_duration_s and let
    # cmd_status fall back to mpv's raw time-pos/duration.
    np_extras: dict = {"text": replay_text}
    # Which record is audible. A clip picker draws its ▸ from this: without it
    # the only handle on "where you are" is the text, and two turns can say the
    # same thing. Absent on a live readout, which is correct — the turn being
    # spoken for the first time is not a record yet.
    if row.get("id") is not None:
        np_extras["history_id"] = row["id"]
    elif row.get("started_at") is not None:
        # A live turn restarted by `<` has no record yet — _live_history_row
        # hands back the turn that is speaking, id None — so name it by the
        # start it will be filed under when it ends. Both stamps answer the
        # same question for _speech_history: which row of the list is this,
        # so that the turn is not also listed as a new one on top of it.
        np_extras["replay_of_started_at"] = row["started_at"]
    source_pane = ex.get("source_pane")
    if source_pane:
        np_extras["source_pane"] = source_pane
    # Carry the clip's conversation (Claude id) + tmux session forward so the
    # next < / > press anchors to the same conversation — keeps the traversal
    # scope stable across the walk (_anchor_session reads source_session).
    src_claude = ex.get("source_session")
    if src_claude:
        np_extras["source_session"] = src_claude
    src_sess = ex.get("source_tmux_session")
    if src_sess:
        np_extras["source_tmux_session"] = src_sess
    src_window = ex.get("source_window")
    if src_window:
        np_extras["source_window"] = src_window
    if have_durations:
        np_extras["total_duration_s"] = sum(clip_durations)
        np_extras["clip_durations_s"] = clip_durations
    # Carry the sentence + paragraph map so `media skip` can step the replay
    # by sentence/paragraph; the tracker keeps current_sentence_idx fresh.
    # One clip per sentence (the local lanes): the playlist position IS the
    # sentence index.
    clip_offsets: list[float] = ex.get("clip_offsets_s") or []
    if clip_sentences and len(clip_sentences) == len(clip_uris):
        np_extras["clip_sentences"] = clip_sentences
        cpi = ex.get("clip_paragraph_idx")
        if cpi and len(cpi) == len(clip_uris):
            np_extras["clip_paragraph_idx"] = cpi
        np_extras["current_sentence_idx"] = 0
        clip_offsets = []            # positions, not offsets, drive this one
    elif (clip_sentences and len(clip_uris) == 1
            and len(clip_offsets) == len(clip_sentences)):
        # One clip holding every sentence (the phone lane, which renders the
        # whole reply in one piece). There is no playlist to step, so the
        # timeline recorded when it first played is what makes the replay
        # followable — without it a replayed reply is just audio.
        np_extras["clip_sentences"] = clip_sentences
        np_extras["clip_offsets_s"] = clip_offsets
        np_extras["current_sentence_idx"] = 0
        # The origin the follower reads (and that skip/pause re-stamp). Set
        # here rather than in the follower so both agree from the first tick.
        np_extras["play_started_at"] = time.time()
    else:
        clip_offsets = []
    if have_durations:
        # Spawn a detached follower so the replay behaves like live playback
        # even though _do_replay returns immediately: it mirrors the player's
        # live position into now_playing (else the popup's bar sits frozen at
        # 00:00 for the whole replay — and forever after, since nothing would
        # clear the row) and, for multi-clip turns with a pane, fires the
        # copy-mode highlight per sentence.
        # TTS_POPUP_PANE is the original pane that opened the popup; TMUX_PANE
        # inside display-popup is the popup's own ephemeral pane.
        pane = _caller_pane()
        # Supersede any tracker still polling from a prior replay. The
        # tracker only self-exits when the speech mpv goes idle, so
        # replaying again before the prior playlist finishes (rapid < / >
        # traversal, re-pressing r/Space) would otherwise leave the old
        # tracker running on the shared socket — it never sees "its"
        # playback end and keeps highlighting the new clip with the old
        # clip's sentences. killpg the previous one (start_new_session ⇒
        # the child's pid is its own pgid). Mirrors the per-pane pidfile
        # pattern _tmux_highlight_text uses for its clear-timer. Killed
        # BEFORE the set_now_playing below so a dying tracker can never
        # race a clear against the fresh row.
        import re as _re
        import signal as _signal
        _pane_safe = _re.sub(r"[^A-Za-z0-9_-]", "_", pane) if pane else "nopane"
        _trk_pidfile = f"/tmp/media-replay-track-{_pane_safe}.pid"
        try:
            with open(_trk_pidfile) as _f:
                _old_pgid = int(_f.read().strip())
            os.killpg(_old_pgid, _signal.SIGTERM)
        except (OSError, ValueError, ProcessLookupError, PermissionError):
            pass
        # Follow along from a known pane, whether the turn is a clip per
        # sentence (playlist position picks the sentence) or one clip holding
        # all of them (the offsets do). A single-clip turn never followed
        # before, which is every phone-lane reply — the lane most replies now
        # take. The position mirror runs regardless.
        _hl = bool(pane and clip_sentences
                   and (len(clip_sentences) == len(clip_uris) > 1
                        or clip_offsets))
        _trk = subprocess.Popen(
            [sys.executable, "-m", "agent_media_core.cli",
             "replay-track",
             "--sentences", json.dumps(clip_sentences) if _hl else "",
             "--offsets", json.dumps(clip_offsets) if _hl else "",
             "--pane", pane,
             "--durations", json.dumps(clip_durations)],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with open(_trk_pidfile, "w") as _f:
                _f.write(str(_trk.pid))
        except OSError:
            pass
        # Stamp the follower as the row's writer: the store's orphan guard
        # then self-heals the row if the tracker dies uncleanly, instead of
        # the bar freezing at its last mirrored position forever.
        np_extras["writer_pid"] = _trk.pid
    StateStore().set_now_playing(
        "speech", uri=clip_uris[0], started_at=time.time(),
        target=SPEECH_TARGET.name, extras=np_extras)
    return 0


def cmd_replay(a) -> int:
    if getattr(a, "id", None) is not None:
        # Clip browser: address the row by stable history id, not a
        # traversal index — indexes shift when a new clip lands while the
        # picker is open.
        for row in _speech_history(2000):
            if row.get("id") == a.id:
                return _replay_row(row)
        print(f"media replay: no clip with id {a.id}", file=sys.stderr)
        return 1
    # Scope < / > / r traversal to the current tmux session's clips.
    return _do_replay(a.index, session=_anchor_session())


def _prev_double_window() -> float:
    """Seconds after a `<` restart within which the next `<` means "previous".

    The position rule alone cannot answer a double-press on the phone lane: a
    press costs one to two seconds of round trips and the restart lands a
    second after that, so by the time the second press reads a position the
    turn is already ~3s in — past the grace window, so `<` restarts again, and
    again. That is the "pressing < twice just replays the same clip" report:
    the rule was being asked about a clock the pressing itself had advanced.

    So a restart leaves a breadcrumb — the same trick rapid `h`/`l` presses use
    (_SKIP_CHAIN_S) for the same reason — and a press that arrives while it is
    fresh steps back whatever the position says. 0 disables the latch.
    """
    try:
        return max(0.0, float(os.environ.get("MEDIA_POPUP_PREV_DOUBLE_S") or 5.0))
    except (TypeError, ValueError):
        return 5.0


def _speech_prev_channel() -> str:
    """The speech breadcrumb's key: per target, because the phone and the local
    player are different walks with different positions."""
    return f"speech-{SPEECH_TARGET.name}"


def _prev_restart_path(channel: str) -> Path:
    return state_dir() / f"prev-restart-{channel}"


def _note_prev_restart(channel: str, idx: int = 0) -> None:
    """Breadcrumb: `<` just restarted `channel`'s item (speech: cursor `idx`)."""
    try:
        p = _prev_restart_path(channel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(idx))
    except OSError:
        pass


def _prev_restart_is_fresh(channel: str, idx: int = 0) -> bool:
    """Did `<` restart this same item within the double-press window?

    Consumed either way, so holding `<` down walks back an item per press
    rather than stepping once and then restarting whatever it landed on.
    """
    p = _prev_restart_path(channel)
    try:
        fresh = time.time() - p.stat().st_mtime < _prev_double_window()
        marked = int(p.read_text().strip())
        p.unlink()
    except (OSError, ValueError):
        return False
    return fresh and marked == idx


def _prev_restart_threshold() -> float:
    """Seconds into an item past which `<` restarts it instead of stepping back."""
    try:
        return max(0.0, float(os.environ.get("MEDIA_POPUP_PREV_RESTART_S") or 3.0))
    except (TypeError, ValueError):
        return 3.0


def _prev_with_restart(elapsed, restart, step_back, channel: str = "item") -> int:
    """⏮ shared by the music/book `<` key: restart the current item if we're
    more than the grace window into it, else step back to the previous one.

    `elapsed` returns seconds into the current item (None/0 when idle → step
    back); `restart` seeks it to 0; `step_back` moves to the previous item.
    `channel` names the breadcrumb the double-press latch leaves, so a second
    press means "previous" even where the position has already run past the
    grace window (_prev_double_window) — a chapter on the phone answers a
    position query no faster than a spoken turn does.
    """
    try:
        pos = float(elapsed() or 0.0)
    except (TypeError, ValueError):
        pos = 0.0
    if _prev_restart_is_fresh(channel) or pos <= _prev_restart_threshold():
        step_back()
    else:
        restart()
        _note_prev_restart(channel)
    return 0


def _restart_current_playlist() -> int:
    """Seek a queued turn back to its own start, in place. 0 on success.

    A replayed turn is pushed as ONE mpv playlist, so entry 0 at position 0 is
    the start of the turn — two cheap property writes (~0.8s even on the phone)
    against re-pushing every clip over the bridge (~5s), and it needs no
    speech-history row at all. Returns non-zero when there is no playlist to
    seek: during a *live* readout each sentence is its own loadfile
    (playlist-count 1), and seeking that to 0 would restart the sentence, not
    the turn — so the caller falls back to a history replay.
    """
    sock = _sock()
    try:
        count = ipc.get_property(sock, "playlist-count", critical=True)
        if not isinstance(count, int) or count < 2:
            return 1
        ipc.set_property(sock, "playlist-pos", 0, critical=True)
        ipc.command(sock, "seek", 0, "absolute", critical=True)
        ipc.set_property(sock, "pause", False, critical=True)
    except (ipc.MpvIpcError, OSError):
        return 1
    return 0


def _replay_playing_turn(idx: int) -> int:
    """Re-push the turn that is *currently playing*, by history index. 0 on success.

    `<` mid-turn means "start this again", so it must never resolve to some
    other conversation's clip: when the playing turn's conversation has
    nothing re-pushable — a lane that rendered on the far side leaves no clip
    this host can serve — this fails and leaves the caller to seek what's
    audible back to zero. Only the step-back branch may widen its scope.
    """
    np = _now_speaking() or {}
    sess = (np.get("extras") or {}).get("source_session")
    if sess and not _speech_history(1, session=sess, include_live=True):
        return 1
    return _do_replay(idx, session=sess or _anchor_session())


def cmd_replay_prev(a) -> int:
    """Popup `<` for speech: "previous" with a restart-first grace window.

    Like a music player's ⏮: if we're already more than
    MEDIA_POPUP_PREV_RESTART_S (default 3s) into the current turn, `<` rewinds
    to that turn's start rather than jumping to the older one. Only when we're
    at/near the start (or nothing's playing) does it step back a turn — or when
    the press lands within MEDIA_POPUP_PREV_DOUBLE_S of our own last restart,
    which is what makes a double-press mean "previous" on a link where the
    presses cost more seconds than the grace window has (_prev_double_window).
    `--idx` is the popup's current history cursor (1 = latest); the resolved
    cursor is printed to stdout so the popup can update its own `hist_idx`.
    """
    idx = max(1, a.idx)
    idle, pos, _dur, *_ = _speech_display_state()
    session = _anchor_session()
    double = _prev_restart_is_fresh(_speech_prev_channel(), idx)
    if (not double and (not idle) and pos is not None
            and pos > _prev_restart_threshold()):
        # Partway through the current turn → restart it, keep the cursor put.
        # In place first (fast, and history-free); a history re-push next; and
        # if there is no row to re-push — the remote-render lane writes none —
        # seek what IS playing back to zero rather than doing nothing at all,
        # because "back to the start" is the one thing `<` was asked for.
        if _restart_current_playlist() != 0 and _replay_playing_turn(idx) != 0:
            try:
                ipc.command(_sock(), "seek", 0, "absolute", critical=True)
            except (ipc.MpvIpcError, OSError):
                pass
        _note_prev_restart(_speech_prev_channel(), idx)
        new_idx = idx
    else:
        # At the start (or idle, or a second press hard on the heels of a
        # restart) → step back a turn; stay put if there's none.
        new_idx = idx + 1
        if _do_replay(new_idx, session=session) != 0:
            new_idx = idx
    print(new_idx)
    return 0


def _clip_index_in_text(captured: str) -> Optional[int]:
    """1-based speech-history index of the most-recent clip whose search anchor
    appears in `captured` pane text, or None if none is present. Shared by
    `p`'s copy-mode path (capture down to the cursor) and its fullscreen path
    (capture the visible screen)."""
    from .intake.submit import _anchor_for

    if not captured:
        return None
    # Collapse whitespace before matching: the terminal word-wraps a response
    # at its content width, so an anchor longer than that width spans two visual
    # rows and a raw substring test misses it. The highlight path keeps anchors
    # to one row because it uses a row-bound tmux search; here we do a plain
    # substring test, so normalize both sides and wrapping stops mattering.
    norm_cap = " ".join(captured.split())
    for i, row in enumerate(_speech_history(200), start=1):
        anchor = _anchor_for(row.get("text") or "")
        if anchor and " ".join(anchor.split()) in norm_cap:
            return i
    return None


def _announce_replay(idx: int) -> int:
    """Flash a ♪ preview of clip `idx` (popup `p` feedback) then replay it."""
    rows = _speech_history(200)
    if 1 <= idx <= len(rows):
        preview = " ".join((rows[idx - 1].get("text") or "").split())
        if len(preview) > 60:
            preview = preview[:57] + "…"
        subprocess.run(["tmux", "display-message", f"♪ {preview}"],
                       capture_output=True)
    return _do_replay(idx)


def cmd_replay_at_cursor(a) -> int:
    """Replay the spoken clip at/just-above the copy-mode cursor (popup `p`).

    "The clip in the sequence before the cursor": capture the caller pane's
    text down to the cursor row, then play the most recent clip whose search
    anchor appears in it — clips below the cursor never appear in the capture,
    so they're excluded for free, and most-recent-first picks the nearest
    preceding utterance. Reuses `_anchor_for` so a clip that the auto-highlight
    can land on is exactly one this can match. If the pane isn't scrolled into
    copy-mode there's no cursor to point with, so it falls back to the most
    recent clip on the *visible screen* (this is what makes `p` work in Claude's
    fullscreen mode, which has no scrollback or copy-mode cursor), and failing
    that to "replay what this pane just said" (the latest clip from this pane).
    """
    pane = _caller_pane()
    if not pane:
        print("media: no caller pane", file=sys.stderr)
        return 1
    # Deliberately NOT session-scoped: the pane-scrollback capture below is
    # itself the scope — only a clip whose text is visible in *this* pane can
    # match. Searching all sessions lets `p` play whatever's above the cursor
    # regardless of which session last spoke or owns the clip (the whole point
    # of `p`: play from the cursor, not from the last-played clip).

    # Cursor state in the caller pane (queryable while the popup overlays it).
    in_mode, cur_y, scroll = "", "", ""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane,
             "#{pane_in_mode}\t#{copy_cursor_y}\t#{scroll_position}"],
            capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            parts = r.stdout.rstrip("\n").split("\t")
            in_mode = parts[0] if len(parts) > 0 else ""
            cur_y = parts[1] if len(parts) > 1 else ""
            scroll = parts[2] if len(parts) > 2 else ""
    except Exception:  # noqa: BLE001
        pass

    # Not scrolled into copy-mode → no cursor to point with. Try the *visible
    # screen* first: replay the most recent clip currently on screen. This is
    # what makes `p` useful in Claude's fullscreen (alt-screen) mode, which has
    # no scrollback or copy-mode cursor — capture-pane there returns just the
    # visible screen, so a match means "the clip I can see". Fall back to this
    # pane's latest clip when nothing on screen matches (e.g. the spoken text
    # scrolled off), preserving the old "play what this pane just said".
    if in_mode.strip() != "1" or not cur_y.strip().isdigit():
        try:
            cap = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", pane],
                capture_output=True, text=True, timeout=4)
            visible = cap.stdout if cap.returncode == 0 else ""
        except Exception:  # noqa: BLE001
            visible = ""
        idx = _clip_index_in_text(visible)
        if idx is not None:
            return _announce_replay(idx)
        idx = _history_index_for_pane(pane)
        if idx is None:
            print("media: this pane has no spoken clip", file=sys.stderr)
            return 1
        return _do_replay(idx)

    # capture-pane line numbers are relative to the live screen (0 = top of the
    # visible pane, negative into history); copy_cursor_y is relative to the
    # scrolled copy-mode view. Subtract scroll_position to convert.
    scroll_n = int(scroll) if scroll.strip().isdigit() else 0
    end_line = int(cur_y) - scroll_n
    try:
        cap = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane,
             "-S", "-32768", "-E", str(end_line)],
            capture_output=True, text=True, timeout=4)
        captured = cap.stdout if cap.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        captured = ""
    if not captured:
        print("media: could not read pane text", file=sys.stderr)
        return 1

    idx = _clip_index_in_text(captured)
    if idx is not None:
        return _announce_replay(idx)

    subprocess.run(
        ["tmux", "display-message", "⊘ no spoken clip above cursor"],
        capture_output=True)
    print("media: no spoken clip above cursor", file=sys.stderr)
    return 1


def _mirror_clock(state, owns, sentences: list, offsets: list,
                  idx: int, elapsed: float) -> None:
    """Write the replay's position from the clock, for a player we can't poll.

    Same fields the polling mirror writes, minus the ones only the player
    knows (pause, speed, mute) — inventing those would be worse than leaving
    the display to fall back.
    """
    try:
        np = state.get_now_playing("speech")
        if not np:
            return
        ex = np.get("extras") or {}
        if not owns(ex):
            return
        ex["live_pos_s"] = elapsed
        ex["live_pos_at"] = time.time()
        ex["writer_pid"] = os.getpid()
        if idx < len(sentences):
            ex["current_sentence"] = sentences[idx]
            ex["current_sentence_idx"] = idx
        state.set_now_playing(
            "speech", uri=np.get("uri") or "",
            started_at=np.get("started_at") or time.time(),
            target=np.get("target") or SPEECH_TARGET.name, extras=ex)
    except Exception:  # noqa: BLE001
        pass


def cmd_replay_track(a) -> int:
    """Internal: follow a replay the way the live intake path follows a reply.

    Spawned detached by _do_replay so it outlives the media-replay process.
    Two jobs per poll tick:
    - Mirror the player's live position/pause/speed/mute into the replay's
      now_playing row (written by _do_replay, stamped with our pid) so the
      popup's progress bar moves during a replay — without this it sat frozen
      at 00:00 for the whole replay and forever after.
    - Fire the copy-mode sentence highlight (multi-clip turns with a pane).
    On observed end-of-playback we clear the row, like the live path's
    ``finally`` does; if we die uncleanly instead, the row still carries our
    pid so the store's orphan guard self-heals it on the next read.
    """
    from .intake.submit import _HighlightScheduler, _playout_delay_s
    sentences: list[str] = json.loads(a.sentences) if a.sentences else []
    durations: list[float] = json.loads(a.durations) if a.durations else []
    # Sentence start times within a SINGLE clip — the phone lane renders the
    # whole reply as one file, so there is no playlist position to read the
    # sentence off and the timeline recorded at first play is what we follow.
    offsets: list[float] = json.loads(a.offsets) if getattr(a, "offsets", "") else []
    pane: str = a.pane
    highlight = bool(sentences and pane)
    # Through the scheduler, not straight to _tmux_highlight_text: it is what
    # notices a sentence that can't be found on screen and gives the status
    # rows over to it. A replay of a reply scrolled out of view is exactly the
    # case that needs them, and going around it meant the rows never appeared.
    highlighter = _HighlightScheduler(
        _playout_delay_s(SPEECH_TARGET.name) if not offsets else 0.0,
        highlight, pane)
    # Cumulative start offset of each CLIP on the turn-wide timeline. (Distinct
    # from the per-sentence `offsets` above, which exist only when one clip
    # holds the whole reply — the two never both apply.)
    clip_starts: list[float] = []
    _acc = 0.0
    for d in durations:
        clip_starts.append(_acc)
        _acc += d
    if highlight:
        # Ensure _tmux_highlight_text sees the right pane + a truthy TMUX.
        os.environ["TMUX_PANE"] = pane
        if not os.environ.get("TMUX"):
            os.environ["TMUX"] = "x"  # fallback: truthy, tmux resolves socket

    state = StateStore()

    def _owns(ex: dict) -> bool:
        # A newer writer (a live reply, or the next replay's tracker) may have
        # taken the row over; only touch a row that's still ours. Our own seed
        # row already carries our pid (_do_replay stamps it at spawn).
        return ex.get("writer_pid") in (None, os.getpid())

    def _mirror(snap: dict) -> int:
        """Mirror the player into the row; returns the sentence index in hand."""
        idx = 0
        try:
            np = state.get_now_playing("speech")
            if not np:
                return idx
            ex = np.get("extras") or {}
            if not _owns(ex):
                return idx
            pos = snap.get("playlist-pos")
            clip = int(pos) if pos is not None and pos >= 0 else 0
            base = clip_starts[clip] if clip < len(clip_starts) else 0.0
            elapsed = base + (snap.get("time-pos") or 0.0)
            ex["live_pos_s"] = elapsed
            ex["live_pos_at"] = time.time()
            ex["live_pause"] = bool(snap.get("pause"))
            ex["live_speed"] = snap.get("speed") or 1.0
            ex["live_mute"] = bool(snap.get("mute"))
            ex["writer_pid"] = os.getpid()
            # One clip per sentence → the playlist position is the sentence.
            # One clip holding all of them → the play position is, read against
            # the recorded offsets.
            idx = clip
            if offsets:
                idx = 0
                for i, off in enumerate(offsets):
                    if elapsed + 0.001 >= off:
                        idx = i
                    else:
                        break
            if idx < len(sentences):
                # Keeps `media current-sentence` (and popup skips) working.
                ex["current_sentence"] = sentences[idx]
                ex["current_sentence_idx"] = idx
            state.set_now_playing(
                "speech", uri=np.get("uri") or "",
                started_at=np.get("started_at") or time.time(),
                target=np.get("target") or SPEECH_TARGET.name,
                extras=ex)
        except Exception:  # noqa: BLE001
            pass
        return idx

    def _finish() -> int:
        highlighter.drain()    # fires the tail, then releases the status rows
        try:
            np = state.get_now_playing("speech")
            if np and _owns(np.get("extras") or {}):
                state.clear_now_playing("speech")
        except Exception:  # noqa: BLE001
            pass
        return 0

    if offsets:
        # One clip, a known timeline, and — on the lane that produces those —
        # a player 400ms away behind a circuit breaker. Polling it is what
        # killed this: the first slow read trips the breaker for 45s, the next
        # five reads are refused outright, and the tracker concludes playback
        # ended and clears the row. Two seconds into a reply that was audibly
        # still going.
        #
        # So don't ask. The offsets say where each sentence starts and the
        # clock says how far in we are, exactly as the live lane does. A pause
        # drifts this (same limitation as the live lane); everything else about
        # it is steadier than the reading it replaced.
        from .intake.submit import elapsed_from_row
        total = sum(durations) or (offsets[-1] + 3.0)
        started = time.time()
        last = -1
        while True:
            # The row owns the timeline: `media skip` re-stamps its origin and
            # a pause freezes it, so reading it back each tick is what keeps a
            # replay in step with the audio instead of with the wall clock.
            row = state.get_now_playing("speech")
            if not row or not _owns(row.get("extras") or {}):
                return _finish()
            elapsed = elapsed_from_row(row.get("extras") or {}, started)
            if elapsed > total + 0.5:
                return _finish()
            idx = 0
            for i, off in enumerate(offsets):
                if elapsed + 0.001 >= off:
                    idx = i
                else:
                    break
            if idx != last:
                last = idx
                _mirror_clock(state, _owns, sentences, offsets, idx, elapsed)
                if highlight and idx < len(sentences):
                    highlighter.show(sentences[idx], first=(idx == 0),
                                     force=False)
            time.sleep(0.1)

    # Wait for mpv to start playing the first clip — _do_replay's loadfile
    # returns immediately and there's a brief idle window before playback
    # kicks in. Without this, the tracker sees idle=True and exits before
    # the first sentence ever fires.
    for _ in range(40):  # up to ~2s
        try:
            if not bool(ipc.get_property(_sock(), "idle-active")):
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.05)

    last_pos = -1
    fail_streak = 0
    while True:
        time.sleep(0.15)
        try:
            # One batched snapshot per tick — over the phone bridge each hop
            # is slow, and this loop is per-tick anyway for the mirror.
            snap = ipc.get_properties(
                _sock(), ["idle-active", "playlist-pos", "time-pos",
                          "pause", "speed", "mute"])
        except Exception:  # noqa: BLE001
            fail_streak += 1
            if fail_streak >= 5:
                return _finish()
            continue
        fail_streak = 0
        if snap.get("idle-active"):
            # Require 2 consecutive idle readings to avoid race with playlist
            # advancement (mpv flickers idle briefly between clips).
            time.sleep(0.15)
            try:
                if bool(ipc.get_property(_sock(), "idle-active")):
                    return _finish()
            except Exception:  # noqa: BLE001
                pass
            continue
        idx = _mirror(snap)
        if highlight and idx != last_pos and 0 <= idx < len(sentences):
            highlighter.show(sentences[idx], first=(idx == 0), force=False)
        last_pos = idx
    return 0


def _hist_ts(r, today) -> str:
    dt = datetime.datetime.fromtimestamp(r.get("started_at") or 0)
    return dt.strftime("%H:%M") if dt.date() == today \
        else dt.strftime("%d %b %H:%M")


def _hist_txt(r) -> str:
    return (r.get("text") or "").replace("\n", " ").strip()


def _clip_rows(n: int = 40, session: Optional[str] = None) -> list[dict]:
    """Recent spoken turns, shaped once for everything that lists them.

    "Clips" are not a second thing beside history; they are history with the
    three questions a picker asks answered — which turn is this (`number`),
    what do I say to get it back (`id`), and am I hearing it now (`current`).
    Three surfaces list this table — `media history`, the popup's fzf browser,
    the phone's picker — and each of them used to decide separately what a row
    looks like, which is how the phone's list and the phone's card heading came
    to disagree about the same turn.

    Rows with no record are dropped. The live turn is one: history is written
    when a turn ends, so what you are hearing has no id yet and nothing can be
    told to play it again. The traversal keeps it (`include_live`) because
    stepping has to count it; a list you choose from does not.

    `text` is not truncated here. How much of a turn fits is the surface's
    business — a terminal, an fzf line and a phone dialog do not agree — but
    what a row *is* should not be.
    """
    playing = ((_now_speaking() or {}).get("extras") or {}).get("history_id")
    today = datetime.date.today()
    out: list[dict] = []
    for r in _speech_history(n, session=session):
        rid = r.get("id")
        if rid is None:
            continue
        ex = r.get("extras") or {}
        out.append({"number": len(out) + 1,
                    "id": rid,
                    "ts": _hist_ts(r, today),
                    # The instant as well as the rendering of it. A list that
                    # groups by day or says "18m ago" cannot get either back
                    # out of "14:02", and the phone's list does both.
                    "at": float(r.get("started_at") or 0.0),
                    "text": _hist_txt(r),
                    "session": ex.get("source_session") or "",
                    "window": str(ex.get("source_window") or "").strip(),
                    # Where it was said, as tmux sees it: which server session
                    # and which pane. The conversation is the finer grain — one
                    # tmux session holds several — so a list that shows both
                    # can nest them, and one that shows neither cannot.
                    "tmux": str(ex.get("source_tmux_session") or "").strip(),
                    "pane": str(ex.get("source_pane") or "").strip(),
                    "current": playing is not None and rid == playing})
    return out


def _print_history_grouped(rows) -> None:
    """tmux-choose-tree-ish rendering of the all-conversations view: one
    ▪window header per conversation (most recently heard first), its clips
    indented beneath with tree connectors, newest first. Header lines carry
    no TAB/history-id field, so the picker's replay step can tell them from
    clip rows and skip them."""
    groups: dict = {}   # source_session -> [rows], insertion = played order
    for r in rows:
        groups.setdefault(r["session"], []).append(r)
    for key, grp in groups.items():
        win = next((r["window"] for r in grp if r["window"]), "")
        # Clips predating source_window still have distinct conversations
        # (keyed by session id) — label those groups by a session-id stub so
        # they stay tellable-apart, not all "(untagged)".
        label = win[:48] if win else (f"…{key[-4:]}" if key else "(untagged)")
        n = len(grp)
        print(f"▪{label} — {n} clip{'s' if n != 1 else ''}")
        for i, r in enumerate(grp):
            branch = "└─" if i == n - 1 else "├─"
            print(f"  {branch} {r['ts']}  {r['text'][:110]}\t{r['id']}")


def cmd_errors(a) -> int:
    """Show recent errors from every component.

    Components log failures they recover from — a render that fell back to
    another engine, a transcript that couldn't be injected — and until now
    nothing read that table, so those failures were invisible unless you
    happened to be tailing the journal.
    """
    import time as _t
    from .state import StateStore
    since = (_t.time() - a.since * 60) if getattr(a, "since", 0) else None
    rows = StateStore().recent_errors(component=a.component or None,
                                      limit=a.n, since=since)
    if not rows:
        scope = f" for {a.component}" if a.component else ""
        window = f" in the last {a.since}m" if getattr(a, "since", 0) else ""
        print(f"no errors{scope}{window}")
        return 0
    for r in rows:
        when = _t.strftime("%m-%d %H:%M:%S", _t.localtime(r["at"]))
        line = f"{when}  {r['component']:<16} {r['message']}"
        extras = r.get("extras")
        if extras and getattr(a, "verbose", False):
            line += f"  {extras}"
        print(line)
    return 0


def cmd_history(a) -> int:
    """List recent spoken clips — every picker's data source, in three skins.

    All three are `_clip_rows` with a different amount of room: plain for a
    terminal, ``--lines`` for fzf, ``--json`` for the phone. What a row *is* is
    decided once, over there.

    ``--session`` scopes to the popup's anchor conversation (falling back to
    all clips when none resolves — same degradation as < / > traversal).
    ``--lines`` emits ``display<TAB>history-id`` rows for an external picker
    (media-popup-clips); unscoped lines carry a ▪window label so interleaved
    conversations stay tellable-apart. ``--group`` (with --lines, unscoped)
    instead groups clips under per-conversation headers, choose-tree style.

    ``--json`` is the wire format between a render host and its origin: the
    words are produced where the conversation happens, and every other host has
    to ask. It carries the picker's field names (`ref`, `title`) because the
    app is what reads it.
    """
    session = _anchor_session() if getattr(a, "session", False) else None
    scoped = session is not None
    if getattr(a, "json", False):
        from .entrypoints.share_control import picker_rows

        print(json.dumps(picker_rows(_clip_rows(a.n, session=session))))
        return 0
    rows = _clip_rows(a.n, session=session)
    if getattr(a, "lines", False) and getattr(a, "group", False) and not scoped:
        _print_history_grouped(rows)
        return 0
    for r in rows:
        if not getattr(a, "lines", False):
            print(f"{r['ts']}  {r['text'][:80]}")
            continue
        label = f" ▪{r['window'][:18]}" if r["window"] and not scoped else ""
        print(f"{r['ts']}{label}  {r['text'][:120]}\t{r['id']}")
    return 0


def cmd_say(a) -> int:
    from .intake._text import strip_markdown
    from .intake._visual import (extract_visual_markers, spawn_visual,
                                 visual_enabled)
    from .intake.submit import submit_event
    text = a.text if a.text else sys.stdin.read()
    if not text.strip():
        return 0
    # `say` callers hand over prose that can carry markdown and [[visual:]]
    # markers (the same conventions hook replies use) — neither is ever worth
    # hearing. A marker still earns its picture: the same fire-and-forget
    # accompaniment the Stop hook spawns.
    raw, hint, _pre, _post = extract_visual_markers(text)
    text = strip_markdown(raw)
    if not text.strip():
        return 0
    if visual_enabled() and hint:
        try:
            spawn_visual(raw, text, hint=hint)
        except Exception:  # noqa: BLE001 — accompaniment, never speech's problem
            pass
    urgent = getattr(a, "urgent", False) or getattr(a, "supersede", False)
    metadata = {}
    if getattr(a, "alert", False):
        # Nobody asked for this one — a timer, a watcher, mail arriving. It is
        # the only class of speech a silenced phone withholds, and the mark is
        # explicit because the producer is the only party that knows without
        # flattering itself (see sinks.speech.set_priority).
        metadata["alert"] = True
    if getattr(a, "supersede", False):
        # supersede implies urgent: barge in AND drop the same-session messages
        # this one interrupts/precedes, rather than letting them resume.
        metadata["supersede"] = True
    submit_event(Event(
        text=text, source=Source.CLI,
        priority=Priority.URGENT if urgent else Priority.NORMAL,
        metadata=metadata))
    return 0


# --- music subcommands -----------------------------------------------------

def _music_status_line(m: "SinkMusic", width: int, hide_idle: bool,
                       bar: bool = True) -> str:
    """One-line music progress bar from MPD status (mirrors cmd_status)."""
    st = m.status_dict()
    state = st.get("state", "stop")
    if state in ("stop", "") or not state:
        return render_status(idle=True, pos=None, dur=None, paused=None,
                             muted=None, width=width, hide_idle=hide_idle)

    def _f(key):
        try:
            return float(st[key]) if st.get(key) else None
        except (ValueError, KeyError):
            return None

    return render_status(idle=False, pos=_f("elapsed"), dur=_f("duration"),
                         paused=(state == "pause"), muted=False,
                         width=width, hide_idle=hide_idle, bar=bar)


def _music_now_label(m: "SinkMusic") -> str:
    """Current track as 'Artist — Title' (the music channel's marquee)."""
    song = m.current_song()
    if (song.get("file") or "").startswith("mpv:"):
        # mpv-routed track: MPD tags are just the bare filename; the renderer
        # has the embedded media-title (and chapter, for DJ sets/albums).
        from .sinks.music import mpv_now_props
        props = mpv_now_props()
        if props:
            label = _mpv_music_label(props)
            if label:
                return label
    title = song.get("Title") or song.get("Name") or ""
    if not title:
        title = (song.get("file") or "").rsplit("/", 1)[-1]
    artist = song.get("Artist") or ""
    return f"{artist} — {title}" if artist and title else title


def _phone_music_props() -> Optional[dict]:
    """One batched snapshot of the phone's music mpv, or None when the phone
    backend isn't configured, isn't reachable, or has nothing loaded.

    `music play --where auto` routes playout to the phone when it's the only
    listener, so the status/label/transport paths below must follow it there —
    reading Mopidy would show an idle rooms queue while the track is audibly
    playing. One `get_properties` batch = one bridge round-trip (a per-property
    read would cost several hundred ms each from this host to the phone).
    """
    from .sinks import music_local
    from .sinks import _mpv_ipc as ipc
    if not music_local.configured():
        return None
    try:
        props = ipc.display_properties(
            music_local.endpoint(),
            ["idle-active", "pause", "time-pos", "duration", "speed",
             "media-title", "chapter-metadata/by-key/title", "volume",
             "path"],
            timeout=1.5)
    except (ipc.MpvIpcError, OSError):
        return None
    if props.get("idle-active") is not False:
        return None       # idle (or unknown) ⇒ the phone isn't the live backend
    return props


def _mpv_music_label(props: dict) -> str:
    """Marquee label from an mpv props snapshot (phone player or the rooms
    Mopidy-Mpv renderer): the embedded title, plus the current chapter when
    the file has chapters. Both caches key downloads by video id, so an
    unembedded file's media-title is a bare `<id>.<ext>` filename — strip the
    extension rather than showing it."""
    chap = str(props.get("chapter-metadata/by-key/title") or "").strip()
    title = str(props.get("media-title") or "").strip()
    if "." in title and " " not in title:
        title = title.rsplit(".", 1)[0]
    if chap and title:
        # Chapter first: on a ~34-col marquee the "what's playing right now"
        # part must be visible before the scroll, not after it.
        return f"{chap} · {title}"
    return chap or title


def _speed_str(props: dict) -> str:
    """Compact speed readout from an mpv props snapshot: '1.25', '' at 1.0×."""
    try:
        v = float(props.get("speed") or 1.0)
    except (TypeError, ValueError):
        return ""
    return "" if abs(v - 1.0) < 1e-3 else f"{v:g}"


def _music_now_status(m: "SinkMusic", width: int, hide_idle: bool,
                      bar: bool = True) -> tuple:
    """(status line, marquee label, speed) for whichever backend is actually
    playing: the phone's local mpv when it has a track loaded, else Mopidy.
    speed is '' at 1.0× or when the track has no speed control (MPD)."""
    props = _phone_music_props()
    if props is not None:
        line = render_status(idle=False, pos=props.get("time-pos"),
                             dur=props.get("duration"),
                             paused=bool(props.get("pause")), muted=False,
                             width=width, hide_idle=hide_idle, bar=bar)
        return line, _mpv_music_label(props), _speed_str(props)
    try:
        song = m.current_song()
    except OSError:
        song = {}
    if (song.get("file") or "").startswith("mpv:"):
        # mpv-routed rooms track: MPD reports no duration and filename-only
        # tags; the renderer knows the real position, length, and title.
        from .sinks.music import mpv_now_props
        mprops = mpv_now_props()
        if mprops:
            line = render_status(idle=False, pos=mprops.get("time-pos"),
                                 dur=mprops.get("duration"),
                                 paused=bool(mprops.get("pause")), muted=False,
                                 width=width, hide_idle=hide_idle, bar=bar)
            return line, _mpv_music_label(mprops), _speed_str(mprops)
    return (_music_status_line(m, width, hide_idle, bar),
            _music_now_label(m), "")


def _music_hold_active() -> Optional[bool]:
    """Whether call-guard's external hold is engaged, or None when unknown.

    A read of the same flag file `media-call-guard --hold` sets, so a control
    surface can *render* a held state without owning the mechanism. Never
    writes: holding and releasing stay call-guard's job.
    """
    try:
        from .call_guard import Config, flag_present
        return bool(flag_present(Config()))
    except Exception:  # noqa: BLE001 — call-guard is optional on a host
        return None


def _ms(v) -> Optional[int]:
    return int(v * 1000) if isinstance(v, (int, float)) else None


def _music_status_json(m: "SinkMusic") -> dict:
    """Structured music-channel snapshot for a control surface.

    Same live-backend rule as `_music_now_status` — the phone's mpv when it
    has a track loaded, else Mopidy — so a front-end reading this can never
    disagree with what the popup shows. One round-trip either way.

    Every field is nullable by design: a surface renders what it got and polls
    again rather than blocking. This is a *read*; the pipeline stays the only
    writer, which is what lets a front-end be removed without leaving state
    behind.
    """
    out: dict = {"backend": None, "uri": None, "title": None, "chapter": None,
                 "pos_ms": None, "dur_ms": None, "paused": None, "speed": None,
                 "volume": None, "held": _music_hold_active()}

    props = _phone_music_props()
    if props is not None:
        chap = str(props.get("chapter-metadata/by-key/title") or "").strip()
        vol = props.get("volume")
        out.update(
            backend="phone",
            title=_mpv_music_label(props) or None,
            chapter=chap or None,
            pos_ms=_ms(props.get("time-pos")),
            dur_ms=_ms(props.get("duration")),
            paused=bool(props.get("pause")),
            speed=props.get("speed"),
            volume=int(vol) if isinstance(vol, (int, float)) else None,
            uri=str(props.get("path") or "") or None,
        )
        return out

    out["backend"] = "mopidy"
    try:
        st = m.status_dict()
    except OSError:
        return out
    state = st.get("state", "stop")
    if state in ("", "stop"):
        return out

    def _f(key):
        try:
            return float(st[key]) if st.get(key) else None
        except (ValueError, KeyError):
            return None

    out.update(paused=(state == "pause"),
               pos_ms=_ms(_f("elapsed")), dur_ms=_ms(_f("duration")))
    try:
        vol = int(st.get("volume", ""))
        out["volume"] = vol if vol >= 0 else None
    except (ValueError, TypeError):
        pass
    try:
        out["title"] = _music_now_label(m) or None
        out["uri"] = m.now_playing_uri()
    except OSError:
        pass
    return out


def _music_live_backend(m: "SinkMusic"):
    """The backend a music-channel control should hit: the phone's local mpv
    when it has a track loaded (playing or paused), else Mopidy. Mirrors
    SinkMusicRouter._observe_backend, which already makes the speech
    coordinator's duck follow the live backend — without this the popup's
    transport keys would drive an idle Mopidy while the phone plays."""
    from .sinks.music_local import SinkMusicLocal, configured
    if configured():
        loc = SinkMusicLocal()
        try:
            if loc.loaded():
                return loc
        except Exception:  # noqa: BLE001 — bridge down ⇒ phone not live
            pass
    return m


def _music_mpv_chapters() -> Optional[tuple[str, list, Optional[int]]]:
    """(endpoint, chapter-list, current index) from the live mpv renderer —
    the phone player first, then the rooms Mopidy-Mpv — or None when neither
    has a track loaded. One batched round-trip per candidate endpoint (the
    phone bridge costs ~600ms per connect). MPD/GStreamer streams never
    appear here: they have no chapter metadata to browse."""
    from .sinks import music_local
    from .sinks import _mpv_ipc as ipc
    from .sinks.music import _mpv_socket
    endpoints = []
    if music_local.configured():
        endpoints.append(music_local.endpoint())
    sock = _mpv_socket()
    if sock and os.path.exists(sock):
        endpoints.append(sock)
    for ep in endpoints:
        # Generous retries: a dozing phone eats the first few round-trips
        # while its radio wakes, and a one-shot failure would misread a
        # playing phone as "no chapters". The picker is user-initiated, so
        # the extra seconds beat the wrong answer.
        attempts = 5 if str(ep).startswith("tcp://") else 1
        try:
            props = ipc.get_properties(
                ep, ["idle-active", "chapter-list", "chapter"],
                timeout=3.0, attempts=attempts)
        except (ipc.MpvIpcError, OSError):
            continue
        if props.get("idle-active") is not False:
            continue
        cur = props.get("chapter")
        return ep, list(props.get("chapter-list") or []), (
            int(cur) if isinstance(cur, int) else None)
    return None


def _cmd_music_chapters(a) -> int:
    """`music chapters [--lines]` / `music chapter N` — browse and jump the
    embedded chapters of the live mpv track (fetched DJ sets/albums). Numbers
    are 1-based to match the printed list; `--lines` emits
    `display<TAB>number` rows for an external picker (media-popup-chapters)."""
    from .sinks import _mpv_ipc as ipc
    got = _music_mpv_chapters()
    if got is None:
        print("media music chapters: no mpv track live "
              "(MPD/GStreamer streams have no chapters)", file=sys.stderr)
        return 1
    ep, chaps, cur = got
    if not chaps:
        print("media music chapters: this track has no chapters",
              file=sys.stderr)
        return 1
    if a.action == "chapter":
        try:
            n = int(a.uri or "")
        except ValueError:
            print(f"media music chapter: bad chapter {a.uri!r} "
                  f"(want 1–{len(chaps)})", file=sys.stderr)
            return 2
        if not 1 <= n <= len(chaps):
            print(f"media music chapter: {n} out of range "
                  f"(1–{len(chaps)})", file=sys.stderr)
            return 2
        try:
            ipc.set_property(ep, "chapter", n - 1, critical=True)
        except (ipc.MpvIpcError, OSError) as e:
            print(f"media music chapter: {e}", file=sys.stderr)
            return 1
        title = str(chaps[n - 1].get("title") or "").strip() or f"chapter {n}"
        print(f"⏭ {n:02d} · {title}")
        return 0
    for i, ch in enumerate(chaps):
        title = str(ch.get("title") or "").strip() or f"chapter {i + 1}"
        mark = "▸" if cur == i else " "
        row = (f"{mark} {i + 1:2d}  "
               f"{_hms(float(ch.get('time') or 0)):>7}  {title}")
        print(f"{row}\t{i + 1}" if a.lines else row)
    return 0


def _book_mpv_chapters(target: str = "") -> Optional[tuple[object, list, Optional[int]]]:
    """(endpoint, chapter-list, current index) for the book channel's mpv, or
    None when nothing is loaded.

    The book is the channel most likely to have chapters — an m4b has them by
    definition, and mpv's ytdl hook lifts YouTube's chapter marks too — and it
    was the one channel that could not show them, on the strength of a comment
    saying a book has none. That was true when the book channel was streams.
    """
    from .sinks import _mpv_ipc as ipc
    from .sinks.book import _socket_for
    from .mcp_server import _book_target

    ep = _socket_for(_book_target(target))
    # Same generosity as the music side: the phone bridge is a TCP hop through
    # a dozing radio, and one timed-out round trip would read as "no chapters".
    attempts = 5 if str(ep).startswith("tcp://") else 1
    try:
        props = ipc.get_properties(
            ep, ["idle-active", "chapter-list", "chapter"],
            timeout=3.0, attempts=attempts)
    except (ipc.MpvIpcError, OSError):
        return None
    if props.get("idle-active") is not False:
        return None
    cur = props.get("chapter")
    return ep, list(props.get("chapter-list") or []), (
        int(cur) if isinstance(cur, int) else None)


def _cmd_book_chapters(a) -> int:
    """`book chapters [--lines]` / `book chapter N` — the music command's twin,
    against the book's mpv. Numbers are 1-based to match the printed list."""
    from .sinks import _mpv_ipc as ipc
    got = _book_mpv_chapters(getattr(a, "target", "") or "")
    if got is None:
        print("media book chapters: nothing loaded on the book channel",
              file=sys.stderr)
        return 1
    ep, chaps, cur = got
    if not chaps:
        print("media book chapters: this book has no chapters",
              file=sys.stderr)
        return 1
    if a.book_cmd == "chapter":
        try:
            n = int(a.number)
        except (TypeError, ValueError):
            print(f"media book chapter: bad chapter {a.number!r} "
                  f"(want 1–{len(chaps)})", file=sys.stderr)
            return 2
        if not 1 <= n <= len(chaps):
            print(f"media book chapter: {n} out of range (1–{len(chaps)})",
                  file=sys.stderr)
            return 2
        try:
            ipc.set_property(ep, "chapter", n - 1, critical=True)
        except (ipc.MpvIpcError, OSError) as e:
            print(f"media book chapter: {e}", file=sys.stderr)
            return 1
        title = str(chaps[n - 1].get("title") or "").strip() or f"chapter {n}"
        print(f"⏭ {n:02d} · {title}")
        return 0
    for i, ch in enumerate(chaps):
        title = str(ch.get("title") or "").strip() or f"chapter {i + 1}"
        mark = "▸" if cur == i else " "
        row = (f"{mark} {i + 1:2d}  "
               f"{_hms(float(ch.get('time') or 0)):>7}  {title}")
        print(f"{row}\t{i + 1}" if getattr(a, "lines", False) else row)
    return 0


def _resolve_music_where(where: str) -> str:
    """Resolve a `--where` value to a concrete backend: 'phone' or 'rooms'.

    ``default`` follows MEDIA_MUSIC_DEFAULT_TARGET, then the speech default
    device. Explicit ``auto`` keeps the old listener-aware routing.
    """
    if where in ("local", "rooms"):
        return "rooms"
    if where == "phone":
        return "phone"
    from .sinks.music_local import configured as _local_configured
    if where in ("", "default"):
        default_target = (os.environ.get("MEDIA_MUSIC_DEFAULT_TARGET")
                          or os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET")
                          or "")
        if default_target in ("phone", "local-phone", "phone-local") and _local_configured():
            return "phone"
        if default_target in ("rooms", "local"):
            return "rooms"
        where = "auto"
    # auto
    if not _local_configured():
        return "rooms"
    from . import snapcast
    default = os.environ.get("MEDIA_MUSIC_AUTO_DEFAULT", "phone")
    try:
        others = snapcast.connected_other_clients()
    except snapcast.SnapcastError:
        return default if default in ("phone", "rooms") else "phone"
    return "rooms" if others else "phone"



def _bookmark_media_id(uri: str) -> str:
    """Stable bookmark key: YouTube id when visible, else URI/path."""
    from .sinks import music_fetch
    if vid := music_fetch.watch_id(uri or ""):
        return vid
    base = (uri or "").rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0]
    if music_fetch.watch_id(stem):
        return stem
    return uri or base


def _save_bookmark(channel: str, media_id: str, uri: str, pos_ms: int,
                   title: str = "", duration_ms: Optional[int] = None,
                   note: str = "", transcript: Optional[str] = None,
                   extras: Optional[dict] = None,
                   range_end: bool = False, slot: str = "") -> int:
    """Save a point bookmark, or let the next bookmark on that item close it.

    Pressing `b` once always creates a new point bookmark and remembers it as
    the pending range start. Pressing `bb` sends `--range-end`, which adds
    `end_pos_ms` to that pending bookmark. A later single `b` starts a fresh
    bookmark instead of closing the previous one.
    """
    st = StateStore()
    pos_ms = max(0, int(pos_ms))
    start = st.get_bookmark_pending(channel, slot=slot)
    same_item = bool(start and start.get("item_id") == media_id)
    if range_end:
        if not same_item:
            print(f"bookmark range: no matching {channel} start", file=sys.stderr)
            return 1
        a, b = int(start.get("pos_ms") or 0), pos_ms
        st.set_bookmark(
            channel=channel, media_id=start.get("media_id") or media_id, uri=uri,
            pos_ms=min(a, b), end_pos_ms=max(a, b), title=title or start.get("title"),
            duration_ms=duration_ms or start.get("duration_ms"),
            note=note or start.get("note"), transcript=transcript or start.get("transcript"),
            extras={**(start.get("extras") or {}), **(extras or {}),
                    "item_id": media_id, "range": True, "slot": slot or "default"},
        )
        st.set_bookmark_pending(channel, None, slot=slot)
        print(f"bookmarked range {fmt_time(min(a, b)/1000.0)}-{fmt_time(max(a, b)/1000.0)} {title}".rstrip())
        return 0

    bookmark_id = f"{media_id}@{pos_ms}"
    data = {
        "channel": channel, "item_id": media_id, "media_id": bookmark_id, "uri": uri,
        "pos_ms": pos_ms, "title": title or None,
        "duration_ms": duration_ms, "note": note or None,
        "transcript": transcript,
        "extras": {**(extras or {}), "slot": slot or "default"},
    }
    st.set_bookmark(
        channel=channel, media_id=bookmark_id, uri=uri, pos_ms=pos_ms,
        title=title or None, duration_ms=duration_ms, note=note or None,
        transcript=transcript,
        extras={**(extras or {}), "item_id": media_id, "slot": slot or "default"},
    )
    st.set_bookmark_pending(channel, data, slot=slot)
    print(f"bookmarked {fmt_time(pos_ms / 1000.0)} {title}".rstrip())
    return 0


def _speech_bookmark(note: str = "", range_end: bool = False,
                     slot: str = "") -> int:
    np = _now_speaking()
    if not np:
        print("media bookmark: no speech loaded", file=sys.stderr)
        return 1
    ex = np.get("extras") or {}
    text = (ex.get("text") or "").strip()
    sent = (ex.get("current_sentence") or "").strip()
    uri = np.get("uri") or f"speech:{np.get('started_at')}"
    title = sent or (" ".join(text.split())[:80] if text else "speech")
    pos = int((np.get("pause_pos_ms") or 0) or 0)
    return _save_bookmark(
        "speech", str(np.get("started_at") or uri), uri, pos,
        title=title, note=note, transcript=text or sent or None,
        extras={"pane": ex.get("pane"), "session": ex.get("session")},
        range_end=range_end, slot=slot)


def _book_bookmark(note: str = "", target: str = "", range_end: bool = False,
                   slot: str = "") -> int:
    srv = _srv()
    np = srv.book_now_playing(target=target or "")
    if np.get("idle"):
        print("media bookmark: no book loaded", file=sys.stderr)
        return 1
    uri = np.get("uri") or ""
    pos = int(np.get("position_ms") or 0)
    dur = int(np.get("duration_ms") or 0) or None
    title = uri.rsplit("/", 1)[-1] or uri
    return _save_bookmark(
        "book", _bookmark_media_id(uri), uri, pos, title=title,
        duration_ms=dur, note=note, extras={"speed": np.get("speed")},
        range_end=range_end, slot=slot)


def _music_bookmark(m: "SinkMusic", note: str = "", range_end: bool = False,
                    slot: str = "") -> int:
    b = _music_live_backend(m)
    uri = b.now_playing_uri() or ""
    if not uri and b is m:
        uri = (m.current_song() or {}).get("file") or ""
    if not uri:
        print("media bookmark: no music loaded", file=sys.stderr)
        return 1
    pos = b.position()
    if pos is None and b is m:
        try:
            pos = int(float((m.status_dict() or {}).get("elapsed") or 0) * 1000)
        except (TypeError, ValueError):
            pos = 0
    props = _phone_music_props()
    if props is None and b is m:
        from .sinks.music import mpv_now_props
        props = mpv_now_props() or {}
    dur = None
    try:
        if props and props.get("duration") is not None:
            dur = int(float(props.get("duration")) * 1000)
    except (TypeError, ValueError):
        dur = None
    _, label, _ = _music_now_status(m, width=0, hide_idle=True, bar=False)
    media_id = _bookmark_media_id(uri)
    return _save_bookmark(
        "music", media_id, uri, pos or 0, title=label or "",
        duration_ms=dur, note=note,
        extras={"backend": "phone" if b is not m else "rooms"},
        range_end=range_end, slot=slot)


def _cmd_bookmarks(limit_s: str = "", channel: Optional[str] = None,
                   json_out: bool = False) -> int:
    try:
        limit = int(limit_s or 20)
    except ValueError:
        limit = 20
    rows = StateStore().list_bookmarks(limit, channel=channel)
    if json_out:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    for bm in rows:
        title = bm.get("title") or bm.get("uri") or bm.get("media_id")
        note = f" — {bm.get('note')}" if bm.get("note") else ""
        print(f"{bm.get('channel') or '?'}  {fmt_time((bm.get('pos_ms') or 0) / 1000.0)}  {title}{note}")
    return 0


def _resume_bookmark(bm: dict) -> int:
    """Play the bookmarked item on its channel, seeking to the saved position.

    music: play (phone/rooms per auto) then seek to pos_ms.
    book:  book_play with an explicit start offset (its own fetch/resume path).
    speech: live speech can't be re-entered mid-clip, so hand back the URI.
    """
    ch = bm.get("channel")
    uri = bm.get("uri") or ""
    pos_ms = int(bm.get("pos_ms") or 0)
    if not uri:
        print("media bookmarks: bookmark has no uri", file=sys.stderr)
        return 1
    if ch == "book":
        srv = _srv()
        r = srv.book_play(uri, resume=False, start_ms=pos_ms, target="")
        if r.get("fetching"):
            print(f"⬇ {r.get('reason', 'fetching')}: {uri}")
            return 0
        if not r.get("ok", True):
            print(r.get("reason", "book play failed"), file=sys.stderr)
            return 1
        print(f"▶ {uri} (from {fmt_time(pos_ms / 1000.0)})")
        return 0
    if ch == "music":
        m = SinkMusic()
        where = _resolve_music_where("auto")
        try:
            if where == "phone":
                from .sinks.music_local import SinkMusicLocal, configured
                if not configured():
                    print("media bookmarks: phone backend not configured",
                          file=sys.stderr)
                    return 2
                SinkMusicLocal().play(uri, replace=True)
            else:
                m.play(uri, replace=True)
        except Exception as e:  # noqa: BLE001
            print(f"media bookmarks: resume failed: {e}", file=sys.stderr)
            return 1
        StateStore().set_music_intent(uri, None)
        if pos_ms > 0:
            _music_live_backend(m).seek_cur(position_ms=pos_ms)
        print(f"▶ {uri} (from {fmt_time(pos_ms / 1000.0)})")
        return 0
    # speech (and anything else): no live resume — emit the reference.
    print(uri)
    return 0


def _cmd_bookmark_pick(channel: Optional[str] = None,
                       resume: bool = True) -> int:
    rows = StateStore().list_bookmarks(500, channel=channel)
    if not rows:
        print("media bookmarks pick: no bookmarks", file=sys.stderr)
        return 1
    if not shutil.which("fzf"):
        print("media bookmarks pick: fzf not installed", file=sys.stderr)
        return 1
    lines = []
    by_key = {}
    for i, bm in enumerate(rows):
        key = str(i)
        title = bm.get("title") or bm.get("uri") or bm.get("media_id")
        searchable = " ".join(str(x or "") for x in (
            bm.get("channel"), title, bm.get("note"), bm.get("transcript"), bm.get("uri")))
        line = f"{key}	{bm.get('channel')}	{fmt_time((bm.get('pos_ms') or 0)/1000.0)}	{searchable}"
        by_key[key] = bm
        lines.append(line)
    proc = subprocess.run(
        ["fzf", "--with-nth", "2..", "--delimiter", "\t", "--prompt", "bookmark> "],
        input="\n".join(lines), text=True, capture_output=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        return proc.returncode
    bm = by_key.get(proc.stdout.split("\t", 1)[0])
    if not bm:
        return 1
    if resume:
        return _resume_bookmark(bm)
    print(bm.get("uri") or "")
    return 0


def cmd_bookmark(a) -> int:
    ch = getattr(a, "channel", "music")
    note = getattr(a, "note", "") or ""
    range_end = bool(getattr(a, "range_end", False))
    slot = getattr(a, "slot", "") or ""
    if ch == "music":
        return _music_bookmark(SinkMusicRouter(SinkMusic()), note, range_end=range_end, slot=slot)
    if ch == "book":
        return _book_bookmark(note, range_end=range_end, slot=slot)
    if ch == "speech":
        return _speech_bookmark(note, range_end=range_end, slot=slot)
    print("media bookmark: unsupported channel", file=sys.stderr)
    return 2


def cmd_bookmarks(a) -> int:
    if getattr(a, "pick", False):
        return _cmd_bookmark_pick(channel=getattr(a, "channel", None),
                                  resume=not getattr(a, "print_uri", False))
    return _cmd_bookmarks(getattr(a, "limit", "20") or "20",
                          channel=getattr(a, "channel", None),
                          json_out=bool(getattr(a, "json", False)))


def cmd_music(a) -> int:
    from .route import coerce_content_type, detect_content_type

    m = SinkMusicRouter(SinkMusic())
    if a.action == "status" and getattr(a, "json", False):
        # Structured read for a control surface. Kept ahead of the formatted
        # branch so the human status line is byte-for-byte unchanged when the
        # flag is absent.
        try:
            print(json.dumps(_music_status_json(m)))
        except Exception as e:  # noqa: BLE001 — a poller must never see a traceback
            print(json.dumps({"backend": None, "error": str(e)}))
        return 0
    if a.action in ("status", "now", "now-status"):
        # All three follow the LIVE backend (phone mpv when it has a track
        # loaded, else Mopidy). `now-status` is the popup's fused form: status
        # line + marquee label + speed readout in one spawn and one phone
        # round-trip (the speed line is '' at 1.0× / no speed control).
        line, label, spd = ("○" if a.show_idle else ""), "", ""
        try:
            line, label, spd = _music_now_status(m, a.width,
                                                 hide_idle=not a.show_idle,
                                                 bar=not a.no_bar)
        except Exception:  # noqa: BLE001 — popup must never see a traceback
            pass
        if a.action == "status":
            print(line)
        elif a.action == "now":
            print(label)
        else:
            print(line)
            print(label)
            print(spd)
        return 0
    if a.action in ("chapters", "chapter"):
        return _cmd_music_chapters(a)
    if a.action == "bookmark":
        return _music_bookmark(m, a.uri or "", range_end=bool(getattr(a, "range_end", False)), slot=getattr(a, "slot", "") or "")
    if a.action == "bookmarks":
        return _cmd_bookmarks(a.uri or "", channel="music")
    if a.action == "play":
        if not a.uri:
            print("media music play: a URI is required", file=sys.stderr)
            return 2
        where = _resolve_music_where(getattr(a, "where", "auto"))
        ct = coerce_content_type(getattr(a, "as_type", None)) or detect_content_type(a.uri)
        if where == "phone":
            from .sinks.music_local import SinkMusicLocal, configured
            if not configured():
                print("media music play --where phone: MEDIA_MUSIC_LOCAL_ENDPOINT "
                      "is unset (phone backend not configured)", file=sys.stderr)
                return 2
            try:
                SinkMusicLocal().play(a.uri, replace=not a.add)
            except Exception as e:  # noqa: BLE001
                print(f"media music play (phone) failed: {e}", file=sys.stderr)
                return 1
            StateStore().set_music_intent(a.uri, ct.value,
                                          getattr(a, "title", "") or None)
            print(f"playing on phone ({ct.value}): {a.uri}")
            return 0
        m.play(a.uri, replace=not a.add)
        StateStore().set_music_intent(a.uri, ct.value,
                                      getattr(a, "title", "") or None)
        print(f"playing ({ct.value}): {a.uri}")
        return 0
    # Everything below is transport — route to the live backend so the keys
    # control what's actually audible (phone mpv or Mopidy).
    b = _music_live_backend(m)
    if a.action == "stop":
        b.stop()
        StateStore().clear_music_intent()
        return 0
    if a.action == "seek":
        # Timecode-aware, mirroring `book seek`: a bare value jumps absolute,
        # a signed one (+90 / -5:00) offsets. MPD seeks the current track only.
        return _do_timecode_seek(
            a.uri or "0",
            jump=lambda s: (b.seek_cur(position_ms=int(max(0.0, s) * 1000)),
                            max(0.0, s))[1],
            offset=lambda s: b.seek_relative(s),
        )
    if a.action == "speed":
        # Pitch-corrected tempo (mpv-routed tracks only — fetched YouTube in
        # rooms, the phone player). MPD/GStreamer streams have no speed knob.
        arg = (a.uri or "").strip()
        if not arg:
            cur = b.current_speed()
            print(f"{cur:.2f}×" if cur is not None
                  else "— (no speed control: no mpv track live)")
            return 0
        if arg in ("reset", "normal", "1x"):
            rate, relative = 1.0, False
        elif arg in ("up", "down"):
            # The popup's [ / ] keys: hop the shared speech/book speed ladder.
            rate = _speed_next(b.current_speed() or 1.0,
                               1 if arg == "up" else -1)
        else:
            relative = arg[0] in "+-"
            try:
                val = float(arg)
            except ValueError:
                print(f"media music speed: bad rate {arg!r} "
                      "(want 0.25–4, ±delta, or 'reset')", file=sys.stderr)
                return 2
            rate = ((b.current_speed() or 1.0) + val) if relative else val
        if not b.set_speed(rate):
            print("media music speed: no mpv track live "
                  "(MPD/GStreamer streams have no speed control)",
                  file=sys.stderr)
            return 1
        cur = b.current_speed()
        print(f"⏩ {cur:.2f}×" if cur is not None else f"⏩ {rate:.2f}×")
        return 0
    if a.action == "volume":
        b.volume_delta(int(float(a.uri or 0)))
        return 0
    if a.action == "prev" and getattr(a, "restart_first", False):
        # Popup `<`: ⏮ semantics — restart the track if we're past its start.
        if b is m:
            elapsed = lambda: (m.status_dict() or {}).get("elapsed")  # noqa: E731
        else:
            elapsed = lambda: (b.position() or 0) / 1000.0  # noqa: E731
        return _prev_with_restart(
            elapsed=elapsed,
            restart=lambda: b.seek_cur(position_ms=0),
            step_back=b.previous,
            channel="music" if b is m else "book",
        )
    if a.action in ("resume", "toggle") and _music_idle(b):
        # Nothing is loaded, so there is nothing to un-pause: reopen the last
        # thing played, the way `book resume` has always done. Before this,
        # `music stop` then `music resume` was silence with no explanation —
        # the channel had forgotten, because the only memory it kept was the
        # intent key that stop deletes.
        last = StateStore().get_music_last()
        if last:
            ct = last.get("content_type") or "music"
            argv = ["music", "play", last["uri"], "--as", ct]
            print(f"↻ resuming last played ({ct}): {last['uri']}")
            return main(argv)
        print("media music resume: nothing loaded and nothing played yet",
              file=sys.stderr)
        return 1
    {
        "pause": b.pause, "resume": b.resume,
        "toggle": b.toggle, "next": b.next, "prev": b.previous,
    }[a.action]()
    return 0


def _music_idle(backend) -> bool:
    """True when the live music backend has nothing loaded at all.

    Deliberately conservative: an unreadable Mopidy raises and counts as NOT
    idle, so a transport key falls through to its ordinary behaviour rather
    than surprising the listener by starting something. The phone backend
    cannot reach here while unreadable — `_music_live_backend` only returns it
    when `loaded()` already said yes.
    """
    try:
        return not backend.now_playing_uri()
    except Exception:  # noqa: BLE001 — see the docstring
        return False


# --- book + channel subcommands -------------------------------------------
#
# The book channel and focus/bed concurrency are orchestrated in mcp_server
# (bookmark-save on switch, playlist cursor, auto-advance). Rather than
# duplicate that here, the CLI calls those same tool functions — they're
# plain callables — and formats the result for the terminal. Imported lazily
# so frequent `media status`/`music` calls (status bar) don't pull in mcp.

def _srv():
    from . import mcp_server as srv
    return srv


def _book_stage_status(width: int, bar: bool = True) -> Optional[str]:
    try:
        from .sinks.book import read_stage_status
        st = read_stage_status()
    except Exception:
        return None
    if not st or st.get("status") not in ("copying", "playing", "error"):
        return None
    # A staging report describes a transfer that was happening *then*. This
    # file is not cleared on success, so an old failure sat in the status line
    # indefinitely — in one case a 44-minute-old error about a different
    # document, covering the clock of the one actually playing. A progress
    # indicator that outlives the thing it reports on is worse than none: it
    # says the current item is broken when it is playing perfectly.
    try:
        age = time.time() - float(st.get("ts") or 0)
    except (TypeError, ValueError):
        age = 0.0
    try:
        stale_after = float(os.environ.get("MEDIA_BOOK_STAGE_STALE_S", "90"))
    except ValueError:
        stale_after = 90.0
    if stale_after > 0 and age > stale_after:
        return None
    if st.get("status") == "error":
        return "! copy failed"
    total = int(st.get("total") or 0)
    copied = int(st.get("copied") or 0)
    if total <= 0:
        return "⬇ copying…"
    if not bar:
        pct = min(100, int(copied * 100 / total))
        return f"⬇ {pct}%"
    return render_status(idle=False, pos=copied / 1000.0, dur=total / 1000.0,
                         paused=False, muted=False, width=width,
                         hide_idle=False, bar=True).replace("▶", "⬇", 1)


def _book_status_line(srv, width: int, hide_idle: bool, bar: bool = True) -> str:
    staged = _book_stage_status(width, bar=bar)
    if staged:
        return staged
    np = srv.book_now_playing(target="")
    if np.get("idle"):
        return render_status(idle=True, pos=None, dur=None, paused=None,
                             muted=None, width=width, hide_idle=hide_idle)
    pos = (np.get("position_ms") or 0) / 1000.0
    dur = (np.get("duration_ms") or 0) / 1000.0 or None
    return render_status(idle=False, pos=pos, dur=dur,
                         paused=bool(np.get("paused")), muted=False,
                         width=width, hide_idle=hide_idle, bar=bar)


def _ok(result: dict) -> int:
    """Print a reason on failure; map the tool dict's ok flag to an exit code."""
    if result.get("ok") is False:
        reason = result.get("reason", "failed")
        print(f"media: {reason}", file=sys.stderr)
        return 1
    return 0


def _cmd_book_playlist(a, srv) -> int:
    pc = a.pl_cmd
    if pc == "new":
        r = srv.book_playlist_new(a.name)
        print(f"playlist {a.name!r}: "
              + ("created" if r.get("created") else "already exists"))
        return 0
    if pc == "add":
        r = srv.book_playlist_add(a.name, list(a.uris))
        print(f"playlist {a.name!r}: {r['added']} added ({r['count']} total)")
        return 0
    if pc == "play":
        r = srv.book_playlist_play(a.name, resume=not a.no_resume,
                                   target=a.target or "")
        if _ok(r):
            return 1
        title = r.get("title") or r.get("uri")
        print(f"▶ {a.name} [{r['index']}] {title}")
        return 0
    if pc == "rm":
        return _ok(srv.book_playlist_rm(a.name))
    # ls
    if a.name:
        pl = srv.book_playlist_ls(a.name)
        if _ok(pl):
            return 1
        cur = pl["cur_index"]
        if not pl["items"]:
            print(f"{a.name}: (empty)")
            return 0
        for it in pl["items"]:
            mark = "→" if it["pos"] == cur else " "
            label = it["title"] or it["uri"]
            print(f"{mark} {it['pos']:>2}  {label}")
        return 0
    lists = srv.book_playlist_ls().get("playlists", [])
    if not lists:
        print("(no book playlists)")
        return 0
    for pl in lists:
        print(f"{pl['name']:<20} {pl['count']:>3} parts  @ {pl['cur_index']}")
    return 0


def _parse_timecode(s: str) -> tuple[float, bool]:
    """Parse a position string into (seconds, relative).

    Accepts ``H:MM:SS`` / ``MM:SS`` / ``SS`` (fractions ok). A leading ``+`` or
    ``-`` makes it relative (skip ±) instead of an absolute jump:
        ``1:33:35`` → absolute 5615s   ``+90`` → +90s   ``-5:00`` → back 5min
    """
    s = s.strip()
    relative = False
    sign = 1.0
    if s[:1] == "+":
        relative, s = True, s[1:]
    elif s[:1] == "-":
        relative, sign, s = True, -1.0, s[1:]
    parts = s.split(":")
    if not parts or not all(parts):
        raise ValueError(f"bad time: {s!r}")
    try:
        secs = 0.0
        for p in parts:
            secs = secs * 60 + float(p)
    except ValueError:
        raise ValueError(f"bad time: {s!r}")
    return sign * secs, relative


def _hms(t: float) -> str:
    """``H:MM:SS`` (or ``M:SS`` under an hour) for seek/skip feedback — keeps
    seconds, unlike fmt_time which drops to ``H:MM`` at audiobook scale."""
    t = int(round(t)); h, rem = divmod(t, 3600); m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _do_timecode_seek(time_str: str, *, jump, offset,
                      force_relative: bool = False) -> int:
    """Channel-agnostic timecode seek, shared by book and music.

    Parses a timecode (``H:MM:SS`` / ``MM:SS`` / ``SS``; a leading ``+``/``-``
    makes it relative) and routes it to one of two channel callbacks:
      ``jump(secs)``   — absolute seek; may return the resulting position (s).
      ``offset(secs)`` — relative seek by ±secs.
    ``force_relative`` makes a bare, unsigned number relative instead of an
    absolute jump (the ``skip`` semantics — ``book skip 30`` means "+30s").
    Prints a one-line confirmation; returns 2 on a malformed timecode.
    """
    try:
        secs, relative = _parse_timecode(time_str)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if force_relative:
        relative = True
    if relative:
        offset(secs)
        print(f"⏩ {'+' if secs >= 0 else '−'}{_hms(abs(secs))}")
    else:
        pos = jump(secs)
        print(f"⏱ {_hms(pos if pos is not None else secs)}")
    return 0


def _book_seek_action(srv, time_str: str, tgt: str, *,
                      force_relative: bool = False) -> int:
    """Move the book playhead, shared by ``book seek`` and ``book skip``."""
    return _do_timecode_seek(
        time_str, force_relative=force_relative,
        jump=lambda s: (srv.book_seek(position_secs=s, target=tgt)
                        .get("position_ms") or 0) / 1000,
        offset=lambda s: srv.book_skip(seconds=s, target=tgt),
    )


def _tmux_session_name() -> str:
    """The tmux session this ran from, or "" outside tmux.

    Cheap and best-effort: it only ever changes the *order* of a list, so a
    failure here costs nothing worth an error path.
    """
    if not os.environ.get("TMUX"):
        return ""
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "#S"],
                           capture_output=True, text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def cmd_doc(a) -> int:
    """Documents, played as short audiobooks.

    Rendering goes to the book channel rather than to speech, and not for
    convenience: that channel already has chapters, a resume position and
    bookmarks, which is exactly the set of things a ten-minute document needs
    and a spoken reply does not. Headings become the chapter marks, so the
    popup's chapter browser navigates the document by section — nobody listens
    to a reference doc front to back.
    """
    from pathlib import Path
    from . import docs as docmod

    if a.doc_cmd == "list":
        # Context is on by default and visible in the rows: the popup is
        # opened from a pane that has a directory and a session, and those are
        # the two things that say what "relevant now" means. `--no-context`
        # gets the plain global list back.
        ctx = not getattr(a, "no_context", False)
        rows = docmod.list_docs(tag=getattr(a, "tag", "") or "",
                                include_inbox=getattr(a, "all", False),
                                cwd=Path.cwd() if ctx else None,
                                session=_tmux_session_name() if ctx else "")
        # `| head` closes the pipe early, and an unhandled BrokenPipeError
        # prints a traceback over the output the user was reading. A listing
        # that can't be piped isn't a listing.
        try:
            for d in rows:
                if getattr(a, "lines", False):
                    # display<TAB>slug — the shape the clip picker consumes.
                    print(f"{d.as_row()}\t{d.slug}")
                else:
                    print(d.as_row())
            sys.stdout.flush()
        except BrokenPipeError:
            try:
                sys.stdout.close()
            finally:
                os._exit(0)
        return 0

    if a.doc_cmd == "agenda":
        from . import agenda as ag
        sections = ag.agenda_sections(ag.load_entries())
        if getattr(a, "text", False):
            print(ag.agenda_text(ag.load_entries()))
            return 0
        # Never cached: the whole value of an agenda is that it is current,
        # and "today" changes underneath any key we could cache it on.
        clip = docmod.render_sections(sections, "agenda", force=True)
        if not clip:
            print("media doc: nothing on the agenda", file=sys.stderr)
            return 1
        if getattr(a, "feed", ""):
            # One episode per day, keyed on the date: re-running the digest
            # replaces this morning's rather than stacking a second copy of
            # the same day beside it. The `digest` feed's retention then
            # clears the week behind you.
            day = time.strftime("%Y-%m-%d")
            return _publish_episode(
                a.feed, clip, guid=f"agenda:{day}", title=f"Agenda {day}",
                description=docmod.episode_notes(sections), source="agenda")
        srv = _srv()
        r = srv.book_play(str(clip), resume=False, start_ms=-1,
                          target=getattr(a, "target", "") or "", title="Agenda")
        if r.get("error"):
            print(f"media doc: {r['error']}", file=sys.stderr)
            return 1
        print("Agenda")
        return 0

    if a.doc_cmd == "play" and getattr(a, "stdin", False):
        # A region or a buffer is not a file. Selection is the editor's job —
        # it knows what is highlighted and what mode the buffer is in — so the
        # contract has to accept text as well as a path, or every editor
        # binding would first have to invent a temporary file.
        import hashlib
        text = sys.stdin.read()
        if not text.strip():
            print("media doc: nothing on stdin", file=sys.stderr)
            return 1
        fmt = getattr(a, "fmt", "") or "md"
        sections = docmod.sections_for(text, fmt)
        if not sections:
            print("media doc: nothing speakable in that text", file=sys.stderr)
            return 1
        title = getattr(a, "title", "") or "Selection"
        # Keyed on the text itself: a buffer changes between readings, and a
        # path-and-mtime key cannot see that.
        key = hashlib.sha256(f"{fmt}|{text}".encode()).hexdigest()[:16]
        clip = docmod.render_sections(sections, f"stdin-{key}", force=False)
        if not clip:
            print("media doc: could not render that text", file=sys.stderr)
            return 1
        if getattr(a, "feed", ""):
            # Keyed on the text, exactly as the render is: send the same
            # region twice and it is one episode, edit it and it is a new one.
            return _publish_episode(
                a.feed, clip, guid=f"stdin:{key}", title=title,
                description=docmod.episode_notes(sections), source="stdin")
        r = _srv().book_play(str(clip), resume=False, start_ms=-1,
                             target=getattr(a, "target", "") or "", title=title)
        if r.get("error"):
            print(f"media doc: {r['error']}", file=sys.stderr)
            return 1
        print(title)
        return 0

    doc = docmod.find_doc(a.name, cwd=Path.cwd())
    if not doc:
        print(f"media doc: no document matching {a.name!r}", file=sys.stderr)
        return 1

    if a.doc_cmd == "text":
        print(docmod.speakable_text(doc.path.read_text(errors="replace"),
                                    doc.fmt))
        return 0

    # Synthesis is the slow part and the reason for the cache; say so, because
    # a first play of a long document is tens of seconds of apparent silence.
    clip = docmod.render_doc(doc.path, force=getattr(a, "force", False))
    if not clip:
        # Say *why* there is nothing. "Nothing to play" reads as a fault in the
        # player; usually it means the document is empty, or is entirely made
        # of the things the projection announces rather than reads.
        try:
            size = doc.path.stat().st_size
        except OSError:
            size = -1
        if size == 0:
            why = "the file is empty"
        elif not docmod.speakable_text(
                doc.path.read_text(errors="replace"), doc.fmt).strip():
            why = "nothing in it is speakable (all code, tables or properties)"
        else:
            why = "rendering failed — check `media errors`"
        print(f"media doc: {doc.path}: {why}", file=sys.stderr)
        return 1
    if getattr(a, "feed", ""):
        # The document's own path is the guid: re-publishing after an edit
        # replaces the episode instead of leaving two versions of one document
        # in the client, which is the shape of confusion nobody unpicks.
        #
        # Published *now*, not on the document's own Date: — the header of a
        # design doc written in June is not where a subscriber should have to
        # go looking for something queued this morning.
        return _publish_episode(
            a.feed, clip, guid=str(doc.path.resolve()), title=doc.title,
            description=docmod.episode_notes(
                docmod.sections_for(doc.path.read_text(errors="replace"),
                                    doc.fmt)),
            source=str(doc.path))
    srv = _srv()
    r = srv.book_play(str(clip), resume=not getattr(a, "no_resume", False),
                      start_ms=-1, target=getattr(a, "target", "") or "",
                      title=doc.title)
    if r.get("error"):
        print(f"media doc: {r['error']}", file=sys.stderr)
        return 1
    print(doc.title)
    return 0


def _publish_episode(name: str, clip, *, guid: str, title: str,
                     description: str = "", source: str = "") -> int:
    """Put a rendered file on a feed and refresh the XML. Prints what it did.

    Publishing is *instead of* playing, not as well as. A document you queue
    for the phone is one you are not listening to at the desk, and the render
    is cached either way — so playing it afterwards is a second command that
    costs nothing but says what it means.
    """
    from . import feed as feedmod

    try:
        ep = feedmod.publish(name, clip, guid=guid, title=title,
                             description=description, source=source)
    except (ValueError, OSError) as e:
        print(f"media doc: could not publish: {e}", file=sys.stderr)
        return 1
    where = f"{name}"
    if _feed_base_url():
        feedmod.write_feed(
            name, base_url=_feed_base_url(),
            token=(os.environ.get("MEDIA_FEED_TOKEN", "") or "").strip())
        where = f"{_feed_base_url().rstrip('/')}/feed/{name}.xml"
    dur = feedmod.hms(ep.duration_s) if ep.duration_s else "?"
    print(f"{ep.title}  ({dur})  → {where}")
    return 0


def _feed_base_url() -> str:
    """Where a subscriber reaches this host.

    No default worth guessing: an enclosure URL is baked into every client's
    database the moment it syncs, so a wrong one is not a mistake you correct,
    it is a mistake you re-subscribe out of. Set MEDIA_FEED_BASE_URL.
    """
    return (os.environ.get("MEDIA_FEED_BASE_URL", "") or "").strip()


def _feed_session_here() -> str:
    """The conversation this pane is holding, or "".

    Ownership before last-speaker, the same order `_anchor_session` uses and
    for the same reason: the registry knows who a pane belongs to even before
    they have said anything, and it does not decay when tmux recycles a pane
    id — whereas "who spoke here last" can name a conversation that ended days
    ago.
    """
    pane = os.environ.get("TTS_POPUP_PANE") or os.environ.get("TMUX_PANE") or ""
    if not pane:
        return ""
    return (_registered_session_for_pane(pane)
            or StateStore().session_for_pane(pane) or "")


def cmd_feed(a) -> int:
    """The spool a podcast client subscribes to.

    Publishing is a separate act from playing, deliberately: a document read
    aloud at the desk and the same document waiting on the phone are different
    requests, and the second one should not require the first.
    """
    from pathlib import Path
    from . import feed as feedmod

    fc = a.feed_cmd

    if fc == "list":
        names = [a.name] if getattr(a, "name", "") else feedmod.feeds()
        if not names:
            print("no feeds published", file=sys.stderr)
            return 0
        for name in names:
            eps = feedmod.episodes(name)
            pol = feedmod.policy(name)
            keep = ", ".join(
                [f"{pol.keep_days}d" if pol.keep_days else "",
                 f"max {pol.keep_max}" if pol.keep_max else ""]).strip(", ")
            n = len(eps)
            print(f"{name}  ({n} episode{'' if n == 1 else 's'}"
                  + (f", keep {keep}" if keep else "") + ")")
            for ep in eps:
                when = time.strftime("%Y-%m-%d %H:%M",
                                     time.localtime(ep.published))
                dur = feedmod.hms(ep.duration_s) if ep.duration_s else "-"
                print(f"  {when}  {dur:>8}  {ep.title}")
        return 0

    if fc == "publish":
        try:
            ep = feedmod.publish(
                a.name, Path(a.audio).expanduser(),
                guid=a.guid or str(Path(a.audio).expanduser().resolve()),
                title=a.title, description=a.description,
                source=a.source)
        except (ValueError, OSError) as e:
            print(f"media feed: {e}", file=sys.stderr)
            return 1
        print(ep.guid)
        return _feed_write(a.name) if _feed_base_url() else 0

    if fc == "remove":
        gone = feedmod.remove(a.name, a.guid)
        if not gone:
            print("media feed: no such episode", file=sys.stderr)
            return 1
        return _feed_write(a.name) if _feed_base_url() else 0

    if fc == "gc":
        for name in ([a.name] if getattr(a, "name", "") else feedmod.feeds()):
            for guid in feedmod.gc(name):
                print(f"{name}\t{guid}")
            if _feed_base_url():
                _feed_write(name)
        return 0

    if fc == "sessions":
        # Which conversations are there to publish? Grouped here rather than
        # in the store: it is a listing for a person choosing one, not a query
        # anything else makes.
        rows = StateStore().recent_history(sink="speech", limit=4000)
        seen: dict = {}
        for r in rows:
            ex = r.get("extras")
            if not isinstance(ex, dict):
                continue
            sess = ex.get("source_session")
            if not sess or ex.get("kind") == "notif":
                continue
            at = float(r.get("started_at") or 0)
            cur = seen.setdefault(sess, {"n": 0, "last": at, "first": at})
            cur["n"] += 1
            cur["last"] = max(cur["last"], at)
            cur["first"] = min(cur["first"], at)
        if not seen:
            print("no conversations in speech history", file=sys.stderr)
            return 1
        order = sorted(seen.items(), key=lambda kv: -kv[1]["last"])[:a.limit]
        from . import session_feed as _sf

        ws = {c["session"]: c["workspace"] for c in _sf.conversations()}
        for sess, info in order:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(info["last"]))
            span = feedmod.hms(info["last"] - info["first"])
            where = f"  [{ws[sess]}]" if ws.get(sess) else ""
            print(f"{sess}  {when}  {info['n']:>3} turns  over {span}{where}")
        return 0

    if fc == "publish-quiet":
        from . import session_feed

        eps = session_feed.publish_quiet(name=a.name or None,
                                         quiet_s=max(60.0, a.quiet_min * 60.0),
                                         limit=max(0, a.limit))
        if not eps:
            return 0
        touched = set()
        for ep in eps:
            landed = session_feed._feed_of(ep, a.name or None)
            touched.add(landed)
            print(f"[{landed}] {ep.title}  ({feedmod.hms(ep.duration_s)})")
        if _feed_base_url():
            for landed in sorted(touched):
                feedmod.write_feed(
                    landed, base_url=_feed_base_url(),
                    token=(os.environ.get("MEDIA_FEED_TOKEN", "") or "").strip())
        return 0

    if fc == "session":
        from . import session_feed

        sess = a.session or _feed_session_here()
        if not sess:
            print("media feed: no conversation here — name one "
                  "(`media feed sessions` lists them)", file=sys.stderr)
            return 1
        ep = session_feed.publish(sess, name=a.name or None)
        if ep is None:
            # The usual cause is not "no such session" but a swept cache: the
            # rows are there and their audio is not. Say so, because the two
            # look identical from the outside and only one is fixable.
            print(f"media feed: nothing to publish for {sess} — no turns with "
                  "audio still on disk", file=sys.stderr)
            return 1
        landed = session_feed._feed_of(ep, a.name or None)
        where = landed
        if _feed_base_url():
            feedmod.write_feed(
                landed, base_url=_feed_base_url(),
                token=(os.environ.get("MEDIA_FEED_TOKEN", "") or "").strip())
            where = f"{_feed_base_url().rstrip('/')}/feed/{landed}.xml"
        print(f"{ep.title}  ({feedmod.hms(ep.duration_s)})  → {where}")
        return 0

    if fc == "tracks":
        # The growing layout: a conversation as an item that appends, one
        # track per turn. Opt-in while the concatenated export is still the
        # one being delivered — both write to the same tree, under different
        # authors, so running this changes nothing about the other.
        from . import book_tracks

        sess = (getattr(a, "session", "") or "").strip()
        rows = ([(sess, *book_tracks.export_session(sess))] if sess
                else book_tracks.export_all(
                    since_hours=float(getattr(a, "since_hours", 24.0))))
        grew = [(session, folder, added) for session, folder, added in rows
                if folder is not None and added]
        for _session, folder, added in grew:
            print(f"{folder}: +{added} track(s)")
        if not sess:
            # The sweep is also when a session that has ended stops being
            # live: the tag is reconciled for every conversation, whether or
            # not anything grew — a session usually ends without a new turn.
            n = book_tracks.sync_live_tags()
            if n:
                print(f"live tag: {n} item(s) changed")
        if not grew:
            print("no new turns")
            # Asked about one conversation by name, answer about it anyway:
            # the tracks can be there while the item's chapters are not,
            # because a manifest can be rebuilt from a tree that was already
            # written. The sweep over every conversation stays quiet.
            if not sess:
                return 0
            grew = [(session, folder, 0) for session, folder, _a in rows
                    if folder is not None]
        if getattr(a, "no_reopen", False):
            return 0

        # Order matters, and it is the whole reason this is not two commands.
        # Re-opening an item asks the server what its duration is now; ask
        # before the scan and the answer is the length it was before the turn
        # landed, which is the position a finished listener would be put back
        # to. So: write the files, make the server look, then re-open.
        from . import library

        if not library.trigger_abs_scan("conversations"):
            print("media feed tracks: no Audiobookshelf to scan — tracks are "
                  "on disk, nothing was re-opened", file=sys.stderr)
            return 0
        for session, folder, _added in grew:
            # Wait for the scan to have actually happened, rather than for a
            # fixed number of seconds — the difference between a reply landing
            # in the app in five seconds and in fifteen.
            book_tracks.wait_for_tracks(
                folder, len(list(folder.glob("*.mp3"))),
                timeout_s=float(os.environ.get("MEDIA_ABS_SCAN_WAIT_S") or 25.0))
            n = book_tracks.publish_chapters(session, folder)
            if n:
                print(f"{folder.name}: {n} chapter(s)")
            described = book_tracks.set_metadata(session, folder)
            if described:
                print(f"{folder.name}: described as {described!r}")
            if book_tracks.reopen(folder):
                print(f"{folder.name}: re-opened (it had been finished)")
        return 0

    if fc == "xml":
        base = _feed_base_url() or "http://localhost"
        sys.stdout.write(feedmod.feed_xml(
            a.name, feedmod.episodes(a.name), base_url=base,
            token=(os.environ.get("MEDIA_FEED_TOKEN", "") or "").strip()))
        return 0

    if fc == "write":
        return _feed_write(a.name)

    print(f"media feed: unknown subcommand {fc}", file=sys.stderr)
    return 2


def _feed_write(name: str) -> int:
    """Regenerate one feed's XML, or say why it can't be."""
    from . import feed as feedmod

    base = _feed_base_url()
    if not base:
        print("media feed: set MEDIA_FEED_BASE_URL (an enclosure URL is baked "
              "into every subscriber's database)", file=sys.stderr)
        return 1
    path = feedmod.write_feed(
        name, base_url=base,
        token=(os.environ.get("MEDIA_FEED_TOKEN", "") or "").strip())
    print(path)
    return 0


def cmd_book(a) -> int:
    srv = _srv()
    bc = a.book_cmd
    tgt = getattr(a, "target", "") or ""

    if bc == "playlist":
        return _cmd_book_playlist(a, srv)
    if bc == "status":
        try:
            print(_book_status_line(srv, a.width, hide_idle=not a.show_idle,
                                    bar=not a.no_bar))
        except Exception:  # noqa: BLE001 — status bar must never see a traceback
            print("○" if a.show_idle else "")
        return 0
    if bc == "now":
        np = srv.book_now_playing(target=tgt)
        if not np.get("idle"):
            print(np.get("uri") or "")
        return 0
    if bc == "meta":
        np = srv.book_now_playing(target=tgt)
        if np.get("idle"):
            print("\n\n")
            return 0
        print(np.get("title") or np.get("media_title") or np.get("uri") or "")
        try:
            from .sinks.book import read_stage_status
            staged = read_stage_status()
        except Exception:
            staged = None
        if staged and staged.get("status") == "copying":
            total = int(staged.get("total") or 0)
            copied = int(staged.get("copied") or 0)
            pct = int(copied * 100 / total) if total else 0
            print(f"copying {pct}%: {staged.get('title') or ''}")
        else:
            print(np.get("chapter_title") or "")
        print(np.get("uri") or "")
        return 0
    if bc == "bookmark":
        return _book_bookmark(getattr(a, "note", "") or "", target=tgt, range_end=bool(getattr(a, "range_end", False)), slot=getattr(a, "slot", "") or "")
    if bc == "play":
        r = srv.book_play(a.uri, resume=not a.no_resume,
                          start_ms=(a.start_ms if a.start_ms is not None else -1),
                          target=tgt, title=getattr(a, "title", "") or "")
        if r.get("fetching"):
            extra = f" [{r['count']} items]" if r.get("count") else ""
            print(f"⬇ {r.get('reason', 'fetching')}{extra}: {r['uri']}")
            return 0
        if not r.get("ok", True):
            print(r.get("reason", "book play failed"), file=sys.stderr)
            return 1
        print(f"▶ {r['uri']} (from {fmt_time((r.get('resumed_from_ms') or 0)/1000)})")
        return 0
    if bc == "import-youtube":
        from . import library
        urls = library.expand_youtube_playlist(a.uri) or [a.uri]
        if not urls or not library.start_fetch_many(urls, play=a.play, target=tgt):
            print("media book import-youtube: audiobook-fetch unavailable or playlist expansion failed", file=sys.stderr)
            return 1
        kind = "playlist" if len(urls) > 1 else "video"
        print(f"⬇ importing YouTube {kind}: {len(urls)} item(s) into Audiobookshelf")
        return 0
    if bc == "resume":
        r = srv.book_resume(target=tgt)
        return _ok(r)
    if bc == "pause":
        return _ok(srv.book_pause(target=tgt))
    if bc == "stop":
        return _ok(srv.book_stop(target=tgt))
    if bc == "next":
        return _ok(srv.book_next(target=tgt))
    if bc == "prev":
        if getattr(a, "restart_first", False):
            # Popup `<`: ⏮ semantics — restart the part if we're past its start.
            np = srv.book_now_playing(target=tgt)
            pos = None if np.get("idle") else (np.get("position_ms") or 0) / 1000.0
            return _prev_with_restart(
                elapsed=lambda: pos,
                restart=lambda: srv.book_seek(position_secs=0,
                                              target=tgt),
                step_back=lambda: srv.book_prev(target=tgt),
                channel="book",
            )
        return _ok(srv.book_prev(target=tgt))
    if bc == "skip":
        # `skip` is relative-only sugar over the shared seek action.
        return _book_seek_action(srv, str(a.secs), tgt, force_relative=True)
    if bc == "seek":
        return _book_seek_action(srv, a.time, tgt)
    if bc == "speed":
        if a.factor in ("up", "down"):
            np = srv.book_now_playing(target=tgt)
            cur = float(np.get("speed") or 1.0) if not np.get("idle") else 1.0
            rate = _speed_next(cur, 1 if a.factor == "up" else -1)
        else:
            rate = 1.0 if a.factor == "reset" else float(a.factor)
        r = srv.book_speed(rate, target=tgt)
        print(f"speed: {r['speed']}")
        return 0
    if bc in ("chapters", "chapter"):
        return _cmd_book_chapters(a)
    if bc == "bed":
        return _ok(srv.book_bed(a.mode, target=tgt))
    return 2


def cmd_focus(a) -> int:
    return _ok(_srv().focus(a.channel, target=getattr(a, "target", "") or ""))


def cmd_abs_scan(a) -> int:
    from . import library
    tgt = getattr(a, "target", "") or None
    if library.trigger_abs_scan(tgt):
        print("Audiobookshelf scan started")
        return 0
    print("media abs-scan: failed to start Audiobookshelf scan", file=sys.stderr)
    return 1


def cmd_search(a) -> int:
    query = " ".join(a.query) if getattr(a, "query", None) else ""
    m = _srv()
    res = m.search(a.channel, query)
    if "error" in res:
        print(f"media search: {res['error']}", file=sys.stderr)
        return 1
    if not res.get("results"):
        print("media search: no results", file=sys.stderr)
        return 1

    if getattr(a, "lines", False):
        for r in res["results"]:
            print(f"{r['title']}\t{r['uri']}")
        return 0

    import shutil
    import subprocess

    if not shutil.which("fzf"):
        print("media search: fzf not installed", file=sys.stderr)
        for r in res["results"]:
            print(f"{r['uri']}  ({r['title']})")
        return 1

    lines = [f"{i}\t{r['title']}\t{r['uri']}" for i, r in enumerate(res["results"])]
    proc = subprocess.run(
        ["fzf", "--with-nth", "2..", "--delimiter", "\t", "--prompt", "search> "],
        input="\n".join(lines), text=True, stdout=subprocess.PIPE,
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return proc.returncode

    try:
        idx = int(proc.stdout.split("\t", 1)[0])
        selected = res["results"][idx]
    except Exception:
        return 1

    print(f"Playing: {selected['title']}")
    if a.channel == "book":
        m.book_play(uri=selected['uri'], title=re.sub(r"  \[[^]]+\]$", "", selected["title"]))
    else:
        m.music_play(uri=selected['uri'])
    return 0


def cmd_channels(a) -> int:
    st = _srv().channels_status()
    print(f"focus: {st.get('focus') or '-'}   bed: {st.get('bed') or '-'}")
    mu = st.get("music") or {}
    bk = st.get("book") or {}
    print(f"music: {mu.get('uri') or '(idle)'}")
    if bk.get("idle"):
        print("book:  (idle)")
    else:
        print(f"book:  {bk.get('uri') or ''}"
              + (" [paused]" if bk.get("paused") else ""))
    return 0


# --- popup channel resolution ---------------------------------------------
#
# `prefix a` should reopen the channel you were last using, but defer to one
# that's actually playing audio. The launcher (media-popup-open) calls
# `media popup-channel` to pick the initial channel; the popup (media-popup)
# calls `media popup-channel --set <chan>` on exit to remember it.

def _popup_channel_file():
    return state_dir() / "popup-channel"


def _channel_is_playing(name: str) -> bool:
    """True when `name` is actively producing audio (not idle, not paused).

    Every probe is best-effort: a missing socket / unreachable MPD / any error
    means "not playing" rather than blowing up the launcher.
    """
    try:
        if name == "speech":
            # Require explicit False: a dead/absent socket returns None, which
            # must read as "not playing" (not as `not None` → truthy).
            return _get("idle-active") is False and _get("pause") is False
        if name == "music":
            return SinkMusic().status_dict(SPEECH_TARGET).get("state") == "play"
        if name == "book":
            # Probe the book sink directly rather than via mcp_server: importing
            # mcp_server pulls in the whole fastmcp framework (~0.47s), and this
            # runs in the popup *launcher* (`popup-channel`), before the popup
            # can even appear — so that import was the bulk of open latency.
            # SinkBook.idle/paused is what channels_status reads anyway (target
            # "local"); short-circuiting skips the paused() probe when idle.
            from .sinks import SinkBook
            b = SinkBook()
            t = Target(name="local")
            return (not b.idle(t)) and (not b.paused(t))
    except Exception:  # noqa: BLE001 — the launcher must never see a traceback
        return False
    return False


def _last_popup_channel() -> Optional[str]:
    try:
        chan = _popup_channel_file().read_text().strip()
    except OSError:
        return None
    return chan if chan in POPUP_CHANNELS else None


def cmd_popup_channel(a) -> int:
    if getattr(a, "set", None):
        if a.set in POPUP_CHANNELS:
            try:
                f = _popup_channel_file()
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(a.set)
            except OSError:
                pass
        return 0
    # Resolve: a single playing channel wins; otherwise (none or several
    # playing — e.g. a music bed under speech) fall back to the last-viewed
    # channel, then speech.
    playing = [c for c in POPUP_CHANNELS if _channel_is_playing(c)]
    if len(playing) == 1:
        print(playing[0])
    else:
        print(_last_popup_channel() or "speech")
    return 0


SELFCHECK_SENTINEL = "selfcheck=1"


def _checkout_root() -> "Optional[Path]":
    """The agent-media git checkout this process's code lives in, if any."""
    from pathlib import Path
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists() and (parent / "packages" / "core").is_dir():
            return parent
    return None


def _sv_dir() -> "Optional[Path]":
    """The runit service directory in use, if this host runs runit."""
    from pathlib import Path
    cands = [os.environ.get("SVDIR"),
             (os.environ.get("PREFIX", "") + "/var/service") if os.environ.get("PREFIX") else None,
             "/data/data/com.termux/files/usr/var/service",
             "/var/service", "/etc/service", "/service"]
    for c in cands:
        if c and Path(c).is_dir():
            return Path(c)
    return None


def _adopted_app_names(root: "Optional[Path]") -> "list[str]":
    """App names from scripts/termux-apps.conf — the non-agent-media installs
    the heal script keeps alive (mopidy, beets). Their services are worth
    watching here: they died in the same python upgrade, just as silently.
    """
    if root is None:
        return []
    conf = root / "packages" / "core" / "scripts" / "termux-apps.conf"
    names = []
    try:
        for line in conf.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            name = line.split("|", 1)[0].strip()
            if name:
                names.append(name)
    except OSError:
        pass
    return names


def _runit_service_states(root: "Optional[Path]") -> "list[tuple[str, str]]":
    """(name, state) for the runit services this checkout is responsible for.

    Ours by two routes: the service dir symlinks into the checkout, or the
    service is one of the adopted apps (`mopidy`, `beets-web`). The same dir
    also holds sshd, snapclient and friends, which agent-media has no business
    grading.
    """
    import subprocess
    from pathlib import Path
    svdir = _sv_dir()
    if svdir is None or root is None:
        return []
    apps = _adopted_app_names(root)
    out = []
    for entry in sorted(svdir.iterdir()):
        try:
            target = entry.resolve()
        except OSError:
            continue
        ours = root in target.parents
        # `mopidy` for app mopidy, `beets-web` for app beets — the service name
        # is the app name or a variant of it.
        if not ours:
            ours = any(entry.name == a or entry.name.startswith(f"{a}-")
                       for a in apps)
        if not ours:
            continue
        # runit's `down` file is the standing "leave this stopped" marker. A
        # parked service looks identical to a dead one in `sv status`, and
        # reporting it forever is how a health check teaches you to ignore it.
        if (target / "down").exists():
            out.append((entry.name, "parked"))
            continue
        try:
            r = subprocess.run(["sv", "status", str(entry)],
                               capture_output=True, text=True, timeout=10,
                               env={**os.environ, "SVDIR": str(svdir)})
            line = (r.stdout or r.stderr or "").strip()
        except (OSError, subprocess.SubprocessError):
            line = "unknown"
        state = "up" if line.startswith("run:") else "down"
        out.append((entry.name, state))
    return out


def _systemd_service_states() -> "list[tuple[str, str]]":
    """(unit, state) for this user's agent-media systemd units."""
    import subprocess
    out = []
    try:
        r = subprocess.run(
            ["systemctl", "--user", "list-units", "--no-legend", "--plain",
             "--all", "agent-media*", "am-*"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return out
    # A unit with a timer beside it is a job, not a daemon: between runs it is
    # `inactive (dead)`, which is what a *healthy* scheduled task looks like.
    # Reporting those as down would put a permanent ⚠ on the status bar for a
    # feed pruner that runs at midnight and is behaving perfectly.
    timed = {line.split()[0][:-len(".timer")]
             for line in (r.stdout or "").splitlines()
             if line.split() and line.split()[0].endswith(".timer")}
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[0].endswith(".service"):
            continue
        unit, active = parts[0], parts[2]
        stem = unit[:-len(".service")]
        if stem in timed and active != "failed":
            # `failed` still counts: a job that errored has something to say,
            # and its next window will not fix it.
            out.append((stem, "timed"))
            continue
        out.append((stem, "up" if active == "active" else active))
    return out


# A restart is one exit; a crash LOOP is several in quick succession. Match the
# threshold crash-notify itself acts on, so a deliberate `sv restart` — which
# also writes a ledger line — isn't reported as breakage. Reporting routine
# restarts is how a health check earns the scroll-past it later dies of.
_CRASH_LOOP_MIN = 3


def _crash_loops(within_s: int = 900,
                 minimum: int = _CRASH_LOOP_MIN) -> "list[tuple[str, int]]":
    """(service, failures) from the runit finish-script crash ledger, for
    services that failed at least `minimum` times inside the window.

    Written by services/_common/crash-notify, which is plain sh precisely so it
    still records when the Python install is the thing that's broken.
    """
    import time as _time
    from pathlib import Path
    d = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    crashdir = d / "agent-media" / "sv-crash"
    out = []
    if not crashdir.is_dir():
        return out
    floor = _time.time() - within_s
    for f in sorted(crashdir.glob("*.log")):
        n = 0
        try:
            for line in f.read_text().splitlines():
                ts = line.split(" ", 1)[0]
                try:
                    if float(ts) >= floor:
                        n += 1
                except ValueError:
                    continue
        except OSError:
            continue
        if n >= minimum:
            out.append((f.stem, n))
    return out


def selfcheck_facts() -> "dict[str, str]":
    """Runtime health of the agent-media install on THIS host.

    `media doctor` compared git HEADs and nothing else, which is exactly how a
    dead install hid for hours on 2026-07-30: the phone's checkout was current
    while every entrypoint raised ModuleNotFoundError (Termux had upgraded
    python out from under site-packages) and media-mcp crash-looped ~1250 times
    in silence. A current checkout says nothing about whether the code can run.

    Returns flat key=value facts so `doctor` can collect them over ssh from
    hosts running a different version of this file.
    """
    import subprocess
    import sys as _sys
    from pathlib import Path

    facts: "dict[str, str]" = {}
    facts["python"] = "%d.%d" % _sys.version_info[:2]

    import agent_media_core
    mod = Path(agent_media_core.__file__).resolve()
    root = _checkout_root()
    facts["module"] = str(mod)
    if root is None:
        # Installed as a copy with no checkout beside it: `git pull` deploys
        # nothing here, which is how call-guard ran weeks-old code.
        facts["install"] = "copy"
    else:
        facts["install"] = "editable"
        facts["checkout"] = str(root)
        try:
            facts["commit"] = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10).stdout.strip()[:7]
        except (OSError, subprocess.SubprocessError):
            pass

    services = _runit_service_states(root) + _systemd_service_states()
    down = [n for n, s in services if s not in ("up", "parked", "timed")]
    parked = [n for n, s in services if s in ("parked", "timed")]
    facts["services"] = str(len(services))
    if down:
        facts["down"] = ",".join(down)
    if parked:
        facts["parked"] = ",".join(parked)   # reported, never a problem
    loops = _crash_loops()
    if loops:
        facts["crashloop"] = ",".join(f"{n}:{c}" for n, c in loops)
    facts.update(_mic_detect_facts())
    facts.update(_hold_facts())
    facts.update(_media_volume_facts())
    facts.update(_dictation_rate_facts())
    facts.update(_mic_block_facts())
    facts.update(_ringer_facts())
    _cache_facts(facts)
    return facts


def _hold_facts() -> "dict[str, str]":
    """A hold that is in effect right now, and how long it has stood.

    The mic-detect facts answer "does anything still fire the trigger". This
    answers the opposite and more urgent question: is something holding *now*.
    A hold ducks music and pauses speech, so a stuck one is total silence with
    every service up — and the release is the half that goes missing, whether
    an ssh call died between --hold and --release or the Automate bridge was
    killed mid-dictation. Neither leaves a trace anywhere else.
    """
    try:
        from .call_guard import (Config, advert_path, hold_age, hold_warn_s,
                                 _flag_source)
    except Exception:                                # pragma: no cover
        return {}
    try:
        advert_path().stat()
    except OSError:
        return {}                                    # no guard here
    cfg = Config()
    age = hold_age(cfg)
    if age is None:
        return {}                                    # nothing held: say nothing
    facts = {"hold_s": str(int(age)), "hold_warn_s": str(int(hold_warn_s()))}
    src = _flag_source(cfg.hold_flag)
    if src:
        facts["hold_src"] = src
    return facts


def _media_volume_facts() -> "dict[str, str]":
    """The Android media-stream volume, where there is one.

    mpv plays into STREAM_MUSIC, so this being zero is total silence with
    every part of the stack reporting healthy — the player unpaused and unmuted at
    volume 150, the renderer fine, the services up. Exactly the failure mode
    the mic-detect heartbeat exists for, and it cost the same hour to find:
    everything healthy, nothing audible.

    Reported, never corrected. Someone silencing their phone means it.
    """
    import shutil
    import subprocess

    if shutil.which("termux-volume") is None:
        return {}                       # not an Android host; nothing to say
    try:
        out = subprocess.run(["termux-volume"], capture_output=True, text=True,
                             timeout=15)
        if out.returncode != 0:
            return {}
        streams = json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}                       # Termux:API missing or wedged
    for stream in streams:
        if stream.get("stream") == "music":
            vol, mx = stream.get("volume"), stream.get("max_volume")
            if vol is None:
                return {}
            return {"media_volume": f"{vol}/{mx}"}
    return {}


def _mic_block_facts() -> "dict[str, str]":
    """What the mic-block service last saw, if it runs here.

    The one place on the phone that can actually read the app-op, so this is
    the difference between "the block has come off again" and "David is doing
    Duolingo" — two things that look identical from the hold rate, which is all
    the companion can see.
    """
    from . import mic_block

    try:
        blob = json.loads(mic_block.state_path().read_text())
    except (OSError, ValueError):
        return {}                       # the service does not run on this host
    if not isinstance(blob, dict) or not blob:
        return {}
    facts = {}
    loose = []
    blind = []
    reverts = 0
    now = time.time()
    for package, entry in blob.items():
        if not isinstance(entry, dict):
            continue
        mode = entry.get("mode", "unknown")
        if mode == "unknown":
            # No shell to read the op with. The service is explicit that this
            # is not evidence either way, so it must not be flattened in with
            # a block we watched come off.
            since = entry.get("last_known_at")
            seen = entry.get("last_known_mode")
            when = (f", last seen {seen} {(now - float(since)) / 3600:.0f}h ago"
                    if since and seen else "")
            blind.append(f"{package}{when}")
        elif mode != mic_block.BLOCKED:
            loose.append(f"{package}={mode}")
        reverts += len([t for t in entry.get("reverts", [])
                        if isinstance(t, (int, float)) and now - t < 86400])
    if loose:
        facts["mic_block"] = "loose:" + ",".join(loose)
    elif blind:
        facts["mic_block"] = "unknown:" + ",".join(blind)
    else:
        facts["mic_block"] = "held"
    if reverts:
        facts["mic_block_reverts_24h"] = str(reverts)
    return facts


def _ringer_facts() -> "dict[str, str]":
    """What the ringer service last published, and what it cost.

    Two halves, and the second is the important one. `ringer=` is the state;
    `alerts_held_24h=` is the *consequence*, and without it a held alert leaves
    no impression anywhere a person looks. "My morning digest went quiet" and
    "TTS is broken" are otherwise the same report, and this stack has twice
    been taken apart in full over a component behaving exactly as designed.

    Silent on hosts that publish nothing, which is every host but the phone.
    """
    facts: dict[str, str] = {}
    from . import ringer

    try:
        snap = json.loads(ringer.state_path().read_text())
    except (OSError, ValueError):
        snap = None
    if isinstance(snap, dict) and snap:
        age = int(max(0.0, time.time() - float(snap.get("checked_at") or 0)))
        if not snap.get("answered"):
            # The service is up and the app is not. Worth saying plainly: the
            # gate is open, so alerts are speaking regardless of the ringer.
            facts["ringer"] = f"unanswered age={age}s"
        else:
            facts["ringer"] = ("quiet" if snap.get("quiet") else "audible")
            facts["ringer_mode"] = str(snap.get("mode", "unknown"))
            if str(snap.get("dnd", "unknown")) not in ("unknown", ""):
                facts["ringer_dnd"] = str(snap.get("dnd"))
            facts["ringer_age_s"] = str(age)
    # Counted where the *deciding* happened, which is the origin — so on a
    # two-host fleet `ringer=` shows up on the phone and `alerts_held_24h=` on
    # red5. That split is honest rather than awkward: each host reports the
    # thing it actually knows, and neither has to ask the other.
    try:
        from .state import StateStore
        held = [r for r in StateStore().recent_errors(
                    component="intake", limit=500,
                    since=time.time() - 86400)
                if (r.get("extras") or {}).get("kind") == "alert-silenced"]
    except Exception:  # noqa: BLE001 — a count, never a reason to fail a check
        held = []
    if held:
        facts["alerts_held_24h"] = str(len(held))
    return facts


def _dictation_rate_facts() -> "dict[str, str]":
    """How often the companion's dictation hold is pausing speech.

    The hold is right to exist — Sam must wait while David talks to his
    keyboard — and it cannot tell a person from the phone's own recogniser by
    anything it can see. On p8a that recogniser holds the microphone for ten
    seconds at a time whenever `com.google.android.as` is not blocked from
    RECORD_AUDIO, which pauses speech every half minute; the block reverts by
    itself, hours later, saying nothing.

    Both times it reverted the report that reached us was "TTS keeps pausing",
    with every component healthy and behaving exactly as designed. The app
    counts its own engagements over a rolling hour for precisely this, so ask
    it: a rate no person produces is the one fact that turns that report into
    an answer.

    Silent unless the app is there and answering — this is a phone-only fact,
    and a host without the companion has nothing to say rather than a zero.
    """
    import urllib.request

    try:
        port = int(os.environ.get("MEDIA_ANDROID_COMPANION_PORT", "8770"))
    except ValueError:
        port = 8770
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/state", timeout=5) as resp:
            state = json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001 — no companion here, or it is busy
        return {}
    if not isinstance(state, dict):
        return {}
    facts = {}
    holds = state.get("dictation_holds_1h")
    if isinstance(holds, int):
        facts["dictation_holds_1h"] = str(holds)
    problem = state.get("dictation_rate")
    if isinstance(problem, str) and problem:
        facts["dictation_rate"] = problem
    return facts


def _mic_detect_facts() -> "dict[str, str]":
    """How long since the external hold trigger last fired, on hosts that run
    the guard.

    Only meaningful where call-guard runs, so absence of the guard means no
    facts rather than a misleading zero.

    Quiet time is measured from the last hold whenever there has been one, and
    only from the guard's start when there has never been one — a guard that
    came up ten minutes ago has had no chance to see a hold yet, and flagging
    that would train everyone to ignore the warning. It used to be measured
    from the LATER of the two, which meant every restart of a supervised
    service reset the clock: deploy, crash or reboot more often than the 24h
    limit and a permanently dead trigger never gets reported at all. The
    restart is exactly when you are least likely to notice.

    The source is reported alongside, because the two things that write the
    flag — the mic-detect bridge and a person running `--hold` — answer
    different questions, and "fired 3h ago" is not evidence of a live trigger
    if it was typed.
    """
    import time as _time
    from pathlib import Path

    try:
        from .call_guard import (advert_path, last_hold_path, last_hold_source,
                                 last_external_hold_path)
    except Exception:                                # pragma: no cover
        return {}

    advert = advert_path()
    try:
        guard_started = advert.stat().st_mtime
    except OSError:
        return {}                                    # no guard here

    facts = {"mic_detect": "watched"}
    last = last_hold_path()
    try:
        last_hold = last.stat().st_mtime
    except OSError:
        last_hold = 0.0
    if last_hold:
        facts["mic_detect_last_hold_s"] = str(int(_time.time() - last_hold))
        src = last_hold_source()
        if src:
            facts["mic_detect_last_hold_src"] = src
    # Quiet is measured from the last hold *nobody typed*: a `--hold` someone
    # ran proves the receiving half works and says nothing about the trigger,
    # yet it used to reset this clock for a day — silencing the alarm at
    # exactly the moment someone was investigating the thing it warned about.
    try:
        last_external = last_external_hold_path().stat().st_mtime
    except OSError:
        last_external = 0.0
    if last_external:
        facts["mic_detect_last_external_s"] = str(
            int(_time.time() - last_external))
    # With no un-typed hold on record there is nothing to measure *from*, so
    # measure from the earliest moment we know we were watching: the guard's
    # start, or an older typed hold, which proves the guard was already up
    # then. Taking guard-start alone would let every deploy reset the clock —
    # the same hole the LATER-of-the-two version had, reopened from the other
    # side, and found within a minute of shipping it: a restart cleared an
    # alarm that had been correct for a day.
    since = last_external or min(x for x in (guard_started, last_hold) if x)
    facts["mic_detect_quiet_s"] = str(int(_time.time() - since))
    return facts


#: Directories that grow on their own, as (fact name, path). Shallow on
#: purpose — see `_dir_mb`.
def _cache_dirs() -> "list[tuple[str, object]]":
    from ._paths import cache_dir, state_dir

    return [("cache_audio_mb", cache_dir() / "audio"),      # rendered speech
            ("cache_books_mb", cache_dir() / "books"),      # fetched longform
            ("cache_docs_mb", cache_dir() / "docs"),        # rendered documents
            ("feed_spool_mb", state_dir() / "feed")]        # published episodes


def _dir_mb(path, depth: int = 1) -> int:
    """Megabytes in `path`, counting one level of subdirectory.

    `du` would be exact and is a subprocess per directory per selfcheck, on
    hosts where selfcheck is answered over ssh. These directories are flat by
    construction — clips, books, one folder per feed — so a scandir gets the
    same answer for the cost of a stat each.
    """
    total = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                    elif e.is_dir(follow_symlinks=False) and depth > 0:
                        total += _dir_mb(e.path, depth - 1) * 1048576
                except OSError:
                    continue
    except OSError:
        return 0
    return total // 1048576


#: When a growing directory is worth mentioning. Not a fault — nothing here is
#: broken at 6GB — but 4.3GB of audiobooks sat on a phone for six weeks after
#: being copied to the hub, and nothing in this report would have said so.
#: The phone is the machine that matters: its disk is the one that cannot be
#: made bigger.
CACHE_WARN_MB = 4096


def _cache_warn_mb() -> int:
    """MEDIA_CACHE_WARN_MB overrides the threshold; 0 switches it off.

    A hub with a terabyte and a phone with a full one want different answers,
    and the fleet reports through the same code.
    """
    try:
        v = int(os.environ.get("MEDIA_CACHE_WARN_MB", "") or CACHE_WARN_MB)
    except ValueError:
        return CACHE_WARN_MB
    return v if v > 0 else 1 << 30


def _cache_facts(facts: "dict[str, str]") -> None:
    for name, path in _cache_dirs():
        mb = _dir_mb(path)
        if mb:
            facts[name] = str(mb)


def health_problems(facts: "dict[str, str]") -> "list[str]":
    """Human-readable problems implied by a selfcheck fact set (empty = well)."""
    problems = []
    if facts.get("selfcheck") == "broken":
        return ["install broken: agent_media_core will not import"]
    if facts.get("selfcheck") == "unsupported":
        return []      # too old to selfcheck; skew reporting still applies
    if facts.get("install") == "copy":
        problems.append("install is a copy, not editable — git pull won't deploy")
    for name, _ in _cache_dirs():
        try:
            mb = int(facts.get(name) or 0)
        except ValueError:
            continue
        if mb >= _cache_warn_mb():
            # Named, with the number, because "a cache is large" is not
            # actionable and "books/ is 4300MB" is: that one turned out to be
            # copies of files already on the hub.
            problems.append(f"{name[:-3].replace('_', '/')} is {mb}MB")
    if facts.get("down"):
        problems.append(f"services down: {facts['down']}")
    if facts.get("crashloop"):
        problems.append(f"crash-looping: {facts['crashloop']}")
    vol = facts.get("media_volume")
    if vol:
        try:
            level = int(vol.split("/")[0])
        except ValueError:
            level = -1
        if level == 0:
            problems.append(
                "media volume is 0 — speech renders and plays into silence")
    held = facts.get("hold_s")
    if held:
        try:
            held_s = float(held)
            warn_s = float(facts.get("hold_warn_s", "300"))
        except ValueError:
            held_s, warn_s = 0.0, 0.0
        if warn_s > 0 and held_s > warn_s:
            who = facts.get("hold_src", "external")
            problems.append(
                f"external hold has stood for {held_s / 60:.0f}m ({who}) — "
                "music is ducked and speech paused; a release probably never "
                "arrived. `media-call-guard --release` lifts it")
    block = facts.get("mic_block", "")
    if block.startswith("loose:"):
        problems.append(
            f"the microphone block is not in force ({block[6:]}) — speech will "
            "be paused every time the phone's recogniser opens the mic. The "
            "mic-block service re-applies it; check that it is running")
    elif block.startswith("unknown:"):
        # Deliberately not a problem. `appops` needs a shell, the phone only
        # has one while Wireless debugging is on, and a network David does not
        # trust is a good reason for it to be off. Reported as a fact so the
        # blindness is visible — but a health flag raised on "cannot see"
        # stands for as long as the phone is away from a trusted wifi, and it
        # costs the whole status line, which is where a person watches speech.
        # The block reverting has a symptom that IS visible without a shell:
        # the hold rate below, which the companion publishes and which the app
        # only calls a problem at a rate no person produces.
        # The fact itself is published (`mic_block=unknown:…`, carrying what
        # was last seen and when), so the blindness is on the selfcheck for
        # anyone reading it. It just is not a fault.
        pass
    rate = facts.get("dictation_rate")
    # Only a complaint when the block is not known to be holding. A high rate
    # with the block in force is a person using their microphone — dictating,
    # or a language app that opens it every forty seconds — and Sam waiting for
    # them is the feature working, not a fault to report.
    if rate and not block == "held":
        problems.append(rate)
    quiet = facts.get("mic_detect_quiet_s")
    if quiet:
        import os as _os
        try:
            limit = float(_os.environ.get("MEDIA_MIC_DETECT_QUIET_MAX_S", "86400"))
        except ValueError:
            limit = 86400.0
        try:
            quiet_s = float(quiet)
        except ValueError:
            quiet_s = 0.0
        if limit > 0 and quiet_s > limit:
            ever = facts.get("mic_detect_last_external_s")
            if ever:
                when = f"last fired {float(ever) / 3600:.0f}h ago"
            elif facts.get("mic_detect_last_hold_s"):
                # A hold, but a typed one — worth saying, so the reader doesn't
                # go looking for a trigger event that never happened.
                typed = float(facts["mic_detect_last_hold_s"]) / 3600
                when = f"never fired; a hold was typed {typed:.0f}h ago"
            else:
                when = "never fired"
            problems.append(
                f"mic-detect quiet for {quiet_s / 3600:.0f}h ({when}) — the "
                "external hold trigger may be dead; speech barge-in fails silently"
            )
    return problems


def repo_note(repo: str, local_head: str, local_branch: str,
              facts: "dict[str, str]") -> "tuple[Optional[str], bool]":
    """How a host's checkout of `repo` compares to ours: (note, counts_as_skew).

    A host parked on a feature branch is not stale — it is somewhere else on
    purpose. Reporting that as skew on every run is noise you learn to scroll
    past, and a health check you scroll past is no health check, so say where
    it is and don't count it. Only a host on the SAME branch at a different
    commit is behind.
    """
    remote_head = facts.get(f"{repo}-head", "")
    if not remote_head or remote_head == local_head:
        return None, False
    remote_branch = facts.get(f"{repo}-branch", "")
    if remote_branch and local_branch and remote_branch != local_branch:
        return f"{repo} on branch {remote_branch}", False
    return f"{repo} skewed: {remote_head[:7]}", True


def parse_selfcheck(text: str) -> "dict[str, str]":
    """Parse `media selfcheck` output (key=value lines) into a dict."""
    facts = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        facts[k.strip()] = v.strip()
    return facts


def cmd_restart_services(a) -> int:
    """Restart the services this checkout owns, so a `git pull` actually lands.

    The counterpart to selfcheck's service enumeration, and deliberately built
    on the same two functions: anything doctor is willing to call "down" here
    is something this command is willing to restart, and nothing else. The
    service dir also holds sshd and snapclient, which agent-media has no
    business bouncing.

    A `down` file is runit's standing "leave this stopped" marker, so a parked
    service is reported and left alone — restarting it would override a
    decision someone made on purpose.
    """
    import subprocess
    from pathlib import Path

    root = _checkout_root()
    svdir = _sv_dir()
    services = ([(n, s, "runit") for n, s in _runit_service_states(root)]
                + [(n, s, "systemd") for n, s in _systemd_service_states()])
    if not services:
        print("no services owned by this checkout")
        return 0

    rc = 0
    for name, state, kind in services:
        if state == "parked":
            print(f"{name}: parked, left alone")
            continue
        if getattr(a, "dry_run", False):
            print(f"{name}: would restart ({kind}, currently {state})")
            continue
        if kind == "runit":
            cmd = ["sv", "restart", str(Path(svdir) / name)]
            env = {**os.environ, "SVDIR": str(svdir)}
        else:
            cmd = ["systemctl", "--user", "restart", f"{name}.service"]
            env = dict(os.environ)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=60, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"{name}: restart failed: {e}")
            rc = 1
            continue
        detail = (r.stdout or r.stderr or "").strip().replace("\n", " ")
        if r.returncode == 0:
            print(f"{name}: restarted{f' ({detail})' if detail else ''}")
        else:
            print(f"{name}: restart FAILED: {detail or r.returncode}")
            rc = 1
    return rc


def cmd_recent(a) -> int:
    """What's been played lately, newest first, across every channel.

    `media history` is the speech channel's own view — clips, with their text
    and their replay ids. This is the media view: what went on the music and
    book channels, and (with `--channel speech`) what was said, in one list.
    """
    rows = StateStore().recent_history(sink=a.channel or None, limit=a.n)
    if a.json:
        print(json.dumps(rows, default=str))
        return 0
    if not rows:
        where = f" on {a.channel}" if a.channel else ""
        print(f"nothing played yet{where}")
        return 0
    now = time.time()
    for r in rows:
        label = (r.get("text") or "").strip().splitlines()[0] if r.get("text") else ""
        label = label or _recent_label(r.get("uri") or "")
        ct = r.get("content_type") or ""
        if a.lines:
            # display<TAB>uri, for a picker that will play the second field.
            print(f"{r['sink']}  {label}\t{r.get('uri') or ''}")
            continue
        print(f"{_ago(now - float(r.get('started_at') or now)):>5}  "
              f"{r['sink']:<6} {ct:<9} {label}")
    return 0


def _recent_label(uri: str) -> str:
    """A readable name for a URI with no title recorded beside it.

    Most rows have no title: the play commands know a URI and nothing more
    until the renderer loads it. A bare `mpv:https://www.youtube.com/watch?v=…`
    is unreadable in a list, so show the tail that identifies it.
    """
    u = uri.split(":", 1)[-1] if uri.startswith(("yt:", "mpv:", "local:")) else uri
    if "youtube.com/watch" in u or "youtu.be/" in u:
        from .sinks.music_fetch import _WATCH_ID_RE
        m = _WATCH_ID_RE.search(u)
        if m:
            return f"youtube:{m.group(1)}"
    # Drop the query and fragment before taking the tail. A signed URL — an
    # Audiobookshelf download, an S3 link — carries a few hundred characters of
    # signature after the `?`, and one such row filled the whole listing.
    from urllib.parse import unquote

    u = u.split("#", 1)[0].split("?", 1)[0]
    tail = unquote(u.rstrip("/").rsplit("/", 1)[-1]) or u
    return tail if len(tail) <= 70 else tail[:69] + "…"


def _ago(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def cmd_share(a) -> int:
    """Play a shared link on whichever channel suits it.

    The Android share sheet reaches this through the on-device listener
    (`media-share`), but it is a first-class command in its own right: paste a
    link and it lands on the right channel without you deciding which.
    """
    from . import share as sharemod

    text = a.text if a.text is not None else sys.stdin.read()
    try:
        url, verdict = sharemod.share(
            text, channel=a.channel, content_type=a.as_type,
            where=a.where, probe_timeout=a.timeout, do_probe=not a.no_probe)
    except sharemod.ShareError as e:
        if a.json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"media share: {e}", file=sys.stderr)
        return 2
    if not a.json:
        print(verdict.line())
        return 0 if a.dry_run else sharemod.dispatch(url, verdict, where=a.where)
    # `--json` is a one-line contract, so the dispatched command's own chatter
    # goes to stderr rather than interleaving with it.
    rc = 0
    if not a.dry_run:
        with contextlib.redirect_stdout(sys.stderr):
            rc = sharemod.dispatch(url, verdict, where=a.where)
    print(json.dumps({"ok": rc == 0, "url": url, "channel": verdict.channel,
                      "content_type": verdict.content_type,
                      "title": verdict.title, "reason": verdict.reason,
                      "played": not a.dry_run}))
    return rc


def cmd_selfcheck(a) -> int:
    """Report this host's install health as key=value lines."""
    print(SELFCHECK_SENTINEL)
    try:
        facts = selfcheck_facts()
    except Exception as e:  # noqa: BLE001
        print(f"error={e}")
        return 1
    for k, v in facts.items():
        print(f"{k}={v}")
    problems = health_problems(facts)
    for p in problems:
        print(f"problem={p}")
    return 1 if problems else 0


# One remote command per host: the selfcheck when it's available, plus the git
# revisions doctor has always compared. The fallbacks matter — a host whose
# install is dead can't run `media selfcheck` to say so, which is precisely the
# case worth shouting about, and a host on an older agent-media doesn't have the
# subcommand at all yet must not be reported as broken.
_REMOTE_PROBE = r"""
out=$(media selfcheck 2>/dev/null)
case "$out" in
  *selfcheck=1*) printf '%s\n' "$out" ;;
  *) V=$HOME/projects/agent-media/.venv/bin/python
     if [ -x "$V" ] && "$V" -c 'import agent_media_core' 2>/dev/null; then
       echo selfcheck=unsupported
     elif command -v media >/dev/null 2>&1 || [ -x "$V" ]; then
       echo selfcheck=broken
     else
       echo selfcheck=absent
     fi ;;
esac
"""


def _remote_probe() -> str:
    """The probe, with one head/branch pair per configured repo appended.

    Generated rather than written out because the repo list is configuration
    now; a literal here would silently disagree with `skew_repos()` the moment
    anyone changed it, and the disagreement would present as a host that is
    never stale.
    """
    lines = [_REMOTE_PROBE]
    for name, rel in skew_repos():
        d = f'"$HOME/{rel}"'
        lines.append(
            f"git -C {d} rev-parse HEAD 2>/dev/null | sed 's/^/{name}-head=/'")
        lines.append(
            f"git -C {d} branch --show-current 2>/dev/null "
            f"| sed 's/^/{name}-branch=/'")
    return "\n".join(lines) + "\n"


# Deploy a skewed host and put the new code into service, in that order: a
# `git pull` alone updates the files an editable install reads, but every
# long-running service is still executing the code it imported at start. A host
# that pulled and didn't restart reads as fixed to the next doctor run (its HEAD
# matches) while continuing to run the stale code — the exact silent-wrong-code
# state the ledger exists to catch.
#
# Restarting is `media restart-services`, invoked AFTER the pull, so the pull
# deploys the very code that knows which services this checkout owns. That also
# means a host too old to have the subcommand gains it in the same round trip.
#
# Nothing here is forced. A dirty checkout is left alone and reported: an
# unattended `git stash`/`reset` on a host David may be mid-edit on is a worse
# outcome than a warning that stays up.
_REMOTE_FIX = r"""
for d in "$@"; do
  [ -d "$d/.git" ] || continue
  n=${d##*/}
  if [ -n "$(git -C "$d" status --porcelain 2>/dev/null)" ]; then
    echo "fix=$n skipped: uncommitted changes"
    continue
  fi
  b=$(git -C "$d" rev-parse --short HEAD 2>/dev/null)
  # Bounded, because the interesting failure is not a refusal but a hang: a
  # network that accepts the TCP connection and then drops the TLS handshake
  # leaves git waiting, the ssh call burns its whole budget, and the host is
  # reported "unreachable" when it is sitting right there — only its git
  # remote is out of reach. `timeout` exits 124 for that, which is the one
  # case worth naming.
  if command -v timeout >/dev/null 2>&1; then
    out=$(timeout 90 git -C "$d" pull --ff-only 2>&1); rc=$?
  else
    out=$(git -C "$d" pull --ff-only 2>&1); rc=$?
  fi
  if [ "$rc" = 0 ]; then
    a=$(git -C "$d" rev-parse --short HEAD 2>/dev/null)
    if [ "$b" = "$a" ]; then
      echo "fix=$n already at $a"
    else
      echo "fix=$n pulled $b -> $a"
    fi
  elif [ "$rc" = 124 ]; then
    echo "fix=$n FAILED: no route to its git remote (pull timed out at $b)"
  else
    echo "fix=$n FAILED: $(printf '%s' "$out" | tr '\n' ' ')"
  fi
done
if out=$(media restart-services 2>&1); then
  printf '%s\n' "$out" | sed 's/^/restart=/'
else
  printf 'restart=FAILED: %s\n' "$(printf '%s' "$out" | tr '\n' ' ')"
fi
"""


def _remote_fix() -> str:
    """`_REMOTE_FIX` with the configured repo directories substituted in.

    Quoted per-entry rather than joined raw: these become a `for d in ...` list
    in sh, so an unquoted path with a space would silently split into two
    directories that do not exist, and the loop would report both as absent.

    Passed via `set --` and iterated as `"$@"`, rather than pasted into the
    `for` list directly. Two reasons, and the tests check a real `sh -n`:
    with no repos configured -- a legitimate "skew monitoring off" -- a direct
    paste produces `for d in ; do`, a syntax error that takes the whole remote
    script down, whereas `set --` with no arguments simply leaves zero
    positional parameters. And a plain variable would not do: `VAR="a" "b"`
    assigns the first word and then tries to EXECUTE the second.
    """
    dirs = " ".join(f'"$HOME/{rel}"' for _, rel in skew_repos())
    return f"set -- {dirs}\n" + _REMOTE_FIX



def _local_repo_state(repos: "list[str]", echo=True) -> "tuple[dict, dict]":
    """(head, branch) per repo for THIS host — what every other host is judged
    against."""
    import subprocess
    from pathlib import Path
    heads, branches = {}, {}
    for r in repos:
        path = str(Path.home() / (dict(skew_repos()).get(r) or f"projects/{r}"))
        try:
            heads[r] = subprocess.run(
                ["git", "-C", path, "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True
            ).stdout.strip()
            if echo:
                print(f"local {r:12}: {heads[r][:7]}")
        except (OSError, subprocess.CalledProcessError):
            continue
        try:
            branches[r] = subprocess.run(
                ["git", "-C", path, "branch", "--show-current"],
                capture_output=True, text=True, check=True
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    return heads, branches


def _scan_local() -> "list[str]":
    """This host's own problems. `doctor` is usually run from the machine doing
    the deploying, and its own install can rot exactly like a remote one."""
    print("checking local...", end="", flush=True)
    try:
        problems = health_problems(selfcheck_facts())
    except Exception as e:  # noqa: BLE001
        problems = [f"selfcheck failed: {e}"]
    print(" " + "; ".join(f"[{p}]" for p in problems) if problems else " ok")
    return problems


def _scan_hosts(hosts: "list[str]", local_hashes: dict,
                local_branches: dict) -> "tuple[list, list, list]":
    """Probe each host once: (skewed, unhealthy, unreachable)."""
    import subprocess
    skewed, unhealthy, unreachable = [], [], []
    for host in hosts:
        print(f"checking {host}...", end="", flush=True)
        notes = []
        host_skewed = False
        try:
            res = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
                 "sh"], input=_remote_probe(),
                capture_output=True, text=True, timeout=45)
        except Exception:  # noqa: BLE001
            print(" unreachable")
            unreachable.append(host)
            continue
        if res.returncode != 0 and not res.stdout.strip():
            print(" unreachable")
            unreachable.append(host)
            continue

        facts = parse_selfcheck(res.stdout)
        for r, l_hash in local_hashes.items():
            note, is_skew = repo_note(r, l_hash, local_branches.get(r, ""), facts)
            if note:
                notes.append(note)
            host_skewed = host_skewed or is_skew
        problems = health_problems(facts)
        notes.extend(problems)

        if notes:
            print(" " + " ".join(f"[{n}]" for n in notes))
        else:
            print(" ok")
        if host_skewed:
            skewed.append(host)
        if problems:
            unhealthy.append(host)
    return skewed, unhealthy, unreachable


def fix_targets(skewed: "list[str]", unhealthy: "list[str]") -> "list[str]":
    """Which hosts `--fix` may deploy to: stale ones, and only those.

    A host in the ledger with `!` is not behind, it is broken — a dead install,
    services down, a crash loop. `git pull` does not fix any of those, and
    restarting a crash-looping service just spins it faster while the output
    scrolls past claiming work was done. Those want a person; skew wants a
    pull. Keeping the automatic path to the second kind only is what makes it
    safe to run without reading first.
    """
    return [h for h in skewed if h not in unhealthy and h != "local"]


def _fix_host(host: str) -> "tuple[bool, list[str]]":
    """Deploy one host. (ok, report lines)."""
    import subprocess
    try:
        res = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, "sh"],
            input=_remote_fix(), capture_output=True, text=True, timeout=300)
    except Exception as e:  # noqa: BLE001
        return False, [f"unreachable while fixing: {e}"]
    lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    ok = res.returncode == 0 and not any("FAILED" in ln for ln in lines)
    if not lines:
        lines = [(res.stderr or "").strip() or "no output"]
    return ok, lines


def _write_ledger(hosts: "list[str]", skewed: "list[str]",
                  unhealthy: "list[str]", unreachable: "list[str]") -> int:
    """Publish the verdict the status bar reads."""
    from pathlib import Path
    # One line per troubled host. A `!` suffix marks "running, but not well" so
    # the status bar can distinguish a stale host from a broken one at a glance.
    entries = [f"{h}!" for h in unhealthy] + [h for h in skewed if h not in unhealthy]
    d = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    ledger = d / "agent-media" / "version-skew.log"
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        if entries:
            # Stamp the local revisions this verdict was formed against, so a
            # reader can tell a current judgement from one that was overtaken
            # by a deploy (see _local_head_sig).
            ledger.write_text(
                f"# judged {_local_head_sig()}\n" + "\n".join(entries) + "\n")
            bits = []
            if skewed:
                bits.append(f"{len(skewed)} skewed")
            if unhealthy:
                bits.append(f"{len(unhealthy)} unhealthy")
            if unreachable:
                bits.append(f"{len(unreachable)} unreachable")
            print(f"\nwrote {' + '.join(bits)} host(s) to ledger.")
            return 1
        else:
            ledger.unlink(missing_ok=True)
            # Never claim health for a host we couldn't reach. Saying "all
            # hosts up to date and healthy" while skipping one is how the phone
            # sat six commits behind unnoticed: the name had changed, every run
            # printed "unreachable", and the summary called it a clean bill.
            #
            # Unreachable still doesn't reach the ledger, though — that drives
            # the status bar, and a laptop that is merely off is not a fault.
            # The distinction is "we didn't look", not "it's broken".
            if unreachable:
                print(f"\n{len(unreachable)} host(s) NOT CHECKED: "
                      f"{', '.join(unreachable)}")
                print(f"{len(hosts) - len(unreachable)} reachable host(s) "
                      f"up to date and healthy.")
                return 0
            print("\nall hosts up to date and healthy.")
            return 0
    except OSError as e:
        log.error("doctor: failed to write ledger: %s", e)
        return 1


def cmd_doctor(a) -> int:
    """Check agent cluster health: version skew AND whether the code can run.

    Skew alone was never enough — a host can sit on the right commit with a
    dead install (see selfcheck_facts). Both kinds of trouble land in the same
    ledger, so the status bar's ⚠ covers both.

    With `--fix`, stale hosts are then pulled and restarted and re-judged, so a
    successful deploy clears the ⚠ in the same run. Diagnosis stays the default:
    this is triggered from a keystroke, never from the status bar's background
    re-check, because a pull onto the phone is a live deploy that can cut speech
    mid-sentence — worth a person's timing, not a timer's.
    """
    # pn was missing from this list, so nothing ever looked at it — it sat on a
    # retired repo with 40 stale symlinks and doctor had no opinion.
    #
    # `p8ar` was the phone's old name; it was renamed to `p8a` in 2026-08, and
    # this list kept the old one. The name stopped resolving, every run printed
    # "unreachable", and unreachable hosts were skipped — so the phone, the host
    # that actually renders speech, went unchecked for weeks while the summary
    # said everything was healthy. It was six commits behind when this was found.
    # No default fleet: these were four of one person's machines, and an
    # unreachable stranger's host reads as a broken install. Unset means
    # "check this host only", which is right for a fresh install.
    # The fleet, in order of decreasing explicitness: the env var, then the
    # peers table, then nothing. Naming machines here is what made this
    # personal -- four of one person's hosts, and an unreachable stranger's
    # host reads as a broken install. Unset with no peers means "check this
    # host only", which is right for a fresh install.
    from .config import peer_hosts
    raw = os.environ.get("MEDIA_DOCTOR_HOSTS")
    hosts = raw.split() if raw is not None else peer_hosts()
    repos = _skew_repo_names()

    local_hashes, local_branches = _local_repo_state(repos)
    unhealthy = ["local"] if _scan_local() else []
    skewed, host_unhealthy, unreachable = _scan_hosts(
        hosts, local_hashes, local_branches)
    unhealthy += host_unhealthy

    targets = fix_targets(skewed, unhealthy)
    if targets and not getattr(a, "fix", False):
        # Named on its own line so the popup can offer the fix without
        # re-deriving which hosts qualify.
        print(f"\nfixable by pull: {', '.join(targets)}")

    if getattr(a, "fix", False):
        if not targets:
            print("\nnothing to fix: no host is merely behind.")
        else:
            print(f"\nfixing {', '.join(targets)} ...")
            failed_fix = []
            for host in targets:
                ok, lines = _fix_host(host)
                for ln in lines:
                    print(f"  {host}: {ln}")
                if not ok:
                    print(f"  {host}: fix incomplete — needs a look")
                    failed_fix.append(host)
            # Re-judge only what we touched; the other hosts' verdicts from the
            # scan above are still current, and a second full sweep would spend
            # four more ssh round trips to re-learn them.
            print()
            re_skewed, re_unhealthy, re_unreachable = _scan_hosts(
                targets, local_hashes, local_branches)
            skewed = [h for h in skewed if h not in targets] + re_skewed
            unhealthy = [h for h in unhealthy if h not in targets] + re_unhealthy
            unreachable = [h for h in unreachable if h not in targets] + re_unreachable
            # A host that is behind AND could not pull is not "merely behind"
            # any more: the automatic path has been tried and did not work, so
            # it wants a person — which is exactly what `!` means to the status
            # bar, and what keeps the next --fix from silently retrying a pull
            # that cannot succeed (a phone on a network that blocks its git
            # remote will fail this way every hour, forever).
            for host in failed_fix:
                if host not in unhealthy:
                    unhealthy.append(host)

    return _write_ledger(hosts, skewed, unhealthy, unreachable)


# --- CLI -------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="media", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="one-line speech progress (for status bar)")
    s.add_argument("--width", type=int, default=12)
    s.add_argument("--show-idle", action="store_true",
                   help="emit '○' when idle instead of empty")
    s.add_argument("--no-bar", action="store_true",
                   help="show only the times (no progress bar)")
    s.add_argument("--title", nargs="?", type=_client_width, const=80,
                   default=None, metavar="CLIENT_WIDTH",
                   help="render the whole status (times + subject title) as one "
                        "background-progress bar; the title width auto-derives "
                        "from CLIENT_WIDTH — pass tmux #{client_width} so it "
                        "fits any screen (default 80; EXPERIMENTAL)")
    s.add_argument("--now-playing", action="store_true",
                   help="append what the music or book channel is playing, "
                        "scrolling if it doesn't fit — so one process renders "
                        "the whole status-bar segment (empty when idle)")
    s.set_defaults(func=cmd_status)

    ps = sub.add_parser("popup-status",
                        help="speech status + subject label + mute-count in one "
                             "shot (3 lines) — the popup's per-refresh aggregate")
    ps.add_argument("--width", type=int, default=12)
    ps.add_argument("--show-idle", action="store_true")
    ps.add_argument("--no-bar", action="store_true")
    ps.add_argument("--act", nargs=argparse.REMAINDER, default=None,
                    help="run this media subcommand in-process before emitting "
                         "the status (fuses the popup's action+redraw into one "
                         "spawn); its stdout is prepended as a leading line. "
                         "Must be last: everything after --act is the action.")
    ps.set_defaults(func=cmd_popup_status)

    sub.add_parser("now", help="text currently being spoken").set_defaults(func=cmd_now)
    s = sub.add_parser("now-pane",
                       help="title of the pane that produced the now-playing speech")
    s.add_argument("--width", type=int,
                   help="marquee-window the title to WIDTH columns (scrolls one "
                        "column per call; used by the popup border title)")
    s.add_argument("--session-only", action="store_true")
    s.set_defaults(func=cmd_now_pane)
    sub.add_parser("goto-pane",
                   help="focus the pane that produced the now-playing speech"
                   ).set_defaults(func=cmd_goto_pane)
    sub.add_parser("goto-track",
                   help="focus the ncmpcpp pane and jump to the now-playing song"
                   ).set_defaults(func=cmd_goto_track)
    sub.add_parser("open-ncmpcpp",
                   help="open a new tmux window running ncmpcpp"
                   ).set_defaults(func=cmd_open_ncmpcpp)
    sub.add_parser("goto-book",
                   help="focus the book's mpvc-tui player pane"
                   ).set_defaults(func=cmd_goto_book)
    sub.add_parser("open-mpvc",
                   help="open a new tmux window running mpvc-tui for the book"
                   ).set_defaults(func=cmd_open_mpvc)
    p_ac = sub.add_parser("ask-context",
                          help="print a one-line 'what I'm listening to' blurb "
                               "for CHANNEL (popup `a` context seed)")
    p_ac.add_argument("--channel", default="speech",
                      choices=POPUP_CHANNELS,
                      help="speech (default) / music / book")
    p_ac.set_defaults(func=cmd_ask_context)
    p_op = sub.add_parser("open-pi",
                          help="open a fresh pi window seeded with my listening "
                               "context + a question (popup `a`)")
    p_op.add_argument("--channel", default="speech",
                      choices=POPUP_CHANNELS,
                      help="which channel's context to seed with")
    p_op.add_argument("question", nargs="?", default="",
                      help="the question to ask (context is prepended)")
    p_op.set_defaults(func=cmd_open_pi)
    p_ask = sub.add_parser("ask",
                           help="put a question to the conversation that has "
                                "been speaking here (see `ask --status`)")
    p_ask.add_argument("question", nargs="?", default="",
                       help="the question; the listening context is prepended")
    p_ask.add_argument("--channel", default="speech", choices=POPUP_CHANNELS,
                       help="which channel's context to prepend")
    p_ask.add_argument("--session", default="",
                       help="address this conversation instead of the newest")
    p_ask.add_argument("--via", default="media ask",
                       help="how the line is tagged in the pane")
    p_ask.add_argument("--status", action="store_true",
                       help="say which conversation would be asked, and stop")
    p_ask.add_argument("--json", action="store_true", help="machine-readable")
    p_ask.add_argument("--dry-run", action="store_true",
                       help="print the line that would be typed")
    p_ask.add_argument("--no-context", action="store_true",
                       help="send the question alone")
    p_ask.add_argument("--no-verify", action="store_true",
                       help="do not wait for the transcript to confirm it")
    p_ask.add_argument("--no-new", action="store_true",
                       help="refuse when nothing is listening instead of "
                            "starting a conversation about it")
    p_ask.set_defaults(func=cmd_ask)
    sub.add_parser("speech-web",
                   help="print/open the visual canvas URL for speech"
                   ).set_defaults(func=cmd_speech_web)
    sub.add_parser("book-web",
                   help="print/open the mpvc-web browser control URL for the book"
                   ).set_defaults(func=cmd_book_web)
    sub.add_parser("music-web",
                   help="print/open the Mopidy-Iris web UI URL for music"
                   ).set_defaults(func=cmd_music_web)
    p_os = sub.add_parser("open-session",
                          help="open a window resuming a Claude Code session")
    p_os.add_argument("session", help="Claude Code session id to resume")
    p_os.set_defaults(func=cmd_open_session)
    sub.add_parser("text", help="spoken text (now-playing or latest history)").set_defaults(func=cmd_text)

    sub.add_parser("highlight-toggle",
                    help="toggle auto-highlight on/off (popup v key)"
                    ).set_defaults(func=cmd_highlight_toggle)

    sub.add_parser("highlight-now",
                    help="force highlight this turn past the keystroke-skip "
                         "until you type again (tmux prefix V)"
                    ).set_defaults(func=cmd_highlight_now)

    s = sub.add_parser("current-sentence",
                        help="active sentence (for status-line karaoke indicator)")
    s.add_argument("--width", type=int, default=80,
                    help="max chars before truncation (default 80)")
    s.add_argument("--follow", action="store_true",
                    help="the follow-along row: silent unless follow-along is "
                         "on (popup `v`), and says so when nothing is playing")
    s.add_argument("--row", type=int, default=None,
                    help="print only this wrapped line (0-based) — one status "
                         "row per --row, so a long sentence wraps rather than "
                         "truncating")
    s.add_argument("--rows", type=int, default=0,
                    help="how many rows the caller laid out, so the last one "
                         "can end with an ellipsis")
    s.set_defaults(func=cmd_current_sentence)

    s = sub.add_parser("follow",
                       help="follow-along pane: the reply being spoken, with "
                            "the current sentence marked (works where the "
                            "copy-mode highlight can't — fullscreen TUIs)")
    s.add_argument("--interval", type=float, default=0.2,
                   help="seconds between repaints (default 0.2)")
    s.add_argument("--width", type=int, default=None,
                   help="columns (default: the terminal's)")
    s.add_argument("--height", type=int, default=None,
                   help="rows (default: the terminal's)")
    s.add_argument("--once", action="store_true",
                   help="print one frame and exit")
    s.set_defaults(func=cmd_follow)
    sub.add_parser("toggle", help="play/pause").set_defaults(func=cmd_toggle)
    s = sub.add_parser("speech-flush",
                       help="drop every queued/pending reply; the clip "
                            "currently speaking plays out")
    s.set_defaults(func=cmd_speech_flush)

    s = sub.add_parser("speech-hold",
                       help="hold NEW speech playback for N seconds "
                            "(bounded by MEDIA_SPEECH_HOLD_MAX_S, default "
                            "300); no args: show; --release: lift now")
    s.add_argument("seconds", nargs="?", type=float, default=None)
    s.add_argument("--release", action="store_true")
    s.add_argument("--owner", default=None,
                   help="whose hold this is (default: MEDIA_SPEECH_HOLD_OWNER, "
                        "else this tmux pane). Speech stays held while any "
                        "owner holds it, and --release only lifts your own.")
    s.add_argument("--all", action="store_true",
                   help="with --release: lift EVERY hold, including other "
                        "sessions'. The escape hatch for a stuck channel.")
    s.set_defaults(func=cmd_speech_hold)

    s = sub.add_parser("converse-reply",
                       help="answer a waiting `converse` with text — for an "
                            "answerer with no mic on the HA Assist path "
                            "(another agent, over the relay)")
    s.add_argument("text", nargs="?", default=None,
                   help="the reply to hand to the waiting converse call")
    s.add_argument("--pending", action="store_true",
                   help="print the question awaiting an answer, if any "
                        "(exit 3 when nothing is armed)")
    s.add_argument("--json", action="store_true",
                   help="with --pending: the whole record (text, asked_at, "
                        "timeout_s) instead of just the question")
    s.add_argument("--wait", type=float, default=0.0, metavar="SECONDS",
                   help="with --pending: block until a question arms, rather "
                        "than racing it and reporting none")
    s.set_defaults(func=cmd_converse_reply)

    sub.add_parser("pause").set_defaults(func=cmd_pause)
    sub.add_parser("resume").set_defaults(func=cmd_resume)
    sub.add_parser("stop").set_defaults(func=cmd_stop)
    sub.add_parser("mute", help="toggle mute").set_defaults(func=cmd_mute)

    p_mp = sub.add_parser("mute-pane",
                          help="durable per-pane (or --session) speech mute")
    p_mp.add_argument("--pane",
                      help="target tmux pane id (default: $TMUX_PANE / speaking pane)")
    p_mp.add_argument("--session", action="store_true",
                      help="target the whole tmux session owning the pane")
    p_mp.add_argument("--current", action="store_true",
                      help="target the currently/last speaking pane (legacy)")
    p_mp.add_argument("--subject", action="store_true",
                      help="target the popup subject: now-playing pane, else caller")
    p_mp.add_argument("state", nargs="?", choices=["on", "off", "toggle"],
                      default="toggle")
    p_mp.set_defaults(func=cmd_mute_pane)
    sub.add_parser("mute-status",
                   help="list per-pane/session speech mutes"
                   ).set_defaults(func=cmd_mute_status)
    sub.add_parser("mute-count",
                   help="print the total number of muted panes+sessions (else nothing)"
                   ).set_defaults(func=cmd_mute_count)
    p_pm = sub.add_parser("pane-muted",
                          help="print '1' if the subject pane is muted (popup indicator)")
    p_pm.add_argument("--pane", help="pane id to check (default: popup subject)")
    p_pm.set_defaults(func=cmd_pane_muted)

    s = sub.add_parser("seek", help="seek relative seconds (+/-)")
    s.add_argument("secs", type=float)
    s.set_defaults(func=cmd_seek)

    s = sub.add_parser("volume", help="adjust volume by delta")
    s.add_argument("delta", type=int)
    s.set_defaults(func=cmd_volume)

    s = sub.add_parser(
        "speed",
        help="set playback speed: a factor, 'reset', or 'up'/'down' (±0.1)")
    s.add_argument("factor")
    s.set_defaults(func=cmd_speed)

    s = sub.add_parser("jump", help="seek to start|end of the current clip")
    s.add_argument("where", choices=("start", "end"))
    s.set_defaults(func=cmd_jump)

    s = sub.add_parser(
        "skip", help="step the reader by a sentence/paragraph (popup h/l/H/L)")
    s.add_argument("--to", type=int, default=None,
                   help="absolute sentence index to play from (ignores --dir)")
    s.add_argument("--unit", choices=("sentence", "paragraph"),
                   default="sentence")
    s.add_argument("--dir", type=int, default=1, help="-1 back, 1 forward")
    s.add_argument("--seek-fallback", type=float, default=5.0,
                   help="seconds to time-seek when there's no sentence sequence")
    s.set_defaults(func=cmd_skip)

    s = sub.add_parser("replay", help="replay the Nth most recent clip (1=latest)")
    s.add_argument("index", nargs="?", type=int, default=1)
    s.add_argument("--id", type=int, default=None,
                   help="replay by stable history id instead (see "
                        "'history --lines'; used by the clip browser)")
    s.set_defaults(func=cmd_replay)

    s = sub.add_parser("replay-prev", help=argparse.SUPPRESS)  # popup < (restart-first)
    s.add_argument("--idx", type=int, default=1)
    s.set_defaults(func=cmd_replay_prev)

    sub.add_parser(
        "replay-at-cursor",
        help="replay the clip at the copy-mode cursor (popup p)"
        ).set_defaults(func=cmd_replay_at_cursor)

    s = sub.add_parser("replay-track", help=argparse.SUPPRESS)
    s.add_argument("--sentences", default="")
    s.add_argument("--offsets", default="")
    s.add_argument("--pane", default="")
    s.add_argument("--durations", default="")
    s.set_defaults(func=cmd_replay_track)

    s = sub.add_parser("errors", help="recent errors from every component")
    s.add_argument("n", nargs="?", type=int, default=20)
    s.add_argument("--component", default="",
                   help="filter to one component (intake, coordinator, "
                        "voice-bridge, hook-claude-code)")
    s.add_argument("--since", type=int, default=0, metavar="MIN",
                   help="only errors from the last MIN minutes")
    s.add_argument("--verbose", "-v", action="store_true",
                   help="include the extras payload")
    s.set_defaults(func=cmd_errors)

    s = sub.add_parser("history", help="list recent spoken clips")
    s.add_argument("n", nargs="?", type=int, default=20)
    s.add_argument("--session", action="store_true",
                   help="only the popup's anchor conversation (all clips "
                        "when none resolves)")
    s.add_argument("--lines", action="store_true",
                   help="print display<TAB>history-id rows for an external "
                        "picker")
    s.add_argument("--group", action="store_true",
                   help="with --lines: group clips under per-conversation "
                        "▪window headers, tmux choose-tree style (the clip "
                        "browser's ^a view)")
    s.add_argument("--json", action="store_true",
                   help="print the clip-picker rows as JSON — what a render "
                        "host asks its origin for")
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("say", help="speak text (stdin if no arg)")
    s.add_argument("text", nargs="?")
    s.add_argument("--urgent", action="store_true",
                   help="barge in: interrupt this session's current speech and "
                        "jump its queue, then let the interrupted message resume")
    s.add_argument("--supersede", action="store_true",
                   help="like --urgent, but DROP the same-session messages this "
                        "one interrupts/precedes instead of resuming them")
    s.add_argument("--alert", action="store_true",
                   help="unattended alert — nobody asked for it (a timer, a "
                        "watcher). Held, not queued, while the target device's "
                        "ringer is on silent; still written to history")
    s.set_defaults(func=cmd_say)

    s = sub.add_parser("bookmark", help="bookmark current media position")
    s.add_argument("note", nargs="?", help="optional note")
    s.add_argument("--range-end", action="store_true", help="finish a range from the last bookmark")
    s.add_argument("--slot", default="", help="named bookmark register (e.g. 1, 2) for overlapping ranges")
    s.add_argument("--channel", choices=("music", "book", "speech"), default="music")
    s.set_defaults(func=cmd_bookmark)

    s = sub.add_parser("bookmarks", help="list media bookmarks")
    s.add_argument("limit", nargs="?", default="20")
    s.add_argument("--channel", choices=("music", "book", "speech"), default=None)
    s.add_argument("--json", action="store_true")
    s.add_argument("--pick", action="store_true",
                   help="choose with fzf and resume the selected bookmark")
    s.add_argument("--print-uri", dest="print_uri", action="store_true",
                   help="with --pick: print the URI instead of resuming")
    s.set_defaults(func=cmd_bookmarks)

    s = sub.add_parser("music", help="music control via Mopidy/MPD")
    s.add_argument("action",
                   choices=("play", "pause", "resume", "stop", "toggle",
                            "next", "prev", "status", "now", "now-status",
                            "seek", "volume", "speed", "bookmark",
                            "bookmarks", "chapters", "chapter"))
    s.add_argument("uri", nargs="?",
                   help="for 'play': Mopidy URI (e.g. yt:https://...); "
                        "for 'seek': time H:MM:SS (absolute) or +90/-5:00 "
                        "(relative); for 'volume': ±delta; for 'speed': "
                        "rate 0.25–4 (absolute), ±delta, 'up'/'down' "
                        "(ladder), 'reset', or empty to show the current "
                        "rate; for 'bookmark': optional note; for "
                        "'bookmarks': optional limit; for 'chapter': "
                        "1-based chapter number (see 'chapters')")
    s.add_argument("--lines", action="store_true",
                   help="for 'chapters': print display<TAB>number rows "
                        "for an external picker")
    s.add_argument("--width", type=int, default=12,
                   help="for 'status': progress-bar width")
    s.add_argument("--show-idle", action="store_true",
                   help="for 'status': emit '○' when idle instead of empty")
    s.add_argument("--no-bar", action="store_true",
                   help="for 'status': show only the times (no progress bar)")
    s.add_argument("--json", action="store_true",
                   help="for 'status': emit a JSON object (backend, uri, "
                        "title, chapter, pos_ms, dur_ms, paused, speed, "
                        "volume, held) instead of the progress bar — for "
                        "control surfaces that need structured state")
    s.add_argument("--add", action="store_true",
                   help="for 'play': queue without clearing the playlist")
    s.add_argument("--restart-first", action="store_true",
                   help="for 'prev': restart the current track if past its "
                        "start (⏮ style; grace = MEDIA_POPUP_PREV_RESTART_S)")
    s.add_argument("--as", dest="as_type", metavar="TYPE",
                   choices=("music", "audiobook", "podcast", "dj-set",
                            "ambient"),
                   help="for 'play': interruption content type "
                        "(audiobook/podcast pause instead of duck)")
    s.add_argument("--range-end", action="store_true",
                   help="for 'bookmark': finish a range from the last bookmark")
    s.add_argument("--slot", default="",
                   help="for 'bookmark': named register (e.g. 1, 2) for overlapping ranges")
    s.add_argument("--title", default="", help=argparse.SUPPRESS)
    s.add_argument("--where", choices=("default", "auto", "local", "rooms", "phone"),
                   default="default",
                   help="for 'play': where to play — 'phone' downloads on the "
                        "phone (residential IP, dodges 403, offline) and plays "
                        "locally; 'rooms'/'local' use Mopidy; 'auto' picks phone "
                        "when it's the only listener (replaces play-music)")
    s.set_defaults(func=cmd_music)

    _add_book_parser(sub)
    _add_licence_parser(sub)

    f = sub.add_parser("focus", help="bring a channel to the front (book|music)")
    f.add_argument("channel", choices=("book", "music"))
    f.add_argument("--target", default="", help="book target; empty follows book/speech default")
    f.set_defaults(func=cmd_focus)

    search = sub.add_parser("search", help="unified search (music/book library)")
    search.add_argument("--lines", action="store_true",
                        help="print title<TAB>uri rows for an external picker")
    search.add_argument("channel", choices=("music", "book"))
    search.add_argument("query", nargs="*")
    search.set_defaults(func=cmd_search)

    abs_scan = sub.add_parser("abs-scan", help="trigger an Audiobookshelf library scan")
    abs_scan.add_argument("--target", default="",
                          help="per-target ABS library (ABS_LIBRARY_<TARGET>); empty = default")
    abs_scan.set_defaults(func=cmd_abs_scan)

    sub.add_parser("channels", help="both channels at a glance (focus, bed, what's on)"
                   ).set_defaults(func=cmd_channels)

    pc = sub.add_parser("popup-channel",
                        help="resolve (or --set) the popup's opening channel")
    pc.add_argument("--set", choices=POPUP_CHANNELS, default=None,
                    help="remember this as the last-viewed channel")
    pc.set_defaults(func=cmd_popup_channel)

    doc = sub.add_parser("doctor",
                         help="check cluster health (version skew + install/services)")
    doc.add_argument("--fix", action="store_true",
                     help="deploy the hosts that are merely behind (pull + "
                          "restart services), then re-judge them")
    doc.set_defaults(func=cmd_doctor)

    rs = sub.add_parser("restart-services",
                        help="restart the services this checkout owns")
    rs.add_argument("--dry-run", action="store_true",
                    help="name what would be restarted, touch nothing")
    rs.set_defaults(func=cmd_restart_services)

    sc = sub.add_parser("selfcheck",
                        help="report this host's install health (key=value lines)")
    sc.set_defaults(func=cmd_selfcheck)

    rc = sub.add_parser("recent",
                        help="what's been played lately, newest first "
                             "(music, book and speech in one list)")
    rc.add_argument("n", nargs="?", type=int, default=20,
                    help="how many rows (default 20)")
    rc.add_argument("--channel", choices=("music", "book", "speech"),
                    default="", help="only this channel")
    rc.add_argument("--lines", action="store_true",
                    help="display<TAB>uri rows for an external picker")
    rc.add_argument("--json", action="store_true")
    rc.set_defaults(func=cmd_recent)

    sh = sub.add_parser("share",
                        help="play a shared link on the channel that fits it")
    sh.add_argument("text", nargs="?",
                    help="a URL, or the text a share sheet sent (the first "
                         "URL in it wins); stdin when omitted")
    sh.add_argument("--channel", choices=("music", "book"), default="",
                    help="override the chosen channel")
    sh.add_argument("--as", dest="as_type", metavar="TYPE", default="",
                    choices=("music", "audiobook", "podcast", "dj-set",
                             "ambient"),
                    help="override the interruption content type")
    sh.add_argument("--where", choices=("default", "auto", "local", "rooms",
                                        "phone"),
                    default="", help="where to play it (as `media music play`)")
    sh.add_argument("--no-probe", action="store_true",
                    help="skip the yt-dlp metadata fetch and classify on the "
                         "URL alone (fast, and much less accurate)")
    sh.add_argument("--timeout", type=float, default=30.0,
                    help="seconds for the metadata fetch (default 30)")
    sh.add_argument("--dry-run", action="store_true",
                    help="print the verdict and play nothing")
    sh.add_argument("--json", action="store_true",
                    help="emit the verdict as JSON")
    sh.set_defaults(func=cmd_share)

    return p


def _licence_mod():
    """Imported on use, not at module import.

    `media` is a hot CLI — every hook invocation pays for whatever the top of
    this file imports — and the licence path is touched by a handful of
    commands. Nothing else in a `media say` should load a curve implementation.
    """
    from . import entitlements
    return entitlements


def cmd_licence_show(a) -> int:
    ent = _licence_mod()
    info = ent.status()
    if getattr(a, "json", False):
        print(json.dumps(info, indent=2, sort_keys=True))
        return 0

    print(f"tier      {info['tier']}")
    if info["valid"]:
        print(f"subject   {info['subject'] or '-'}")
        print(f"features  {', '.join(info['features']) or '-'}")
        print(f"key       {info['key_id']}")
        if info["expires_at"]:
            print("expires   " + time.strftime(
                "%Y-%m-%d", time.localtime(info["expires_at"])))
        else:
            print("expires   never")
    elif info["have_token"]:
        # The token exists and did not verify. `entitlements` already logged
        # why at warning level; say plainly here that it is not in effect,
        # because "tier free" alone reads as "no licence installed".
        print("licence   present but NOT valid — running as free tier")
    else:
        print("licence   none installed")
    print(f"file      {info['path']}")
    print(f"keys      {', '.join(info['trusted_keys']) or 'none trusted'}")
    return 0


def cmd_licence_add(a) -> int:
    ent = _licence_mod()
    token = a.token
    if token == "-":
        token = sys.stdin.read()
    token = token.strip()

    path = ent.licence_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    ent.refresh()

    # Verify what was just written rather than what was passed in, and do not
    # refuse a token that fails: a licence for a key this core does not vendor
    # yet is a real situation (an install lagging a release), and throwing the
    # user's token away is worse than keeping an inert one on disk.
    info = ent.status()
    if info["valid"]:
        print(f"licence installed: {info['tier']} — {path}")
        return 0
    print(f"licence written to {path}, but it does not verify here — "
          "running as free tier (see `media licence show`)", file=sys.stderr)
    return 1


def cmd_licence_remove(a) -> int:
    ent = _licence_mod()
    path = ent.licence_path()
    try:
        path.unlink()
    except FileNotFoundError:
        print("no licence installed")
        return 0
    ent.refresh()
    print(f"removed {path}")
    return 0


def cmd_licence_check(a) -> int:
    """Exit status only, so shell and hooks can gate on a feature."""
    ent = _licence_mod()
    ok = ent.feature_enabled(a.feature)
    if not getattr(a, "quiet", False):
        print("yes" if ok else "no")
    return 0 if ok else 1


def cmd_licence_keygen(a) -> int:
    """Developer-side: a signing key pair, printed, stored nowhere.

    There is no seller of record yet, so this exists to make the whole path
    exercisable offline. Printing the seed rather than writing it is the point
    — a private key that a CLI drops into the user's home is a private key
    that ends up in a backup.
    """
    import os as _os

    from ._ed25519 import public_key

    seed = _os.urandom(32)
    pub = public_key(seed)
    print(f"seed (PRIVATE, keep off disk)  {seed.hex()}")
    print(f"public key                     {pub.hex()}")
    print()
    print("Trust it on this host by adding to ~/.config/agent-media/config.toml:")
    print()
    print("    [licence.keys]")
    print(f'    {a.kid} = "{pub.hex()}"')
    return 0


def cmd_licence_mint(a) -> int:
    """Developer-side stub mint. The real one lives server-side and is not
    open source; this signs the same token so everything downstream is real."""
    import binascii

    ent = _licence_mod()

    raw = a.seed
    if raw.startswith("@"):
        from pathlib import Path

        raw = Path(raw[1:]).expanduser().read_text().strip()
    try:
        seed = binascii.unhexlify(raw.strip())
    except (binascii.Error, ValueError):
        print("media licence mint: --seed must be 64 hex characters",
              file=sys.stderr)
        return 2
    if len(seed) != 32:
        print("media licence mint: --seed must be 32 bytes (64 hex chars)",
              file=sys.stderr)
        return 2

    now = int(time.time())
    payload = {
        "kid": a.kid,
        "sub": a.sub,
        "tier": a.tier,
        "feat": sorted({f.strip().lower() for f in a.feature if f.strip()}),
        "iat": now,
        "exp": now + int(a.days) * 86400 if a.days else 0,
    }
    print(ent.encode(payload, seed))
    return 0


def _add_licence_parser(sub) -> None:
    """`media licence ...` — what this install has paid for.

    Deliberately a flat, boring CRUD surface. The interesting design is in
    `entitlements`; here the only judgement calls are that `add` keeps a token
    it cannot verify (an install can lag the key that signed it) and that
    `check` reports through its exit status (so a hook can gate without
    parsing).
    """
    lic = sub.add_parser("licence", aliases=["license"],
                         help="show or install the licence for paid features")
    lic.set_defaults(func=cmd_licence_show, json=False)
    ls = lic.add_subparsers(dest="licence_cmd")

    lshow = ls.add_parser("show", help="tier, features, expiry")
    lshow.add_argument("--json", action="store_true")
    lshow.set_defaults(func=cmd_licence_show)

    ladd = ls.add_parser("add", help="install a token ('-' reads stdin)")
    ladd.add_argument("token")
    ladd.set_defaults(func=cmd_licence_add)

    lrm = ls.add_parser("remove", help="delete the installed licence")
    lrm.set_defaults(func=cmd_licence_remove)

    lchk = ls.add_parser("check", help="exit 0 if a feature is licensed")
    lchk.add_argument("feature")
    lchk.add_argument("--quiet", "-q", action="store_true",
                      help="say nothing; report through the exit status")
    lchk.set_defaults(func=cmd_licence_check)

    lkg = ls.add_parser("keygen",
                        help="dev: generate a signing key pair (no mint yet)")
    lkg.add_argument("--kid", default="dev", help="key id (default dev)")
    lkg.set_defaults(func=cmd_licence_keygen)

    lmint = ls.add_parser("mint", help="dev: sign a token with a local seed")
    lmint.add_argument("--seed", required=True,
                       help="64 hex chars, or @path to a file holding them")
    lmint.add_argument("--kid", default="dev")
    lmint.add_argument("--sub", default="", help="who it is for")
    lmint.add_argument("--tier", default="plus",
                       help="free|plus|studio (a label; gate on features)")
    lmint.add_argument("--feature", action="append", default=[],
                       help="granted feature; repeatable. 'visual.*' grants a "
                            "whole heading, '*' grants everything")
    lmint.add_argument("--days", type=int, default=0,
                       help="valid for N days (default 0 = perpetual)")
    lmint.set_defaults(func=cmd_licence_mint)


def _add_book_parser(sub) -> None:
    """The `media book ...` subtree — the longform/audiobook channel.

    Mirrors `media music` but with book-shaped transport (resume bookmarks,
    skip ±s, speed) and playlists. `--target rooms|local|phone` overrides where
    the book plays; empty uses MEDIA_BOOK_DEFAULT_TARGET, then speech default.
    """
    doc = sub.add_parser("doc", help="listen to a document (docs/, org, ...)")
    doc.set_defaults(func=cmd_doc)
    d = doc.add_subparsers(dest="doc_cmd", required=True)

    dl = d.add_parser("list", help="list documents")
    dl.add_argument("--lines", action="store_true",
                    help="display<TAB>slug rows for the popup picker")
    dl.add_argument("--no-context", action="store_true",
                    help="plain global list: ignore the current directory "
                         "and tmux session")
    dl.add_argument("--all", action="store_true",
                    help="include unclarified inbox captures (a queue, not "
                         "a library — left out by default)")
    dl.add_argument("--tag", default="",
                    help="only documents carrying this filetag/keyword "
                         "(PARA membership is a tag, not a folder)")

    dp = d.add_parser("play", help="render (cached) and play on the book channel")
    dp.add_argument("name", nargs="?", default="",
                    help="slug, path, or a substring of either")
    dp.add_argument("--stdin", action="store_true",
                    help="read the text to speak from stdin (a region or a "
                         "buffer, sent by an editor) instead of a file")
    dp.add_argument("--fmt", default="md", choices=("md", "org"),
                    help="markup of the --stdin text (default md)")
    dp.add_argument("--title", default="", help="display title for --stdin")
    dp.add_argument("--target", default="", help="rooms|local|phone")
    dp.add_argument("--no-resume", action="store_true")
    dp.add_argument("--force", action="store_true",
                    help="re-render even if the cached audio is current")
    dp.add_argument("--feed", nargs="?", const="docs", default="",
                    metavar="NAME",
                    help="publish to a feed instead of playing (default "
                         "feed: docs)")

    dt = d.add_parser("text", help="print the speakable projection (no audio)")
    dt.add_argument("name")

    da = d.add_parser("agenda", help="today's agenda, spoken")
    da.add_argument("--text", action="store_true", help="print, don't play")
    da.add_argument("--target", default="", help="rooms|local|phone")
    da.add_argument("--feed", nargs="?", const="digest", default="",
                    metavar="NAME",
                    help="publish to a feed instead of playing (default "
                         "feed: digest)")

    fd = sub.add_parser("feed", help="the podcast spool (docs, talks, digest)")
    fd.set_defaults(func=cmd_feed)
    f = fd.add_subparsers(dest="feed_cmd", required=True)

    fl = f.add_parser("list", help="feeds and their episodes")
    fl.add_argument("name", nargs="?", default="", help="one feed only")

    fp = f.add_parser("publish", help="take custody of an audio file")
    fp.add_argument("name", help="feed name (docs, talks, digest, ...)")
    fp.add_argument("audio", help="the rendered file; it is copied, not moved")
    fp.add_argument("--title", default="", help="episode title")
    fp.add_argument("--guid", default="",
                    help="stable id — republishing it replaces the episode "
                         "(default: the audio's absolute path)")
    fp.add_argument("--description", default="", help="shown in the client")
    fp.add_argument("--source", default="",
                    help="where it came from: a doc path, a session id")

    fr = f.add_parser("remove", help="unpublish one episode")
    fr.add_argument("name")
    fr.add_argument("guid")

    fg = f.add_parser("gc", help="apply the retention policy")
    fg.add_argument("name", nargs="?", default="", help="one feed only")

    fs = f.add_parser("session",
                      help="publish a conversation as one chaptered episode")
    fs.add_argument("session", nargs="?", default="",
                    help="Claude session id (default: this pane's)")
    fs.add_argument("--feed", dest="name", default="",
                    help="feed to publish to (default: the conversation's "
                         "tmux workspace, or talks)")

    fq = f.add_parser("publish-quiet",
                      help="publish every conversation that has gone quiet")
    fq.add_argument("--feed", dest="name", default="",
                    help="publish everything to one feed instead of one per "
                         "workspace")
    fq.add_argument("--quiet-min", type=float, default=60.0,
                    help="minutes of silence before a conversation counts as "
                         "finished (default 60)")
    fq.add_argument("--limit", type=int, default=0,
                    help="stop after this many (0 = no limit)")

    fss = f.add_parser("sessions", help="conversations available to publish")
    fss.add_argument("--limit", type=int, default=15)

    ft = f.add_parser("tracks",
                      help="write conversations as items that GROW — one "
                           "track per turn, appended as turns land")
    ft.add_argument("--session", default="",
                    help="one conversation (Claude session id); default all "
                         "that have spoken lately")
    ft.add_argument("--since-hours", type=float, default=24.0,
                    help="only conversations with a turn this recently "
                         "(0 = every one there has ever been)")
    ft.add_argument("--no-reopen", action="store_true",
                    help="do not clear isFinished on an item that grew")

    fx = f.add_parser("xml", help="print the feed XML (does not write it)")
    fx.add_argument("name")

    fw = f.add_parser("write", help="regenerate <feed>/feed.xml from the spool")
    fw.add_argument("name")

    book = sub.add_parser("book", help="longform / audiobook channel")
    book.set_defaults(func=cmd_book)
    b = book.add_subparsers(dest="book_cmd", required=True)

    bp = b.add_parser("play", help="play longform audio (resumes by default)")
    bp.add_argument("uri", help="yt:https://..., http(s) stream, or file path")
    bp.add_argument("--no-resume", action="store_true",
                    help="start from the beginning, ignoring the bookmark")
    bp.add_argument("--start-ms", type=int, default=None,
                    help="explicit start offset in ms")
    bp.add_argument("--target", default="", help="rooms|local|phone")
    bp.add_argument("--title", default="", help=argparse.SUPPRESS)

    biy = b.add_parser("import-youtube", help="download a YouTube video/playlist into Audiobookshelf")
    biy.add_argument("uri", help="YouTube video or playlist URL")
    biy.add_argument("--target", default="", help="rooms|local|phone")
    biy.add_argument("--play", action="store_true", help="play the last fetched item when ready")

    br = b.add_parser("resume", help="resume the book (reopens the last if idle)")
    br.add_argument("--target", default="")
    b.add_parser("pause", help="pause and save the place")
    b.add_parser("stop", help="stop, saving the place to resume later")
    bn = b.add_parser("next", help="next part of the active playlist")
    bn.add_argument("--target", default="")
    bpv = b.add_parser("prev", help="previous part of the active playlist")
    bpv.add_argument("--target", default="")
    bpv.add_argument("--restart-first", action="store_true",
                     help="restart the current part if past its start (⏮ style; "
                          "grace = MEDIA_POPUP_PREV_RESTART_S)")

    bk = b.add_parser("skip", help="relative ±seconds (default +30); alias of "
                                   "`seek` with a forced-relative offset")
    bk.add_argument("secs", nargs="?", type=float, default=30.0)
    bk.add_argument("--target", default="")

    bsk = b.add_parser("seek", help="jump to a time (H:MM:SS); +/- for relative")
    bsk.add_argument("time", help="absolute 1:33:35 / 93:35 / 5615, or +90 / -5:00")
    bsk.add_argument("--target", default="")

    bs = b.add_parser("speed", help="set playback speed (factor or 'reset')")
    bs.add_argument("factor")

    bch = b.add_parser("chapters", help="list the loaded book's chapters")
    bch.add_argument("--lines", action="store_true",
                     help="print display<TAB>number rows for an external picker")
    bch.add_argument("--target", default="")
    bcn = b.add_parser("chapter", help="jump to a chapter (1-based; see 'chapters')")
    bcn.add_argument("number")
    bcn.add_argument("--target", default="")

    bbed = b.add_parser("bed", help="how music behaves under a foregrounded book")
    bbed.add_argument("mode", choices=("duck", "pause"))

    bst = b.add_parser("status", help="one-line book progress (for status bar)")
    bst.add_argument("--width", type=int, default=12)
    bst.add_argument("--show-idle", action="store_true")
    bst.add_argument("--no-bar", action="store_true")

    bnow = b.add_parser("now", help="URI of what the book channel is reading")
    bnow.add_argument("--target", default="")
    bmeta = b.add_parser("meta", help="book title/chapter/URI for popup")
    bmeta.add_argument("--target", default="")

    bbm = b.add_parser("bookmark", help="bookmark current book position")
    bbm.add_argument("note", nargs="?")
    bbm.add_argument("--range-end", action="store_true")
    bbm.add_argument("--slot", default="")
    bbm.add_argument("--target", default="")

    pl = b.add_parser("playlist", help="manage book playlists")
    pl.set_defaults(func=cmd_book)
    pls = pl.add_subparsers(dest="pl_cmd", required=True)

    pn = pls.add_parser("new", help="create an empty playlist")
    pn.add_argument("name")
    pa = pls.add_parser("add", help="append part URIs to a playlist")
    pa.add_argument("name")
    pa.add_argument("uris", nargs="+", help="one or more part URIs, in order")
    ppl = pls.add_parser("play", help="play a playlist at its remembered place")
    ppl.add_argument("name")
    ppl.add_argument("--no-resume", action="store_true",
                     help="start the playlist over from the first part")
    ppl.add_argument("--target", default="")
    pls_ls = pls.add_parser("ls", help="list playlists, or one list's parts")
    pls_ls.add_argument("name", nargs="?", default="")
    prm = pls.add_parser("rm", help="delete a playlist (keeps part bookmarks)")
    prm.add_argument("name")


def _end_opts_before_time(argv: list[str]) -> list[str]:
    """Insert ``--`` so a dash-led colon timecode parses as the seek argument.

    argparse reads a bare ``-5`` as a negative number but treats a dash-led
    *colon* timecode (``-5:00``) as an unknown option. For each seek-like
    subcommand — ``book seek``/``book skip`` and ``music seek`` — terminate
    option parsing right before a dash-led time value so a relative offset
    isn't mistaken for a flag.
    """
    a = list(argv)
    for i, tok in enumerate(a):
        seekish = ((tok in ("seek", "skip") and i > 0 and a[i - 1] == "book")
                   or (tok == "seek" and i > 0 and a[i - 1] == "music"))
        if not seekish:
            continue
        j = i + 1
        if (j < len(a) and a[j].startswith("-")
                and a[j] not in ("--", "-h", "--help", "--target")):
            a.insert(j, "--")
        break
    return a


def main(argv=None) -> int:
    from .intake._env import load_env_file
    load_env_file("media-cli")
    if argv is None:
        argv = sys.argv[1:]
    args = _build_parser().parse_args(_end_opts_before_time(argv))
    try:
        return args.func(args)
    except ipc.MpvIpcError as e:
        print(f"media: speech broker not reachable: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
