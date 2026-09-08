"""Claude Code intake adapter.

Reads Claude Code's hook JSON from stdin, extracts the speech text, and
hands it to `submit_event`. Replaces the legacy bash hook
(`packages/audio-relay/src/agent_audio_relay/shell/hooks/claude-code-tts-hook.sh`).

Settings.json wires it as (note `"async": true` — see below):

    "hooks": {
      "Stop":         [{"hooks":[{"type":"command",
                                  "command":"media-hook-claude-code",
                                  "async":true,"timeout":120}]}],
      "UserPromptSubmit": [{"hooks":[{"type":"command",
                                  "command":"media-hook-claude-code",
                                  "async":true,"timeout":30}]}],
      "Notification": [{"hooks":[{"type":"command",
                                  "command":"media-hook-claude-code",
                                  "async":true,"timeout":30}]}],
      "PreToolUse":   [{"matcher":"AskUserQuestion",
                        "hooks":[{"type":"command",
                                  "command":"media-hook-claude-code",
                                  "async":true,"timeout":30}]}]
    }

`async: true` runs the hook in the background so Claude Code never blocks on
TTS. Claude Code still terminates async hooks at their timeout and when the
session exits, so the Stop path detaches playback into a `setsid` child that
reparents to init and outlives both (see `_play_detached`). A single fork is
enough now that async handles non-blocking — no double-fork.

PreToolUse(AskUserQuestion) is what actually reads a multiple-choice prompt
aloud — Claude Code never fires a Notification for the question modal, so the
read-out has to hang off the tool's pre-execution hook.

Behaviours preserved from the bash version:
  - Sources `~/.config/agent-audio-relay.env` (or `RELAY_ENV_FILE`) so
    OPENAI_API_KEY etc. don't have to live in settings.json.
  - Notification suppression: skip if another notif fired within 120s,
    or a Stop played within 90s.
  - Stop: speak `last_assistant_message` from the payload; fall back to the
    transcript JSONL when it's absent/empty. Skip tool-call-only turns.
  - Dedup key (text-hash) collapses duplicate Stop / Stop+notif races.

Long-text routing (the old `CLAUDE_TTS_LONG_THRESHOLD` split into
tts-stream) is gone — Phase 3 of the restructure locked in a single
stream-only render path. The realtime engine produces audio in chunks
fast enough that long replies are no longer a special case.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path  # still used by _stamp_dir, _latest_assistant_text
from typing import Optional

from .._paths import state_dir
from ..state import StateStore
from ..types import Event, Priority, Source
from ._env import load_env_file
from ._text import strip_markdown
from .submit import submit_event


log = logging.getLogger(__name__)

# Distinguishable-accent voices used to give each tmux session its own
# voice when no explicit MEDIA_SESSION_VOICE_MAP pin matches. Override the
# whole set with MEDIA_SESSION_VOICE_POOL (comma-separated).
_DEFAULT_VOICE_POOL = (
    "en-AU-NatashaNeural",  # Australian
    "en-NZ-MollyNeural",    # New Zealand
    "en-GB-SoniaNeural",    # British
    "en-IE-EmilyNeural",    # Irish
    "en-CA-ClaraNeural",    # Canadian
    "en-GB-LibbyNeural",    # British (younger)
)


def _tmux(args: list[str], timeout: float = 2.0) -> str:
    """Run a tmux command, return stripped stdout or empty string."""
    import subprocess
    try:
        r = subprocess.run(["tmux", *args],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _session_name() -> str:
    """Current tmux session name, or "" when not running inside tmux."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return ""
    return _tmux(["display-message", "-p", "-t", pane, "#{session_name}"])


def _source_place() -> dict:
    """The pane this hook is running in, and the two tmux names for it.

    Resolved *here* rather than in the submitter, which is the only place that
    can be sure of the answer: the hook runs inside the pane at the moment the
    turn ends, while the submitter runs after a reply has been rendered and has
    waited its turn in the speech queue — long enough, on a conversation that
    just said goodbye, for the window to have closed. tmux answers a question
    about a pane that has gone with a successful empty string, so those clips
    landed in history with no session and no window at all, and the phone's list
    grouped every one of them under "no session".

    Empty values are left out rather than sent as "": the submitter treats a
    missing key as "ask tmux yourself", which is right for a hook that isn't in
    tmux at all, and an empty string would tell it the answer is nothing.
    """
    pane = os.environ.get("TMUX_PANE") or ""
    if not pane:
        return {}
    out = {"pane": pane}
    info = _tmux(["display-message", "-p", "-t", pane,
                  "#{session_name}\t#{window_name}"])
    sess, _, window = info.partition("\t")
    if sess.strip():
        out["tmux"] = sess.strip()
    if window.strip():
        out["window"] = window.strip()
    return out


