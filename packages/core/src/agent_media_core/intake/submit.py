"""Submit: render text → play through sink-speech.

The intake "happy path" for any event source. Bypasses the legacy
drop-dir + watcher chain — render is in-process, playback is dispatched
straight to sink-speech via the route Coordinator.

Adapters (`hook_claude_code`, `cli`, future `matrix` / `ha-stt`) all
land here. The shape is intentionally narrow: take a populated
`Event`, hand back a history row id.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import re
import subprocess
import textwrap
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from .. import _lock as fcntl
from .._notify import notify
from ..render import render_text
from ..route import Coordinator
from ..sinks.speech import SinkSpeech, _env_key
from ..state import StateStore
from ..types import Event, Priority, Target


log = logging.getLogger(__name__)


_DEFAULT_ENGINE = "edge"
_DEFAULT_VOICE: Optional[str] = None


def _default_engine() -> str:
    return (os.environ.get("MEDIA_RENDER_ENGINE")
            or os.environ.get("CLAUDE_TTS_ENGINE")  # legacy
            or _DEFAULT_ENGINE)


def _resolve_engine(event: Event) -> str:
    return event.engine or _default_engine()


def _resolve_voice(event: Event, engine: str) -> Optional[str]:
    """Resolve the voice for the *selected* engine.

    Voices live in engine-specific namespaces (edge 'en-AU-NatashaNeural',
    openai 'marin', qwen 'Cherry'), so resolution must know which engine will
    render — otherwise one engine's voice gets force-fed to another, which e.g.
    makes DashScope reject the request (qwen 400 InvalidParameter). Precedence:

      1. event.voice                  — explicit per-event override
      2. MEDIA_RENDER_VOICE_<ENGINE>  — per-engine config (the canonical knob)
      3. MEDIA_RENDER_VOICE           — generic, but ONLY when this engine is
                                        the configured default engine, so a
                                        generic voice can't bleed onto another
                                        engine
      4. CLAUDE_TTS_VOICE             — legacy, ONLY when this engine matches
                                        the legacy CLAUDE_TTS_ENGINE it paired
                                        with
      5. None                         — render_text falls back to the engine's
                                        own built-in default voice

    Returning None is safe: render_text applies the right per-engine default.
    """
    if event.voice:
        return event.voice

    per_engine = os.environ.get(f"MEDIA_RENDER_VOICE_{engine.upper().replace('-', '_')}")
    if per_engine:
        return per_engine

    if engine == (os.environ.get("MEDIA_RENDER_ENGINE") or _DEFAULT_ENGINE):
        generic = os.environ.get("MEDIA_RENDER_VOICE")
        if generic:
            return generic

    legacy_engine = os.environ.get("CLAUDE_TTS_ENGINE")
    if legacy_engine and engine == legacy_engine:
        legacy_voice = os.environ.get("CLAUDE_TTS_VOICE")
        if legacy_voice:
            return legacy_voice

    return _DEFAULT_VOICE


def _ext_for(engine: str) -> str:
    """qwen / realtime / kokoro emit WAV, others MP3."""
    return "wav" if engine in ("qwen", "realtime", "kokoro") else "mp3"


def _split_sentences_with_paragraphs(text: str) -> tuple[list[str], list[int]]:
    """Segment text into sentences plus a parallel paragraph index per sentence.

    Splits on paragraph breaks first, then on sentence-ending punctuation
    within each paragraph. Common abbreviations are masked so they don't
    produce spurious splits. The returned paragraph indices are 0-based and
    monotonically non-decreasing; the popup uses them so H/L can jump a whole
    paragraph at a time while h/l step one sentence.
    """
    _ABBREV = re.compile(
        r'\b(Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|etc|e\.g|i\.e|'
        r'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec|[A-Z])\.'
    )

    def _sentences_in(para: str) -> list[str]:
        masked = _ABBREV.sub(lambda m: m.group(0)[:-1] + '\x00', para)
        parts = re.split(r'(?<=[.!?])\s+', masked.strip())
        return [p.replace('\x00', '.').strip() for p in parts if p.strip()]

    paragraphs = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    raw: list[tuple[str, int]] = []  # (sentence, paragraph index)
    for pi, para in enumerate(paragraphs):
        for s in _sentences_in(para):
            raw.append((s, pi))

    # Merge very short fragments (< 20 chars) into the preceding sentence
    # so standalone words like "Yes." or "OK." don't become solo clips — but
    # only within the same paragraph, so a short sentence that opens a new
    # paragraph stays its own clip and H/L paragraph-nav keeps working.
    sentences: list[str] = []
    para_idx: list[int] = []
    for part, pi in raw:
        if len(part) < 20 and sentences and para_idx[-1] == pi:
            sentences[-1] += ' ' + part
        else:
            sentences.append(part)
            para_idx.append(pi)
    if not sentences:
        return [text.strip()], [0]
    return sentences, para_idx


def _split_sentences(text: str) -> list[str]:
    """Sentence-level chunks for progressive TTS + highlight (paragraph map dropped)."""
    return _split_sentences_with_paragraphs(text)[0]


def _highlight_flag_path() -> Path:
    """File flag controlling auto-highlight: contents "1" = on, anything else = off."""
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    return state / "agent-media" / "auto-highlight"


def _is_auto_highlight_enabled() -> bool:
    """Auto-highlight is opt-in. Env override wins; otherwise read flag file."""
    env = os.environ.get("MEDIA_AUTO_HIGHLIGHT")
    if env is not None:
        return env != "0"
    try:
        return _highlight_flag_path().read_text().strip() == "1"
    except OSError:
        return False


def toggle_auto_highlight() -> bool:
    """Flip the auto-highlight flag. Returns the new state (True = on)."""
    new_state = not _is_auto_highlight_enabled()
    p = _highlight_flag_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("1" if new_state else "0")
    return new_state


def _pane_scroll_pos(pane: str) -> tuple[bool, str]:
    """(in_copy_mode, scroll_position) for `pane`.

    scroll_position is lines scrolled up from the live bottom; it is only
    meaningful while the pane is in copy-mode (empty otherwise).
    """
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane,
             "#{pane_in_mode}\t#{scroll_position}"],
            capture_output=True, text=True)
        if r.returncode != 0:
            return (False, "")
        in_mode, _, pos = r.stdout.rstrip("\n").partition("\t")
        return (in_mode.strip() == "1", pos.strip())
    except Exception:  # noqa: BLE001
        return (False, "")


def _last_client_activity(pane: str) -> Optional[int]:
    """Epoch of the most recent *user input* on `pane`'s session, or None.

    We want last-keystroke time, not last-output. `#{window_activity}` /
    `#{pane_activity}` track output, which is useless here: a Claude Code (or
    any TUI) pane redraws its spinner/status continuously, so output-activity
    is always ≈now even when the user is idle. `#{client_activity}` instead
    tracks when the *attached client* last sent data — i.e. real keystrokes —
    and freezes while the user isn't typing.

    A client is per-attachment, and this runs in a hook subprocess with no
    client of its own, so we resolve the pane's session and take the max
    client_activity across the clients attached to it. None = couldn't tell
    (no session / no clients / tmux error).
    """
    try:
        s = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_id}"],
            capture_output=True, text=True)
        if s.returncode != 0:
            return None
        sid = s.stdout.strip()
        if not sid:
            return None
        r = subprocess.run(
            ["tmux", "list-clients", "-t", sid, "-F", "#{client_activity}"],
            capture_output=True, text=True)
        if r.returncode != 0:
            return None
        epochs = [int(x) for x in r.stdout.split() if x.strip().isdigit()]
        return max(epochs) if epochs else None
    except Exception:  # noqa: BLE001
        return None


def _pane_recent_keystrokes(pane: str, within_s: float) -> bool:
    """True if the user typed in `pane`'s session within the last `within_s`s.

    Used to skip a highlight turn while the user is actively typing (the
    highlight would otherwise yank copy-mode out from under them). Backed by
    `_last_client_activity` (client input, not pane output — see there).
    Fails open (returns False) when we can't tell, so highlighting still
    happens rather than silently never running.
    """
    if within_s <= 0:
        return False
    last = _last_client_activity(pane)
    if last is None:
        return False
    return (time.time() - last) < within_s


def _force_highlight_flag_path() -> Path:
    """File flag for "highlight the next turn(s) even if I just typed".

    Contents = the epoch the user pressed the force key (popup/tmux
    `highlight-now`). Stays in effect until they type again (client activity
    moves past that epoch), at which point the gate clears it.
    """
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    return state / "agent-media" / "force-highlight"


# Backstop so a forgotten/stale force-highlight flag can't override the
# keystroke-skip indefinitely (a real press self-heals on the next keystroke).
_FORCE_MAX_AGE_S = 1800


def set_force_highlight() -> None:
    """Stamp the force-highlight flag with the current time (the key press)."""
    p = _force_highlight_flag_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(int(time.time())))


def _popup_open_flag_path() -> Path:
    """Marker written by `media-popup-open` while the control popup is open.

    Contents = the pane the popup is controlling. An open popup means the user
    is attending to playback, so the highlight overrides its keystroke-skip
    while it's up."""
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    return state / "agent-media" / "popup-open"


def _popup_open_for(pane: str) -> bool:
    """True if the control popup is currently open for `pane`."""
    try:
        return _popup_open_flag_path().read_text().strip() == pane
    except OSError:
        return False


def _force_highlight_active(pane: str) -> bool:
    """True if a force-highlight press is still in effect for `pane`.

    Active from the press until the user types again ("types again" = client
    activity strictly past the press epoch; pressing the force key is itself
    client input at the press second, so equal-second still counts as active).
    Expired flags are unlinked so they don't linger.

    Fails to *inactive* when we can't read client activity, and ignores a flag
    older than FORCE_MAX_AGE_S — otherwise a stale flag (e.g. left by a test, or
    a moment when no client is attached so activity reads None) would silently
    override the keystroke-skip forever. A genuine press self-heals on the next
    keystroke; the max-age is just a backstop."""
    p = _force_highlight_flag_path()
    try:
        pressed = int(p.read_text().strip())
    except (OSError, ValueError):
        return False
    if time.time() - pressed > _FORCE_MAX_AGE_S:
        p.unlink(missing_ok=True)
        return False
    last = _last_client_activity(pane)
    if last is None:
        return False                      # can't tell → don't override
    if last > pressed:
        p.unlink(missing_ok=True)
        return False
    return True


def _cursor_sig(pane: str) -> str:
    """A signature of the copy-mode cursor/viewport, to detect movement."""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane,
             "#{scroll_position}\t#{copy_cursor_x}\t#{copy_cursor_y}"],
            capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _strip_markdown_inline(s: str) -> str:
    """Drop inline markdown markers the terminal renderer hides, so a snippet
    built from the *raw* spoken text matches the *rendered* pane text.

    e.g. the agent says "use `media toggle`" but Claude Code renders the code
    span without backticks, so a search for the literal backticked snippet
    never matches. Only markers that are unambiguously formatting are removed
    (backticks, **bold**, ~~strike~~, [text](url), heading #) — single * / _
    are left alone since they're often literal (source_pane, a*b)."""
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)   # [text](url) -> text
    s = s.replace("`", "")                            # inline code backticks
    s = re.sub(r"\*\*|__|~~", "", s)                  # bold / strikethrough
    s = re.sub(r"^\s*#{1,6}\s+", "", s)               # ATX heading marker
    return s


def _pane_anchor_width(pane: str) -> int:
    """Max anchor length that fits on one visual row of `pane`.

    Claude Code wraps its output at the full pane width (measured: a 32-col
    pane's content rows reach exactly 32), so cap to pane_width − 1 (a hair of
    slack at the wrap column), clamped to [15, 50]. Falls back to 50 (the old
    fixed cap) when the width can't be resolved.
    """
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_width}"],
            capture_output=True, text=True, timeout=2)
        w = int(r.stdout.strip()) if r.returncode == 0 else 0
    except Exception:  # noqa: BLE001
        w = 0
    if w <= 0:
        return 50
    return min(50, max(15, w - 1))
def _anchor_for(text: str, max_len: int = 50) -> Optional[str]:
    """Single-line search anchor for spoken `text`, normalized to match the
    *rendered* pane (markdown stripped). Returns the plain (un-escaped) snippet
    — the longest line, trimmed to a word boundary within `max_len` chars so it
    fits on one visual row — or None if no line is long enough (>=15 chars) to
    be a unique search target. Shared by the auto-highlight (clip->cursor) and
    `replay-at-cursor` (cursor->clip) so both normalize text identically; if
    they drift, a clip that highlights wouldn't match-at-cursor.

    `max_len` defaults to 50 but the highlight path passes the target pane's
    width: tmux's search-backward matches within ONE visual row, so on a narrow
    pane (e.g. a 32-col phone) a 50-char anchor wraps and never matches. The
    anchor is always a prefix of a logical line, which renders from the left
    margin, so capping it to the pane width keeps it on that first row.
    """
    text = _strip_markdown_inline(text)
    # tmux search matches within one visual row, so flattening newlines (which
    # span wrapped rows) would never match — anchor on the longest single line.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    anchor = max(lines, key=len) if lines else text.strip()
    # A too-short anchor (one-word sentence, a bare heading) isn't unique:
    # search-backward then lands on spurious text. Skip those.
    if len(anchor) < 15:
        return None
    if len(anchor) <= max_len:
        return anchor
    if max_len < 15:
        # Pane too narrow to hold a unique (>=15 char) single-row anchor.
        return None
    cut = anchor[:max_len].rfind(" ")
    return anchor[:cut] if cut > 15 else anchor[:max_len]


def _pane_alternate_on(pane: str) -> bool:
    """True if `pane` is on the alternate screen — a fullscreen TUI (Claude Code
    and friends). That also means no tmux scrollback, so copy-mode only ever
    sees the *visible* screen, and the app likely has its own scroll/transcript
    view (e.g. Claude's Ctrl+O) that a held copy-mode would block."""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{alternate_on}"],
            capture_output=True, text=True)
        return r.returncode == 0 and r.stdout.strip() == "1"
    except Exception:  # noqa: BLE001
        return False


def _highlight_pidfile(pane: str) -> str:
    """Path to the per-pane clear-timer PID file. One file per pane so a new
    highlight can kill the previous sentence's pending clear-timer."""
    _pane_safe = re.sub(r"[^A-Za-z0-9_-]", "_", pane)
    return f"/tmp/media-highlight-clear-{_pane_safe}.pid"


