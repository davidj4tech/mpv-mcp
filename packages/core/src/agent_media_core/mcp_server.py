"""MCP control surface for agent-media.

Two entrypoints over the same tool definitions:

  * `media-mcp`      — stdio transport, for Claude Code (user-scope
                       registration via `claude mcp add`).
  * `media-mcp-http` — streamable-HTTP transport, for remote callers
                       (a remote room, HA, anything off-box). Bind via
                       MEDIA_MCP_HOST / MEDIA_MCP_PORT (defaults
                       127.0.0.1:8765 — set MEDIA_MCP_HOST to the
                       Tailscale IP to expose on the tailnet).

Tools cover the surface docs/reference/restructure.md called for: speech.{pause,
resume,stop,now_playing,history,replay_last} and music.{play,pause,
resume,stop,volume,now_playing,seek} plus a convenience `say` that
submits a one-shot Event through the same intake pipeline the hooks
use.

Replaces the legacy Node `packages/media-mcp/server.js` end-to-end.
"""

import logging
import os
import threading
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer


def _host() -> str:
    return os.environ.get("MEDIA_MCP_HOST", "127.0.0.1")


def _port() -> int:
    try:
        return int(os.environ.get("MEDIA_MCP_PORT", "8765"))
    except ValueError:
        return 8765

from .intake._env import load_env_file
from .route import (
    BED_DUCK,
    BED_PAUSE,
    FOCUS_BOOK,
    FOCUS_MUSIC,
    apply_focus,
    bed_strategy,
    coerce_content_type,
    detect_content_type,
    resolve,
)
from . import library, phone_player
from .sinks import SinkBook, SinkMusic, SinkSpeech
from .sinks.book import normalize_uri
from .sinks.music_router import SinkMusicRouter
from .state import StateStore
from .types import Event, Priority, Source, Target


log = logging.getLogger(__name__)

# host/port are NOT constructor args in the mcp 2.x API — they are run()
# kwargs for the HTTP transports (see main_http). Passing them here would be
# silently ignored and the streamable-HTTP server would bind the SDK default
# 127.0.0.1:8000 instead of the configured address, i.e. unreachable over the
# tailnet. stdio (main) ignores both either way.
mcp = MCPServer("agent-media")


# --- shared singletons ----------------------------------------------------

def _state() -> StateStore:
    if not hasattr(_state, "_v"):
        _state._v = StateStore()  # type: ignore[attr-defined]
    return _state._v  # type: ignore[attr-defined]


def _speech() -> SinkSpeech:
    if not hasattr(_speech, "_v"):
        _speech._v = SinkSpeech()  # type: ignore[attr-defined]
    return _speech._v  # type: ignore[attr-defined]


def _music() -> SinkMusicRouter:
    if not hasattr(_music, "_v"):
        _music._v = SinkMusicRouter()  # type: ignore[attr-defined]
    return _music._v  # type: ignore[attr-defined]


def _book() -> SinkBook:
    if not hasattr(_book, "_v"):
        _book._v = SinkBook()  # type: ignore[attr-defined]
    return _book._v  # type: ignore[attr-defined]


#: How long to wait for mpv to publish `media-title` after a load, so the play
#: history can show a name instead of a filename. Short by design: this sits
#: between the caller asking for a book and being told it started. Set 0 to
#: skip it entirely.
TITLE_WAIT_S = 2.0
_TITLE_POLL_S = 0.15


def _note_media_title(book: "SinkBook", state: StateStore, target, uri: str) -> None:
    """Record the title mpv reports for a freshly-loaded book, if it has one.

    A history row is written when a book is *put on*, when the only thing known
    is a URI. mpv learns the real name a moment later, and a listing of
    `td565-video-2026-08-11-15-42-38.mp3` is what happens if nobody goes back
    for it.

    Bounded, synchronous and best-effort: a background thread cannot be used
    here because the CLI shares this code path and exits as soon as the command
    returns, taking any thread with it. So it polls briefly and gives up — a
    missing title costs a nicer label, and nothing else.
    """
    try:
        wait = float(os.environ.get("MEDIA_BOOK_TITLE_WAIT_S", TITLE_WAIT_S))
    except (TypeError, ValueError):
        wait = TITLE_WAIT_S
    if wait <= 0:
        return
    from .sinks import _mpv_ipc as ipc
    from .sinks.book import _socket_for

    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            props = ipc.display_properties(_socket_for(target),
                                           ["path", "media-title"], timeout=1.0)
        except Exception:  # noqa: BLE001 — the broker is allowed to be busy
            props = None
        if props:
            # Only trust a title that belongs to the file we just asked for:
            # mpv may still have the previous one open.
            if props.get("path") == uri:
                title = str(props.get("media-title") or "").strip()
                if title:
                    state.set_history_title("book", uri, title)
                    return
        time.sleep(_TITLE_POLL_S)


def _save_book_bookmark(book: SinkBook, state: StateStore,
                        target: Target) -> None:
    """Persist the currently-open book's position as its resume bookmark.

    Called before pause/stop/switch so `book resume` (or reopening the same
    URI) lands where the listener left off. Best-effort and spawn-free.
    """
    try:
        np = state.get_now_playing("book")
        if not np:
            return
        pos = book.position(target)
        if pos is not None and pos > 0:
            state.set_resume_pos(np["uri"], pos)
    except Exception:  # noqa: BLE001
        pass


def _target(name: str) -> Target:
    return Target(name=name or "local")


def _music_target(name: str = "") -> Target:
    """Resolve the music channel output target.

    Empty uses MEDIA_MUSIC_DEFAULT_TARGET; if unset, follow speech so the
    connected/default device can own music just like books.
    """
    if not name:
        name = (os.environ.get("MEDIA_MUSIC_DEFAULT_TARGET")
                or os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET")
                or "local")
    return Target(name=name or "local")


def _book_target(name: str = "") -> Target:
    """Resolve the book channel's output target.

    Empty uses MEDIA_BOOK_DEFAULT_TARGET; if unset, follow
    MEDIA_SPEECH_DEFAULT_TARGET so book playback lands wherever speech does.
    """
    if not name:
        name = (os.environ.get("MEDIA_BOOK_DEFAULT_TARGET")
                or os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET")
                or "local")
    return Target(name=name or "local")


# --- say: one-shot synthesize+play ----------------------------------------