def _voice_for_session(sess: str) -> Optional[str]:
    """Pick a TTS voice for the given tmux session name.

    Resolution order:
      1. Return None (→ daemon default voice) when disabled via
         MEDIA_SESSION_VOICE_ENABLED=0 or when not in tmux (no session).
      2. Explicit pin from MEDIA_SESSION_VOICE_MAP, formatted
         "name=voice,name=voice"; first exact session-name match wins.
      3. Stable hash of the session name into the voice pool
         (MEDIA_SESSION_VOICE_POOL overrides the built-in accent set), so a
         given session always gets the same voice without configuration.
    """
    if not sess or os.environ.get("MEDIA_SESSION_VOICE_ENABLED", "1") == "0":
        return None

    for pair in os.environ.get("MEDIA_SESSION_VOICE_MAP", "").split(","):
        name, _, voice = pair.partition("=")
        if name.strip() == sess and voice.strip():
            return voice.strip()

    pool_env = os.environ.get("MEDIA_SESSION_VOICE_POOL", "")
    pool = [v.strip() for v in pool_env.split(",") if v.strip()] \
        or list(_DEFAULT_VOICE_POOL)
    if not pool:
        return None
    h = int(hashlib.sha1(sess.encode("utf-8")).hexdigest(), 16)
    return pool[h % len(pool)]


def _notif_label(sess: str) -> str:
    """Build a "where am I" prefix for the notification text.

    Includes:
      - hostname (short) when there's >1 tmux session running and
        MEDIA_NOTIF_LABEL_HOST != "0" (default on).
      - tmux session name (always, when in tmux)
      - pane title when set (via `select-pane -T` or terminal escape); omitted
        when empty or identical to the session name.

    Returns "" outside tmux or when the user disabled labelling
    (MEDIA_NOTIF_LABEL=0). The AskUserQuestion path uses _ask_location_label
    instead (hierarchical host/session omission + window-name pane locator).
    """
    if os.environ.get("MEDIA_NOTIF_LABEL", "1") == "0":
        return ""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return ""

    pane_title = _tmux(["display-message", "-p", "-t", pane, "#{pane_title}"])
    sess_count_s = _tmux(["list-sessions", "-F", "#{session_name}"])

    parts: list[str] = []

    sess_count = len([s for s in sess_count_s.splitlines() if s.strip()])
    if sess_count > 1 and os.environ.get("MEDIA_NOTIF_LABEL_HOST", "1") != "0":
        import socket
        host = socket.gethostname().split(".")[0]
        if host:
            parts.append(host)

    if sess:
        parts.append(sess)

    if pane_title and pane_title != sess:
        parts.append(pane_title)

    return " / ".join(parts)


def _active_client_session() -> Optional[str]:
    """Session name of the most-recently-active attached client on this tmux
    server (the one the user last typed in), or None if no client is attached.

    Used as the "current" reference for hierarchical label omission: clients
    attach to the local server, so an attached client means the user is on this
    host (→ host is current, omit it), and its session is the one they're
    working in (→ omit that session from the label).
    """
    out = _tmux(["list-clients", "-F", "#{client_activity}\t#{session_name}"])
    best_ts, best_sess = -1, None
    for line in out.splitlines():
        ts_s, _, sess = line.partition("\t")
        try:
            ts = int(ts_s)
        except ValueError:
            continue
        if ts > best_ts and sess:
            best_ts, best_sess = ts, sess
    return best_sess


def _ask_location_label() -> str:
    """"Where is this question?" prefix for the AskUserQuestion notif.

    Announces host / session / pane, but omits — hierarchically, relative to
    the active tmux client (where the user last typed) — whatever is "current":
      - host:    dropped when a client is attached to this server (user is here)
      - session: dropped when it's the session the user is working in
      - pane:    always kept — window name + window.pane index (the window name
                 tracks the Claude conversation's title, a useful locator; the
                 *pane title* is the transient "AskUserQuestion" tool-status and
                 is deliberately not used)

    So a question from the foreground session reads just its pane; one from a
    background session adds the session; one from a host with nobody attached
    adds the host too. Returns "" outside tmux or when MEDIA_NOTIF_LABEL=0.
    """
    if os.environ.get("MEDIA_NOTIF_LABEL", "1") == "0":
        return ""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return ""

    info = _tmux(["display-message", "-p", "-t", pane,
                  "#{session_name}\t#{window_name}\t#{window_index}\t#{pane_index}"])
    fields = info.split("\t")
    if len(fields) < 4:
        return ""
    sess, win_name, win_idx, pane_idx = (f.strip() for f in fields[:4])

    active = _active_client_session()
    parts: list[str] = []

    # host — only when nobody is attached here (so the user isn't on this host)
    if active is None and os.environ.get("MEDIA_NOTIF_LABEL_HOST", "1") != "0":
        import socket
        host = socket.gethostname().split(".")[0]
        if host:
            parts.append(host)

    # session — only when it isn't the session the user is working in
    if sess and (active is None or sess != active):
        parts.append(sess)

    # pane — always; window name + index, skipping a name that just repeats
    # the session we already announced
    idx = f"{win_idx}.{pane_idx}" if win_idx and pane_idx else ""
    if win_name and win_name != sess:
        parts.append(f"{win_name} {idx}".strip())
    elif idx:
        parts.append(idx)

    return " / ".join(parts)