def _kill_pending_clear(pane: str) -> None:
    """Kill any in-flight clear-timer for `pane` and drop its PID file.

    The clear-timer is a detached `sleep …; tmux send-keys -X …` process group
    (see `_tmux_highlight_text`). Without this, turning auto-highlight off — or
    ending playback — leaves the timer alive to fire `cancel`/`clear-selection`
    into the pane a beat later, yanking the view out from under the user."""
    import signal as _signal
    pidfile = _highlight_pidfile(pane)
    try:
        with open(pidfile) as _f:
            _pgid = int(_f.read().strip())
        try:
            os.killpg(_pgid, _signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    except (OSError, ValueError):
        pass
    try:
        os.unlink(pidfile)
    except OSError:
        pass


def _force_cancel_copy_mode(pane: str) -> None:
    """Cancel copy-mode on `pane` and verify it actually left the mode.

    A single `-X cancel` is a no-op if the pane isn't in copy-mode, and can be
    lost to a race with an entering highlight; re-check `#{pane_in_mode}` and
    retry once so we never strand the pane inside tmux copy-mode (which would
    eat the app's own scroll/transcript keys)."""
    for _ in range(2):
        in_mode, _pos = _pane_scroll_pos(pane)
        if not in_mode:
            return
        subprocess.run(["tmux", "send-keys", "-t", pane, "-X", "cancel"],
                       capture_output=True)


def _tmux_highlight_text(text: str, *, first: bool = False,
                         force: bool = False) -> bool:
    """Re-anchor copy-mode in the source pane onto the spoken text.

    Returns True when the sentence was found and marked — which is also the
    answer to "can you see these words?". copy-mode searches the scrollback of
    a normal pane and the visible screen of an alternate-screen one, so a
    failed search means the text is off screen with no way to reach it. That is
    exactly when a reader needs the words repeated somewhere else (see
    `_HighlightScheduler`), and it costs nothing extra to ask.

    Each call jumps to the bottom and searches backward for this sentence,
    so it tracks the right line regardless of prior position — including
    while the user has scrolled up in copy-mode. (We used to no-op when the
    user scrolled away from our last highlight; that rule is gone — the
    keystroke-recency skip in `_run` is the gentler way to stay out of the
    user's way, so highlighting now always follows the spoken text.)

    On an alternate-screen pane (Claude Code & other fullscreen TUIs) this is a
    *transient pulse* — flash then drop out of copy-mode so the app's own scroll
    keys (Claude's Ctrl+O) stay usable; on a normal-screen pane it parks the
    viewport on the sentence (scroll-and-hold). See `transient` below.

    Off by default — opt-in via the popup's `v` toggle (which writes to
    `$XDG_STATE_HOME/agent-media/auto-highlight`). `MEDIA_AUTO_HIGHLIGHT=1`
    in env can override on a per-host basis. `first` and `force` are accepted
    for call-site compatibility but no longer change anchoring (every call
    re-anchors).
    """
    if not os.environ.get("TMUX"):
        return False
    if not _is_auto_highlight_enabled():
        return False
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return False

    # Transient pulse vs scroll-and-hold. On an alternate-screen pane (Claude
    # Code & other fullscreen TUIs) we flash the sentence then drop out of
    # copy-mode, so the pane returns to the app's live view and its own
    # scroll/transcript keys (Claude's Ctrl+O) aren't blocked — and there's no
    # scrollback to hold onto anyway. On a normal-screen pane we keep the
    # scroll-and-hold follow-along (real scrollback to read along, no fullscreen
    # view to step aside for). A dumped pane (above) is now normal-screen with a
    # transcript to hold, so force scroll-and-hold there. Override with
    # MEDIA_HIGHLIGHT_TRANSIENT=1/0.
    _t_env = os.environ.get("MEDIA_HIGHLIGHT_TRANSIENT")
    if _t_env in ("0", "1"):
        transient = (_t_env == "1")
    else:
        transient = _pane_alternate_on(pane)

    # Build the search anchor (markdown stripped, longest single line, trimmed
    # to one visual row). Cap to the target pane's width: tmux search-backward
    # is row-bound, so on a narrow pane (a 32-col phone) a 50-char anchor wraps
    # and matches nothing — the main reason highlighting "doesn't work on the
    # phone". None = no line unique enough to search for — leave the prior
    # highlight in place rather than stranding the view at the bottom.
    snippet = _anchor_for(text, max_len=_pane_anchor_width(pane))
    if snippet is None:
        return False

    # Selection length = snippet length, capped so the highlight always fits
    # within a single visual row. cursor-right N beyond the row would drag
    # the viewport down, breaking the "viewport stays at sentence start"
    # invariant. Plain snippet (pre-regex-escape) gives the visual char count.
    select_len = len(snippet)

    snippet = re.sub(r'([][(){}^$.*+?|\\])', r'\\\1', snippet)

    # Flash duration: the selection stays visible for this long, then is
    # cleared while staying in copy-mode — pane stays scrolled to the
    # spoken text but the highlight fades. 0 = no auto-clear (selection
    # persists until the next sentence's highlight replaces it).
    flash_ms = int(os.environ.get("MEDIA_HIGHLIGHT_FLASH_MS", "1500"))

    pidfile = _highlight_pidfile(pane)

    # Per-pane PID file so each new highlight can kill the previous
    # sentence's pending clear-timer before it races into our selection.
    _kill_pending_clear(pane)

    # Remember where the pane sat before we touch it, so a failed search can
    # put the view back instead of stranding the reader at the bottom.
    _prev_in_mode, _prev_pos = _pane_scroll_pos(pane)

    try:
        # Ensure copy-mode is active (no-op if it already is, e.g. the user
        # scrolled the pane — which leaves it in copy-mode).
        subprocess.run(["tmux", "copy-mode", "-t", pane],
                       capture_output=True)
        # Re-anchor from the bottom on EVERY sentence, then search backward.
        # The old code searched *forward* from the previous match's cursor
        # for sentences 2..N, which a manual scroll between sentences would
        # throw off (the cursor moves with the user). history-bottom +
        # search-backward finds the latest occurrence of this sentence
        # regardless of where the viewport currently sits.
        subprocess.run(["tmux", "send-keys", "-t", pane, "-X", "history-bottom"],
                       capture_output=True)
        _before = _cursor_sig(pane)
        subprocess.run(["tmux", "send-keys", "-t", pane, "-X",
                        "search-backward", snippet],
                       capture_output=True)
        # If the search matched nothing, the cursor is still at the bottom
        # (history-bottom moved it there). Don't strand the reader at the end
        # of the buffer: restore the prior viewport if we had one (scroll back
        # up the same number of lines), otherwise just leave copy-mode.
        if _cursor_sig(pane) == _before:
            if _prev_in_mode and _prev_pos.isdigit() and int(_prev_pos) > 0:
                subprocess.run(["tmux", "send-keys", "-t", pane, "-X",
                                "-N", _prev_pos, "scroll-up"],
                               capture_output=True)
            else:
                subprocess.run(["tmux", "send-keys", "-t", pane, "-X", "cancel"],
                               capture_output=True)
            return False
        subprocess.run(["tmux", "send-keys", "-t", pane, "-X",
                        "begin-selection"],
                       capture_output=True)
        if select_len > 0:
            subprocess.run(["tmux", "send-keys", "-t", pane,
                            "-X", "-N", str(select_len), "cursor-right"],
                           capture_output=True)
        if flash_ms > 0:
            # After the flash window, end the highlight. Transient (alt-screen):
            # `cancel` drops out of copy-mode entirely so the app's own keys work
            # again between pulses. Otherwise `clear-selection` fades the mark but
            # stays in copy-mode, leaving the viewport parked on the sentence.
            #
            # The transient/hold choice is re-checked at *fire* time, not frozen
            # here: if the pane has since flipped to the alternate screen (the
            # user opened Claude's detailed-transcript / Ctrl+O view), we must
            # `cancel` rather than `clear-selection` — otherwise we'd leave the
            # pane parked in tmux copy-mode, eating the app's own scroll keys.
            # start_new_session makes this proc the session leader, so its PID is
            # its pgid; we record it so the next highlight can killpg it cleanly.
            _hold = "0" if transient else "1"
            proc = subprocess.Popen(
                ["sh", "-c",
                 f"sleep {flash_ms / 1000:.2f}; "
                 f'if [ "{_hold}" = "1" ] && '
                 f'[ "$(tmux display-message -p -t {pane} '
                 f"'#{{alternate_on}}' 2>/dev/null)\" != \"1\" ]; then "
                 f"tmux send-keys -t {pane} -X clear-selection 2>/dev/null; "
                 f"else tmux send-keys -t {pane} -X cancel 2>/dev/null; fi"],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                with open(pidfile, "w") as _f:
                    _f.write(str(proc.pid))
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        return False
    return True


def _follow_session(pane: str) -> tuple[str, int]:
    """(session, client width) for `pane`, or ("", 0) when tmux can't say."""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane or "",
             "#{session_name}\t#{client_width}"], capture_output=True, text=True)
        if r.returncode != 0:
            return "", 0
        name, _, width = r.stdout.strip().partition("\t")
        return name, (int(width) if width.isdigit() else 0)
    except (OSError, ValueError):
        return "", 0


def publish_follow_text(sentence: Optional[str], pane: str = "") -> None:
    """Put the follow-along rows' text where the status bar can read it now.

    The bar used to shell out for it — `#(media current-sentence …)` — and a
    `#()` runs at most once per `status-interval` and serves a cached result in
    between. Measured against a sentence written at a known moment: **1 to 2
    seconds** late, which for a thing whose only job is to keep pace with
    speech is the whole point missed. It also spawned a Python process per
    second per client to re-answer a question nothing had asked.

    A tmux option is read at draw time, so a `set` plus a status refresh puts
    the words up immediately and costs no process at all. `sentence=None`
    clears the rows to the idle hint.
    """
    rows = int(os.environ.get("MEDIA_FOLLOW_ROWS", "4") or 0)
    if rows <= 0 or not os.environ.get("TMUX"):
        return
    pane = pane or os.environ.get("TMUX_PANE") or ""
    session, width = _follow_session(pane)
    if not session:
        return
    width = max(20, width or 80)
    if not _is_auto_highlight_enabled():
        # Silent when the feature is off — the same answer the rows gave when
        # they were rendered by `media current-sentence --follow`, and the
        # reason the bar can safely keep its formats defined at all times.
        sentence = ""
        lines = [""]
    elif sentence:
        text = " ".join(str(sentence).split())
        lines = textwrap.wrap(f"♪ {text}", width=width - 1,
                              subsequent_indent="  ")[:rows] or [""]
        if len(lines) == rows and len(f"♪ {text}") > sum(len(x) for x in lines):
            lines[-1] = lines[-1][:max(0, width - 2)].rstrip() + "…"
    else:
        lines = ["#[fg=colour244]♪ follow-along on#[default]"]
    argv = ["tmux"]
    for i in range(rows):
        argv += ([";"] if i else []) + [
            "set", "-t", session, f"@am_follow_{i}",
            lines[i] if i < len(lines) else ""]
    argv += [";", "refresh-client", "-S"]
    try:
        subprocess.run(argv, capture_output=True)
    except OSError:
        pass