@mcp.tool()
def say(text: str,
        voice: str = "",
        engine: str = "",
        target: str = "local",
        priority: str = "normal",
        supersede: bool = False) -> dict:
    """Synthesize `text` and play it through sink-speech.

    Args:
        text: What to speak.
        voice: Override the render voice (engine-specific). Empty = use default.
        engine: Override engine (edge / openai / qwen / realtime).
            Empty = use MEDIA_RENDER_ENGINE default.
        target: Sink target. Default "local".
        priority: "low" / "normal" / "high" / "urgent". Within one session,
            only "urgent" interrupts what's already speaking (a barge-in);
            lower priorities queue and play in submission order.
        supersede: Only meaningful with priority "urgent". By default an urgent
            barge-in lets the message it interrupted resume afterwards; set
            supersede=True to DROP the same-session messages it interrupts or
            was queued ahead of instead. Use when this message replaces them.
    """
    from .intake.submit import submit_event

    try:
        prio = Priority(priority)
    except ValueError:
        prio = Priority.NORMAL

    metadata = {"kind": "say"}
    if supersede:
        metadata["supersede"] = True

    history_id = submit_event(Event(
        text=text, source=Source.MCP,
        priority=prio,
        voice=voice or None,
        engine=engine or None,
        target=_target(target),
        metadata=metadata,
    ), state=_state())
    return {"history_id": history_id}


@mcp.tool()
def converse(text: str,
             target: str = "",
             timeout_s: float = 90.0,
             voice: str = "",
             engine: str = "") -> dict:
    """Speak `text`, then wait for the human's spoken reply and return it.

    Use for a genuine mid-task question — a choice you cannot make from the
    code, an ambiguity worth one sentence of clarification. The call does NOT
    return until there is a reply or `timeout_s` elapses, so it costs the human
    their attention: for anything that needs no answer, use `say`.

    The human still initiates speaking (tap-to-talk / wake word); this only
    routes their next transcript here instead of into the tmux pane. An
    answerer with no microphone on that path — another agent, over the relay —
    can reply with `media converse-reply "<text>"` instead; `media
    converse-reply --pending` shows them what was asked.

    Args:
        text: The question to speak.
        target: Sink target. Empty = MEDIA_SPEECH_DEFAULT_TARGET.
        timeout_s: How long to wait for a reply before giving up.
        voice: Override the render voice. Empty = default.
        engine: Override engine (edge / openai / qwen / realtime).

    Returns {"reply": "..."} — or {"reply": None, "reason": ...} on timeout or
    if another converse call already holds the rendezvous.
    """
    from .capture import doorbell
    from .capture.rendezvous import Busy, Rendezvous
    from .intake.submit import submit_event

    tgt = _target(target or os.environ.get(
        "MEDIA_SPEECH_DEFAULT_TARGET", "local"))

    # kind=converse keeps the question out of speech_history's response list
    # (same treatment as notif clips) — it's a prompt, not a reply.
    submit_event(Event(
        text=text, source=Source.MCP,
        priority=Priority.NORMAL,
        voice=voice or None,
        engine=engine or None,
        target=tgt,
        metadata={"kind": "converse"},
    ), state=_state())

    # submit_event blocks until the clips finish, but "finished" means mpv is
    # done — with a Snapcast target the audio is still in flight down the
    # buffer. Opening the ear on that edge transcribes the tail of our own
    # question. Poll idle (cheap, and it returns True on IPC error so a hiccup
    # can't wedge us) and then wait out the buffer.
    _await_quiet(tgt)

    try:
        with Rendezvous(timeout_s=timeout_s, question=text) as rv:
            log.info("converse: listening (up to %.0fs)", timeout_s)
            # The doorbell survives the question not being heard — David in
            # another room, or an answerer (Cece) who cannot hear at all.
            doorbell.ring(text, timeout_s)
            doorbell.post(text, timeout_s)
            t0 = time.monotonic()
            try:
                reply = rv.wait()
            finally:
                doorbell.clear()
    except Busy as exc:
        log.warning("converse: %s", exc)
        return {"reply": None, "reason": str(exc)}

    waited = time.monotonic() - t0
    if reply is None:
        log.info("converse: no reply after %.1fs", waited)
        return {"reply": None, "reason": "timeout"}
    log.info("converse: reply after %.1fs (%d chars)", waited, len(reply))
    return {"reply": reply}


def _await_quiet(target: Target, max_wait_s: float = 120.0) -> None:
    """Block until the spoken prompt has actually left the speakers.

    MEDIA_CONVERSE_ECHO_MS covers the sink-to-ear buffer for the target; the
    parec->snapfifo path is bounded at ~500ms, so the default leaves headroom.
    """
    speech = _speech()
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline and not speech.idle(target):
        time.sleep(0.1)
    try:
        echo_ms = int(os.environ.get("MEDIA_CONVERSE_ECHO_MS", "900"))
    except ValueError:
        echo_ms = 900
    time.sleep(max(echo_ms, 0) / 1000)


# --- speech sink controls --------------------------------------------------

@mcp.tool()
def speech_pause(target: str = "local") -> dict:
    """Pause the speech sink (mid-clip). Use `speech_resume` to continue."""
    _speech().pause(_target(target))
    return {"ok": True}


@mcp.tool()
def speech_resume(target: str = "local") -> dict:
    """Resume the speech sink."""
    _speech().resume(_target(target))
    return {"ok": True}


@mcp.tool()
def speech_stop(target: str = "local") -> dict:
    """Stop the speech sink. Drops the current clip."""
    _speech().stop(_target(target))
    return {"ok": True}


@mcp.tool()
def speech_now_playing(target: str = "local") -> dict:
    """What the speech sink is currently playing — path + position."""
    t = _target(target)
    s = _speech()
    return {"idle": s.idle(t), "position_ms": s.position(t)}


_PROMPT_KINDS = {"notif", "converse"}


def _is_prompt_clip(row: dict) -> bool:
    """True for clips that asked the human something rather than answering."""
    extras = row.get("extras")
    return (isinstance(extras, dict)
            and extras.get("kind") in _PROMPT_KINDS)


@mcp.tool()
def speech_history(limit: int = 10) -> list[dict]:
    """The last N speech clips. Most recent first."""
    # Exclude "Claude is waiting" notif clips and converse questions: they're
    # prompts, not responses.
    # Over-fetch so filtering still leaves `limit` real responses.
    rows = _state().recent_history(sink="speech", limit=max(limit * 4, limit + 50))
    rows = [r for r in rows if not _is_prompt_clip(r)]
    return rows[:limit]


@mcp.tool()
def recently_played(limit: int = 20, channel: str = "") -> list[dict]:
    """What's been played lately on the music and book channels, newest first.

    One row per item that was deliberately put on — a queue that auto-advanced
    through many tracks is one row, not many. `channel` filters to
    music/book/speech; empty returns all three interleaved.

    Use it to answer "what was that thing I had on yesterday" and to replay it:
    each row carries the `uri` that `music_play` or `book_play` takes.
    """
    if channel and channel not in ("music", "book", "speech"):
        return []
    return _state().recent_history(sink=channel or None, limit=max(1, limit))