def _client_focused_recently(within_seconds: int) -> bool:
    """True if our tmux pane's window is currently displayed by an
    attached client whose user-input activity is within `within_seconds`.

    Used to suppress the "Claude is waiting" notif when the user is
    clearly at the screen and will see the prompt without an audio cue.
    Uses `client_activity` (keystroke/mouse timestamp) — not session or
    window activity, which bumps on assistant output too.
    """
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return False
    state = _tmux(["display-message", "-p", "-t", pane,
                   "#{window_active}:#{session_attached}"])
    try:
        win_active, sess_attached = state.split(":", 1)
    except ValueError:
        return False
    if win_active != "1":
        return False
    try:
        if int(sess_attached or "0") < 1:
            return False
    except ValueError:
        return False
    sess = _tmux(["display-message", "-p", "-t", pane, "#{session_name}"])
    if not sess:
        return False
    out = _tmux(["list-clients", "-t", sess, "-F", "#{client_activity}"])
    now = int(time.time())
    for line in out.splitlines():
        try:
            ts = int(line.strip())
        except ValueError:
            continue
        if now - ts < within_seconds:
            return True
    return False


def _client_pane_focused() -> bool:
    """True if our pane is *the* pane the user is looking at right now: the
    active pane of its session's active window, with an attached client.

    Unlike `_client_focused_recently`, this ignores keystroke recency — it
    answers "is this exactly the focused pane?", not "was the user typing
    here lately?". Used to keep a notification from *interrupting* whatever
    is currently being spoken: a notif from the focused pane is downgraded
    from HIGH to NORMAL so it queues behind the current clip instead of
    preempting it. Requires an attached client so a detached (walked-away)
    session still gets the interrupting HIGH cue.
    """
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return False
    state = _tmux(["display-message", "-p", "-t", pane,
                   "#{window_active}:#{pane_active}:#{session_attached}"])
    try:
        win_active, pane_active, sess_attached = state.split(":", 2)
    except ValueError:
        return False
    if win_active != "1" or pane_active != "1":
        return False
    try:
        return int(sess_attached or "0") >= 1
    except ValueError:
        return False


def _stamp_dir() -> Path:
    d = state_dir() / "claude-stamps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_stamp(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _write_stamp(path: Path, value: int) -> None:
    try:
        path.write_text(str(value))
    except OSError:
        pass


def _claim_once(key: str, ttl_seconds: int = 300) -> bool:
    """Atomic, cross-process "I will speak this" claim.

    Returns True if the claim is ours (proceed) or False if a live claim for
    the same key already exists (skip). Unlike the history-based `_dedup_seen`,
    this is race-safe: the marker is written *here*, at decide time, before the
    event queues on the playback lock. One AskUserQuestion modal fires two hooks
    (PreToolUse + Notification); they run concurrently and a queued-but-not-yet-
    played event has no history row yet, so both used to pass `_dedup_seen` and
    read the question twice. An `O_EXCL` create lets exactly one of them win.
    A stale claim (older than `ttl_seconds`, e.g. from a crashed hook) is taken
    over. Best-effort: on any filesystem error we return True rather than drop
    the read.
    """
    try:
        d = _stamp_dir() / "ask-claims"
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return True
    path = d / hashlib.sha1(key.encode("utf-8")).hexdigest()
    now = int(time.time())
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, str(now).encode())
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        try:
            prev = int(path.read_text().strip())
        except (OSError, ValueError):
            prev = 0
        if now - prev < ttl_seconds:
            return False  # a fresh claim already owns this key
        # Stale — reclaim it (best-effort) and proceed.
        try:
            path.write_text(str(now))
        except OSError:
            pass
        return True
    except OSError:
        return True


def _format_ask_question(tool_input: dict) -> str:
    """Render an AskUserQuestion tool input as speakable text.

    A multiple-choice question goes out as a *tool call*, so its text never
    lands in the assistant's spoken reply — the user would otherwise only hear
    the generic "waiting for input" notification. Speak the question(s) and
    their option labels so the choice is audible. Option descriptions are
    omitted (too verbose for TTS); the labels carry the gist.
    """
    questions = tool_input.get("questions") or []
    blocks: list[str] = []
    multi = len(questions) > 1
    for i, q in enumerate(questions, 1):
        qtext = (q.get("question") or "").strip()
        if not qtext:
            continue
        lead = f"Question {i}. " if multi else ""
        opts = q.get("options") or []
        labels = [(o.get("label") or "").strip() for o in opts]
        labels = [l for l in labels if l]
        choice = "".join(f" Option {n}: {l}." for n, l in enumerate(labels, 1))
        tail = " You can pick more than one." if q.get("multiSelect") else ""
        blocks.append(f"{lead}{qtext}{choice}{tail}")
    return " ".join(blocks)