def _status_rows(session: str) -> Optional[int]:
    """How many status rows this session shows.

    tmux spells one row `on` and none `off`; only 2..5 are numbers. Reading it
    back as an int alone would make "one row" unrepresentable, and setting it
    to `1` is an error ("unknown value: 1"), not a synonym.
    """
    try:
        r = subprocess.run(["tmux", "show", "-t", session, "-v", "status"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
        raw = r.stdout.strip()
        return {"on": 1, "off": 0}.get(raw, None) or int(raw)
    except (OSError, ValueError):
        return None


def _set_follow_rows(show: bool, pane: str = "") -> None:
    """Give the spoken sentence its own status rows — or take them back.

    The row costs nothing while it sits there empty, but it isn't free: it is a
    row of the terminal. And most of the time it is redundant, because the
    words are on screen already — either in the pane's scrollback or in the
    app's own view. So the rows appear only when the sentence cannot be seen,
    and go away when the reply ends.

    Per session, not globally: another session's panes should not resize
    because this one is speaking. `MEDIA_FOLLOW_ROWS` (default 4) is how many
    rows the sentence gets — enough that a long one wraps whole rather than
    ending in an ellipsis; 0 turns the whole mechanism off and leaves the
    status bar however you configured it. Keep it in step with how many
    status-format rows the tmux config lays out.
    """
    rows = int(os.environ.get("MEDIA_FOLLOW_ROWS", "4") or 0)
    if rows <= 0:
        return
    if not os.environ.get("TMUX"):
        return
    # Three heights, not two:
    #
    #   off            1  just the bar. The rows render nothing when the
    #                     feature is off, so any more would be blank lines.
    #   on             2  one row: the sentence being spoken, or — between
    #                     replies — that following along is switched on. This
    #                     is the confirmation that the switch did something,
    #                     and it has to exist when nothing is playing, which is
    #                     when the switch is usually thrown.
    #   on + unreachable   1 + MEDIA_FOLLOW_ROWS: the words can't be seen in
    #                     the pane, so the bar carries the whole sentence.
    #
    # The scheduler's `enabled` is about *this turn* (hook source, not typing
    # just now); whether following along is on at all lives in the flag, which
    # _tmux_highlight_text checks separately — so asking it here is what stops
    # the bar growing to display nothing.
    following = _is_auto_highlight_enabled()
    if show and not following:
        return
    try:
        target = pane or os.environ.get("TMUX_PANE") or ""
        if not target:
            return
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{session_name}"],
            capture_output=True, text=True)
        session = r.stdout.strip()
        if r.returncode != 0 or not session:
            return
        want = (1 + rows) if show else (2 if following else 1)
        # Only when it differs: every set redraws the client, and a redraw of
        # the status bar re-runs the very commands that render it.
        if _status_rows(session) == want:
            return
        subprocess.run(
            ["tmux", "set", "-t", session, "status",
             str(want) if want > 1 else "on"],   # `1` is an error, `on` is one
            capture_output=True)
    except OSError:
        pass


def _follow_helper() -> str:
    """Absolute path to media-follow-pane, or its bare name as a last resort.

    A hook inherits a minimal PATH — /usr/bin and little else — so relying on
    ~/.local/bin being on it means the view silently never opens in exactly the
    case it exists for. We know where our own source tree is, and the helper
    ships beside it. (Same lesson as render's `_engine_path`.)
    """
    import shutil
    found = shutil.which("media-follow-pane")
    if found:
        return found
    local = Path(__file__).resolve().parent.parent.parent.parent / "tmux" / "media-follow-pane"
    return str(local) if local.exists() else "media-follow-pane"


def ensure_follow_view(open_: bool = True, *, pane: str = "",
                       deliberate: bool = False) -> None:
    """Open (or close) the follow-along pane to match "I want to read along".

    Opt-in (`MEDIA_FOLLOW_AUTO=1`): a pane that opens itself takes rows from
    the conversation on every reply, which is a lot to charge for reading along
    — the status line's sentence row costs one row of chrome and no layout. So
    the coupling is there for whoever wants the fuller view without a keypress,
    and off otherwise; `prefix F` opens it on demand either way.

    Opening is `auto`, not `open`: where there is room the view splits in
    alongside, and where there isn't it stays shut rather than putting a window
    you cannot see into the window list. `prefix F` (and the highlight toggle,
    which is a deliberate press) still open it there, and say where it went.

    Best-effort and detached: this runs on the path to speaking, and a tmux
    that is slow, absent or unhappy must never delay or fail an utterance.
    """
    if not os.environ.get("TMUX"):
        return
    if open_:
        # The pane is the heavyweight surface — it charges the conversation
        # rows — so it stays opt-in (MEDIA_FOLLOW_AUTO=1) even when `v` asks
        # for follow-along; the status row already carries the same sentence
        # for a row of chrome. Where it IS opted into, `v` opens and closes it
        # along with everything else.
        if os.environ.get("MEDIA_FOLLOW_AUTO") != "1":
            return
        if not _is_auto_highlight_enabled():
            return              # following along was never asked for
    env = dict(os.environ)
    if pane:
        env["MEDIA_FOLLOW_TARGET"] = pane
    if not open_:
        action = "close"
    else:
        action = "open" if deliberate else "auto"
    try:
        subprocess.Popen([_follow_helper(), action],
                         env=env, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        pass                    # not installed on this host; the highlight
                                # still works where it can


class _HighlightScheduler:
    """Fire `_tmux_highlight_text` so the on-screen highlight lands *with* the
    audio instead of ahead of it.

    Speech routed through Snapcast (the `rooms` target) is only audible a fixed
    buffer later (snapserver.conf `buffer`, exposed as MEDIA_SNAPCAST_LATENCY_MS).
    play() returns as soon as mpv starts feeding the sink, so highlighting then
    runs ~that buffer ahead of the listener. We defer each natural highlight by
    that delay on a daemon timer — non-blocking, so the reader keeps feeding the
    next clip gaplessly meanwhile (a blocking sleep would inject silence on clips
    shorter than the delay).

    Because the reader advances on mpv-idle (feed done) while the listener is
    still hearing the clip, several timers can be in flight for back-to-back
    short clips; they're left to fire in order at their own onset times rather
    than cancelling one another. `cancel_pending()` drops the queue on a manual
    skip-to-end; `show(force=True)` (a manual h/l/H/L jump) abandons the queue
    and highlights immediately for instant feedback on the keypress; `drain()`
    lets the natural tail fire before the (often short-lived) process exits.
    """

    def __init__(self, delay_s: float, enabled: bool, pane: str = ""):
        self._delay = delay_s
        self._enabled = enabled
        self._pane = pane or os.environ.get("TMUX_PANE") or ""
        self._rows_shown = False
        self._lock = threading.Lock()
        self._timers: list[threading.Timer] = []
        self._dbg = bool(os.environ.get("MEDIA_HL_DEBUG"))
        if self._dbg:
            self._log(f"INIT delay_s={delay_s} enabled={enabled} pid={os.getpid()}")

    def _log(self, msg: str) -> None:
        try:
            with open("/tmp/am-hl-debug.log", "a") as f:
                f.write(f"{time.time():.3f} {msg}\n")
        except OSError:
            pass

    def _reap(self) -> None:
        with self._lock:
            self._timers = [t for t in self._timers if t.is_alive()]

    def publish(self, sentence: str) -> None:
        """The rows' text, on every path that speaks a sentence."""
        publish_follow_text(sentence, self._pane)

    def _seen(self, found: bool) -> None:
        """Latch the status rows open the first time a sentence can't be found.

        Per-sentence flipping would be the obvious rule and the wrong one: the
        rows change the session's status height, so every flip resizes the
        panes and makes a fullscreen TUI redraw. One reply that alternated
        visible/not-visible sentences would strobe. Once the reader has been
        sent to the rows, they stay for the rest of the reply.
        """
        if found or self._rows_shown:
            return
        self._rows_shown = True
        _set_follow_rows(True, self._pane)

    def done(self) -> None:
        """Give the rows back at the end of a reply.

        Unconditional, not only when we opened them: turning follow-along on
        mid-reply opens them from the popup, outside this object's knowledge,
        and rows nobody closes stay open. Setting a height that is already set
        is a no-op, and one reply speaks at a time per session.
        """
        self._rows_shown = False
        publish_follow_text(None, self._pane)     # back to the idle hint
        _set_follow_rows(False, self._pane)

    def show(self, sentence: str, *, first: bool, force: bool) -> None:
        # Re-asked per sentence, not frozen at the start of the reply: the
        # keystroke skip that usually turns `enabled` off is about having been
        # typing a moment ago, and pressing `v` (or `prefix V`) mid-reply is
        # exactly the statement that you have stopped and are attending now.
        if not self._enabled and not _force_highlight_active(self._pane):
            return
        if force or self._delay <= 0:
            self.cancel_pending()
            if self._dbg:
                self._log(f"SHOW-NOW force={force} delay={self._delay} {sentence[:30]!r}")
            self.publish(sentence)
            self._seen(_tmux_highlight_text(sentence, first=first, force=force))
            return
        self._reap()
        if self._dbg:
            self._log(f"SCHEDULE +{self._delay}s {sentence[:30]!r}")

        def _fire():
            if self._dbg:
                self._log(f"FIRE {sentence[:30]!r}")
            self.publish(sentence)
            self._seen(_tmux_highlight_text(sentence, first=first))

        t = threading.Timer(self._delay, _fire)
        t.daemon = True
        with self._lock:
            self._timers.append(t)
        t.start()

    def cancel_pending(self) -> None:
        with self._lock:
            timers, self._timers = self._timers, []
        for t in timers:
            t.cancel()

    def drain(self) -> None:
        with self._lock:
            timers = list(self._timers)
        for t in timers:
            t.join()
        self.done()


def _highlight_wanted(event: Event, pane: str) -> bool:
    """Whether this turn should paint the copy-mode follow-along highlight.

    CLI text is never in the pane, so there is nothing to highlight. Otherwise
    skip the turn if the user has typed in the source pane recently: grabbing
    copy-mode mid-keystroke yanks the view out from under them. Two things
    override the skip ("I'm attending — follow this one"): the `highlight-now`
    force key (tmux `prefix V`, until they type again), and the control popup
    being open for this pane.
    """
    from ..types import Source as _Source
    if event.source in (_Source.CLI,):
        return False
    window_s = float(os.environ.get("MEDIA_HIGHLIGHT_KEYSTROKE_S", "5"))
    if (pane and _pane_recent_keystrokes(pane, window_s)
            and not _force_highlight_active(pane)
            and not _popup_open_for(pane)):
        return False
    return True


def _playout_delay_s(target_name: str) -> float:
    """How long after we start a clip its audio is actually *heard*, in seconds.

    Speech routed through Snapcast (`rooms`) is a fixed buffer late
    (MEDIA_SNAPCAST_LATENCY_MS). A target that plays on another device via its
    own local mpv has no such buffer — only the bridge hop and mpv start — so a
    per-target override wins: MEDIA_SPEECH_PLAYOUT_MS_<TARGET>.
    """
    key = f"MEDIA_SPEECH_PLAYOUT_MS_{target_name.upper().replace('-', '_')}"
    return float(os.environ.get(key)
                 or os.environ.get("MEDIA_SNAPCAST_LATENCY_MS", "500")) / 1000.0


def _apportioned_offsets(sentences: list[str], total: float) -> list[float]:
    """Sentence start times guessed from the total, by share of characters.

    The honest fallback for a renderer that reports a duration but no sentence
    marks. Speech rate is near enough constant within one voice that character
    share tracks time share; it drifts within a reply, but a highlight a beat
    off is worth far more than no highlight at all.
    """
    weights = [max(1, len(s)) for s in sentences]
    span = sum(weights)
    offsets: list[float] = []
    acc = 0
    for w in weights:
        offsets.append(total * acc / span)
        acc += w
    return offsets


def _offsets_from_marks(marks: dict[int, float], count: int,
                        total: float) -> Optional[list[float]]:
    """The renderer's own sentence boundaries, when they can be trusted.

    Returns None — meaning "approximate instead" — unless there is exactly one
    mark per sentence, in order, inside the clip. A count mismatch means the far
    side split the text differently from us (different commits, most likely),
    and highlighting sentence k at sentence j's onset is worse than a smooth
    approximation: it points confidently at the wrong words.
    """
    if count <= 0 or sorted(marks) != list(range(count)):
        return None
    offsets = [marks[i] for i in range(count)]
    if offsets != sorted(offsets) or offsets[0] < 0:
        return None
    if total > 0 and offsets[-1] > total + 1.0:
        return None
    return offsets


def stamp_speech_pause(state: StateStore, paused: Optional[bool] = None) -> None:
    """Record that the audio stopped (or started again) on the now-playing row.

    The lanes that play on another device follow their timeline on a clock, so
    a pause has to be written down or the highlight reads on through the
    silence. `paused_at` freezes the reading where it stood; the resume adds
    the pause's length to `play_started_at`, which is the same correction as
    never having stopped. `paused=None` flips whatever the row says — that is
    how the remote lane's `cycle pause` reports itself.

    Called both by whoever issues a pause here (cli's toggle) and by the report
    stream when the far side says its player was paused by something else.
    """
    try:
        np = state.get_now_playing("speech")
        if not np:
            return
        ex = np.get("extras") or {}
        was = bool(ex.get("paused_at"))
        now_paused = (not was) if paused is None else bool(paused)
        if now_paused == was:
            return
        now = time.time()
        if now_paused:
            ex["paused_at"] = now
        else:
            held = now - float(ex["paused_at"])
            started = float(ex.get("play_started_at") or 0)
            if started:
                ex["play_started_at"] = started + held
            # Same correction for the other clock: a reading taken before the
            # pause is still that reading, but the time since it was taken no
            # longer all counts as playing.
            read_at = float(ex.get("live_pos_at") or 0)
            if read_at:
                ex["live_pos_at"] = read_at + held
            ex.pop("paused_at", None)
        ex["live_pause"] = now_paused
        state.set_now_playing(
            "speech", uri=np.get("uri") or "",
            started_at=np.get("started_at") or now,
            target=np.get("target") or os.environ.get(
                "MEDIA_SPEECH_DEFAULT_TARGET", "local"),
            extras=ex)
    except Exception:  # noqa: BLE001 — a pause must still reach the player
        pass


def elapsed_from_row(extras: dict, origin: float) -> float:
    """How far into the reply we are, according to the row.

    The lanes that play on another device follow a timeline on the clock,
    because asking the player costs ~600ms on a link that drops a quarter of
    its packets. A clock does not know the audio was paused — so the row says
    so instead: `paused_at` freezes the reading at the moment the pause was
    issued, and the resume pushes `play_started_at` forward by however long it
    lasted. Both are written by whoever issues the pause (cli's toggle), which
    is the only party that reliably knows it happened.
    """
    base = float(extras.get("play_started_at") or origin)
    paused_at = extras.get("paused_at")
    now = float(paused_at) if paused_at else time.time()
    return max(0.0, now - base)


def carry_pause_stamp(prior: dict, extras: dict, live_seen: bool) -> None:
    """Keep `paused_at` across a rebuild of the now-playing row, in place.

    The clip lane rebuilds the row from scratch on every mark, because
    everything on it — the text, the clips, the offsets — belongs to the
    process doing the marking. `paused_at` does not: it is stamped by whoever
    pressed pause, in another process, between two marks. So the rebuild wiped
    it about a second after it was written, and `stamp_speech_pause`'s resume
    correction was quietly retired on that lane — with no record of when the
    silence began, there was nothing to take off the clock when it ended.

    `live_seen` says whether this pass actually read the player, because that
    is what decides who wins: a reading is fresher than a stamp and may say the
    pause is over, while no reading at all cannot contradict one.
    """
    held = prior.get("paused_at")
    if extras.get("live_pause"):
        # Paused, and no stamp to date it: the pause came from somewhere that
        # does not stamp — the app's own transport, call-guard. Date it now
        # rather than leave the row saying paused-since-never.
        extras["paused_at"] = held or time.time()
    elif not live_seen and held:
        extras["paused_at"] = held


class _SentenceFollower:
    """Keep `current_sentence` moving for a lane whose audio plays elsewhere.

    On the phone lane the whole reply is one POST: the phone renders it and
    plays it on its own mpv, so red5's sentence loop never runs and nothing here
    can observe playback without paying a ~600ms round trip per poll on a link
    that already drops a quarter of its packets. But the renderer says when it
    is about to start and where each sentence begins, and a clock is free — so
    we follow the *timeline* rather than the player.

    That is enough for everything that reads the row: `media current-sentence`,
    `media highlight-now`, the popup's sentence view, and the copy-mode
    follow-along highlight.
    """

    def __init__(self, state: StateStore, target_name: str, started_at: float,
                 sentences: list[str], *, pane: str, highlight: bool,
                 delay_s: float, on_sentence=None):
        self.state = state
        self.target_name = target_name
        self.started_at = started_at
        self.sentences = sentences
        self.pane = pane
        self.highlight = highlight
        self.delay_s = delay_s
        # Told each time the sentence changes, so a display somewhere else can
        # follow the reply rather than show its opening line for two minutes.
        self.on_sentence = on_sentence
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def timeline(self, marks: dict[int, float],
                 total: float) -> Optional[tuple[list[float], bool]]:
        """(sentence start times, measured?) — or None if there's nothing to follow.

        Measured marks when the renderer reported a usable set, otherwise the
        duration apportioned across the sentences. The flag rides along to the
        row so a display can tell a measured timeline from a guessed one.
        """
        if not self.sentences or total <= 0:
            return None
        measured = _offsets_from_marks(marks, len(self.sentences), total)
        if measured is None and marks:
            log.info("intake: remote renderer reported %d sentence marks for "
                     "%d sentences; approximating instead",
                     len(marks), len(self.sentences))
        if measured is not None:
            return measured, True
        return _apportioned_offsets(self.sentences, total), False

    def start(self, offsets: list[float], play_started_at: float) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, args=(offsets, play_started_at), daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.5) -> None:
        """Stop following and wait, so a late write can't resurrect a cleared row."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self, offsets: list[float], play_started_at: float) -> None:
        highlighter = _HighlightScheduler(self.delay_s, self.highlight,
                                          self.pane)
        if self.highlight and self.pane:
            # _tmux_highlight_text reads the pane from the environment and needs
            # a truthy TMUX; this thread's process may be the detached playback
            # child, which has neither. Same fixup cmd_replay_track does.
            os.environ["TMUX_PANE"] = self.pane
            if not os.environ.get("TMUX"):
                os.environ["TMUX"] = "x"
        mine = os.getpid()
        current = -1
        try:
            while not self._stop.is_set():
                np = self.state.get_now_playing("speech")
                if not np:
                    return                      # the reply ended, or was cut
                extras = np.get("extras") or {}
                if extras.get("writer_pid") not in (None, mine):
                    return                      # someone else owns the row now
                # Re-read the origin each tick rather than trusting the one we
                # started with: `media skip` re-stamps it to seek this lane (one
                # clip, no playlist), a pause freezes it, and a follower running
                # off a stale origin would drag the highlight straight back to
                # where it was — or read on through silence.
                elapsed = elapsed_from_row(extras, play_started_at)
                idx = 0
                for i, off in enumerate(offsets):
                    if elapsed + 0.001 >= off:
                        idx = i
                    else:
                        break
                if idx != current:
                    current = idx
                    extras["current_sentence"] = self.sentences[idx]
                    extras["current_sentence_idx"] = idx
                    try:
                        self.state.set_now_playing(
                            "speech",
                            uri=np.get("uri") or f"remote-say:{self.target_name}",
                            started_at=np.get("started_at") or self.started_at,
                            target=np.get("target") or self.target_name,
                            extras=extras)
                    except Exception:  # noqa: BLE001
                        pass
                    highlighter.show(self.sentences[idx], first=(idx == 0),
                                     force=False)
                    if self.on_sentence is not None:
                        try:
                            self.on_sentence(self.sentences[idx])
                        except Exception:  # noqa: BLE001 — never break the follow
                            pass
                self._stop.wait(0.1)
        except Exception:  # noqa: BLE001 — a highlight must never break speech
            pass
        finally:
            highlighter.drain()


def _speech_lock_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return state / "agent-media" / "speech-playback.lock"


def _speech_wait_dir() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return state / "agent-media" / "speech-waiters"


def _speech_supersede_dir() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return state / "agent-media" / "speech-supersede"


def _speech_events_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return state / "agent-media" / "speech-events.jsonl"


def _speech_event(kind: str, **fields) -> None:
    """Append a start/end breadcrumb to the speech event log.

    One JSON object per line, newest last: {"ts": ..., "event": "start"|"end",
    "text": ..., "session": ..., ...}. Served by the visual-canvas server's
    /speech endpoint, so an outside agent — a voice-mode Claude peeking through
    the tmux relay — can see when playback began and ended and what was said,
    without opening the sqlite store. Best-effort throughout: speech must never
    fail because its diary could not be written.
    """
    try:
        path = _speech_events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        rec: dict = {"ts": round(time.time(), 3), "event": kind}
        rec.update({k: v for k, v in fields.items() if v not in (None, "")})
        with path.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        # Trim once the log grows past ~256 KiB, keeping the newest 200 events.
        # Two writers racing the trim can lose a line from the OLD tail, never
        # corrupt a fresh one — acceptable for a diary that only ever needs its
        # recent past.
        if path.stat().st_size > 256 * 1024:
            tail = path.read_text().splitlines()[-200:]
            path.write_text("\n".join(tail) + "\n")
    except OSError:
        pass


def _speech_flush_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return state / "agent-media" / "speech-flush"


def _speech_hold_path() -> Path:
    """The unnamed holder's marker — one file, as it has always been."""
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return state / "agent-media" / "speech-hold"


def _speech_hold_dir() -> Path:
    """Per-owner markers, one file each.

    A directory rather than one file holding a map of owners: two holders
    racing on a read-modify-write of a shared file can drop one of the two
    holds, and the loser is silently un-held while it believes it is holding.
    Separate files need no lock — each holder only ever writes its own — and an
    expired one is reaped by whoever reads next, exactly like the single
    marker.
    """
    return _speech_hold_path().with_name("speech-hold.d")


_OWNER_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


def _owner_marker(owner: str) -> Path:
    """Marker path for `owner`, or raise if the name cannot be a filename.

    Owners come from callers (`--owner cece`), so they are untrusted input on
    a path: a name containing a slash or `..` would write outside the hold
    directory. Validated rather than sanitised — silently rewriting an owner
    name would let two different callers collide on one marker.
    """
    if not _OWNER_OK.match(owner):
        raise ValueError(
            f"invalid hold owner {owner!r}: use letters, digits, dot, dash or "
            "underscore (max 32 chars)"
        )
    return _speech_hold_dir() / owner


def request_speech_flush() -> float:
    """Drop every *pending* reply — queued behind the speaker, still
    rendering, or waiting out a hold — as of now (`media speech-flush`).

    Writes a monotonic-max timestamp marker, exactly like the supersede
    marker but global: any submission whose seq predates it skips playback at
    the last checkpoint before its first clip would play. Two deliberate
    limits: the clip already *speaking* is not cut (that is what pause / stop
    / supersede are for), and a flushed reply STILL writes its history row,
    marked flushed — the flush cancels audio, never the archived record.
    """
    now = time.time()
    try:
        path = _speech_flush_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            prev = float(path.read_text().strip())
        except (OSError, ValueError):
            prev = 0.0
        path.write_text(repr(max(prev, now)))
    except OSError:
        pass
    return now


def _speech_flushed(seq: float) -> bool:
    """True when a flush was requested after this submission was made."""
    try:
        return float(_speech_flush_path().read_text().strip()) > seq
    except (OSError, ValueError):
        return False


def set_speech_hold(seconds: float, owner: str | None = None) -> float:
    """Hold the START of new speech playback, returning the expiry epoch
    (0.0 if the marker could not be written).

    The timeout is mandatory and lives inside the marker itself, clamped to
    MEDIA_SPEECH_HOLD_MAX_S (default 300): expiry needs no process alive to
    enforce it, so a crashed or forgetful holder can never silence the phone
    forever. In-flight audio is not paused (pair with `media pause` for
    that); held replies keep their queue order and play when the hold lifts.

    With `owner`, the hold is one of several: speech stays held while ANY
    owner's hold is live, and releasing yours cannot lift someone else's.
    Without one it writes the original single marker, so an existing caller
    keeps working unchanged.
    """
    try:
        cap = float(os.environ.get("MEDIA_SPEECH_HOLD_MAX_S", "300"))
    except ValueError:
        cap = 300.0
    until = time.time() + max(1.0, min(float(seconds), cap))
    path = _speech_hold_path() if owner is None else _owner_marker(owner)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(repr(until))
    except OSError:
        return 0.0
    # Published only after the marker is written: the mirror must never claim a
    # hold that does not exist locally, and the local write is what decides.
    _mirror("speech", owner, "hold", until - time.time())
    return until


def release_speech_hold(owner: str | None = None, everyone: bool = False) -> None:
    """Lift a hold. Yours by default — never everyone's by accident.

    `owner=None` releases the unnamed marker only, so a legacy caller lifts
    exactly what it set. `everyone=True` is the deliberate override for a
    stuck channel, and is the only way one caller can drop another's hold.
    """
    if everyone:
        targets = [_speech_hold_path(), *_speech_hold_dir().glob("*")]
    elif owner is not None:
        targets = [_owner_marker(owner)]
    else:
        targets = [_speech_hold_path()]
    for path in targets:
        try:
            path.unlink()
        except OSError:
            pass
    _mirror("speech", "*" if everyone else owner, "release")


def _mirror(channel: str, owner: str | None, action: str,
            ttl_s: float | None = None) -> None:
    """Best-effort publish to the relay's floor mirror. Never load-bearing.

    Imported lazily and wrapped: this is a hot path (every hold, every release)
    and the mirror is an observability feature. Nothing about a hold may depend
    on the relay being installed, reachable, or working.
    """
    try:
        from ..capture.floor import publish
        publish(channel, owner or "unnamed", action, ttl_s)
    except Exception:  # noqa: BLE001
        pass


def _read_marker(path: Path) -> float:
    """One marker's expiry, reaping it if it has passed. 0.0 when not held."""
    try:
        until = float(path.read_text().strip())
    except (OSError, ValueError):
        return 0.0
    if until <= time.time():
        try:
            path.unlink()
        except OSError:
            pass
        return 0.0
    return until


def speech_holders() -> dict[str, float]:
    """Live holds as ``{owner: expiry}``; the unnamed one appears as ''.

    Expired markers are reaped on read, so an abandoned hold cleans itself up
    the first time anyone looks — including a holder that died between its two
    edges, which is the failure the mandatory expiry exists to bound.
    """
    held: dict[str, float] = {}
    legacy = _read_marker(_speech_hold_path())
    if legacy:
        held[""] = legacy
    try:
        markers = sorted(_speech_hold_dir().glob("*"))
    except OSError:
        markers = []
    for path in markers:
        until = _read_marker(path)
        if until:
            held[path.name] = until
    return held


def speech_hold_until() -> float:
    """The latest expiry among live holds, or 0.0 when nothing holds.

    Speech is held while ANY owner holds it, so the consumer waits for the
    last one to lift. Unchanged for the single-holder case.
    """
    held = speech_holders()
    return max(held.values()) if held else 0.0


def _wait_speech_hold(refresh: "Optional[Callable[[], None]]" = None,
                      refresh_every_s: float = 30.0) -> None:
    """Block while a hold is active. Re-reads the marker each tick so an early
    `media speech-hold --release` lifts it immediately; the expiry stored in
    the marker bounds the wait even if nobody ever releases it.

    `refresh` is called on entry and every `refresh_every_s` while the wait
    lasts. Callers waiting outside the playback token pass their
    `announce()` so their place in their session's queue does not age out
    (MEDIA_SPEECH_PENDING_TTL_S) — a wait longer than the pending TTL would
    otherwise let a later sibling speak first the moment the hold lifts.
    """
    last = 0.0
    while speech_hold_until() > 0.0:
        if refresh is not None and (last == 0.0
                                    or time.monotonic() - last >= refresh_every_s):
            refresh()
            last = time.monotonic()
        time.sleep(0.2)


# Priority -> numeric rank. Higher rank preempts lower; equal ranks queue.
_PRIO_RANK = {
    Priority.LOW: 0,
    Priority.NORMAL: 10,
    Priority.HIGH: 20,
    Priority.URGENT: 30,
}


def _rank_of(priority: Priority) -> int:
    return _PRIO_RANK.get(priority, _PRIO_RANK[Priority.NORMAL])


def _order_session(source_pane: str, source_session: str) -> str:
    """The identity the playback lock orders a clip within (see
    `_SpeechPlaybackLock`): same identity -> canonical submission order,
    different identity -> priority preemption.

    Prefer the *pane* over the Claude session id. One pane is one conversation's
    worth of speech no matter which producer emitted it, but the producers don't
    agree on a session id: the Stop / PreToolUse hooks tag events with the hook
    payload's session id while the `say` MCP tool tags none at all. Keying on
    that id makes a spoken lead-in and the AskUserQuestion read-out that follows
    it look like two different sessions, so the HIGH-priority question preempts
    the prose it was meant to follow. Both producers live in (or were spawned
    from) the agent's pane and inherit its TMUX_PANE, so the pane is the id they
    do share. Off tmux there is no pane, so fall back to the session id and the
    old behaviour.
    """
    return source_pane or source_session


def _pending_ttl_s() -> float:
    """How long a *pending* (announced-but-not-yet-rendered) waiter entry keeps
    holding its place in its session's queue. A render that takes longer than
    this is assumed broken, and the entry stops blocking its siblings."""
    try:
        return float(os.environ.get("MEDIA_SPEECH_PENDING_TTL_S", "120"))
    except ValueError:
        return 120.0


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check used to reap stale waiter entries."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # e.g. EPERM — alive but not ours
    return True


class _SpeechPlaybackLock:
    """Priority-aware serialization of speech *playback* across processes.

    Every session's hook talks to the one shared sink-speech broker and the
    one `am` Snapcast stream, so only one may play at a time. An exclusive
    `flock` on `speech-playback.lock` is the "currently speaking" token.
    Without it concurrent readers `loadfile replace` over each other and poll
    the same idle state, so their audio interleaves and each pane's highlight
    desyncs hopelessly.

    Priority (set by the intake hooks: notifications/prompts = HIGH, responses
    = NORMAL) decides what happens when *another session* wants the token:

      * HIGH / URGENT  -> preempt: the current speaker steps aside at its next
                          sentence boundary (`should_yield` -> `yield_to_higher`)
                          and resumes when the higher clip finishes.
      * NORMAL         -> queue: wait for the token, never interrupt.
      * LOW            -> skip: if the token is already held, give up rather
                          than queue (ambient announcements aren't worth a wait).

    Priority preemption is scoped to *cross-session* contention only. Within a
    single Claude session, speech does not preempt speech: a session's own clips
    play in submission (canonical) order regardless of priority, so a short
    HIGH notification can't cut ahead of that same session's longer NORMAL
    reply — it queues behind it. "Session" here is the *pane* wherever there is
    one (see `_order_session`), so everything one conversation says — hook
    read-outs and `say` MCP calls alike — is ordered together rather than
    preempting itself. (A clip with neither pane nor session id is treated as
    its own session, so it still preempts by priority as before.) Same-session
    ordering is enforced at admission via a per-clip submission timestamp; a
    clip already speaking otherwise finishes rather than being cut short.

    That timestamp is the caller's *submission* time (`acquire(seq=...)`), NOT
    the moment the token is asked for — rendering happens before acquire() and
    takes longer for a longer reply, so acquire-time ordering would hand the
    queue to whichever sibling was shortest. For the same reason a sibling that
    is still rendering announces itself as a *pending* waiter up front
    (`announce()`), so a later, faster-rendering sibling can see it and defer
    instead of speaking first. Pending entries are same-session-ordering only:
    they never preempt another session and never make a speaker yield, and they
    stop counting after MEDIA_SPEECH_PENDING_TTL_S so a wedged render can't
    mute its session forever.

    The one same-session exception is URGENT — a deliberate "stop and hear this"
    barge-in. An URGENT clip interrupts its session's in-progress clip at the
    next boundary and jumps ahead of its queued siblings. By default the
    interrupted clip resumes afterwards (nothing lost); if the URGENT clip is
    tagged `supersede`, the messages it interrupts/precedes are dropped instead
    (see `should_abort` and the `speech-supersede` marker).

    Waiters announce themselves in a lockless registry — one file per waiter
    under `speech-waiters/`, named `<pid>.<token>` and holding the waiter's
    rank, submission time, and session id — so a holder can tell whether anyone
    with precedence is waiting. Dead-pid
    entries are reaped on scan, so a crashed waiter never wedges anyone, and
    `flock` is released on fd close / process death, so a crashed holder frees
    the token. A genuinely *wedged* holder would hold it indefinitely, so
    non-LOW waiters give up after MEDIA_SPEECH_LOCK_TIMEOUT_S (default 600) and
    play unserialized rather than be lost — but a holder that's *deliberately*
    paused (popup Space) is exempted from that give-up, so a queued reply never
    overtakes it and clobbers its now_playing name. Set MEDIA_SPEECH_SERIALIZE=0
    to disable.

    Rendering is intentionally left outside the lock so sessions still render
    their clips in parallel; only the broker hand-off serializes.
    """

    def __init__(self) -> None:
        self._fd: Optional[int] = None
        self._rank: int = _PRIO_RANK[Priority.NORMAL]
        # The Claude session this speech belongs to. Priority preemption only
        # applies *across* sessions; within one session speech never preempts
        # speech and siblings play in submission (canonical) order. Empty means
        # "unknown" and is treated as distinct from every other waiter, so a
        # message with no session id still preempts by priority as before.
        self._session: str = ""
        # Submission time, used to order same-session siblings. Set once in
        # announce()/acquire() and preserved across yield_to_higher() re-takes.
        self._seq: float = 0.0
        # True between announce() and acquire(): we're still rendering, so we
        # hold our place in our session's queue but don't contend with anyone.
        self._pending: bool = False
        # Whether this is a supersede barge-in — one that drops the same-session
        # messages it interrupts/precedes instead of letting them resume.
        self._supersede: bool = False
        # Lazily-created read-only handle for polling holder progress while we
        # wait for the token (see _holder_progress_sig).
        self._progress_store: Optional[StateStore] = None
        # Unique per instance so two locks in one process (e.g. tests) don't
        # collide; the pid prefix lets the waiter scan reap dead entries.
        self._token = f"{os.getpid()}.{uuid.uuid4().hex}"

    # ---- waiter registry -------------------------------------------------

    def _register(self) -> None:
        try:
            d = _speech_wait_dir()
            d.mkdir(parents=True, exist_ok=True)
            # Four lines: rank, submission seq, session id, pending flag. The
            # session keeps its own line so an id containing odd characters
            # can't corrupt the numeric fields; the pending flag is appended
            # after it rather than inserted, so older three-line (and
            # single-line, rank-only) files still parse.
            (d / self._token).write_text(
                f"{self._rank}\n{self._seq!r}\n"
                f"{self._session}\n{1 if self._pending else 0}")
        except OSError:
            pass

    def _unregister(self) -> None:
        try:
            (_speech_wait_dir() / self._token).unlink()
        except OSError:
            pass

    def _same_session(self, other: str) -> bool:
        """True only when both sessions are known and identical. Unknown ("")
        sessions are treated as distinct, so priority preemption still applies
        when a message carries no session id."""
        return bool(self._session) and bool(other) and other == self._session

    def _other_waiters(self) -> "list[tuple[int, float, str, bool]]":
        """(rank, seq, session, pending) for every *other* live waiter. Reaps
        stale (dead-pid) entries as a side effect, and drops pending entries
        whose render has blown past MEDIA_SPEECH_PENDING_TTL_S."""
        out: "list[tuple[int, float, str, bool]]" = []
        pending_floor = time.time() - _pending_ttl_s()
        try:
            entries = list(_speech_wait_dir().iterdir())
        except OSError:
            return out
        for f in entries:
            if f.name == self._token:
                continue
            try:
                pid = int(f.name.split(".", 1)[0])
            except (ValueError, IndexError):
                continue
            if not _pid_alive(pid):
                try:
                    f.unlink()
                except OSError:
                    pass
                continue
            try:
                lines = f.read_text().splitlines()
                rank = int(lines[0].strip())
            except (OSError, ValueError, IndexError):
                continue
            try:
                seq = float(lines[1].strip()) if len(lines) > 1 else 0.0
            except ValueError:
                seq = 0.0
            session = lines[2] if len(lines) > 2 else ""
            pending = len(lines) > 3 and lines[3].strip() == "1"
            if pending and seq < pending_floor:
                continue    # render wedged/abandoned; stop holding the queue
            out.append((rank, seq, session, pending))
        return out

    def _preempting_rank(self) -> int:
        """Highest rank among other live waiters from a *different* session;
        -1 if none. Same-session waiters are excluded — within a session,
        priority never preempts (canonical order wins). Pending (still
        rendering) waiters are excluded too: they hold a place in their own
        session's queue, they don't contend for the token."""
        best = -1
        for rank, _seq, session, pending in self._other_waiters():
            if pending or self._same_session(session):
                continue
            best = max(best, rank)
        return best

    def _earlier_sibling_waiting(self) -> bool:
        """True if a same-session waiter was submitted before me. It must speak
        first to preserve canonical order, regardless of either one's priority.

        Counts *pending* siblings — ones still rendering their clips. A long
        reply takes longer to render than a short one submitted after it, so
        without this a two-sentence follow-up sails past its own session's
        still-rendering predecessor and the pair is heard back to front."""
        for _rank, seq, session, _pending in self._other_waiters():
            if self._same_session(session) and seq < self._seq:
                return True
        return False

    def _is_urgent(self) -> bool:
        return self._rank >= _PRIO_RANK[Priority.URGENT]

    def _urgent_sibling_waiting(self) -> bool:
        """True if a same-session URGENT clip is waiting. URGENT is the one
        same-session case that DOES barge in: a deliberate "stop and hear this"
        that interrupts (and jumps ahead of) our own earlier message rather than
        queueing behind it in canonical order. Everything below URGENT still
        queues within a session. A pending (still rendering) URGENT sibling
        doesn't count — it barges in once it actually wants the token, and
        until then there's nothing to hand over to."""
        for rank, _seq, session, pending in self._other_waiters():
            if pending:
                continue
            if self._same_session(session) and rank >= _PRIO_RANK[Priority.URGENT]:
                return True
        return False

    # ---- token acquisition ----------------------------------------------

    @staticmethod
    def _disabled() -> bool:
        return os.environ.get("MEDIA_SPEECH_SERIALIZE", "1").lower() in ("0", "false", "no")

    def announce(self, priority: Priority = Priority.NORMAL, *,
                 session: str = "", seq: float = 0.0) -> None:
        """Claim a place in this session's queue *before* rendering starts.

        Rendering runs outside the lock (deliberately — sessions render in
        parallel), and a long reply takes longer to render than a short one
        submitted right after it. Without this the short one reaches acquire()
        first, finds nobody waiting, and speaks ahead of its own predecessor.
        Announcing publishes a pending waiter entry so the sibling defers.

        Idempotent-ish and best-effort: acquire() rewrites the same entry as a
        real waiter, release() removes it. Safe to skip calling — ordering then
        degrades to the pre-existing acquire-time behaviour.
        """
        if self._disabled():
            return
        self._rank = _rank_of(priority)
        self._session = session or ""
        self._seq = seq or time.time()
        self._pending = True
        self._register()

    def acquire(self, priority: Priority = Priority.NORMAL, *,
                session: str = "", supersede: bool = False,
                seq: float = 0.0) -> None:
        if self._disabled():
            return
        self._rank = _rank_of(priority)
        self._session = session or ""
        # Stamp submission order for same-session sibling ordering. Prefer the
        # caller's submission time (`seq`) over "now": now is post-render, and
        # ordering by it hands the queue to whichever sibling rendered fastest.
        # Set once — yield_to_higher() re-takes without restamping, so a yielded
        # reply keeps its original place among its session's clips.
        self._seq = seq or self._seq or time.time()
        # No longer merely pending: from here we actually contend for the token.
        self._pending = False
        # A supersede barge-in publishes a per-session marker at its own seq so
        # the same-session clips it interrupts/precedes (all with an earlier
        # seq) can see they've been dropped and abort. Only meaningful with a
        # real session and an URGENT rank (the only same-session barge-in).
        self._supersede = bool(supersede) and bool(self._session) \
            and self._rank >= _PRIO_RANK[Priority.URGENT]
        if self._supersede:
            self._mark_supersede()
        # LOW announcements skip rather than queue when anything's playing.
        self._take(skip_if_busy=self._rank <= _PRIO_RANK[Priority.LOW])

    def _holder_progress_sig(self) -> Optional[tuple]:
        """A cheap signature of the current speaker's progress: (clip uri,
        message start). The shared speech now_playing row is rewritten every
        sentence with the new clip uri, so this changes as long as someone is
        actively speaking — and stays put when the holder is wedged or gone.
        Returns None if it can't be read (treated as "no progress info").
        """
        store = self._progress_store
        if store is None:
            try:
                store = self._progress_store = StateStore()
            except Exception:  # noqa: BLE001
                return None
        try:
            np = store.get_now_playing("speech")
        except Exception:  # noqa: BLE001
            return None
        if not np:
            return None
        return (np.get("uri"), np.get("started_at"))

    def _holder_paused(self) -> bool:
        """True when the current speech holder is *deliberately* paused (popup
        Space), as opposed to wedged. A paused clip stops advancing its
        now_playing uri, so without this the progress-aware give-up below can't
        tell it apart from a stalled holder and would overtake it — clobbering
        the shared speech now_playing row (and thus the popup/status name) with
        the overtaking session. Authoritative for both local and remote targets:
        reads the broker's live `pause` property from whatever target is
        actually playing. Best-effort — any read failure returns False so a
        genuinely wedged/unreadable holder still times out as before, and an
        *idle* broker is never counted as paused: pause with nothing loaded is
        a leftover, not a deliberate hold.
        """
        store = self._progress_store
        if store is None:
            try:
                store = self._progress_store = StateStore()
            except Exception:  # noqa: BLE001
                return False
        try:
            np = store.get_now_playing("speech")
        except Exception:  # noqa: BLE001
            return False
        if not np:
            return False
        # Prefer the live pause the playlist/remote loop mirrors into extras
        # (a local DB hit, no bridge round-trip); fall back to reading the
        # broker directly for the local per-sentence loop, which doesn't record
        # it. The uri-mirrored `live_pause` is only trustworthy while it exists.
        ex = np.get("extras") or {}
        if isinstance(ex, dict) and "live_pause" in ex:
            return bool(ex.get("live_pause"))
        try:
            from ..sinks.speech import _socket_for
            from ..sinks import _mpv_ipc as ipc
            sock = _socket_for(Target(name=np.get("target") or "local"))
            # An idle broker has nothing loaded, so its `pause` is left over
            # from whatever played last, not a clip the user chose to hold.
            # Granting grace for it protects nothing and lets a wedged holder
            # buy the extra window on top of its own — seen 2026-08-05, when a
            # hook wedged at 16:35 with the sink parked at pause=true and
            # idle-active=true, and every later reply queued behind it.
            if ipc.get_property(sock, "idle-active"):
                return False
            return bool(ipc.get_property(sock, "pause"))
        except Exception:  # noqa: BLE001
            return False

    def _take(self, *, skip_if_busy: bool = False) -> None:
        try:
            path = _speech_lock_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:  # noqa: BLE001
            log.warning("speech lock: open failed (%s); proceeding unserialized", e)
            return
        self._register()
        # Progress-aware give-up: the timeout measures how long the *current
        # speaker* has been STUCK, not how long we've waited. While someone is
        # actively speaking their clip `uri` in the shared speech now_playing
        # row advances every sentence; each change pushes the deadline forward.
        # So a long-but-healthy reply (or a queue of them) never forces us to
        # bail and play unserialized — only a genuinely wedged/paused holder,
        # whose clip stops advancing, still times out after `timeout`. Without
        # this, two long replies tripped the flat 600s deadline and interleaved.
        timeout = float(os.environ.get("MEDIA_SPEECH_LOCK_TIMEOUT_S", "600"))
        deadline = time.monotonic() + timeout
        last_sig = self._holder_progress_sig()
        # A deliberately-paused holder buys ONE extra grace window (so a queued
        # reply doesn't overtake a pane the user just paused), but not infinite
        # grace: renewing the deadline every poll would let a paused pane block
        # a waiter forever. After the single renewal the give-up fires as it
        # would for a wedged holder, so the waiter proceeds unserialized.
        paused_grace_used = False
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    # Token held by someone else.
                    if skip_if_busy:
                        log.info("speech lock: low-priority clip skipped (busy)")
                        os.close(fd)
                        return
                    sig = self._holder_progress_sig()
                    if sig is not None and sig != last_sig:
                        # Current speaker advanced a clip — it's healthy, reset.
                        last_sig = sig
                        deadline = time.monotonic() + timeout
                    elif self._holder_paused() and not paused_grace_used:
                        # Deliberately paused (popup Space), not wedged: a paused
                        # clip stops advancing its uri, so the sig check above
                        # can't see it's healthy. Grant one extra grace window so
                        # we don't overtake — and don't clobber the paused clip's
                        # now_playing name — the instant the user pauses. EXTEND
                        # the deadline (not reset it): the first poll fires at ~t0
                        # when the deadline is still ~now+timeout, so a reset would
                        # be a no-op — adding a window is what actually buys the
                        # extra grace. Bounded (once): a still-paused holder past
                        # that window is then treated like any stalled one.
                        paused_grace_used = True
                        deadline += timeout
                    if time.monotonic() >= deadline:
                        log.warning("speech lock: holder stalled >%ss; proceeding "
                                    "unserialized", timeout)
                        os.close(fd)
                        return
                    time.sleep(0.2)
                    continue
                # Got the token, but hand it back if someone else should go
                # first, then retry. Reasons to defer: a strictly-higher
                # *other-session* waiter (priority wins admission across
                # sessions, no matter who won the raw flock race), or — unless
                # we ourselves are URGENT — a same-session waiter that outranks
                # our place in the queue: an URGENT sibling (deliberate barge-in
                # jumps to the front) or an *earlier* sibling (siblings otherwise
                # play in submission order, so priority never lets a later clip
                # jump its session's queue). An URGENT clip defers to nobody in
                # its own session; the strictly-earliest non-URGENT sibling never
                # defers to a sibling either, so admission always progresses.
                if (self._preempting_rank() > self._rank
                        or (not self._is_urgent()
                            and (self._urgent_sibling_waiting()
                                 or self._earlier_sibling_waiting()))):
                    # Bound the deferral on the same progress-aware deadline as
                    # the wait above, and for the same reason: whoever we're
                    # standing aside for normally takes the token straight away
                    # (and every clip anyone plays pushes the deadline out), but
                    # if nobody ever does — a sibling wedged between announce()
                    # and acquire(), say — we'd spin here forever rather than
                    # merely speak out of order. Checked before handing the
                    # token back, so on give-up we keep the lock we hold.
                    sig = self._holder_progress_sig()
                    if sig is not None and sig != last_sig:
                        last_sig = sig
                        deadline = time.monotonic() + timeout
                    if time.monotonic() >= deadline:
                        log.warning("speech lock: deferred >%ss with nobody "
                                    "taking the token; proceeding", timeout)
                        self._fd = fd
                        return
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    time.sleep(0.2)
                    continue
                self._fd = fd
                return
        finally:
            # A holder is no longer a waiter; also clears the entry on give-up.
            self._unregister()

    def should_yield(self) -> bool:
        """True when we should step aside at the next clip boundary: for a
        strictly higher-priority speaker from a *different* session, or for a
        same-session URGENT barge-in. A same-session clip below URGENT never
        interrupts an in-progress clip — once we're speaking we finish, and
        canonical order is enforced at admission (an earlier sibling wins the
        token next), so an ordinary sibling never cuts a clip short."""
        if self._fd is None:
            return False
        return (self._preempting_rank() > self._rank
                or self._urgent_sibling_waiting())

    def _supersede_path(self) -> Path:
        key = hashlib.sha1(self._session.encode("utf-8")).hexdigest()
        return _speech_supersede_dir() / key

    def _mark_supersede(self) -> None:
        """Publish 'drop everything in this session older than my seq'. Keyed by
        session, so at most one marker per session; a later supersede only ever
        raises the bar (max), never lowers it."""
        try:
            d = _speech_supersede_dir()
            d.mkdir(parents=True, exist_ok=True)
            path = self._supersede_path()
            try:
                prev = float(path.read_text().strip())
            except (OSError, ValueError):
                prev = 0.0
            path.write_text(repr(max(prev, self._seq)))
        except OSError:
            pass

    def should_abort(self) -> bool:
        """True when a same-session supersede has dropped this clip — a later
        URGENT clip in our session was tagged `supersede`, so everything it
        interrupted or was queued ahead of (every clip with an earlier seq)
        should stop rather than play/resume. The superseding clip itself has
        seq == marker, so it never aborts itself; clips submitted *after* it
        (larger seq) are unaffected."""
        if self._disabled() or not self._session:
            return False
        try:
            marker = float(self._supersede_path().read_text().strip())
        except (OSError, ValueError):
            return False
        return marker > self._seq

    def yield_to_higher(self) -> None:
        """Step aside for a higher-priority waiter, then re-take the token.

        Blocks until re-acquired. Call only between clips (broker idle), so
        there's nothing to pause or seek — the caller just replays its next
        sentence once this returns.
        """
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        time.sleep(0.05)  # grace for the higher waiter to grab the token
        # Reclaim with precedence over fresh *equal-rank* replies. We're an
        # in-progress message that was forced aside for a notification; without
        # this bump we'd race a not-yet-started reply of the same priority for
        # the token on the way back and could lose — so a whole other long reply
        # plays before we resume (the A -> notif -> B -> A interleave). Bump just
        # above our base rank but below the next tier, so genuine HIGH speakers
        # still preempt us; clamp so repeated yields can't creep into HIGH.
        if self._rank < _PRIO_RANK[Priority.HIGH]:
            self._rank = min(self._rank + 1, _PRIO_RANK[Priority.HIGH] - 1)
        self._take()

    def release(self) -> None:
        if self._fd is None:
            self._unregister()
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            self._unregister()

    def __enter__(self) -> "_SpeechPlaybackLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> bool:
        self.release()
        return False


def _audio_dir() -> Path:
    """Where rendered audio lands. Cache-y, GC-able. Per-user.

    On Termux+proot, the proot-side `/home/<user>` is bind-mounted to
    the Termux-native `/data/data/com.termux/files/home`, but services
    running outside the proot (sink-speech under runit) only see the
    Termux-native path. Prefer that root when it's present so audio
    paths handed over IPC resolve identically from both sides.
    """
    explicit = os.environ.get("MEDIA_AUDIO_DIR")
    if explicit:
        d = Path(explicit)
        d.mkdir(parents=True, exist_ok=True)
        return d
    termux_home = Path("/data/data/com.termux/files/home")
    base: Path
    if termux_home.is_dir():
        base = Path(os.environ.get("XDG_CACHE_HOME",
                                   str(termux_home / ".cache")))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME",
                                   str(Path.home() / ".cache")))
    d = base / "agent-media" / "audio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _clip_duration(path: Path) -> float:
    """Return audio duration in seconds via ffprobe, or 0.0 on failure."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        return float(r.stdout.strip()) if r.returncode == 0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _tmux_session_for_pane(pane: str) -> str:
    """Resolve a tmux pane id (e.g. ``%41``) to its session name, or "".

    Best-effort: returns "" when there's no pane, no tmux server, or the pane
    has already closed.
    """
    if not pane or "#{" in pane:
        return ""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"],
            capture_output=True, text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _tmux_window_for_pane(pane: str) -> str:
    """The conversation title for a pane — its tmux window name, captured now
    while the pane is alive.

    Persisted into the speech extras so the popup / status bar can name the
    speaker even when its pane can't be resolved at *display* time: a pane
    renumbered by a tmux-resurrect restore, closed since, or — for a rooms hub
    — living on a *different host* entirely. Mirrors the cli `_subject_label`
    preference: the window name (which tracks the stable Claude conversation
    title) over the transient, spinner-prefixed pane title; falls back to a
    spinner-stripped pane title only when the window has no usable name.
    """
    if not pane or "#{" in pane:
        return ""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane,
             "#{window_name}\t#{pane_title}"],
            capture_output=True, text=True, timeout=2)
    except Exception:  # noqa: BLE001
        return ""
    if r.returncode != 0:
        return ""
    window_name, _, pane_title = r.stdout.strip().partition("\t")
    label = window_name.strip()
    if not label or label in {"zsh", "bash", "sh", "fish"}:
        label = re.sub(r"^[⠀-⣿]\s*", "", pane_title.strip())
    return label


def _source_place(metadata, pane: str) -> tuple[str, str]:
    """Where a clip was said — ``(tmux session, window name)`` — preferring what
    the caller already knew over what tmux can still be asked.

    Both used to be resolved here, from the pane id, on the grounds that the
    submitter runs while the pane is alive. It does not always. A reply is
    rendered and queued before a word of it is spoken, and the turns that end a
    conversation — the goodbyes — are followed by the window closing. Ask tmux
    about a pane that has just gone and it answers, successfully, with nothing:
    empty session, empty window, and a clip that files itself on the phone's
    list under "no session" beside the cron reminders that never had one.

    So the hook, which runs *in* the pane at the moment the turn ends, puts the
    two names in the event metadata, and this prefers them. The live lookup
    stays as the fallback for every caller that isn't the hook — `media say`
    from a shell knows its pane and nothing else — and for a hook too old to
    have sent them.
    """
    md = metadata or {}
    tmux = str(md.get("tmux") or "").strip()
    window = str(md.get("window") or "").strip()
    return (tmux or _tmux_session_for_pane(pane),
            window or _tmux_window_for_pane(pane))


def _nav_flag_path(target: Target) -> Path:
    """File the popup writes to request a sentence/paragraph jump (`media skip`).

    Holds the absolute target sentence index for the live reader loop to jump
    to next. One per target since there's a single broker per target.
    """
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    d = state / "agent-media"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"nav-request-{target.name}"


def _read_nav_request(target: Target) -> Optional[int]:
    """Pop a pending nav request (target sentence index), or None. Clears it."""
    path = _nav_flag_path(target)
    try:
        raw = path.read_text().strip()
        path.unlink()
    except (FileNotFoundError, OSError):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _wait_for_clip(sink: SinkSpeech, target: Target,
                   on_poll: Optional[Callable[[], None]] = None) -> Optional[int]:
    """Wait for sink-speech to start then finish the current clip.

    Returns None on natural end-of-clip; returns an absolute sentence index
    when the popup requested a sentence/paragraph jump (`media skip`), so the
    reader loop can re-load that sentence instead of advancing by one. The
    nav check runs even while paused, so you can step the highlight forward or
    back through a paused response.

    `on_poll`, if given, is invoked once per polling iteration (including
    while paused and during the initial wait-for-start) — used to watch the
    broker mute state and toggle the music duck to match.

    Requires two consecutive idle readings before declaring done — a
    single True from sink.idle() can be a transient IPC error mid-play
    (the method returns True on MpvIpcError), which would otherwise cause
    the next clip to cut the current one short.
    """
    for _ in range(20):
        if on_poll is not None:
            on_poll()
        if not sink.idle(target):
            break
        time.sleep(0.05)
    idle_streak = 0
    elapsed = 0
    while elapsed < 1200:
        if on_poll is not None:
            on_poll()
        nav = _read_nav_request(target)
        if nav is not None:
            return nav
        # A user pause (popup Space) holds the clip here indefinitely: a
        # paused clip never goes idle, so without this it would burn the
        # ~120s budget and then force-advance to the next sentence,
        # resuming the response on its own. Hold without consuming budget;
        # resume picks up where it left off.
        if sink.paused(target):
            time.sleep(0.1)
            continue
        if sink.idle(target):
            idle_streak += 1
            if idle_streak >= 3:
                break
        else:
            idle_streak = 0
        time.sleep(0.1)
        elapsed += 1
    return None


class _MuteDuckWatcher:
    """Track the speech broker's mute state across a response and toggle the
    music duck to match: a mid-response mute (popup `m`) makes the remaining
    sentences silent, so the ducked music can come back up; un-muting re-ducks
    it while audible speech resumes.

    State lives here (not in `_wait_for_clip`, which is per-clip) so a mute set
    during one sentence is remembered across the sentence boundary and isn't
    re-ducked when the next silent clip loads. Restore at end-of-response is
    left to `coordinator.after_speech()`, which is idempotent with whatever
    duck state we leave behind.
    """

    def __init__(self, sink: SinkSpeech, target: Target,
                 coordinator: Coordinator) -> None:
        self._sink = sink
        self._target = target
        self._coord = coordinator
        # A fresh response always un-mutes itself on sentence 0
        # (reset_state), so we start from "audible / ducked".
        self._muted = False

    def poll(self, muted: Optional[bool] = None) -> None:
        if muted is None:
            try:
                muted = self._sink.muted(self._target)
            except Exception:  # noqa: BLE001
                return  # can't read mute → don't touch the duck
        if muted == self._muted:
            return
        self._muted = muted
        if muted:
            self._coord.release_music_duck()
        else:
            self._coord.reapply_music_duck()


def _remote_playlist(target: Target) -> bool:
    """Use the autonomous gapless-playlist path for a *remote* speech target.

    A tcp:// speech socket means the player (the phone's mpv) is on another host
    reached over a bridge. Driving it sentence-by-sentence over that bridge is
    fragile (a dropped poll stalls or cuts a clip), so for remote targets we load
    the whole response as a gapless playlist and let the player run it itself,
    just following playlist-pos for the popup. Local/rooms (unix-socket) targets
    keep the per-sentence loop, which gives finer control on a reliable socket.
    """
    from ..sinks.speech import _socket_for
    return str(_socket_for(target)).startswith("tcp://")


def _wait_and_claim_broker(sink: "SinkSpeech", target: Target) -> None:
    """Cross-host serialization for a *shared remote* (tcp://) broker.

    The playback flock only serializes this host; the phone broker is driven by
    every host. Before we stop+clear it with our playlist, claim an owner token
    that lives on the broker itself (mpv user-data) and wait out any other host
    that actively holds it. This mirrors the flock's progress-aware give-up: a
    healthy remote holder keeps refreshing its claim's deadline, so we keep
    waiting; a crashed/stalled one's claim expires, so we take over. No-op for
    local/rooms targets (the flock already covers them) and best-effort — the
    token machinery must never wedge a reply, so any trouble just proceeds.
    """
    if not _remote_playlist(target):
        return
    claim = getattr(sink, "claim_broker", None)
    if claim is None:
        return
    timeout = float(os.environ.get("MEDIA_SPEECH_LOCK_TIMEOUT_S", "600"))
    deadline = time.monotonic() + timeout
    last_seen: Optional[float] = None
    while True:
        try:
            info = sink.active_other_owner(target)
        except Exception:  # noqa: BLE001
            info = None
        if info is None:
            try:
                if claim(target):
                    return
            except Exception:  # noqa: BLE001
                return
        else:
            # Another host holds it; while its claim's deadline keeps advancing
            # it's alive, so reset our give-up and keep waiting rather than
            # clobber a healthy long reply on the other machine.
            try:
                od = float(info.get("deadline", 0))
            except (TypeError, ValueError):
                od = 0.0
            if od != last_seen:
                last_seen = od
                deadline = time.monotonic() + timeout
        if time.monotonic() >= deadline:
            who = info.get("owner") if isinstance(info, dict) else "?"
            log.warning("intake: remote broker held by %s and not advancing "
                        ">%ss; proceeding", who, timeout)
            return
        time.sleep(0.3)


_SENTENCE_MARK_RE = re.compile(r"^SENTENCE\s+(\d+)\s+([0-9]*\.?[0-9]+)\s*$")


def _watch_remote_progress(proc, state: StateStore, target_name: str,
                           started_at: float,
                           report: Optional[dict] = None,
                           follower: Optional[_SentenceFollower] = None,
                           source: Optional[dict] = None,
                           on_about_to_play=None) -> None:
    """Read the remote renderer's report lines and make the popup honest.

    A remote renderer may announce, on stdout, three things about what it is
    about to play:

        CLIP <basename>          the file it rendered, in the dir this target's
                                 clips already resolve against
        SENTENCE <idx> <offset>  where sentence <idx> starts, in seconds
        DURATION <seconds>       how long that file is

    That is the entire protocol, and every line is optional. They arrive once,
    immediately before playback starts, and `DURATION` comes last — it is the
    line that says "now", so the marks have to be in hand by the time it lands.
    The duration plus the local clock at the moment it arrives are enough to
    extrapolate position without polling a link that drops a quarter of its
    packets; the marks extend that from a bare progress bar to per-sentence
    state (see `_SentenceFollower`). The basename is what makes the reply
    *replayable*: the audio already exists on the far side, so asking for it
    again is a local loadfile there, not a transfer from here.

    A renderer that says nothing (Android TTS, a bare `say`) is unaffected: the
    row carries no duration and no clip, and the display stays blank, which is
    the correct answer when nothing has been measured.

    `on_about_to_play`, when given, is called once on the first of these lines
    to arrive. They are sent immediately before playback and nothing else on
    this lane knows that moment: the caller's own call returned when the text
    was handed over, which on a phone that renders its own audio is ten seconds
    early. That is what the callback is for — see `Coordinator.duck_music_now`.

    `report`, when given, collects what was announced for the caller to record
    in history — the thread outlives neither the process nor the caller's wait.

    Best-effort throughout. This is a progress bar; it must never be the reason
    an utterance fails.
    """
    if not proc.stdout:
        return
    duration = 0.0
    clip = ""
    marks: dict[int, float] = {}
    announced = False
    try:
        # readline(), not `for raw in proc.stdout`: iterating a pipe reads
        # AHEAD, so these two short lines sat in Python's buffer until the far
        # side closed the stream — by which point the utterance had finished
        # and the timeline they carry was worthless. Measured on the phone
        # lane: ~7.2s with iteration vs ~4.4s with readline, on a call that
        # returned at ~15s.
        #
        # The caller's command must not buffer either. curl holds its output
        # when stdout is a pipe unless told otherwise, which defeated this
        # entirely regardless of how we read: MEDIA_REMOTE_SAY_CMD_* wants
        # `--no-buffer` (or the renderer's equivalent), or the report arrives
        # only at exit and the progress bar never appears.
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if on_about_to_play is not None and line:
                # Any of the report lines will do: they are all sent in the
                # same breath, just before the first sample.
                try:
                    on_about_to_play()
                except Exception:  # noqa: BLE001 — a duck is never worth the reply
                    pass
                on_about_to_play = None
            if line.startswith("CLIP "):
                clip = line.split(None, 1)[1].strip()
                if report is not None and clip:
                    report["clip"] = clip
                continue
            if line.startswith("SENTENCE "):
                m = _SENTENCE_MARK_RE.match(line)
                if m:
                    marks[int(m.group(1))] = float(m.group(2))
                continue
            if line.startswith("PAUSE "):
                # The far side's player was paused or resumed by something that
                # isn't us — a media key, the notification controls, a call.
                # Our clock can't see that; this is how it finds out.
                stamp_speech_pause(state, line.split(None, 1)[1].strip() == "1")
                continue
            if not line.startswith("DURATION ") or announced:
                continue
            try:
                duration = float(line.split(None, 1)[1])
            except (IndexError, ValueError):
                continue
            if duration <= 0:
                continue
            if report is not None:
                report["duration"] = duration
            play_started_at = time.time()
            extras = {"kind": "remote-say", "writer_pid": os.getpid(),
                      **(source or {}),
                      "total_duration_s": duration,
                      # Stamped here, not at submit: rendering happens before
                      # this line is sent, and counting that as playback would
                      # start the bar seconds ahead of the audio.
                      "play_started_at": play_started_at}
            if clip:
                extras["clip_uris"] = [clip]
                extras["clips_remote"] = True
            timeline = follower.timeline(marks, duration) if follower else None
            offsets, measured = timeline if timeline else (None, False)
            if offsets:
                # The sentence view every surface reads: `clip_sentences` names
                # them, `clip_offsets_s` says where each one starts in the one
                # clip the far side rendered — which is also what lets `media
                # skip` seek by sentence on a lane that has no playlist to step.
                extras["clip_sentences"] = follower.sentences
                extras["clip_offsets_s"] = offsets
                extras["clip_durations_s"] = [
                    b - a for a, b in zip(offsets, offsets[1:] + [duration])]
                extras["sentence_marks"] = measured
                extras["current_sentence"] = follower.sentences[0]
                extras["current_sentence_idx"] = 0
                if report is not None:
                    # For history: the audio lives on the far side and can be
                    # replayed there, so the timeline that makes it followable
                    # has to outlive this process too.
                    report["sentences"] = follower.sentences
                    report["offsets"] = offsets
            state.set_now_playing(
                "speech", uri=f"remote-say:{target_name}",
                started_at=started_at, target=target_name, extras=extras)
            announced = True
            if offsets:
                follower.start(offsets, play_started_at)
            # Keep reading rather than returning: closing this end early would
            # SIGPIPE a renderer that still has something to say, and the far
            # side's stdout is also how a late failure reaches us.
    except Exception:  # noqa: BLE001 — a progress bar must not break speech
        return


def _kill_process_group(proc) -> None:
    """Kill a Popen and everything it spawned. Best-effort, never raises.

    Only safe because the process was started with start_new_session=True, so
    it leads its own group and we can't signal anything of ours.
    """
    import os
    import signal
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except OSError:
            return
        try:
            proc.wait(timeout=3)
            return
        except Exception:  # noqa: BLE001 — still alive; escalate to SIGKILL
            continue


def _remote_say_cmd(target: Target) -> str:
    """The remote-say lane for one target, or "" to render and play locally.

    Resolution mirrors every other per-target setting (see sinks.speech._env_key):

      1. ``MEDIA_REMOTE_SAY_CMD_<TARGET>`` — this target's lane
      2. ``MEDIA_REMOTE_SAY_CMD``         — the host's lane for any target

    The fallback is what keeps single-lane hosts working untouched, but it is
    also why the target used to be a label and nothing more: one global command
    served every target, so asking for `rooms` was heard wherever the global
    lane pointed. A target that should render locally now says so with the off
    sentinel — ``MEDIA_REMOTE_SAY_CMD_ROOMS=-`` — which is exactly why the
    sentinel had to exist: an empty value here is indistinguishable from an
    unset one, and unset means "fall back to the global lane".
    """
    per_target = os.environ.get(_env_key("MEDIA_REMOTE_SAY_CMD", target.name))
    if per_target is not None:
        return per_target
    return os.environ.get("MEDIA_REMOTE_SAY_CMD", "")


def _ringer_target() -> str:
    """Which target the phone's ringer has anything to say about.

    The phone being on silent is a fact about the phone. It is not a fact about
    the lounge speakers, and a `--target local` reply must be unaffected by it —
    otherwise "silence my phone" quietly becomes "silence the house", which
    nobody asked for and which would take a while to attribute.
    """
    return os.environ.get("MEDIA_RINGER_TARGET", "phone").strip()


def _ringer_hold(target: Target, event: Event) -> "dict | None":
    """The verdict holding this event back, or None to speak it.

    Three conditions, all required, and the order is the cheap ones first so
    the ordinary reply never touches the broker:

      1. the event is **alert-class** — something mechanical decided to speak,
         nobody asked for it. Marked explicitly by its producer (`media say
         --alert`), never inferred: a timer knows it is a timer, and an
         assistant guessing at its own interruption-worthiness is judging a
         case it has an interest in (see `sinks.speech.set_priority`).
      2. it is aimed at the **device with the ringer**;
      3. a **fresh** verdict from that device says quiet.

    Anything unknown at step 3 — no publisher, a dead companion, a stale
    snapshot, an unreachable bridge — is not quiet. The two errors are not
    symmetric: a wrongly-spoken alert is one noise at the wrong moment, while a
    wrongly-swallowed one is silent by construction and indistinguishable from
    the TTS stack being broken. This codebase has spent whole afternoons on
    that second shape (mic-block reverts, media volume at 0/25), so this fails
    towards sound every time.
    """
    if not (event.metadata or {}).get("alert"):
        return None
    wanted = _ringer_target()
    if not wanted or target.name != wanted:
        return None
    from ..sinks import speech as _speech

    verdict = _speech.read_ringer(target)
    if not verdict or not verdict.get("quiet"):
        return None
    log.info("intake: alert held — %s is quiet (mode=%s dnd=%s)",
             target.name, verdict.get("mode"), verdict.get("dnd"))
    return verdict


def _record_silenced(state: StateStore, event: Event, target: Target,
                     text: str, verdict: "dict | None" = None) -> Optional[int]:
    """Write the alert down without speaking it, and leave a trail.

    Not rendered. A muted pane renders because someone may unmute and replay
    it; an alert held overnight has no such future — its moment is what made it
    an alert, and by morning the words are all that is left worth keeping. The
    remote-say lane already writes clipless rows, so `speech history` and the
    clip browser handle this shape.

    The `state.log_error` line is the point of the whole function. Without it,
    "my morning digest stopped talking" and "TTS is broken" are the same
    observation, and `media doctor` has nothing to say about a stack that is
    behaving perfectly.
    """
    try:
        state.log_error("intake", "alert held: device is on silent",
                        extras={"kind": "alert-silenced",
                                "target": target.name,
                                "source": event.source.value,
                                "mode": (verdict or {}).get("mode", "unknown"),
                                "dnd": (verdict or {}).get("dnd", "unknown"),
                                "text": text[:160]})
    except Exception:  # noqa: BLE001 — a breadcrumb, never the caller's problem
        pass
    try:
        return state.add_history(
            sink="speech",
            uri=f"silenced:{target.name}",
            started_at=time.time(),
            ended_at=time.time(),
            target=target.name,
            source=event.source.value,
            text=text,
            extras={"silenced": "ringer", "alert": True,
                    **{k: v for k, v in (event.metadata or {}).items()
                       if k in ("kind", "session", "pane")}},
        )
    except Exception:  # noqa: BLE001
        return None


def _arm_feed_debounce() -> None:
    """Tell the feed a turn has landed, if this host publishes one at all."""
    try:
        from ..feed_debounce import arm
        arm()
    except Exception:  # noqa: BLE001 — the poll is the safety net
        pass


def _duck_grace_s() -> float:
    """How long to wait for the far side to say it is about to play.

    The deadline for a renderer that never announces anything, not a pause for
    one that does — an announcement applies the duck the moment it lands. Long
    enough that the phone's own render (seconds, measured) is not cut short by
    it; short enough that a silent renderer talks over at most this much music.
    """
    try:
        return max(0.0, float(os.environ.get("MEDIA_SPEECH_DUCK_GRACE_S", "3")))
    except (TypeError, ValueError):
        return 3.0


def _submit_remote_say(text: str, cmd: str, coordinator: Coordinator,
                       state: StateStore, event: Event) -> Optional[int]:
    """Render a reply on a remote low-latency hub instead of locally.

    Used when ``MEDIA_REMOTE_SAY_CMD`` is set (e.g. red5, whose rooms listen to
    a remote Snapcast hub). The whole reply text is piped to the remote renderer
    over **stdin** — so no shell on the far side reinterprets quotes/`$`/etc. —
    and the call blocks until the remote finishes, so ``before_speech`` /
    ``after_speech`` bracket the audio and music ducks for its full duration.

    Serialized by the same cross-process speech lock as local playback, so two
    sessions can't render into the hub's fifo at once. Best-effort: a remote
    hiccup is logged, never raised, and the duck is always restored.
    """
    import subprocess

    timeout = float(os.environ.get("MEDIA_REMOTE_SAY_TIMEOUT", "180"))
    seq = time.time()
    session = (event.metadata or {}).get("session") or ""
    lock = _SpeechPlaybackLock()
    lock.acquire(event.priority, session=session,
                 supersede=bool((event.metadata or {}).get("supersede")),
                 seq=seq)
    # Superseded before we started: skip this whole-reply remote render. (The
    # remote say is one blocking pipe, so this is the only place it can drop —
    # once handed off it can't be cut mid-utterance like the clip loops.)
    if lock.should_abort():
        lock.release()
        return None
    # Same last-checkpoint gate as the local path: wait out an active hold,
    # then drop if a flush arrived while we were queued or held.
    _wait_speech_hold()
    if _speech_flushed(seq):
        lock.release()
        return None
    started_at = time.time()
    history_id: Optional[int] = None
    failure: Optional[str] = None      # set if the far side never spoke the text
    report: dict = {}                  # what the renderer announced (clip, duration)
    # Resolve the target exactly as the local path does. This branch runs before
    # submit_event's own resolution, so event.target is usually None here — and
    # recording a placeholder like "remote" is not cosmetic: _active_speech_target
    # reads this row to decide which player the popup talks to, so an unmatched
    # name sends pause/resume to the idle local mpv while the audio plays on the
    # phone. The control appears to do nothing.
    target_name = (event.target.name if event.target
                   else os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET", "local"))
    # Tag the row exactly as the local path does. These are not decoration:
    # `media replay` and the popup's < / > scope traversal to one conversation
    # by extras.source_session, and rows carrying no tag are *excluded* rather
    # than leaked across conversations. Writing only `session` here meant every
    # phone-lane reply was invisible to that scoping — so `r` reported nothing
    # to replay and the popup's history navigation skipped straight past a
    # whole lane's worth of speech, while the same clip replayed fine when
    # addressed by id.
    source_pane = (event.metadata or {}).get("pane") or os.environ.get("TMUX_PANE", "")
    source_tmux_session, source_window = _source_place(event.metadata, source_pane)
    # The same identity goes on the LIVE row, not just history: the status bar
    # and popup name the speaker through now_playing.extras.source_pane, and a
    # row without it resolves to *the caller's* pane instead — so whichever
    # window you happened to be looking at got its name printed over the reply
    # that was actually playing. source_window is the conversation title as it
    # stood when this was said, which is what those surfaces should show while
    # it plays, however far the pane has moved on since.
    source_extras = {"source_pane": source_pane,
                     "source_session": session,
                     "source_tmux_session": source_tmux_session,
                     "source_window": source_window}
    # Per-sentence state for a lane that renders and plays the whole reply on
    # another device. We split the text the same way the local path does; the
    # renderer tells us where each sentence lands (or, failing that, how long
    # the whole thing is), and the follower walks that timeline on the clock.
    _follow_highlight = _highlight_wanted(event, source_pane)
    ensure_follow_view(pane=source_pane)   # see submit_event: not gated on the
                                           # keystroke skip, which is about
                                           # copy-mode, not about our own pane
    follower = _SentenceFollower(
        state, target_name, started_at, _split_sentences(text),
        pane=source_pane, highlight=_follow_highlight,
        delay_s=_playout_delay_s(target_name),
        on_sentence=coordinator.speaking_line)
    # Start the remote pause before anything blocking, so its ssh overlaps the
    # lock wait and the render instead of being paid in series inside
    # before_speech(). Both local render paths have always done this; the phone
    # lane never did, which is why the coordination cost showed up here and not
    # there — on this link it was 17s of a 25s utterance.
    coordinator.pre_pause_remote()
    duck_timer: Optional[threading.Timer] = None
    try:
        # Everything but the music duck, which waits for the far side to say it
        # is about to play. This lane hands over text and returns; the audio
        # starts when the phone has finished rendering it, which was measured on
        # 2026-08-18 at ten seconds later. Ducking at hand-over is a hole in the
        # music that opens before anything fills it.
        coordinator.before_speech(
            title=source_window, priority=event.priority.value,
            defer_music=True,
            # The first sentence, not the reply: the follower will move this on
            # as the far side speaks, and until it does the card should show
            # what is about to be said rather than all of it at once.
            text=follower.sentences[0] if follower.sentences else text)
        # ...and a renderer that announces nothing (Android TTS, a bare `say`)
        # would then never duck at all. So the wait is bounded: whichever comes
        # first, the announcement or this, applies it exactly once.
        duck_timer = threading.Timer(_duck_grace_s(), coordinator.duck_music_now)
        duck_timer.daemon = True
        duck_timer.start()
        _speech_event("start", text=text[:400], session=session,
                      source=event.source.value, target="remote-say")
        # The remote renders and plays; nothing is on this host to observe. Say
        # so explicitly, or the popup and `speech now-playing` sit blank for the
        # whole utterance and the control surface looks broken rather than
        # remote. uri is the command, since there is no clip here to point at.
        try:
            # writer_pid lets the display's zombie guard notice if this process
            # dies mid-utterance; without it a crash would freeze the row.
            # Deliberately no total_duration_s: the audio is rendered and played
            # on another device and never reports back, so any figure here would
            # be invented. The popup shows nothing rather than a progress bar
            # tracking a length nobody measured.
            state.set_now_playing("speech", uri=f"remote-say:{target_name}",
                                  started_at=started_at, target=target_name,
                                  extras={"kind": "remote-say", "text": text[:400],
                                          **source_extras,
                                          "writer_pid": os.getpid()})
        except Exception:  # noqa: BLE001 — observability must not break speech
            pass
        try:
            # Not subprocess.run(..., timeout=): with shell=True the timeout
            # kills the *shell*, and whatever it spawned (an ssh, here) is
            # reparented and runs forever. A remote renderer that blocks — an
            # Android TTS that never returns because the screen is off, say —
            # then leaks one orphaned ssh per utterance until the far end runs
            # out of session slots and every other remote call starts failing
            # too. Own the whole process group and kill it.
            proc = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            # Hand over the text, then listen for the one line the far side may
            # send back before it starts playing (see _watch_remote_progress).
            try:
                if proc.stdin:
                    proc.stdin.write(text.encode())
                    proc.stdin.close()
            except OSError:
                pass
            watcher = threading.Thread(
                target=_watch_remote_progress,
                args=(proc, state, target_name, started_at, report, follower,
                      source_extras, coordinator.duck_music_now),
                daemon=True)
            watcher.start()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                raise
            # A non-zero exit is the far side telling us the reply was never
            # spoken — a renderer that failed, or, as happened here, a command
            # pointing at a script that no longer exists. Unchecked, the only
            # symptom is silence: history still records the utterance as though
            # it had played, so `media history` agrees with the agent that it
            # spoke and nothing anywhere disagrees with the room. Speech is
            # write-only over this link; the exit status is the entire feedback
            # channel, so refusing to read it is refusing to know.
            if proc.returncode:
                raise RuntimeError(          # handled just below, never escapes
                    f"remote renderer exited {proc.returncode}")
        except Exception as e:  # noqa: BLE001 — remote render must never crash the hook
            failure = str(e)
            log.warning("intake: remote-say failed: %s", e)
            try:
                state.log_error("intake", "remote-say failed",
                                extras={"detail": str(e), "cmd": cmd[:200],
                                        "source": event.source.value})
            except Exception:  # noqa: BLE001
                pass
        finally:
            # Before after_speech/clear: a follower still walking its timeline
            # would otherwise write the row back moments after we cleared it,
            # leaving a dead reply on every surface until the next one lands.
            if duck_timer is not None:
                duck_timer.cancel()
            follower.stop()
            coordinator.after_speech()
            _speech_event("end", text=text[:160], session=session,
                          source=event.source.value, target="remote-say")
            # History too: replay can't work (the audio never existed here),
            # but the transcript is what `speech history` and the clip browser
            # actually show, and losing it means the phone lane leaves no trace
            # of anything the agent said.
            try:
                state.clear_now_playing("speech")
                history_id = state.add_history(
                    sink="speech",
                    uri=f"remote-say:{target_name}",
                    started_at=started_at,
                    ended_at=time.time(),
                    target=target_name,
                    source=event.source.value,
                    text=text,
                    extras={"kind": "remote-say", "cmd": cmd[:200],
                            **source_extras,
                            # The transcript must not claim what the room
                            # didn't hear; the clip browser reads this too.
                            **({"failed": failure} if failure else {}),
                            # What the renderer said it made. `clips_remote`
                            # tells replay the audio is already on the far
                            # side, so it must not try to ship it there.
                            **({"clip_uris": [report["clip"]],
                                "clips_remote": True} if report.get("clip")
                               else {}),
                            **({"total_duration_s": report["duration"]}
                               if report.get("duration") else {}),
                            # The sentence timeline, so a replay of this clip
                            # can follow along as the live reply did. One clip
                            # holding every sentence needs the offsets: there
                            # is no playlist position to read them off.
                            **({"clip_sentences": report["sentences"],
                                "clip_offsets_s": report["offsets"]}
                               if report.get("offsets") else {}),
                            **{k: v for k, v in (event.metadata or {}).items()
                               if k in ("kind", "session")}},
                )
            except Exception:  # noqa: BLE001
                pass
    finally:
        lock.release()
    return history_id


def submit_event(event: Event,
                 *,
                 state: Optional[StateStore] = None,
                 coordinator: Optional[Coordinator] = None,
                 sink: Optional[SinkSpeech] = None) -> Optional[int]:
    """Render `event` sentence-by-sentence and play through sink-speech.

    Each sentence is rendered to its own clip and played in order. The
    source tmux pane highlights the current sentence as it starts playing
    (karaoke-style). Returns the history-row id, or None on failure.

    Blocks until all clips finish. Callers that need fire-and-forget
    should run this in a thread.
    """
    text = event.text.strip()
    if not text:
        return None

    state = state or StateStore()
    coordinator = coordinator or Coordinator(state=state)
    sink = sink or SinkSpeech()
    target = event.target or Target(
        name=os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET", "local"))

    # The device asked for quiet, and this is an alert nobody asked for. Held
    # before the render, not after: unlike a muted pane there is nothing to
    # replay a clip *for* once the moment has gone, and the words survive in
    # history either way. See `_ringer_silenced`.
    held = _ringer_hold(target, event)
    if held:
        return _record_silenced(state, event, target, text, held)

    # Remote-say bridge: on a headless feeder host (e.g. red5) whose rooms now
    # listen to a remote low-latency Snapcast hub, render the
    # reply *there* instead of locally — the hub renders the text to its own
    # Snapcast fifo. The coordinator still ducks from here (it drives the rooms
    # snapserver over the tailnet via MEDIA_SNAP_JSONRPC_HOST), so music dips
    # under speech as before. Env-gated per target (see _remote_say_cmd): no
    # lane for this target ⇒ the local render+play path below is unchanged.
    remote_say = _remote_say_cmd(target)
    if remote_say:
        return _submit_remote_say(text, remote_say, coordinator, state, event)

    engine = _resolve_engine(event)
    voice = _resolve_voice(event, engine)
    ext = _ext_for(engine)

    audio_dir = _audio_dir()
    # Per-submission unique: second-resolution time is NOT enough — two
    # concurrent sessions (both source "claude-code") finishing a reply in the
    # same second would render to identical clip paths and clobber each other's
    # audio, so one session could end up playing another's clips. pid + a short
    # random token guarantees uniqueness across processes and within one.
    stamp = (time.strftime("%Y%m%dT%H%M%S")
             + f"-{os.getpid()}-{uuid.uuid4().hex[:6]}")
    started_at = time.time()

    # The tmux pane that produced this speech (the Claude Code TTS hook runs
    # inside the agent's pane, so TMUX_PANE points at it). Persisted into
    # now_playing/history so the popup can show *which* pane is currently
    # talking, rather than the pane that happens to be active.
    source_pane = (event.metadata or {}).get("pane") or os.environ.get("TMUX_PANE", "")
    # The Claude Code session id (from the hook payload), persisted so the
    # popup can resume the conversation when its source pane has since been
    # closed — `goto-pane` falls back to `claude --resume <session>`.
    source_session = (event.metadata or {}).get("session") or ""
    order_session = _order_session(source_pane, source_session)
    # Claim this reply's place in its session's speech queue *now*, before the
    # renders below: a shorter sibling submitted a moment later would otherwise
    # finish rendering first, find the queue empty, and speak ahead of us.
    # Created here, acquired for real once the clips exist (and released on
    # every path out, including muted / render-failed).
    playback_lock = _SpeechPlaybackLock()
    playback_lock.announce(event.priority, session=order_session,
                           seq=started_at)
    # The tmux session that owns the source pane, and the conversation title
    # (window name) of it. Persisted so the popup's < / > can scope history
    # traversal to "this tmux session's clips", and the status bar can name the
    # speaker, without resolving a (possibly since-closed, renumbered, or
    # remote) pane id at browse time. See _source_place for who is asked.
    source_tmux_session, source_window = _source_place(event.metadata, source_pane)

    # Durable per-pane / per-session mute (popup `M` / `media mute-pane`): a
    # muted pane still renders its clips and records a replayable history row,
    # but is never played through the broker and never ducks music. Decided
    # once, up front, so we also skip the remote pre-pause below.
    muted = state.resolve_mute(source_pane, source_tmux_session)
    if muted:
        # Nothing will be played, so give the queue slot announced above back
        # immediately rather than making this session's next reply wait on a
        # render it will never hear.
        playback_lock.release()

    fallback_info: dict = {}
    _fallback_lock = threading.Lock()

    def _on_fallback(failed_engine: str, err: str) -> None:
        short = err.strip().splitlines()[0] if err else "no detail"
        fb = os.environ.get("MEDIA_RENDER_FALLBACK_ENGINE") or "edge"
        kind = "render-fallback"
        if "insufficient_quota" in err:
            kind = "render-quota"
        log.warning("intake: %s engine failed (%s); falling back to %s",
                    failed_engine, short, fb)
        state.log_error("intake",
                        f"render {failed_engine} failed, fell back to {fb}",
                        extras={"kind": kind, "engine": failed_engine,
                                "fallback_engine": fb, "detail": short[:300],
                                "source": event.source.value})
        with _fallback_lock:
            fallback_info.update({
                "from_engine": failed_engine,
                "fallback_engine": fb,
                "kind": kind,
                "detail": short[:300],
            })
        if kind == "render-quota":
            title = f"agent-media: {failed_engine} quota exhausted"
            body = f"Falling back to {fb} for now."
        else:
            title = f"agent-media: {failed_engine} render failed"
            body = f"Falling back to {fb}. {short[:120]}"
        notify(key=f"render-fallback-{failed_engine}",
               title=title, content=body)

    # Start remote MPRIS detect-and-pause in background so SSH cold-connect
    # (~4.8s) overlaps with sentence rendering below. Skipped when muted —
    # nothing will play, so there's nothing to pause for.
    if not muted:
        coordinator.pre_pause_remote()

    sentences, sent_para = _split_sentences_with_paragraphs(text)

    # Submit all sentence renders in parallel. Sentence 0 starts playing as
    # soon as its render finishes (~0.5s); the rest are done by then.
    outfiles = [
        audio_dir / f"{stamp}--{event.source.value}--{i:03d}.{ext}"
        for i in range(len(sentences))
    ]
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(sentences) or 1)
    futures = [
        executor.submit(render_text, sentence, outfile,
                        engine=engine, voice=voice, on_fallback=_on_fallback)
        for sentence, outfile in zip(sentences, outfiles)
    ]
    executor.shutdown(wait=False)  # don't block; futures stay live

    # First clip path for sidecar / now_playing (render may still be in flight).
    first_clip = outfiles[0]

    # Sidecar lives next to the first clip so the popup can show the full text.
    try:
        first_clip.with_suffix(".txt").write_text(text)
    except OSError as e:  # noqa: BLE001
        log.warning("intake: text sidecar write failed: %s", e)

    do_highlight = _highlight_wanted(event, os.environ.get("TMUX_PANE") or "")
    # Not gated on do_highlight: the keystroke skip exists because grabbing
    # copy-mode mid-keystroke yanks the view out from under you, and the follow
    # pane does nothing of the sort — it is a surface of our own. Gating it too
    # meant the view stayed shut for the reply that *answers what you just
    # typed*, which is every reply.
    ensure_follow_view(pane=source_pane)

    # Phase 1: resolve all render futures and collect clip durations.
    # Parallel renders are mostly done by now; future.result() is instant
    # for finished ones and waits briefly for the last stragglers.
    clip_data: list[tuple[str, Path]] = []  # (sentence, clip_path)
    clip_para: list[int] = []               # paragraph index per surviving clip
    for sentence, pi, outfile, future in zip(sentences, sent_para,
                                             outfiles, futures):
        try:
            ok, err = future.result()
        except Exception as exc:  # noqa: BLE001
            log.warning("intake: render future raised: %s", exc)
            continue
        if not ok:
            log.warning("intake: render failed for sentence (%s): %s", engine, err)
            state.log_error("intake", f"render failed ({engine})",
                            extras={"err": err, "source": event.source.value})
            continue
        clip_path = outfile
        with _fallback_lock:
            has_fallback = bool(fallback_info)
        if has_fallback and clip_path.suffix == ".wav":
            renamed = clip_path.with_suffix(".mp3")
            try:
                clip_path.rename(renamed)
                clip_path = renamed
            except OSError:
                pass
        clip_data.append((sentence, clip_path))
        clip_para.append(pi)

    if not clip_data:
        playback_lock.release()   # nothing to say; stop holding our queue slot
        return None

    # Compute per-clip offsets for a single spanning progress bar. ffprobe is a
    # subprocess per clip; probe them in parallel so a multi-sentence reply
    # doesn't add ~0.2s × N to time-to-first-audio.
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(clip_data))) as _dpool:
        durations = list(_dpool.map(_clip_duration, [p for _, p in clip_data]))
    total_duration_s = sum(durations)

    # Delay the highlight so it fires when the audio is actually *heard*, not
    # when mpv reports idle.
    _highlight_delay_s = _playout_delay_s(target.name)

    # Cumulative start offset of each clip on the response-wide timeline.
    offsets: list[float] = []
    _acc = 0.0
    for d in durations:
        offsets.append(_acc)
        _acc += d
    _clip_sentences = [s for s, _ in clip_data]

    def _archive(*, flushed: bool = False) -> Optional[int]:
        """The one history write, shared by every path that records this reply
        — played, muted, or flushed — so they cannot drift in what they store.
        The archive is sacrosanct: a reply that rendered gets its row whether
        or not its audio was ever heard, and nothing downstream (flush
        included) may alter or remove it."""
        extras = {"engine": engine, "voice": voice,
                  "priority": event.priority.value,
                  "source_pane": source_pane,
                  "source_session": source_session,
                  "source_tmux_session": source_tmux_session,
                  "source_window": source_window,
                  "clip_uris": [str(p) for _, p in clip_data],
                  "clip_sentences": _clip_sentences,
                  "clip_durations_s": durations,
                  "clip_paragraph_idx": clip_para,
                  **(event.metadata or {})}
        if fallback_info:
            extras["fallback"] = fallback_info
        if muted:
            extras["muted"] = True   # rendered but never played (popup can replay)
        if flushed:
            extras["flushed"] = True   # playback cancelled by speech-flush;
            #                            rendered and archived, never heard
        row = state.add_history(
            sink="speech",
            uri=str(first_clip),
            started_at=started_at,
            ended_at=time.time(),
            target=target.name,
            source=event.source.value,
            text=text,
            extras=extras,
        )
        # The turn just ended, which is the only moment anything knows the
        # conversation *might* be over. Push the publish deadline out; the last
        # turn to do so is the one that gets to fire. Detached and never
        # raising — an unpublished episode is a wait, a blocked reply is
        # silence in the room.
        _arm_feed_debounce()
        return row

    # A muted pane skips playback entirely: the clips are already rendered
    # (above), so we fall straight through to the history write below — no
    # broker, no before/after_speech, no duck.
    if not muted:
        # Serialize playback across sessions: only one response feeds the shared
        # broker/Snapcast stream at a time (rendering above already ran in
        # parallel). Acquired before before_speech() so other media isn't paused
        # while we're still queued behind another speaker. `seq=started_at` is
        # what keeps same-session order canonical — see announce() above.
        # An active hold (`media speech-hold N`) gates the START of playback,
        # and it is waited out *before* the token is taken, not while holding
        # it.
        #
        # Holding the token through the wait was the obvious reading of "we
        # keep our place in the queue", and the expiry in the marker was
        # supposed to bound it. What that missed is that a hold is not one
        # timed event: call-guard re-asserts it every 15s for as long as the
        # phone's mic looks hot, so the marker never expires and the wait has
        # no bound in practice. One reply then sat on the token for as long as
        # the mic kept opening, and since the token is global, *every* session's
        # speech queued behind a reply that was not playing — until each waiter
        # gave up after MEDIA_SPEECH_LOCK_TIMEOUT_S and played unserialized.
        # Ten minutes, which is what David heard.
        #
        # Nothing is lost by waiting outside: a hold silences the whole channel,
        # so no one else could have played during it either. What changes is
        # that the token is free while nobody may speak, so the order things are
        # said in when the hold lifts is decided by the queue rather than by who
        # happened to be sitting on the lock. The wait keeps our announcement
        # warm so a sibling cannot age us out of our own session's order.
        def _keep_our_place() -> None:
            playback_lock.announce(event.priority, session=order_session,
                                   seq=started_at)

        while True:
            _wait_speech_hold(refresh=_keep_our_place)
            playback_lock.acquire(
                event.priority, session=order_session,
                supersede=bool((event.metadata or {}).get("supersede")),
                seq=started_at)
            # Superseded before we started (a later URGENT in this session
            # dropped us): hand the token straight back and skip playback
            # entirely — no broker claim, no music duck, no history row.
            if playback_lock.should_abort():
                playback_lock.release()
                return None
            if speech_hold_until() <= 0.0:
                break
            # A hold landed in the gap between the wait and the take. Hand the
            # token back rather than sitting on it: the invariant is that the
            # broker is never fed under a hold, not that we own the lock while
            # waiting for one.
            playback_lock.release()
        # Flushed while queued, rendering, or held (`media speech-flush`):
        # cancel only the playback. The reply is still archived below, marked
        # flushed, so the history a human browses later holds the full text
        # either way — only the audio is skipped. The clip already SPEAKING
        # when the flush was requested is deliberately not cut; pause / stop /
        # supersede exist for that.
        if _speech_flushed(started_at):
            playback_lock.release()
            return _archive(flushed=True)
        # Cross-host: also claim the shared remote broker so another machine's
        # reply can't stop+clear our still-playing playlist. Waits out a healthy
        # remote holder, takes over an expired one. No-op for local/rooms.
        _wait_and_claim_broker(sink, target)
        # Grade B: push all clips to the remote player's local dir up front
        # (no-op for local/rooms), so each play below is a local loadfile —
        # no per-sentence network fetch to stall a long reply.
        # Defensive getattr: prefetch is a newer Sink method; a minimal sink
        # (or test double) without it just skips the pre-fetch (no-op anyway for
        # non-remote targets).
        getattr(sink, "prefetch", lambda *a, **k: None)(
            [p for _, p in clip_data], target)
        played_any = False
        n = len(clip_data)
        highlighter = _HighlightScheduler(_highlight_delay_s, do_highlight,
                                          source_pane)
        # Drop any stale jump request left by a previous response.
        _nav_flag_path(target).unlink(missing_ok=True)
        try:
            coordinator.before_speech(
                title=source_window, priority=event.priority.value,
                # The first sentence; the clip loop moves it on from there.
                text=clip_data[0][0] if clip_data else text)
            # Speech-started breadcrumb — the moment we commit to feeding the
            # broker. Its "end" twin is in the finally below, so every exit
            # (finished, superseded, yielded-then-done, error) closes the pair.
            _speech_event("start", text=text[:400], session=source_session,
                          pane=source_pane, source=event.source.value,
                          target=target.name)
            mute_watcher = _MuteDuckWatcher(sink, target, coordinator)
            # Shared per-clip marker — drives the popup (status bar, current
            # sentence, skip map). Identical for both playback paths below.
            def _mark(idx: int, live: Optional[dict] = None) -> None:
                sentence_i, clip_i = clip_data[idx]
                extras = {"text": text, "source": event.source.value,
                          "engine": engine, "voice": voice,
                          "clip_offset_s": offsets[idx],
                          "total_duration_s": total_duration_s,
                          "source_pane": source_pane,
                          "source_session": source_session,
                          "source_tmux_session": source_tmux_session,
                          "source_window": source_window,
                          "current_sentence": sentence_i,
                          "current_sentence_idx": idx,
                          "clip_paragraph_idx": clip_para,
                          "clip_sentences": _clip_sentences,
                          # The whole turn's clips, known up front on this
                          # lane. History records them only when the turn
                          # ends, so this is what lets the turn you are
                          # hearing be replayed (popup `r` / `<`) instead of
                          # the one before it.
                          "clip_uris": [str(p) for _, p in clip_data],
                          "clip_durations_s": list(durations),
                          "writer_pid": os.getpid()}
                # Figure-bearing message ([[visual:]]/[[reveal:]]): surfaces as
                # the ▣ indicator in the status bar / popup / canvas badge.
                if event.metadata.get("visual"):
                    extras["visual"] = event.metadata["visual"]
                # A multiple-choice question: the row is on the alert lane but
                # is a real turn, and a reader wants the options as options
                # rather than the run-together sentence the voice was given.
                if event.metadata.get("ask"):
                    extras["ask"] = event.metadata["ask"]
                if live is not None:
                    # Mirror the *remote* player's live state into now_playing so a
                    # status read (popup redraw) is a local DB hit, not a ~600ms
                    # bridge round-trip to the phone. Timeline position = the start
                    # of this clip plus how far mpv is into it.
                    tp = live.get("time-pos")
                    extras["live_pos_s"] = (offsets[idx] + tp
                                            if tp is not None else offsets[idx])
                    # When it was read. A position without one is only true at
                    # the instant it was taken, and this lane takes one per
                    # sentence — on the gapless playlist, where the player
                    # advances itself, once for the whole reply.
                    extras["live_pos_at"] = time.time()
                    extras["live_pause"] = bool(live.get("pause"))
                    extras["live_speed"] = live.get("speed") or 1.0
                    extras["live_mute"] = bool(live.get("mute"))
                # One local read, so a pause stamped between marks is not
                # thrown away by this one. See carry_pause_stamp.
                carry_pause_stamp(
                    (state.get_now_playing("speech") or {}).get("extras") or {},
                    extras, live is not None)
                state.set_now_playing(
                    "speech", uri=str(clip_i), started_at=started_at,
                    target=target.name, extras=extras)
                # And on the broker, where a card can read it: the sentence
                # being spoken, as it is spoken.
                coordinator.speaking_line(sentence_i)

            if _remote_playlist(target):
                # Autonomous gapless playlist: load every clip and let the remote
                # player advance through them itself — no per-sentence drive to
                # stall, and gapless (no inter-sentence gap). We only *follow*
                # playlist-pos to move the popup/highlight; a dropped poll lags
                # the follow-along, it never cuts the audio.
                try:
                    sink.play_playlist([p for _, p in clip_data], target)
                    played_any = True
                except Exception as e:  # noqa: BLE001
                    log.warning("intake: play_playlist failed: %s", e)
                    state.log_error("intake", "play_playlist failed",
                                    extras={"detail": str(e),
                                            "source": event.source.value})
                # Seed now_playing immediately so a status read (popup) shows the
                # response as playing right away, before the first bridge snapshot
                # lands (~0.6s) to fill in the live position.
                if played_any:
                    _mark(0)
                i = -1
                nav_jump = False
                misses = 0
                last_ms = -1
                stall = 0
                # Hold the playback token until the reply's audio is really done,
                # not merely until we can still *see* the player. Losing the
                # follow-along (a flaky bridge trips the misses/stall guards below)
                # must NOT release the token early: a queued equal-priority reply
                # would then grab it and play_playlist stop+clears our still-
                # playing audio — the "long reply gets cut off and never comes
                # back" bug. We know the whole reply's duration, so on a blind
                # bail we keep the token until that's plausibly elapsed (the
                # blind-hold tail after this loop). `finished` is set only when we
                # positively observe the end (idle, or a skip past the last clip).
                finished = False
                hard_deadline = time.monotonic() + (total_duration_s or 0.0) + 5.0
                last_broker_refresh = time.monotonic()
                while played_any:
                    # Superseded by a later URGENT in this session — drop the
                    # rest of the playlist instead of yielding-and-resuming.
                    if playback_lock.should_abort():
                        highlighter.cancel_pending()
                        sink.stop(target)
                        finished = True
                        break
                    # Step aside for a higher-priority speaker (e.g. a
                    # notification) waiting on the token, then resume this reply —
                    # the remote counterpart of the per-clip path's yield. The
                    # phone plays the playlist autonomously, so to hand the broker
                    # over cleanly we stop it, drop our broker claim (else a
                    # same-host higher speaker would wait out our claim's TTL),
                    # yield the flock until the higher clip finishes, then reload
                    # the full playlist and jump back to where we were — reloading
                    # the whole list (not just the tail) keeps playlist-pos mapping
                    # 1:1 to the sentence index for the popup/highlight.
                    if playback_lock.should_yield():
                        resume_i = i if 0 <= i < n else 0
                        highlighter.cancel_pending()
                        sink.stop(target)
                        getattr(sink, "release_broker",
                                lambda *a, **k: None)(target)
                        playback_lock.yield_to_higher()
                        _wait_and_claim_broker(sink, target)
                        try:
                            sink.play_playlist([p for _, p in clip_data], target)
                            if resume_i > 0:
                                sink.set_playlist_pos(resume_i, target)
                        except Exception as e:  # noqa: BLE001
                            log.warning("intake: resume after yield failed: %s", e)
                        # Re-arm follow state; recompute the blind-hold deadline for
                        # only the audio that's left (wall-clock advanced while the
                        # higher-priority reply played).
                        i = -1
                        nav_jump = True
                        misses = 0
                        last_ms = -1
                        stall = 0
                        remaining = max(0.0, total_duration_s - offsets[resume_i])
                        hard_deadline = time.monotonic() + remaining + 5.0
                        last_broker_refresh = time.monotonic()
                        _mark(resume_i)
                        continue
                    # Keep our cross-host broker claim alive while we play so
                    # another machine's reply doesn't take it for a stalled one.
                    if time.monotonic() - last_broker_refresh > 5.0:
                        getattr(sink, "refresh_broker",
                                lambda *a, **k: None)(target)
                        last_broker_refresh = time.monotonic()
                    # One batched snapshot per tick (pos/idle/pause/time) instead
                    # of four separate ~600ms bridge hops — keeps the follow-along
                    # tight rather than lagging the audio by seconds.
                    snap = sink.snapshot(target)
                    if not snap:
                        misses += 1
                        if misses > 50:        # ~5s fully unreadable → bail
                            break
                        time.sleep(0.1)
                        continue
                    misses = 0
                    mute_watcher.poll(snap.get("mute"))  # from the same snapshot
                    nav = _read_nav_request(target)
                    if nav is not None:
                        if nav >= n:
                            highlighter.cancel_pending()
                            sink.stop(target)
                            finished = True   # skip past last clip = intentional end
                            break
                        sink.set_playlist_pos(max(0, nav), target)
                        nav_jump = True
                        stall = 0
                    if snap.get("pause"):
                        if 0 <= i < n:
                            _mark(i, live=snap)  # reflect the pause in now_playing
                        stall = 0
                        time.sleep(0.1)
                        continue
                    if snap.get("idle-active"):
                        finished = True
                        break  # playlist finished
                    pos = snap.get("playlist-pos")
                    if pos is None or pos < 0:
                        time.sleep(0.1)   # loaded but not on an entry yet
                        continue
                    if pos != i and 0 <= pos < n:
                        i = pos
                        highlighter.show(clip_data[i][0],
                                         first=(i == 0), force=nav_jump)
                        nav_jump = False
                        stall = 0
                    if 0 <= i < n:
                        # Every tick, not just on sentence change: keep the mirrored
                        # live position/pause/speed/mute fresh so the popup's redraw
                        # reads a current local snapshot.
                        _mark(i, live=snap)
                    # Stall guard: if playback time isn't advancing while we're
                    # not paused (a wedged clip, or another process clobbering the
                    # shared broker), bail so a response can never hang. A gapless
                    # clip boundary resets time-pos, which counts as progress.
                    ms = snap.get("time-pos")
                    if ms is not None and ms != last_ms:
                        last_ms = ms
                        stall = 0
                    else:
                        stall += 1
                        if stall > 80:         # ~8s with no progress → give up
                            log.warning("intake: playlist stalled; ending follow")
                            break
                    time.sleep(0.1)
                # Blind-hold: the follow loop stopped but we never positively saw
                # the playlist end (the bridge went unreadable / stalled). The
                # phone is most likely still playing our clips, so keep the token
                # until we can confirm it's idle again or the reply's own duration
                # has elapsed — otherwise a queued reply clobbers the remaining
                # audio. Trust only a *readable* idle: snapshot() returns None on a
                # dead bridge, and sink.idle() reports idle on IPC error, so either
                # alone would release us straight back into the clobber.
                if played_any and not finished:
                    log.info("intake: lost follow-along; holding speech token "
                             "until audio completes")
                    while time.monotonic() < hard_deadline:
                        snap = sink.snapshot(target)
                        if snap and snap.get("idle-active"):
                            break
                        time.sleep(0.5)
            else:
                i = 0
                nav_jump = False  # True when this clip was reached via a popup skip
                while 0 <= i < n:
                    # Superseded by a later URGENT in this session — drop the
                    # remaining sentences instead of yielding-and-resuming.
                    if playback_lock.should_abort():
                        break
                    # Step aside between sentences if a higher-priority speaker
                    # (e.g. a notification) is waiting; resume it once that's done.
                    if playback_lock.should_yield():
                        playback_lock.yield_to_higher()
                    sentence, clip_path = clip_data[i]
                    _mark(i)
                    try:
                        # Only the first sentence resets a lingering pause/mute;
                        # later sentences preserve a pause made mid-response.
                        sink.play(str(clip_path), target, reset_state=(i == 0))
                        played_any = True
                    except Exception as e:  # noqa: BLE001
                        log.warning("intake: sink-speech.play failed: %s", e)
                        state.log_error("intake", "sink-speech play failed",
                                        extras={"detail": str(e),
                                                "source": event.source.value})
                        i += 1
                        nav_jump = False
                        continue
                    # Highlight is deferred by the Snapcast buffer so it lands with
                    # the audio (a manual jump forces it on immediately).
                    highlighter.show(sentence, first=(i == 0), force=nav_jump)
                    nav = _wait_for_clip(sink, target, on_poll=mute_watcher.poll)
                    if nav is None:
                        i += 1
                        nav_jump = False
                    else:
                        # Popup sentence/paragraph jump; past the last clip = end.
                        if nav >= n:
                            highlighter.cancel_pending()
                            break
                        i = max(0, nav)
                        nav_jump = True
        finally:
            highlighter.drain()
            coordinator.after_speech()
            _speech_event("end", text=text[:160], session=source_session,
                          pane=source_pane, source=event.source.value,
                          target=target.name, played=bool(played_any))
            state.clear_now_playing("speech")
            # Drop the cross-host broker claim before the flock so the next host
            # (and the next local waiter) can take over immediately. No-op local.
            getattr(sink, "release_broker", lambda *a, **k: None)(target)
            playback_lock.release()

        if not played_any:
            return None

    return _archive()


def submit_stream(sentences,
                  event: Event,
                  *,
                  state: Optional[StateStore] = None,
                  coordinator: Optional[Coordinator] = None,
                  sink: Optional[SinkSpeech] = None) -> Optional[int]:
    """Streaming sibling of `submit_event`: speak sentences as they arrive.

    `sentences` is an iterable yielding cleaned sentence strings as a producer
    (e.g. a model's token stream) completes them. Each sentence is rendered the
    instant it arrives and played in order through the same long-running
    sink-speech broker, so audio for sentence 1 starts while the model is still
    generating the rest — the key win over `submit_event`, which needs the
    whole reply first.

    Remote players are paused/resumed once (not per sentence). Best-effort
    karaoke highlight + back/forward nav over already-spoken sentences; the
    response-wide progress bar grows as sentences arrive (total length isn't
    known up front). One history row is written at the end.

    Blocks until the producer is exhausted and all clips finish. Callers that
    need fire-and-forget should run this in a thread.
    """
    from ..types import Source as _Source

    state = state or StateStore()
    coordinator = coordinator or Coordinator(state=state)
    sink = sink or SinkSpeech()
    target = event.target or Target(
        name=os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET", "local"))

    # The device asked for quiet, and this is an alert nobody asked for. Held
    # before the render, not after: unlike a muted pane there is nothing to
    # replay a clip *for* once the moment has gone, and the words survive in
    # history either way. See `_ringer_silenced`.
    held = _ringer_hold(target, event)
    if held:
        # `sentences` is consumed only on this branch — the streaming path's
        # whole point is not to wait for the producer, and an alert being held
        # is the one case where we need every word before deciding anything.
        return _record_silenced(state, event, target,
                                " ".join(sentences), held)

    # Remote-say bridge: on a headless feeder host (e.g. red5) whose rooms now
    # listen to a remote low-latency Snapcast hub, render the
    # reply *there* instead of locally — the hub renders the text to its own
    # Snapcast fifo. The coordinator still ducks from here (it drives the rooms
    # snapserver over the tailnet via MEDIA_SNAP_JSONRPC_HOST), so music dips
    # under speech as before. Env-gated per target (see _remote_say_cmd): no
    # lane for this target ⇒ the local render+play path below is unchanged.
    remote_say = _remote_say_cmd(target)
    if remote_say:
        return _submit_remote_say(" ".join(sentences), remote_say, coordinator, state, event)

    engine = _resolve_engine(event)
    voice = _resolve_voice(event, engine)
    ext = _ext_for(engine)

    audio_dir = _audio_dir()
    # Per-submission unique: second-resolution time is NOT enough — two
    # concurrent sessions (both source "claude-code") finishing a reply in the
    # same second would render to identical clip paths and clobber each other's
    # audio, so one session could end up playing another's clips. pid + a short
    # random token guarantees uniqueness across processes and within one.
    stamp = (time.strftime("%Y%m%dT%H%M%S")
             + f"-{os.getpid()}-{uuid.uuid4().hex[:6]}")
    started_at = time.time()

    source_pane = (event.metadata or {}).get("pane") or os.environ.get("TMUX_PANE", "")
    source_session = (event.metadata or {}).get("session") or ""
    order_session = _order_session(source_pane, source_session)
    source_tmux_session, source_window = _source_place(event.metadata, source_pane)
    # Durable per-pane / per-session mute: render the stream into clips for
    # popup replay/history, but never play it or duck music. See submit_event.
    muted = state.resolve_mute(source_pane, source_tmux_session)
    do_highlight = event.source not in (_Source.CLI,)
    if do_highlight:
        ensure_follow_view(pane=source_pane)   # self-gates on the flag
    # Snapcast buffers audio after mpv starts writing, so the sound reaches the
    # listener a beat later than play() returns. Hold the highlight that long so
    # it lands with the speech rather than ahead of it.
    _highlight_delay_s = float(
        os.environ.get("MEDIA_SNAPCAST_LATENCY_MS", "500")) / 1000.0

    fallback_info: dict = {}
    _fallback_lock = threading.Lock()

    def _on_fallback(failed_engine: str, err: str) -> None:
        short = err.strip().splitlines()[0] if err else "no detail"
        fb = os.environ.get("MEDIA_RENDER_FALLBACK_ENGINE") or "edge"
        kind = "render-fallback"
        if "insufficient_quota" in err:
            kind = "render-quota"
        log.warning("intake-stream: %s engine failed (%s); falling back to %s",
                    failed_engine, short, fb)
        state.log_error("intake",
                        f"render {failed_engine} failed, fell back to {fb}",
                        extras={"kind": kind, "engine": failed_engine,
                                "fallback_engine": fb, "detail": short[:300],
                                "source": event.source.value})
        with _fallback_lock:
            fallback_info.update({"from_engine": failed_engine,
                                  "fallback_engine": fb, "kind": kind,
                                  "detail": short[:300]})
        if kind == "render-quota":
            title = f"agent-media: {failed_engine} quota exhausted"
            body = f"Falling back to {fb} for now."
        else:
            title = f"agent-media: {failed_engine} render failed"
            body = f"Falling back to {fb}. {short[:120]}"
        notify(key=f"render-fallback-{failed_engine}", title=title, content=body)

    # Shared, lock-guarded clip table: the producer thread appends sentences and
    # kicks off their renders the instant they arrive; the play loop consumes in
    # order. Renders run a few ahead of playback via a bounded pool.
    workers = max(1, int(os.environ.get("MEDIA_STREAM_RENDER_WORKERS", "3") or 3))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    cond = threading.Condition()
    sents: list[str] = []
    futures: list = []
    paths: list[Path] = []
    producer_done = threading.Event()

    def _produce() -> None:
        try:
            for s in sentences:
                if not s or not s.strip():
                    continue
                outfile = audio_dir / f"{stamp}--{event.source.value}--{len(sents):03d}.{ext}"
                fut = pool.submit(render_text, s, outfile,
                                  engine=engine, voice=voice, on_fallback=_on_fallback)
                with cond:
                    sents.append(s)
                    futures.append(fut)
                    paths.append(outfile)
                    cond.notify_all()
        except Exception as exc:  # noqa: BLE001
            log.warning("intake-stream: producer raised: %s", exc)
        finally:
            producer_done.set()
            with cond:
                cond.notify_all()

    def _get(i: int):
        """(sentence, future, path) for clip i, waiting until it's enqueued.
        Returns None when no clip i will ever exist (producer finished)."""
        with cond:
            while i >= len(sents) and not producer_done.is_set():
                cond.wait(timeout=0.5)
            if i >= len(sents):
                return None
            return sents[i], futures[i], paths[i]

    # Skipped when muted — nothing plays, so there's nothing to pause for.
    if not muted:
        coordinator.pre_pause_remote()
    _nav_flag_path(target).unlink(missing_ok=True)
    producer = threading.Thread(target=_produce, daemon=True)
    producer.start()

    durations: dict[int, float] = {}   # measured clip durations, by index
    played_any = False
    first_clip: Optional[Path] = None

    if muted:
        # Drain the producer into clips so the response is in history and the
        # popup can replay it — but never play it or touch the coordinator.
        i = 0
        try:
            while True:
                item = _get(i)
                if item is None:
                    break
                sentence, fut, clip_path = item
                try:
                    ok, err = fut.result()
                except Exception as exc:  # noqa: BLE001
                    log.warning("intake-stream: render future raised: %s", exc)
                    i += 1
                    continue
                if not ok:
                    log.warning("intake-stream: render failed (%s): %s", engine, err)
                    state.log_error("intake", f"render failed ({engine})",
                                    extras={"err": err, "source": event.source.value})
                    i += 1
                    continue
                with _fallback_lock:
                    has_fallback = bool(fallback_info)
                if has_fallback and clip_path.suffix == ".wav":
                    renamed = clip_path.with_suffix(".mp3")
                    try:
                        clip_path.rename(renamed)
                        clip_path = renamed
                        paths[i] = renamed
                    except OSError:
                        pass
                durations[i] = _clip_duration(clip_path)
                if first_clip is None:
                    first_clip = clip_path
                played_any = True
                i += 1
        finally:
            producer_done.wait(timeout=2.0)
            pool.shutdown(wait=False)
        # Sidecar with the full text so the popup shows the whole response.
        if first_clip is not None:
            with cond:
                known = list(sents)
            try:
                first_clip.with_suffix(".txt").write_text(" ".join(known))
            except OSError:
                pass
    else:
        before_called = False
        mute_watcher = _MuteDuckWatcher(sink, target, coordinator)
        highlighter = _HighlightScheduler(_highlight_delay_s, do_highlight,
                                          source_pane)
        # Serialize playback across sessions (rendering keeps streaming in
        # parallel via the producer thread while we wait our turn for the broker).
        playback_lock = _SpeechPlaybackLock()
        playback_lock.acquire(
            event.priority, session=order_session,
            supersede=bool((event.metadata or {}).get("supersede")),
            seq=started_at)
        i = 0
        nav_jump = False
        try:
            while True:
                # Superseded by a later URGENT in this session — drop the rest
                # (whether or not we've started) instead of resuming.
                if playback_lock.should_abort():
                    break
                # Step aside between sentences for a higher-priority speaker;
                # resume this clip once it's done. Only after the first clip has
                # played, so before_speech ran and there's something to resume.
                if before_called and playback_lock.should_yield():
                    playback_lock.yield_to_higher()
                item = _get(i)
                if item is None:
                    break
                sentence, fut, clip_path = item
                try:
                    ok, err = fut.result()
                except Exception as exc:  # noqa: BLE001
                    log.warning("intake-stream: render future raised: %s", exc)
                    i += 1
                    nav_jump = False
                    continue
                if not ok:
                    log.warning("intake-stream: render failed (%s): %s", engine, err)
                    state.log_error("intake", f"render failed ({engine})",
                                    extras={"err": err, "source": event.source.value})
                    i += 1
                    nav_jump = False
                    continue
                with _fallback_lock:
                    has_fallback = bool(fallback_info)
                if has_fallback and clip_path.suffix == ".wav":
                    renamed = clip_path.with_suffix(".mp3")
                    try:
                        clip_path.rename(renamed)
                        clip_path = renamed
                        paths[i] = renamed
                    except OSError:
                        pass

                if not before_called:
                    coordinator.before_speech(title=source_window,
                                  priority=event.priority.value, text=text)
                    before_called = True

                durations[i] = _clip_duration(clip_path)
                with cond:
                    known = list(sents)
                if first_clip is None:
                    first_clip = clip_path
                # Keep the first clip's text sidecar updated with everything spoken
                # so far, so the popup can show the running response.
                try:
                    first_clip.with_suffix(".txt").write_text(" ".join(known))
                except OSError:
                    pass

                offset = sum(durations.get(k, 0.0) for k in range(i))
                total = sum(durations.values())  # grows as more clips render
                state.set_now_playing(
                    "speech", uri=str(clip_path), started_at=started_at,
                    target=target.name,
                    extras={"text": " ".join(known), "source": event.source.value,
                            "engine": engine, "voice": voice,
                            "clip_offset_s": offset,
                            "total_duration_s": total,
                            "source_pane": source_pane,
                            "source_session": source_session,
                            "source_tmux_session": source_tmux_session,
                            "source_window": source_window,
                            "current_sentence": sentence,
                            "current_sentence_idx": i,
                            "clip_sentences": known,
                            # The clips rendered so far. History only learns
                            # them when the turn ends, so without this the
                            # turn you are *hearing* can't be replayed — the
                            # popup's `r` and `<` would restart the previous
                            # turn instead. Mid-stream that list is partial by
                            # nature; replaying what has been spoken is still
                            # the right answer to "again".
                            "clip_uris": [str(p) for p in paths],
                            "clip_durations_s": [durations.get(k, 0.0)
                                                 for k in range(len(paths))],
                            "streaming": True,
                            "writer_pid": os.getpid()})
                try:
                    sink.play(str(clip_path), target, reset_state=(i == 0))
                    played_any = True
                except Exception as e:  # noqa: BLE001
                    log.warning("intake-stream: sink-speech.play failed: %s", e)
                    state.log_error("intake", "sink-speech play failed",
                                    extras={"detail": str(e),
                                            "source": event.source.value})
                    i += 1
                    nav_jump = False
                    continue
                # Deferred so the highlight lands with the Snapcast-buffered audio.
                highlighter.show(sentence, first=(i == 0), force=nav_jump)

                nav = _wait_for_clip(sink, target, on_poll=mute_watcher.poll)
                if nav is None:
                    i += 1
                    nav_jump = False
                else:
                    with cond:
                        count = len(sents)
                        done = producer_done.is_set()
                    if nav >= count:
                        # "Skip to the end": stop if the producer's finished,
                        # otherwise fall through to whatever arrives next.
                        if done:
                            highlighter.cancel_pending()
                            break
                        i = count
                        nav_jump = False
                    else:
                        i = max(0, nav)
                        nav_jump = True
        finally:
            highlighter.drain()
            coordinator.after_speech()
            state.clear_now_playing("speech")
            playback_lock.release()
            producer_done.wait(timeout=2.0)
            pool.shutdown(wait=False)

    if not played_any:
        return None

    with cond:
        all_sents = list(sents)
        all_paths = list(paths)
    full_text = " ".join(all_sents)
    extras = {"engine": engine, "voice": voice,
              "priority": event.priority.value,
              "source_pane": source_pane,
              "source_session": source_session,
              "source_tmux_session": source_tmux_session,
              "source_window": source_window,
              "clip_uris": [str(p) for p in all_paths],
              "clip_sentences": all_sents,
              "clip_durations_s": [durations.get(k, 0.0) for k in range(len(all_paths))],
              "streaming": True,
              **(event.metadata or {})}
    if fallback_info:
        extras["fallback"] = fallback_info
    if muted:
        extras["muted"] = True   # rendered but never played (popup can replay)
    return state.add_history(
        sink="speech",
        uri=str(first_clip),
        started_at=started_at,
        ended_at=time.time(),
        target=target.name,
        source=event.source.value,
        text=full_text,
        extras=extras,
    )