@mcp.tool()
def errors(limit: int = 20, component: str = "", since_minutes: int = 0) -> list[dict]:
    """Recent errors from every component — intake, coordinator, voice-bridge.

    Check this when something "just didn't happen": a reply that never sounded,
    a transcript that went nowhere, speech that fell back to another engine.
    Components log failures they recover from silently, so an empty result is
    meaningful and a repeated message usually indicates a broken assumption
    rather than a blip.

    Args:
        limit: Max rows, most recent first.
        component: Filter to one component (e.g. "intake", "voice-bridge").
        since_minutes: Only errors from the last N minutes. 0 = no limit.
    """
    since = (time.time() - since_minutes * 60) if since_minutes > 0 else None
    return _state().recent_errors(component=component or None,
                                  limit=limit, since=since)


@mcp.tool()
def speech_replay_last(target: str = "local") -> dict:
    """Replay the most recent speech clip."""
    # Skip notif/converse prompt clips: replay the last real response.
    rows = _state().recent_history(sink="speech", limit=50)
    rows = [r for r in rows if not _is_prompt_clip(r)]
    if not rows:
        return {"ok": False, "reason": "no history"}
    uri = rows[0].get("uri")
    if not uri:
        return {"ok": False, "reason": "history row missing uri"}
    _speech().play(uri, _target(target))
    return {"ok": True, "uri": uri}


def _last_speaking_pane() -> str:
    """The pane currently (or most recently) speaking, from the state store.

    The MCP daemon has no tmux pane of its own, so a pane-less `mute_pane`
    call targets whoever last spoke — the same "the pane I'm hearing" intent
    the popup uses.
    """
    st = _state()
    np = st.get_now_playing("speech") or {}
    ex = np.get("extras") if isinstance(np.get("extras"), dict) else {}
    pane = (ex or {}).get("source_pane") or ""
    if pane:
        return pane
    rows = st.recent_history(sink="speech", limit=1)
    if rows and isinstance(rows[0].get("extras"), dict):
        return rows[0]["extras"].get("source_pane") or ""
    return ""


@mcp.tool()
def mute_pane(pane: str = "", session: str = "", state: str = "toggle") -> dict:
    """Durably mute/unmute a tmux pane's (or a whole tmux session's) speech.

    A muted pane still renders and is recorded to history (so it can be
    replayed), but is never played live and never ducks music. With neither
    `pane` nor `session` given, targets the pane that is currently (or was
    last) speaking. `state` is `on`, `off`, or `toggle`.
    """
    st = _state()
    if session:
        scope, key = "session", session
    else:
        key = pane or _last_speaking_pane()
        if not key:
            return {"ok": False, "reason": "no pane (pass pane= or session=)"}
        scope = "pane"
    if state == "on":
        muted = True
    elif state == "off":
        muted = False
    else:  # toggle this scope's own override (no tmux here for an effective flip)
        muted = not bool(st.get_mute(scope, key))
    st.set_mute(scope, key, muted)
    # Muting also stops the covered pane's in-flight clip so it takes effect
    # immediately (the response is already in history, still replayable).
    stopped = False
    if muted:
        np = st.get_now_playing("speech") or {}
        ex = np.get("extras") if isinstance(np.get("extras"), dict) else {}
        ex = ex or {}
        covered = (ex.get("source_pane") == key if scope == "pane"
                   else ex.get("source_tmux_session") == key)
        if covered:
            _speech().stop(_target("local"))
            stopped = True
    return {"ok": True, "scope": scope, "key": key, "muted": muted,
            "stopped_current": stopped}


@mcp.tool()
def mute_status() -> dict:
    """All durable per-pane / per-session speech mutes."""
    return _state().list_mutes()


# --- music sink controls --------------------------------------------------

@mcp.tool()
def music_play(uri: str, replace: bool = True, target: str = "",
               content_type: str = "") -> dict:
    """Play a URI on the music sink (Mopidy) — music or longform alike.

    Args:
        uri: Mopidy URI — e.g. `yt:https://...`, `https://stream.url`,
            `local:track:...`.
        replace: Clear the queue first (default True).
        content_type: How speech should interrupt this. `music`/`dj-set`/
            `ambient` duck the volume; `audiobook`/`podcast` pause and
            resume (with a short rewind) so you don't miss narration.
            Defaults to auto-detection from the URI — which classifies a
            bare YouTube/HTTP URL as music, so set `audiobook` explicitly
            for spoken-word content from YouTube.
    """
    _music().play(uri, _music_target(target), replace=replace)
    ct = coerce_content_type(content_type) or detect_content_type(uri)
    _state().set_music_intent(uri, ct.value)
    return {"ok": True, "uri": uri, "content_type": ct.value}


@mcp.tool()
def music_pause(target: str = "") -> dict:
    """Pause music."""
    _music().pause(_music_target(target))
    return {"ok": True}


@mcp.tool()
def music_resume(target: str = "") -> dict:
    """Resume music. If nothing is loaded, reopen the last thing played —
    the same behaviour `book_resume` has."""
    m, t = _music(), _music_target(target)
    try:
        loaded = bool(m.now_playing_uri(t))
    except Exception:  # noqa: BLE001 — unreadable means "just resume"
        loaded = True
    if not loaded:
        last = _state().get_music_last()
        if not last:
            return {"ok": False, "reason": "no music to resume"}
        ct = last.get("content_type") or "music"
        m.play(last["uri"], t, replace=True)
        _state().set_music_intent(last["uri"], ct)
        return {"ok": True, "uri": last["uri"], "content_type": ct,
                "reopened": True}
    m.resume(t)
    return {"ok": True}


@mcp.tool()
def music_stop(target: str = "") -> dict:
    """Stop music and clear the playlist."""
    _music().stop(_music_target(target))
    _state().clear_music_intent()
    return {"ok": True}


@mcp.tool()
def music_volume(level: int, target: str = "") -> dict:
    """Set music volume (0-100). For temporary ducking during speech,
    let the route coordinator handle it — this is for the listener's
    own preference.
    """
    _music().duck(_music_target(target), max(0, min(100, level)))
    return {"ok": True, "level": level}


@mcp.tool()
def music_speed(rate: float, target: str = "") -> dict:
    """Set music playback speed (pitch-corrected tempo; 1.0 = normal,
    clamped to 0.25–4.0). Works on mpv-routed tracks — fetched YouTube in
    the rooms cache and the phone player — which is all YouTube playback
    now. MPD/GStreamer streams (radio, local:) have no speed control;
    those return ok=False.
    """
    ok = _music().set_speed(max(0.25, min(4.0, rate)), _music_target(target))
    return {"ok": ok, "rate": max(0.25, min(4.0, rate))}