def _latest_assistant_text(transcript_path: Path) -> str:
    """Walk the JSONL transcript from the end, return the most recent
    assistant turn's joined text content. If the latest assistant turn is an
    AskUserQuestion tool call (no text), speak the synthesized question instead.
    Empty string if the latest turn is other tool-call-only or unparseable.
    """
    try:
        lines = transcript_path.read_text().splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        parts = []
        ask: Optional[dict] = None
        for c in msg.get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                parts.append(c.get("text") or "")
            elif c.get("type") == "tool_use" and c.get("name") == "AskUserQuestion":
                ask = c.get("input") or {}
        text = "\n".join(p for p in parts if p)
        if text:
            return text
        if ask is not None:
            spoken = _format_ask_question(ask)
            if spoken:
                return spoken
        # Other tool-use-only turn — keep searching backward for the last text.
    return ""


def _latest_ask_question(transcript_path: Path) -> str:
    """If the latest assistant turn contains an AskUserQuestion tool call,
    return its synthesized speakable text; else "".

    AskUserQuestion fires a *Notification* (the turn pauses awaiting input),
    not a Stop — and the generic notif message ("Claude is waiting for your
    input") never includes the question. At notif-fire time the AskUserQuestion
    is the live last assistant turn (no tool_result appended yet), so we read it
    here and speak the actual question + options instead of the generic nudge.
    """
    try:
        lines = transcript_path.read_text().splitlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        for c in msg.get("content") or []:
            if (isinstance(c, dict) and c.get("type") == "tool_use"
                    and c.get("name") == "AskUserQuestion"):
                return _format_ask_question(c.get("input") or {})
        # First assistant turn found isn't an AskUserQuestion → not our case.
        return ""
    return ""


def _ask_lead_text(transcript_path: Path) -> str:
    """Assistant prose that precedes the question in the *same* turn.

    When Claude writes an explanation and then calls AskUserQuestion, PreToolUse
    speaks only the synthesized question, and Stop never fires while the turn is
    paused on the modal — so that lead-in prose would otherwise be silently
    dropped. In a real transcript the text and the tool_use are written as
    *separate* JSONL lines within one turn (text, then a tool_use-only line), so
    walk back over the contiguous run of assistant lines collecting `text`
    blocks, stopping at the turn boundary (the first user / tool_result line).
    Returns "" unless that run actually carries the AskUserQuestion call.
    """
    try:
        lines = transcript_path.read_text().splitlines()
    except OSError:
        return ""
    texts: list[str] = []
    seen_ask = False
    saw_assistant = False
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") or {}
        if msg.get("role") != "assistant":
            if saw_assistant:
                break  # hit the turn boundary
            continue   # trailing non-assistant lines before the turn
        saw_assistant = True
        line_parts: list[str] = []
        for c in msg.get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                line_parts.append(c.get("text") or "")
            elif (c.get("type") == "tool_use"
                  and c.get("name") == "AskUserQuestion"):
                seen_ask = True
        joined = "\n".join(p for p in line_parts if p)
        if joined:
            texts.append(joined)
    if not seen_ask:
        return ""
    texts.reverse()
    return "\n".join(t for t in texts if t).strip()


def _dedup_seen(state: StateStore, text: str, ttl_seconds: int = 300) -> bool:
    """Crude text-hash dedup over the recent history table.

    Returns True if this exact text has been spoken in the last
    `ttl_seconds` (so caller should skip emitting again).
    """
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()
    cutoff = time.time() - ttl_seconds
    for row in state.recent_history(sink="speech", limit=20):
        if (row.get("started_at") or 0) < cutoff:
            break
        extras = row.get("extras") or {}
        if isinstance(extras, str):
            try:
                extras = json.loads(extras)
            except json.JSONDecodeError:
                extras = {}
        if extras.get("dedup_key") == key:
            return True
        if (row.get("text") or "") == text:
            return True
    return False