@mcp.tool()
def music_now_playing(target: str = "") -> dict:
    """Current track URI + playback position in ms."""
    t = _music_target(target)
    m = _music()
    return {"uri": m.now_playing_uri(t), "position_ms": m.position(t)}


@mcp.tool()
def music_seek(position_ms: int, target: str = "") -> dict:
    """Seek the current music track to absolute position (ms)."""
    _music().seek_cur(_music_target(target), max(0, position_ms))
    return {"ok": True, "position_ms": position_ms}


# --- book sink controls (longform channel) --------------------------------
#
# The book channel is a *separate* player from music: its own queue, its own
# position, and durable resume-by-URI bookmarks. Speech pauses it (and
# rewinds a touch) rather than ducking. It runs as its own mpv broker on the
# local box and lazy-starts on first `book_play`.

@mcp.tool()
def book_play(uri: str, resume: bool = True, start_ms: int = -1,
              target: str = "", title: str = "") -> dict:
    """Play longform audio (audiobook / podcast) on the book channel.

    Use this instead of `music_play` for spoken-word you want to come back
    to: the book channel remembers where you were, and speech pauses it
    instead of talking over it. Accepts the same URIs as `music_play`
    (`yt:https://...`, http(s) streams, file paths) — a leading `yt:` is
    stripped for the underlying mpv player.

    Args:
        uri: What to play.
        resume: If True (default) and no explicit start_ms, resume from this
            URI's saved bookmark.
        start_ms: Explicit start offset (ms). -1 (default) means use the
            bookmark when `resume`, else start from the beginning.
    """
    b, st, t = _book(), _state(), _book_target(target)
    norm = normalize_uri(uri)
    # Download-first: a YouTube URL is unplayable directly on a datacenter IP
    # (datacenter hosts get 403'd). Resolve it to a cached local file, or start a
    # phone-side fetch (audiobook-fetch) that auto-plays when it finishes.
    #
    # EXCEPT on a residential host (e.g. the phone): no 403, so the book mpv can
    # play the YouTube URL directly via yt-dlp — no fetch/cache indirection.
    # Gated by MEDIA_BOOK_DIRECT_YT=1 so datacenter hosts keep the safe path.
    playlist = library.expand_youtube_playlist(norm)
    if playlist:
        cached_by_url = {
            u: library.cached_path(library.video_id(u) or "", t)
            for u in playlist
        }
        uncached = [u for u, p in cached_by_url.items() if p is None]
        started = library.start_fetch_many(uncached, play=True, target=t) if uncached else False
        first_cached = next((p for p in cached_by_url.values() if p is not None), None)
        if first_cached is not None and not started:
            norm = str(first_cached)
        else:
            return {"ok": False, "fetching": started, "uri": norm,
                    "count": len(playlist), "uncached": len(uncached),
                    "reason": (f"downloading {len(uncached)} playlist item(s) on phone; "
                               "will auto-play the last fetched item when ready"
                               if started else "playlist not cached and audiobook-fetch unavailable")}
    elif library.is_youtube(norm):
        vid = library.video_id(norm)
        cached = library.cached_path(vid, t) if vid else None
        if cached is not None:
            norm = str(cached)
        elif os.environ.get("MEDIA_BOOK_DIRECT_YT", "0") not in ("", "0", "false", "no"):
            pass  # residential: fall through and let the book mpv stream it
        else:
            started = library.start_fetch(norm, play=True, target=t)
            return {"ok": False, "fetching": started, "uri": norm,
                    "reason": ("downloading on phone; will auto-play when ready"
                               if started
                               else "not cached and audiobook-fetch unavailable")}
    if t.name not in ("", "local") and not norm.startswith(("http://", "https://", "rtsp://")):
        p = Path(norm).expanduser()
        # The phone's own player first (Sasonica's ExoPlayer, see phone_player):
        # if the file is an item in the app's Audiobookshelf and the app takes
        # it, nothing is staged and mpv is never touched. None = fall through.
        if p.is_file():
            if start_ms is not None and start_ms >= 0:
                start = start_ms
            elif resume:
                start = st.get_resume_pos(norm) or 0
            else:
                start = 0
            taken = phone_player.play(p, t, start_ms=start)
            if taken:
                _save_book_bookmark(b, st, t)
                display_title = title.strip() or taken.get("title") or p.stem
                st.set_now_playing(sink="book", uri=norm, started_at=time.time(),
                                   content_type="audiobook", target=t.name,
                                   extras={"title": display_title, "player": "sasonica",
                                           "item_id": taken.get("item_id")})
                st.set_book_last(norm, display_title)
                st.clear_playlist_active()
                return {"ok": True, "uri": norm, "resumed_from_ms": start, "target": t.name,
                        "player": "sasonica", "state": taken}
        if p.is_file():
            try:
                from .sinks.book import remote_cached_path, start_stage_local_for_remote
                if remote_cached_path(p, t):
                    pass
                elif start_stage_local_for_remote(
                    p, t, start_ms=(start_ms if start_ms is not None else -1), title=title
                ):
                    return {"ok": False, "fetching": True, "uri": norm,
                            "reason": "copying to phone cache; will auto-play when ready"}
            except Exception:
                pass
    # Save the outgoing book's place before switching away from it.
    _save_book_bookmark(b, st, t)
    if start_ms is not None and start_ms >= 0:
        start = start_ms
    elif resume:
        start = st.get_resume_pos(norm) or 0
    else:
        start = 0
    b.play(norm, t, start_ms=start)
    display_title = title.strip()
    if not display_title:
        try:
            from urllib.parse import unquote
            display_title = Path(unquote(norm.split("?", 1)[0])).stem
        except Exception:
            pass
    st.set_now_playing(sink="book", uri=norm, started_at=time.time(),
                       content_type="audiobook", target=t.name,
                       extras={"title": display_title} if display_title else None)
    st.set_book_last(norm, display_title or None)
    # `display_title` may only be the filename stem — that is what the block
    # above falls back to. The real name lives in the file and mpv publishes it
    # as `media-title` a moment after the load, so go back and fetch it.
    _note_media_title(b, st, t, norm)
    # An ad-hoc book breaks the playlist context, so `book next` won't try to
    # advance a list the listener has stepped away from.
    st.clear_playlist_active()
    return {"ok": True, "uri": norm, "resumed_from_ms": start, "target": t.name}


@mcp.tool()
def book_resume(target: str = "") -> dict:
    """Resume the book channel. If nothing is loaded, reopen the last book
    played, at its saved bookmark."""
    b, st, t = _book(), _state(), _book_target(target)
    if phone_player.has_item(t):
        return {"ok": phone_player.request(t, "/resume", timeout=8.0) is not None, "player": "sasonica"}
    if b.idle(t):
        last = st.get_book_last()
        if not last:
            return {"ok": False, "reason": "no book to resume"}
        start = st.get_resume_pos(last) or 0
        b.play(last, t, start_ms=start)
        st.set_now_playing(sink="book", uri=last, started_at=time.time(),
                           content_type="audiobook", target=t.name)
        return {"ok": True, "uri": last, "resumed_from_ms": start}
    b.resume(t)
    return {"ok": True}


@mcp.tool()
def book_pause(target: str = "") -> dict:
    """Pause the book channel and save its place."""
    b, t = _book(), _book_target(target)
    if phone_player.has_item(t):
        return {"ok": phone_player.request(t, "/pause", timeout=8.0) is not None, "player": "sasonica"}
    _save_book_bookmark(b, _state(), t)
    b.pause(t)
    return {"ok": True}


@mcp.tool()
def book_stop(target: str = "") -> dict:
    """Stop the book channel, saving its place first so you can resume later."""
    b, st, t = _book(), _state(), _book_target(target)
    if phone_player.has_item(t):
        ok = phone_player.request(t, "/stop", timeout=8.0) is not None
        st.clear_now_playing("book")
        st.clear_playlist_active()
        return {"ok": ok, "player": "sasonica"}
    _save_book_bookmark(b, st, t)
    b.stop(t)
    st.clear_now_playing("book")
    st.clear_playlist_active()
    return {"ok": True}


@mcp.tool()
def book_skip(seconds: float = 30, target: str = "") -> dict:
    """Skip the book by ±seconds (negative = back). Default +30s."""
    t = _book_target(target)
    if phone_player.has_item(t):
        return {"ok": phone_player.request(t, "/jump", {"by": seconds}, timeout=8.0) is not None,
                "seconds": seconds, "player": "sasonica"}
    _book().skip(seconds, t)
    return {"ok": True, "seconds": seconds}


@mcp.tool()
def book_seek(position_secs: float, target: str = "") -> dict:
    """Seek the book to an absolute position (seconds from the start).

    Unlike `book_skip` (which moves ±relative), this jumps to a specific
    time — e.g. `position_secs=5615` for 1:33:35. Clamped to the file length.
    """
    t = _book_target(target)
    if phone_player.has_item(t):
        s = phone_player.request(t, "/seek", {"t": position_secs}, timeout=8.0)
        return {"ok": s is not None, "position_ms": int((s or {}).get("t", position_secs) * 1000),
                "player": "sasonica"}
    pos = _book().seek_to(position_secs, t)
    return {"ok": True, "position_ms": pos}


@mcp.tool()
def book_speed(rate: float, target: str = "") -> dict:
    """Set book playback speed (0.25–4.0; 1.0 = normal)."""
    t = _book_target(target)
    if phone_player.has_item(t):
        s = phone_player.request(t, "/speed", {"rate": rate}, timeout=8.0)
        return {"ok": s is not None, "speed": (s or {}).get("rate", rate), "player": "sasonica"}
    applied = _book().set_speed(rate, t)
    return {"ok": True, "speed": applied}


@mcp.tool()
def book_now_playing(target: str = "") -> dict:
    """What the book channel is playing — URI, position, duration, speed."""
    t = _book_target(target)
    try:
        from .sinks import _mpv_ipc as ipc
        from .sinks.book import _socket_for
        props = ipc.display_properties(_socket_for(t), [
            "idle-active", "path", "time-pos", "duration", "pause", "speed",
            "media-title", "chapter-metadata/by-key/title",
            "metadata/by-key/album", "metadata/by-key/artist",
        ], timeout=1.2)
    except Exception:
        return {"idle": True}
    if props.get("idle-active") is True:
        return {"idle": True}
    uri = props.get("path")
    info = {
        "idle": False,
        "uri": uri,
        "position_ms": (int(float(props["time-pos"]) * 1000)
                        if props.get("time-pos") is not None else None),
        "duration_ms": (int(float(props["duration"]) * 1000)
                        if props.get("duration") is not None else None),
        "paused": bool(props.get("pause")),
        "speed": float(props.get("speed") or 1.0),
    }
    for prop, key in (("media-title", "media_title"),
                      ("chapter-metadata/by-key/title", "chapter_title"),
                      ("metadata/by-key/album", "album"),
                      ("metadata/by-key/artist", "artist")):
        if props.get(prop):
            info[key] = props[prop]
    # Prefer the title remembered when agent-media started this exact playback.
    # Never apply pending/copying state to a different live mpv URI, or the popup
    # can claim a selected future book while the previous book is still loaded.
    try:
        np = _state().get_now_playing("book") or {}
        state_uri = str(np.get("uri") or "")
        live_uri = str(uri or "")
        live_name = Path(live_uri.split("?", 1)[0]).name
        state_name = Path(state_uri.split("?", 1)[0]).name
        same_playback = bool(state_uri) and (
            live_uri.startswith(state_uri)
            or state_uri.startswith(live_uri)
            or (live_name and state_name and live_name == state_name)
        )
        if same_playback:
            ex = np.get("extras") or {}
            if isinstance(ex, dict):
                info.update({k: v for k, v in ex.items()
                             if k in ("title", "chapter_title", "source") and v})
    except Exception:
        pass
    return info


# --- book playlists -------------------------------------------------------
#
# A book playlist is an ordered list of part URIs (chapters / episodes) with
# a remembered cursor. Within-part offset resume reuses the per-URI book
# bookmarks; the playlist only tracks which part. `book_playlist_play` opens
# the part at the cursor; `book_next`/`book_prev` step the cursor. The active
# playlist is remembered so `book_next` knows what to advance.

def _play_playlist_part(name: str, index: int, target: Target,
                        resume_part: bool = True) -> dict:
    """Open the playlist `name` at `index` on the book channel.

    Saves the outgoing book's bookmark first, points the playlist cursor at
    `index`, plays that part (resuming within it from its own bookmark when
    `resume_part`), and marks the playlist active. Shared by play/next/prev.
    """
    b, st = _book(), _state()
    item = st.get_playlist_item(name, index)
    if item is None:
        return {"ok": False, "reason": "index out of range", "index": index}
    _save_book_bookmark(b, st, target)
    uri = normalize_uri(item["uri"])
    start = (st.get_resume_pos(uri) or 0) if resume_part else 0
    b.play(uri, target, start_ms=start)
    st.set_playlist_index(name, index)
    st.set_playlist_active(name)
    st.set_now_playing(sink="book", uri=uri, started_at=time.time(),
                       content_type="audiobook", target=target.name)
    st.set_book_last(uri)
    _ensure_autoadvance_watcher()
    return {"ok": True, "playlist": name, "index": index, "uri": uri,
            "title": item["title"], "resumed_from_ms": start}