def _emit_ask(ask: str, payload: dict, lead: str = "") -> int:
    """Speak a synthesized AskUserQuestion (question + option labels).

    Prefixes the hierarchical host/session/pane label, bypasses focus-
    suppression and the notif/stop cooldown windows (the user explicitly wants
    questions read out, and they're infrequent), and text-dedups against the
    recent history so a PreToolUse fire and a stray Notification can't double
    up. Downgrades HIGH→NORMAL when this is the focused pane so the cue queues
    behind whatever is currently speaking instead of preempting it.

    `lead` is any assistant prose that preceded the question in the same turn;
    it's spoken before the question so the explanation isn't lost to the modal.
    """
    sess = _session_name()
    label = _ask_location_label()
    body = f"{lead} {ask}".strip() if lead else ask
    msg = f"{label}: {body}" if label else body
    # Race-safe dedup, claimed *before* the event queues on the playback lock.
    # PreToolUse and Notification both fire for one modal; the history-based
    # check below can't see a sibling that's queued-but-not-yet-played, so on
    # its own it lets the question be read twice. Key on the raw `ask` (not
    # `msg`) so the two fires collapse even when one carries lead-in prose or a
    # different location label than the other.
    if not _claim_once(ask):
        return 0
    state = StateStore()
    if _dedup_seen(state, msg):
        return 0
    _write_stamp(_stamp_dir() / "notif-last", int(time.time()))
    priority = Priority.NORMAL if (
        os.environ.get("MEDIA_NOTIF_NO_INTERRUPT_FOCUSED", "1") != "0"
        and _client_pane_focused()) else Priority.HIGH
    submit_event(Event(text=msg, source=Source.CLAUDE_CODE,
                       priority=priority,
                       voice=_voice_for_session(sess),
                       metadata={"kind": "notif", "ask": True,
                                 "session": payload.get("session_id") or "",
                                 **_source_place()}),
                 state=state)
    return 0


def _handle_pretooluse(payload: dict) -> int:
    """PreToolUse path — the *real* AskUserQuestion trigger.

    Claude Code does NOT fire a Notification when an AskUserQuestion modal is
    shown (verified: a real question sat unanswered 9 minutes with zero notifs),
    so the old Notification-based read-out never actually ran on a live
    question. PreToolUse fires right as the tool is about to execute — i.e. as
    the modal appears — and hands us `tool_input` directly, no transcript walk.
    We only care about AskUserQuestion; every other tool returns immediately.
    """
    if payload.get("tool_name") != "AskUserQuestion":
        return 0
    ask = strip_markdown(_format_ask_question(payload.get("tool_input") or {}).strip())
    if not ask:
        return 0
    # Best-effort: prepend any prose Claude wrote before the question in this
    # same turn. NOTE (verified 2026-06-25, re-verified 2026-07-31): at PreToolUse fire
    # time this almost always yields nothing, and it is NOT fixable here. The
    # PreToolUse payload carries no assistant text (only tool_name/tool_input/
    # transcript_path/etc.), AND Claude Code does not flush the current turn to
    # the transcript JSONL until *after* the question is answered — a 4s probe
    # showed the file frozen, tail ending in the previous tool_result, the lead
    # text + AskUserQuestion tool_use lines entirely absent. So neither source
    # has the prose while the modal is up; retrying/waiting can't help. The
    # working path is for the *caller* to speak the lead via the `say` tool
    # before invoking AskUserQuestion (see the global CLAUDE.md rule); the
    # question then queues behind it rather than preempting, because the
    # playback lock orders by pane — see _order_session in intake/submit.py.
    # This call is left as a harmless fallback in case a future Claude Code
    # flushes earlier.
    # Re-probe (2026-07-31): a live watcher on the transcript saw the lead-text
    # and AskUserQuestion lines appear together only *after* the answer, 59s
    # after the modal went up. Still frozen; still not fixable here.
    lead = ""
    tp_raw = (payload.get("transcript_path") or "").strip()
    if tp_raw:
        tp = Path(tp_raw)
        if tp.is_file():
            lead = strip_markdown(_ask_lead_text(tp)).strip()
    return _emit_ask(ask, payload, lead=lead)


def _handle_notification(payload: dict) -> int:
    """Notif path: prefer Claude's `message` field, dedup-skip if a
    Stop just played or a notif fired within the cooldown windows.

    AskUserQuestion is handled by the PreToolUse path (`_handle_pretooluse`),
    not here — Claude Code doesn't emit a Notification for the modal. We still
    keep a belt-and-braces check: if the live last assistant turn *is* an
    AskUserQuestion (e.g. a future Claude Code does start notifying), speak it,
    bypassing focus-suppression / cooldowns. Text-dedup collapses any overlap
    with the PreToolUse read-out.
    """
    ask = ""
    tp_raw = (payload.get("transcript_path") or "").strip()
    if tp_raw:
        tp = Path(tp_raw)
        if tp.is_file():
            ask = strip_markdown(_latest_ask_question(tp).strip())

    if ask:
        return _emit_ask(ask, payload)

    msg = strip_markdown((payload.get("message") or "").strip())
    if not msg:
        return 0

    # If the user has been at the screen recently, don't nag them with
    # audio — they'll see the prompt. Tunable via env, 0 disables.
    focus_window = int(os.environ.get("MEDIA_NOTIF_FOCUS_SUPPRESS", "180"))
    if focus_window > 0 and _client_focused_recently(focus_window):
        return 0

    sess = _session_name()
    label = _notif_label(sess)
    if label:
        msg = f"{label}: {msg}"

    stamps = _stamp_dir()
    now = int(time.time())
    last_notif = _read_stamp(stamps / "notif-last")
    last_stop = _read_stamp(stamps / "stop-last")
    if last_notif and (now - last_notif) < 120:
        return 0
    if last_stop and (now - last_stop) < 90:
        return 0
    _write_stamp(stamps / "notif-last", now)

    # A notif from the pane the user is actively looking at shouldn't cut off
    # whatever is currently being spoken — speak it, but at NORMAL so it queues
    # instead of preempting. Tunable via env, 0 keeps the old always-HIGH behaviour.
    priority = Priority.HIGH
    if os.environ.get("MEDIA_NOTIF_NO_INTERRUPT_FOCUSED", "1") != "0" \
            and _client_pane_focused():
        priority = Priority.NORMAL

    submit_event(Event(text=msg, source=Source.CLAUDE_CODE,
                       priority=priority,
                       voice=_voice_for_session(sess),
                       metadata={"kind": "notif",
                                 "session": payload.get("session_id") or "",
                                 **_source_place()}))
    return 0


def _play_now(event: Event) -> None:
    """Dedup, stamp, and submit the Stop reply on a *fresh* StateStore.

    Runs in the detached child (or inline under MEDIA_HOOK_NO_DETACH). Opening the
    StateStore *here* — after the fork — is what keeps the WAL connection out of
    the parent: an inherited WAL handle used to corrupt the child's wal-index
    locking, so the parent's exit (or an unrelated reader) would unlink the
    -wal/-shm out from under us and our now_playing writes would vanish (grey
    status bar, wrong popup subject, "pane already closed" goto).

    Dedup + the stop stamp live here too (not in the parent), so the parent never
    opens a StateStore at all — nothing to close-before-fork, nothing to leak.
    """
    state = StateStore()
    if _dedup_seen(state, event.text):
        return
    event.metadata["dedup_key"] = hashlib.sha1(
        event.text.encode("utf-8")).hexdigest()
    # Commit to speaking: the notif-suppression window keys off this stamp, and
    # detached playback won't report back.
    _write_stamp(_stamp_dir() / "stop-last", int(time.time()))
    # Opt-in visual accompaniment: hand the raw reply to `media-visual`
    # fire-and-forget *before* the (slow) summary/describe rewrites below, so
    # image generation runs concurrently with them and with playback. After
    # dedup, so a suppressed duplicate reply doesn't repaint the canvas either.
    # Popped unconditionally — the raw reply must never ride into submit_event.
    visual_raw = event.metadata.pop("visual_raw", None)
    visual_hint = event.metadata.pop("visual_hint", "") or ""
    visual_reveal = event.metadata.pop("visual_reveal", None)
    spawn_epoch = time.time()
    if visual_raw:
        try:
            from ._visual import spawn_visual
            spawn_visual(visual_raw, event.text,
                         event.metadata.get("session") or "",
                         hint=visual_hint,
                         key=event.metadata.get("dedup_key") or "")
        except Exception:  # noqa: BLE001 — accompaniment, never playback's problem
            pass
    if visual_raw and visual_reveal:
        # [[reveal:]]: speak up to the marker, hold until the canvas shows
        # the image (bounded — see wait_for_fresh_visual), then speak on.
        try:
            _play_reveal(event, visual_reveal, spawn_epoch, state)
            return
        except Exception:  # noqa: BLE001 — fall back to the plain single play
            pass
    # Optional LLM spoken-summary rewrite (opt-in). Runs *here*, in the detached
    # child, so the network call never blocks the hook. Dedup already keyed off
    # the mechanically-stripped original above, keeping it deterministic; on any
    # failure we keep event.text as-is. `summary_raw` holds the un-stripped reply
    # so the model can describe code/tables rather than the "…omitted." stubs.
    # Event is a frozen dataclass, so text rewrites go through object.__setattr__
    # (the same in-place escape hatch the metadata mutations above rely on) —
    # a plain `event.text = …` raises FrozenInstanceError.
    raw = event.metadata.pop("summary_raw", None)
    if raw:
        try:
            from ._summary import summarize_for_speech
            spoken = summarize_for_speech(raw)
            if spoken:
                object.__setattr__(event, "text", strip_markdown(spoken))
        except Exception:  # noqa: BLE001 — detached; fall back to event.text
            pass
    else:
        # Per-block describe (opt-in, no whole-reply summary): re-strip the raw
        # reply here in the detached child with describe=True, so un-readable
        # code blocks / tables become a spoken description instead of an
        # "…omitted." stub. Same off-the-fork placement as the summary, so the
        # (slow, local) model call never blocks the hook. Falls back to event.text.
        describe_raw = event.metadata.pop("describe_raw", None)
        if describe_raw:
            try:
                described = strip_markdown(describe_raw, describe=True)
                if described:
                    object.__setattr__(event, "text", described)
            except Exception:  # noqa: BLE001 — detached; keep event.text
                pass
    submit_event(event, state=state)