# --- book event watcher: playlist auto-advance + EOF self-heal ------------
#
# The book broker is a single long-lived mpv. One daemon thread (started the
# first time a playlist plays, and at service boot) watches its async event
# stream and reacts to two kinds of `end-file`:
#
#   reason=eof   → a part ended naturally: advance the active playlist. A
#                  user stop/skip/replace ends with reason `stop`, so manual
#                  control never auto-advances.
#   reason=error → playback broke (the resolved YouTube media URL carries a
#                  ~6h `expire=`; pausing across it, or a network drop, ends
#                  the file and leaves mpv idle with the entry still queued).
#                  Reload the last book at the live position so an expired-URL
#                  stall self-heals without a keypress. A consecutive-failure
#                  cap keeps a genuinely dead stream from hot-looping.
#
# Both live in the long-running MCP server process, which is where playlist
# playback is driven — so no separate watcher process or service is needed.

_autoadvance_thread: "threading.Thread | None" = None
_autoadvance_lock = threading.Lock()

# Self-heal tuning: stop rehealing after this many consecutive error end-files
# with no intervening settled playback (the stream is dead, not just expired);
# a clean stretch of playback resets the streak.
_HEAL_MAX_CONSECUTIVE = 3
_HEAL_RECOVERED_AFTER_S = 5.0


def _advance_after_eof() -> None:
    """Advance the active playlist one part. No-op if none is active.

    Called from the watcher thread when a part ends naturally. Walks off the
    end by clearing the active pointer (the playlist is finished) rather than
    looping.
    """
    st = _state()
    name = st.get_playlist_active()
    if not name:
        return
    pl = st.get_playlist(name)
    if pl is None:
        st.clear_playlist_active()
        return
    nxt = pl["cur_index"] + 1
    np = st.get_now_playing("book")
    t = _book_target((np or {}).get("target") or "")
    if nxt >= len(pl["items"]):
        st.clear_playlist_active()
        st.clear_now_playing("book")
        log.info("book playlist %r finished", name)
        return
    _play_playlist_part(name, nxt, t)


def _reheal_after_error(last_pos_ms: "int | None",
                        last_pos_uri: "str | None" = None) -> bool:
    """Reload the last book at the best-known position after an error
    end-file. Prefers the live-tracked position over the saved bookmark so a
    self-heal never restarts from zero; reloads onto the same target the book
    was last playing to. Returns True if a load was issued.

    The live position is only usable when it was read from *this* book:
    `last_pos_uri` is the file it was read from, and a mismatch means the
    watcher's last heartbeat happened before the switch. On 2026-08-17 a
    shared 61-minute set was loaded, errored, and was rehealed at 1h19m —
    where the podcast playing before it had got to — which lands past the end
    of the file and errors again immediately. So a position from another file
    is no position at all; fall back to this one's bookmark.
    """
    from .sinks.book import normalize_uri

    st, b = _state(), _book()
    uri = st.get_book_last()
    if not uri:
        return False
    norm = normalize_uri(uri)
    live = last_pos_ms if last_pos_uri is None or last_pos_uri == norm else None
    pos = live if live and live > 0 else st.get_resume_pos(norm)
    np = st.get_now_playing("book")
    t = _book_target((np or {}).get("target") or "")
    log.warning("book self-heal: reloading %s at %sms on %s", norm, pos, t.name)
    b.play(norm, t, start_ms=(pos or 0))
    return True


def _autoadvance_loop() -> None:
    from .sinks import _mpv_ipc as ipc

    from .sinks.book import normalize_uri

    sock = _book()._sock
    last_pos_ms: "int | None" = None
    last_pos_uri: "str | None" = None
    failures = 0
    last_load_at = time.monotonic()
    while True:
        try:
            for msg in ipc.event_stream(sock):
                if msg is None:
                    # Heartbeat: remember the live position so an error
                    # end-file can reload where we were, and clear the failure
                    # streak once playback has settled back in. The path is
                    # read in the same breath as the position, because a
                    # position without the file it came from is what rehealed
                    # a fresh book at the previous one's offset.
                    try:
                        pos = ipc.get_property(sock, "time-pos")
                        if pos is not None:
                            last_pos_ms = int(pos * 1000)
                            path = ipc.get_property(sock, "path")
                            last_pos_uri = normalize_uri(path) if path else None
                            if (time.monotonic() - last_load_at) > _HEAL_RECOVERED_AFTER_S:
                                failures = 0
                    except (OSError, ipc.MpvIpcError):
                        pass
                    continue
                ev = msg.get("event")
                if ev == "start-file":
                    last_load_at = time.monotonic()
                    continue
                if ev != "end-file":
                    continue
                reason = msg.get("reason")
                if reason == "eof":
                    try:
                        _advance_after_eof()
                    except Exception:  # noqa: BLE001 — never kill the watcher
                        log.exception("book auto-advance failed")
                    failures = 0
                    continue
                if reason != "error":
                    continue  # stop/quit/redirect — never reheal
                # Error end-file: self-heal unless the stream looks truly dead.
                failures += 1
                if failures > _HEAL_MAX_CONSECUTIVE:
                    log.warning("book self-heal: giving up after %d consecutive "
                                "errors (stream looks dead)", failures - 1)
                    continue
                time.sleep(min(2.0 * failures, 8.0))  # back off a flapping net
                try:
                    if _reheal_after_error(last_pos_ms, last_pos_uri):
                        last_load_at = time.monotonic()
                except Exception:  # noqa: BLE001 — never kill the watcher
                    log.exception("book self-heal: reload failed")
        except (OSError, ipc.MpvIpcError):
            pass
        # Broker gone or never came up; back off then retry.
        time.sleep(2.0)


def _ensure_autoadvance_watcher() -> None:
    """Start the auto-advance watcher once; idempotent and thread-safe."""
    global _autoadvance_thread
    with _autoadvance_lock:
        if _autoadvance_thread is not None and _autoadvance_thread.is_alive():
            return
        _autoadvance_thread = threading.Thread(
            target=_autoadvance_loop, name="book-autoadvance", daemon=True)
        _autoadvance_thread.start()


@mcp.tool()
def book_playlist_new(name: str) -> dict:
    """Create an empty book playlist. Add parts with `book_playlist_add`."""
    created = _state().create_playlist(name, channel="book")
    return {"ok": True, "playlist": name, "created": created}


@mcp.tool()
def book_playlist_add(name: str, uris: list[str]) -> dict:
    """Append one or more part URIs (in order) to a book playlist.

    Accepts the same URIs as `book_play` (`yt:https://...`, http(s) streams,
    file paths). Creates the playlist if it doesn't exist yet.
    """
    st = _state()
    st.create_playlist(name, channel="book")  # no-op if it exists
    count = st.add_playlist_items(name, list(uris))
    return {"ok": True, "playlist": name, "count": count, "added": len(uris)}