def _play_reveal(event: Event, reveal: dict, spawn_epoch: float,
                 state: StateStore) -> None:
    """Speak `reveal['pre']`, hold until the canvas shows an image fresher
    than `spawn_epoch` (bounded by MEDIA_VISUAL_REVEAL_TIMEOUT — a hung
    generator must never mute a reply), then speak `reveal['post']`. The two
    parts share the session, so the broker plays them in canonical order; the
    hold only delays when part two is *enqueued*. Runs in the detached child."""
    from dataclasses import replace

    from ._visual import wait_for_fresh_visual

    def part(txt: str) -> Event:
        md = dict(event.metadata)
        md["dedup_key"] = hashlib.sha1(txt.encode("utf-8")).hexdigest()
        return replace(event, text=txt, metadata=md)

    submit_event(part(reveal["pre"]), state=state)
    wait_for_fresh_visual(spawn_epoch)
    submit_event(part(reveal["post"]), state=state)


def _play_detached(event: Event) -> None:
    """Render + play `event` in a session-detached child that outlives the hook.

    The hook is wired `async: true`, so Claude Code doesn't block on it — but it
    still terminates async hooks at their timeout and when the session exits.
    Playback of a long reply must survive both, so we fork once and `setsid`: the
    child leads a new session and is reparented to init on our exit, escaping
    Claude Code's process-group teardown. (async already handles non-blocking, so
    no second "return fast" fork is needed — this replaces the old double-fork.)

    All DB work happens in the child via `_play_now` on its own StateStore, so no
    WAL connection is ever inherited across the fork. Falls back to inline play if
    fork is unavailable, and MEDIA_HOOK_NO_DETACH=1 forces inline (tests/debug).
    """
    if os.environ.get("MEDIA_HOOK_NO_DETACH"):
        _play_now(event)
        return
    try:
        pid = os.fork()
    except OSError:
        _play_now(event)  # no fork → inline, bounded by the hook timeout
        return
    if pid > 0:
        return  # parent: return at once; the child reparents to init on our exit
    # --- child: new session (survives session-group teardown), detach stdio so
    #     Claude Code's hook pipe sees EOF, then play to completion ---
    try:
        os.setsid()
    except OSError:
        pass
    # Detach stdio so Claude Code's hook pipe sees EOF — but keep stdout/stderr
    # observable: a detached child that dies mid-playback (kill, crash) leaves a
    # stale now_playing mirror and an unrestored duck with zero trace when its
    # output goes to /dev/null. Warnings (logging's lastResort handler writes
    # WARNING+ to stderr) and tracebacks land in the log instead.
    try:
        logp = Path.home() / ".cache" / "agent-media" / "hook-play.log"
        try:
            logp.parent.mkdir(parents=True, exist_ok=True)
            if logp.exists() and logp.stat().st_size > 2_000_000:
                logp.unlink()   # crude rotation — it's a diagnostic tail, not an archive
            logfd = os.open(str(logp), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        except OSError:
            logfd = None
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        for fd in (1, 2):
            try:
                os.dup2(logfd if logfd is not None else devnull, fd)
            except OSError:
                pass
        print(f"--- hook-play pid={os.getpid()} {time.strftime('%F %T')} "
              f"text={event.text[:60]!r}", file=sys.stderr, flush=True)
    except OSError:
        pass
    try:
        _play_now(event)
    except Exception:  # noqa: BLE001 — detached; log it, there is no caller
        import traceback
        traceback.print_exc()
    finally:
        os._exit(0)


def _handle_stop(payload: dict) -> int:
    """Stop path: get the assistant's final text and speak it (detached)."""
    # Prefer the text straight off the Stop payload — no transcript read, parse,
    # or flush-retry. `last_assistant_message` is "the full text of Claude's
    # response" (Stop/SubagentStop only).
    # Inline [[visual:]]/[[reveal:]] markers make the reply's picture
    # purposeful (see _visual.py). Always stripped from what gets spoken.
    from ._visual import extract_visual_markers
    raw = (payload.get("last_assistant_message") or "").strip()
    raw, vis_hint, reveal_pre, reveal_post = extract_visual_markers(raw)
    text = strip_markdown(raw)
    if not text:
        # Fall back to the transcript JSONL: covers older Claude Code (no field)
        # and a tool-only turn (empty field, but the walk may still find earlier
        # spoken text or an AskUserQuestion to synthesize). The flush-retry only
        # matters on this path, where we're racing the transcript write.
        raw_path = (payload.get("transcript_path") or "").strip()
        if not raw_path:
            return 0
        tp = Path(raw_path)
        if not tp.is_file():
            return 0
        for _ in range(5):
            try:
                ok = tp.stat().st_size > 0 and (time.time() - tp.stat().st_mtime) <= 5
            except OSError:
                ok = False
            if ok:
                break
            time.sleep(0.1)
        raw = _latest_assistant_text(tp)
        raw, vis_hint, reveal_pre, reveal_post = extract_visual_markers(raw)
        text = strip_markdown(raw)
    if not text:
        return 0

    # Detach playback: a long reply's audio can outlast the hook's async timeout
    # / the session exiting, which would cut it off mid-sentence. Hand it to a
    # backgrounded process and return immediately. Dedup, the stop stamp, and all
    # now_playing writes happen there on a fresh StateStore (see _play_now), so
    # the parent opens no DB connection to fork across.
    metadata = {"kind": "stop", "session": payload.get("session_id") or "",
                **_source_place()}
    # Opt-in LLM spoken-summary: carry the raw reply so the detached child can
    # rewrite it for speech (only worth it past a length threshold).
    # Opt-in visual accompaniment: carry the raw reply so the detached child
    # can hand it to `media-visual` (see _visual.py). An author marker (hint)
    # bypasses the length gate — the author explicitly asked for a picture.
    # A [[reveal:]] marker additionally carries the split halves so playback
    # can hold between them until the image is up.
    from ._visual import visual_enabled, visual_min_chars
    if visual_enabled() and (vis_hint or len(raw) >= visual_min_chars()):
        metadata["visual_raw"] = raw
        if vis_hint:
            metadata["visual_hint"] = vis_hint
            # Indicator flag: rides into now_playing extras so the status
            # bar / popup / canvas can mark figure-bearing messages (▣).
            metadata["visual"] = ("reveal" if reveal_pre is not None
                                  else "figure")
        if reveal_pre is not None:
            pre = strip_markdown(reveal_pre)
            post = strip_markdown(reveal_post or "")
            if pre and post:
                metadata["visual_reveal"] = {"pre": pre, "post": post}
    from ._summary import summary_enabled, summary_min_chars, describe_enabled
    if "visual_reveal" in metadata:
        # A reveal splits the mechanically-stripped text at an exact marker
        # position; the summary/describe rewrites would move or erase that
        # point, so they sit this reply out.
        pass
    elif summary_enabled() and len(raw) >= summary_min_chars():
        metadata["summary_raw"] = raw
    elif describe_enabled():
        # No whole-reply summary, but per-block describe is on: carry the raw
        # reply so the detached child can describe its code/tables (off the fork).
        metadata["describe_raw"] = raw
    _play_detached(
        Event(text=text, source=Source.CLAUDE_CODE,
              priority=Priority.NORMAL,
              voice=_voice_for_session(_session_name()),
              metadata=metadata))
    return 0


# A prompt longer than this is a paste — a log, a diff, a document — not a
# line in a conversation, and rendering it would put minutes of a synthetic
# voice reading code onto the shelf. Skipped rather than truncated: a cut-off
# transcript line reads as a bug, a missing one reads as a paste.
PROMPT_RECORD_LIMIT = 2000


def _handle_user_prompt(payload: dict) -> int:
    """UserPromptSubmit — the listener's own words, typed at the keyboard.

    A reply sent from the player is recorded as a listener turn by the canvas
    (reply.py) and shows up in the transcript; a prompt typed into the terminal
    went nowhere, so a conversation read back from the shelf had every answer
    and none of the questions. This records it by the same path — rendered in
    the listener's voice, one history row, the shelf publish armed — so the
    transcript reads the same whichever way the words arrived.

    Detached like playback: the render takes a second or two and must not sit
    in front of the prompt, and the hook is killed at its timeout.
    """
    text = " ".join(str(payload.get("prompt") or "").split())
    session = payload.get("session_id") or ""
    if not text or not session:
        return 0
    # A slash command is an instruction to the harness, not something said.
    if text.startswith("/"):
        return 0
    if len(text) > PROMPT_RECORD_LIMIT:
        log.info("hook: prompt of %d chars not recorded (paste)", len(text))
        return 0

    def record() -> None:
        try:
            from agent_media_core import book_tracks

            book_tracks.record_listener_turn(session, text)
        except Exception as e:  # noqa: BLE001 — the prompt reached Claude either way
            log.warning("hook: could not record the prompt (%s)", e)

    if os.environ.get("MEDIA_HOOK_NO_DETACH"):
        record()
        return 0
    try:
        pid = os.fork()
    except OSError:
        record()
        return 0
    if pid > 0:
        return 0
    try:
        os.setsid()
    except OSError:
        pass
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
    except OSError:
        pass
    try:
        record()
    finally:
        os._exit(0)


def main() -> int:
    if os.environ.get("MEDIA_HOOK_ENABLED", "1") == "0":
        return 0
    if os.environ.get("CLAUDE_TTS_ENABLED", "1") == "0":
        return 0

    load_env_file("hook-claude-code")

    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        return 0
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    event_name = payload.get("hook_event_name")
    try:
        if event_name == "PreToolUse":
            return _handle_pretooluse(payload)
        if event_name == "Notification":
            return _handle_notification(payload)
        if event_name == "Stop":
            return _handle_stop(payload)
        if event_name == "UserPromptSubmit":
            return _handle_user_prompt(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("hook: %s handler failed: %s", event_name, e)
        try:
            StateStore().log_error("hook-claude-code",
                                   f"{event_name} failed",
                                   extras={"detail": str(e)})
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