@mcp.tool()
def book_playlist_play(name: str, resume: bool = True, target: str = "") -> dict:
    """Play a saved book playlist, resuming at its remembered part + offset.

    For spoken requests like "play my Dune audiobook" / "put on the <name>
    playlist" / "continue my book" where <name> is a saved playlist.

    Args:
        name: The playlist to play.
        resume: If True (default), start at the playlist's saved cursor and
            within that part at its saved bookmark. If False, start over from
            the first part.
    """
    st, t = _state(), _book_target(target)
    pl = st.get_playlist(name)
    if pl is None:
        return {"ok": False, "reason": f"no playlist {name!r}"}
    if not pl["items"]:
        return {"ok": False, "reason": f"playlist {name!r} is empty"}
    index = pl["cur_index"] if resume else 0
    if index >= len(pl["items"]):
        index = 0
    return _play_playlist_part(name, index, t, resume_part=resume)


@mcp.tool()
def book_next(target: str = "") -> dict:
    """Next part of the active book playlist — "next chapter", "skip ahead",
    "play the next part", "next episode"."""
    st, t = _state(), _book_target(target)
    name = st.get_playlist_active()
    if not name:
        return {"ok": False, "reason": "no active playlist"}
    pl = st.get_playlist(name)
    if pl is None:
        return {"ok": False, "reason": f"playlist {name!r} gone"}
    nxt = pl["cur_index"] + 1
    if nxt >= len(pl["items"]):
        return {"ok": False, "reason": "end of playlist", "playlist": name}
    return _play_playlist_part(name, nxt, t)


@mcp.tool()
def book_prev(target: str = "") -> dict:
    """Previous part of the active book playlist — "previous chapter",
    "go back a part", "last episode"."""
    st, t = _state(), _book_target(target)
    name = st.get_playlist_active()
    if not name:
        return {"ok": False, "reason": "no active playlist"}
    pl = st.get_playlist(name)
    if pl is None:
        return {"ok": False, "reason": f"playlist {name!r} gone"}
    prv = pl["cur_index"] - 1
    if prv < 0:
        return {"ok": False, "reason": "at start of playlist", "playlist": name}
    return _play_playlist_part(name, prv, t)


@mcp.tool()
def book_playlist_ls(name: str = "") -> dict:
    """List book playlists, or the parts of one if `name` is given."""
    st = _state()
    if not name:
        return {"playlists": st.list_playlists(channel="book")}
    pl = st.get_playlist(name)
    if pl is None:
        return {"ok": False, "reason": f"no playlist {name!r}"}
    return pl


@mcp.tool()
def book_playlist_rm(name: str) -> dict:
    """Delete a book playlist (its parts' bookmarks are kept)."""
    removed = _state().delete_playlist(name)
    return {"ok": removed, "playlist": name,
            **({} if removed else {"reason": "no such playlist"})}


# --- channel concurrency: focus + bed -------------------------------------
#
# The book and music channels can play at once (book in front, music as a
# quiet bed). `focus` chooses which is in front; `book_bed` chooses whether
# the music bed ducks (instrumental) or pauses (lyrics) under the book.

def _abs_config() -> tuple[str, str]:
    """Audiobookshelf URL/token, accepting the existing abs-bridge env names."""
    url = os.environ.get("MEDIA_AUDIOBOOKSHELF_URL") or os.environ.get("ABS_URL") or ""
    token = os.environ.get("MEDIA_AUDIOBOOKSHELF_TOKEN") or os.environ.get("ABS_TOKEN") or ""
    if url and token:
        return url, token
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
    except OSError:
        pass
    return url, token


def search(channel: str, query: str = "") -> dict:
    """Search for media across ABS, red5 cache, and the connected device."""
    import shlex
    import subprocess
    import urllib.parse
    import urllib.request
    import json
    if channel == "book":
        url, token = _abs_config()
        if not url or not token:
            return {"error": "Audiobookshelf URL/token not set (MEDIA_AUDIOBOOKSHELF_* or ABS_* in abs-bridge.env)"}
        url = url.rstrip("/")
        req = urllib.request.Request(f"{url}/api/libraries", headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                libraries = json.loads(r.read())["libraries"]
        except Exception as e:
            return {"error": f"failed to fetch libraries: {e}"}

        q = query.casefold().strip()
        results = []
        for lib in libraries:
            # Fetch the library and let fzf/client-side filtering do the search.
            # ABS's /search endpoint is version-sensitive and returns 400 here.
            req_url = f"{url}/api/libraries/{lib['id']}/items?limit=1000"
            req = urllib.request.Request(req_url, headers={"Authorization": f"Bearer {token}"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    res = json.loads(r.read())
            except Exception:
                continue
            for item in res.get("results", []):
                metadata = item.get("media", {}).get("metadata", {})
                title = metadata.get("title") or item.get("title")
                author = metadata.get("authorName") or ""
                searchable = " ".join((str(title or ""), str(author), str(item.get("relPath") or ""))).casefold()
                if not title or (q and q not in searchable):
                    continue
                # /play is a session endpoint and 404s for API tokens; /download streams bytes.
                uri = f"{url}/api/items/{item['id']}/download"
                label = f"{title} — {author}" if author else title
                results.append({"uri": uri, "title": f"{label}  [ABS]"})

        # Merge red5 cache/library files that ABS may not know about.
        # `search` has no target param; use the default book target so the
        # per-target dir resolvers behave exactly as the untargeted default.
        t = _book_target("")
        exts = {".m4a", ".m4b", ".mp3", ".opus", ".ogg", ".flac", ".wav", ".webm", ".mka"}
        cache_root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "agent-media" / "books"
        default_local_dirs = f"{cache_root} {library.abs_import_dir(t)} {library.library_dir(t)}"
        local_dirs = os.environ.get("MEDIA_BOOK_LOCAL_DIRS", default_local_dirs)
        seen_paths: set[str] = set()
        for token in shlex.split(local_dirs):
            root = Path(os.path.expandvars(token)).expanduser()
            if not root.is_dir():
                continue
            label = "red5-cache" if root == cache_root else "red5-library"
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.name.startswith(".") or path.suffix.lower() not in exts:
                    continue
                key = str(path.resolve())
                if key in seen_paths:
                    continue
                seen_paths.add(key)
                title = path.stem
                searchable = " ".join((title, str(path.relative_to(root)))).casefold()
                if q and q not in searchable:
                    continue
                results.append({"uri": str(path), "title": f"{title}  [{label}]"})

        # Merge connected-device cache files. These are playable directly when
        # the book target follows speech to `phone`.
        target_name = (os.environ.get("MEDIA_BOOK_DEFAULT_TARGET")
                       or os.environ.get("MEDIA_SPEECH_DEFAULT_TARGET") or "")
        if target_name:
            host = (os.environ.get(f"MEDIA_BOOK_CACHE_SSH_{target_name.upper().replace('-', '_')}")
                    or os.environ.get(f"MEDIA_SPEECH_CLIP_SSH_{target_name.upper().replace('-', '_')}")
                    or os.environ.get("MEDIA_MUSIC_LOCAL_SSH") or "")
            remote_dirs = os.environ.get(
                f"MEDIA_BOOK_CACHE_DIRS_{target_name.upper().replace('-', '_')}",
                "${XDG_CACHE_HOME:-$HOME/.cache}/agent-media/books",
            )
            if host:
                find_expr = " -o ".join(f"-iname '*{ext}'" for ext in sorted(exts))
                remote = (
                    "for d in " + remote_dirs + "; do "
                    "[ -d \"$d\" ] && find \"$d\" -type f \\( " + find_expr + " \\); "
                    "done 2>/dev/null"
                )
                try:
                    proc = subprocess.run(
                        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", host, remote],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8,
                        check=False,
                    )
                except Exception:
                    proc = None
                if proc and proc.returncode == 0:
                    for line in proc.stdout.splitlines():
                        path_s = line.strip()
                        if not path_s:
                            continue
                        title = Path(path_s).stem
                        if q and q not in f"{title} {path_s}".casefold():
                            continue
                        results.append({"uri": path_s, "title": f"{title}  [{target_name}-cache]"})
        return {"results": results}
    return {"error": "not implemented"}

@mcp.tool()
def focus(channel: str, target: str = "") -> dict:
    """Bring a channel to the front; push the other into its bed.

    Args:
        channel: "book" → music drops to a quiet bed (or pauses, per the
            current `book_bed` mode) and the book plays at full. "music" →
            the book pauses (its place is saved) and music returns to full.
    """
    ch = channel.strip().lower()
    if ch not in (FOCUS_BOOK, FOCUS_MUSIC):
        return {"ok": False, "reason": f"unknown channel {channel!r}"}
    b, m, st = _book(), _music(), _state()
    mt = _target(target or "local")
    bt = _book_target(target)
    # Save the book's place before focus pauses it.
    if ch == FOCUS_MUSIC:
        _save_book_bookmark(b, st, bt)
    result = apply_focus(ch, music=m, book=b, state=st,
                         music_target=mt, book_target=bt)
    return {"ok": True, **result}


@mcp.tool()
def book_bed(mode: str, target: str = "") -> dict:
    """Set how the music bed behaves under a foregrounded book.

    Args:
        mode: "duck" — keep music playing quietly under the narration
            (good for instrumental); "pause" — pause music entirely while
            the book is in front (good for lyrics). Applies immediately if
            a book is currently in front.
    """
    md = mode.strip().lower()
    if md not in (BED_DUCK, BED_PAUSE):
        return {"ok": False, "reason": f"mode must be 'duck' or 'pause', got {mode!r}"}
    st = _state()
    st.set_book_bed(md)
    # If the book is already in front, re-apply so the change takes effect now.
    reapplied = False
    if st.get_focus() == FOCUS_BOOK:
        apply_focus(FOCUS_BOOK, music=_music(), book=_book(), state=st,
                    music_target=_target(target or "local"),
                    book_target=_book_target(target))
        reapplied = True
    return {"ok": True, "bed": md, "applied_now": reapplied}


@mcp.tool()
def channels_status() -> dict:
    """Both channels at a glance — what's playing, plus focus and bed mode."""
    b, m = _book(), _music()
    t = _target("local")
    pol = resolve(_state())
    book_state: dict = {"idle": b.idle(t)}
    if not book_state["idle"]:
        book_state.update(uri=b.now_playing_uri(t), position_ms=b.position(t),
                          paused=b.paused(t), speed=b.speed(t))
    return {
        "focus": pol.focus,
        "bed": pol.bed,
        "music": {"uri": m.now_playing_uri(t), "position_ms": m.position(t)},
        "book": book_state,
    }


# --- entrypoint -----------------------------------------------------------

def _configure_logging() -> None:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    """stdio entrypoint — for Claude Code and other local MCP clients."""
    load_env_file("media-mcp")
    _configure_logging()
    # Watch for playlist part-ends host-wide, so a playlist started from the
    # CLI (a short-lived process that can't host the watcher) still advances.
    _ensure_autoadvance_watcher()
    mcp.run()


# The full server advertises 37 tools. Every one of them costs context in every
# session of every MCP client that connects, whether or not media gets touched
# that day — so exposing the lot to a coding agent is a poor trade when the CLI
# already covers the same ground for one Bash call and no standing cost.
#
# This subset is chosen by *who initiates*: things an agent decides to do
# mid-task, where it also wants to see the result. Speech is deliberately absent
# — `say` and the visual markers are driven by the Stop hook, which is strictly
# better than a tool call (it happens automatically at end of turn and cannot be
# forgotten). Everything omitted here remains available via the `media` CLI and
# via the full media-mcp entrypoint.
NARROW_TOOLS = (
    "converse",            # genuinely interactive; awkward to drive from a CLI
    "errors",              # lets an agent see what just went wrong
    "music_play",
    "music_pause",
    "music_resume",
    "music_now_playing",
)


def _narrow_tool_names() -> list[str]:
    """Allowlist for the narrow entrypoint; MEDIA_MCP_TOOLS overrides."""
    raw = os.environ.get("MEDIA_MCP_TOOLS", "").strip()
    names = [t.strip() for t in raw.split(",") if t.strip()]
    return names or list(NARROW_TOOLS)


def main_narrow() -> None:
    """stdio entrypoint exposing only the agent-initiated subset of tools.

    Same server object as main(); the tools are registered by decorator at
    import and the unwanted ones are dropped before serving, so there is no
    second registry to keep in sync as tools are added.
    """
    load_env_file("media-mcp")
    _configure_logging()
    keep = set(_narrow_tool_names())
    known = {t.name for t in mcp._tool_manager.list_tools()}
    missing = keep - known
    if missing:
        # A typo in MEDIA_MCP_TOOLS would otherwise silently serve fewer tools.
        log.warning("media-mcp narrow: unknown tool(s) ignored: %s",
                    ", ".join(sorted(missing)))
    for name in sorted(known - keep):
        mcp.remove_tool(name)
    _ensure_autoadvance_watcher()
    mcp.run()


def main_http() -> None:
    """streamable-HTTP entrypoint — for remote callers over Tailscale."""
    load_env_file("media-mcp-http")
    _configure_logging()
    _ensure_autoadvance_watcher()
    log.info("media-mcp http listening on %s:%d", _host(), _port())
    mcp.run(transport="streamable-http", host=_host(), port=_port())


if __name__ == "__main__":
    main()
