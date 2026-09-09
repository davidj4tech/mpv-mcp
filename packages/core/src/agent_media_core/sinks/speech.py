"""sink-speech: plays TTS clips through a long-running mpv broker.

The broker is started by the `sink-speech` runit service (or systemd
equivalent) with `--idle=yes --ao=openal --input-ipc-server=<socket>`.
This class talks to the socket; it does not spawn mpv itself.
"""

from __future__ import annotations

import logging
import os
import socket as _socket
import time
from pathlib import Path
from typing import Optional

from ..types import Target
from . import _mpv_ipc as ipc


log = logging.getLogger(__name__)

DEFAULT_TARGET = Target(name="local")

# Cross-host owner claim for a *shared remote* broker (the phone's mpv, driven
# by every host over the tcp:// bridge). The local playback flock only
# serializes one host; this token — stored in mpv `user-data` on the broker
# itself, so all hosts see the same value — stops a second machine's reply from
# stop+clearing another's still-playing playlist. Requires mpv >= 0.36.
_BROKER_OWNER_KEY = "user-data/am-owner"
# How long a claim stays valid without a refresh. Long enough to ride out a
# brief bridge hiccup, short enough that a crashed holder frees the broker soon.
BROKER_TTL_S = 20.0


def _broker_default_volume() -> float:
    """The broker's configured resting volume — the same MEDIA_SPEECH_VOLUME the
    `sink-speech` run script launches mpv with (default 130, louder than mpv's
    nominal 100). unduck restores to this so a duck cycle can't quietly pull
    speech below its intended level. Keep the two defaults in step: a mismatch
    means the first duck/unduck cycle silently re-levels the broker."""
    try:
        return float(os.environ.get("MEDIA_SPEECH_VOLUME", "130"))
    except (TypeError, ValueError):
        return 130.0


def _broker_max_volume() -> float:
    """The broker's --volume-max ceiling (MEDIA_SPEECH_VOLUME_MAX, default 200).
    Duck levels clamp to this rather than a bare 100 so they stay valid across
    the broker's amplified range."""
    try:
        return float(os.environ.get("MEDIA_SPEECH_VOLUME_MAX", "200"))
    except (TypeError, ValueError):
        return 200.0


def _broker_owner_id() -> str:
    """Stable id for this claimer. Same-host/same-pid callers are already
    serialized by the local flock, so host:pid is enough to tell hosts apart."""
    return f"{_socket.gethostname()}:{os.getpid()}"


def _socket_for(target: Target) -> "str | Path":
    """Resolve the IPC endpoint for a target (decision 1C).

    All targets share the single local mpv broker socket at
    `$XDG_STATE_HOME/agent-media/sink-speech.sock`; the *output device*
    is what differs per target (see `_device_for`). A per-target endpoint
    can be set with `MEDIA_SPEECH_SOCKET_<TARGET>` — either a Unix socket
    path, or a `tcp://host:port` to drive a *remote* mpv over a bridge
    (Grade B: red5 drives the phone's sink-speech). A tcp:// override is
    returned as a raw string — `Path()` would collapse `tcp://` to `tcp:/`.
    """
    override = os.environ.get(_env_key("MEDIA_SPEECH_SOCKET", target.name))
    if override:
        return override if override.startswith("tcp://") else Path(override)
    state = Path(os.environ.get("XDG_STATE_HOME",
                                str(Path.home() / ".local" / "state")))
    return state / "agent-media" / "sink-speech.sock"


def _env_key(prefix: str, target_name: str) -> str:
    return f"{prefix}_{target_name.upper().replace('-', '_')}"


#: mpv `user-data` key the on-device companion app watches to know that the
#: audio focus about to be taken is ours.
SPEAKING_PROPERTY = "user-data/agent-media/speaking"


def set_speaking(on: bool, target: Target = DEFAULT_TARGET) -> bool:
    """Tell anything watching this broker that a response is in flight.

    The companion app on the phone holds audio focus on the music mpv's behalf
    and ducks it when focus is lost. It must not duck for *our own* speech —
    the coordinator already ducks that same mpv, and two duckers on one volume
    lose the restore between them (2026-08-14, music left at 10 for two hours).

    The app cannot work out whose loss it is by watching playback, and that is
    not a tuning problem: mpv takes the output when it *opens* a clip, and with
    a response rendered and relayed ahead of time the loss arrived 37 s before
    the first clip was staged (p8a, 20:26:52 vs 20:27:29). Any window narrow
    enough to be useful is too narrow to catch that, and any window wide enough
    stops ducking real interruptions.

    So we say so, in band, over the socket the app already watches. mpv's
    `user-data` is observable and arbitrary, needs no script and no new channel,
    and an mpv too old to have it (< 0.36) simply reports an error we ignore —
    the app keeps its own weaker heuristics as the fallback.

    Best-effort by construction: this is diagnostics for someone else's duck,
    never a reason to delay or drop a clip. Returns True when the write landed.
    """
    try:
        ipc.set_property(_socket_for(target), SPEAKING_PROPERTY, bool(on))
        return True
    except (ipc.MpvIpcError, OSError) as e:
        log.debug("sink-speech: speaking flag %s failed: %s", on, e)
        return False


#: mpv `user-data` key carrying how much interruption this reply is worth.
#: The companion app reads it to choose a tier: wait quietly, ask David with a
#: card, or take the room. See docs/proposals/2026-08-16-two-assistants-one-room.md.
PRIORITY_PROPERTY = "user-data/agent-media/priority"


def set_priority(priority: str, target: Target = DEFAULT_TARGET) -> bool:
    """Say what this reply is worth interrupting for.

    The same in-band channel as `set_speaking`, for the same reason: the phone
    has to decide what to do about a reply *before* it is audible, and the only
    thing it can see is the broker. A `Priority` value ("low"/"normal"/"high"/
    "urgent"), passed through as the string it already is.

    Who sets it is the interesting half, and it is not decided here. A
    mechanical source — an alarm, a timer, mail arriving — knows its own
    urgency and cannot flatter itself; an assistant marking its own words
    urgent is judging a case it has an interest in. Both can, and the explicit
    one wins; this function only carries the answer.

    Best-effort, like every other flag on this socket: never a reason to delay
    or drop a clip.
    """
    value = (priority or "").strip() or "normal"
    try:
        ipc.set_property(_socket_for(target), PRIORITY_PROPERTY, value)
        return True
    except (ipc.MpvIpcError, OSError) as e:
        log.debug("sink-speech: priority flag %s failed: %s", value, e)
        return False


#: mpv `user-data` key carrying whether the *device* wants to be quiet.
#:
#: Written by the phone-side `ringer-state` service (see
#: :mod:`agent_media_core.ringer`), read here by whichever host is about to
#: speak. It travels on the broker for the same reason the owner claim does:
#: it is a fact every host must agree on, and the broker is the one thing they
#: all already talk to. The alternative — the origin asking the phone over ssh
#: per alert — puts a network round-trip and a second failure mode into the say
#: path for a question the phone could simply have left lying where everyone
#: passes.
RINGER_PROPERTY = "user-data/agent-media/ringer"

#: How old a published verdict may be before it stops counting.
#:
#: The publisher polls far faster than this; anything approaching it means the
#: service, the app, or the phone is gone. A verdict from a dead publisher is
#: not evidence about a live phone, and the direction of the error matters:
#: believing a stale "quiet" swallows alerts silently for as long as it takes
#: someone to notice, while believing a stale "audible" costs one spoken alert.
RINGER_MAX_AGE_S = 300.0


def set_ringer(snapshot: dict, target: Target = DEFAULT_TARGET) -> bool:
    """Publish the device's quiet/audible verdict on this broker.

    Called on the phone against its own local socket. Best-effort like every
    other flag here — a broker that is down leaves the property absent, and
    absent means speak.
    """
    try:
        ipc.set_property(_socket_for(target), RINGER_PROPERTY, dict(snapshot))
        return True
    except (ipc.MpvIpcError, OSError) as e:
        log.debug("sink-speech: ringer flag failed: %s", e)
        return False


def read_ringer(target: Target = DEFAULT_TARGET,
                max_age_s: float = RINGER_MAX_AGE_S,
                timeout: float = 2.0) -> "dict | None":
    """What the device last said about wanting to be quiet, or None.

    None is the answer to every question that did not get one — no publisher,
    an mpv too old for `user-data`, a broker that is down, a bridge that timed
    out, a snapshot older than `max_age_s`, a payload that is not a snapshot.
    Callers must treat all of them as "speak". There is exactly one way to be
    silenced, and it is a fresh verdict that says so.
    """
    try:
        raw = ipc.get_property(_socket_for(target), RINGER_PROPERTY,
                               timeout=timeout)
    except (ipc.MpvIpcError, OSError) as e:
        log.debug("sink-speech: ringer read failed: %s", e)
        return None
    if not isinstance(raw, dict):
        return None
    checked = raw.get("checked_at")
    if not isinstance(checked, (int, float)):
        return None
    age = time.time() - float(checked)
    if age > max_age_s:
        log.debug("sink-speech: ringer verdict is %.0fs old — ignoring", age)
        return None
    return raw


#: mpv property that overrides `media-title`. The phone's speech card and the
#: car display both read `media-title`, and a rendered clip's is its filename.
TITLE_PROPERTY = "force-media-title"

#: mpv `user-data` key carrying the words of the reply itself.
#:
#: `media-title` names the *conversation* — that is what a car display has room
#: for and what tells two replies from different windows apart. It does not say
#: what was said, and the phone's own list does. Two surfaces, two answers to
#: "what is this", and no way to have both from one field.
#:
#: So the words ride beside it. `user-data` because that is the channel the app
#: already watches for the speaking flag and the priority: observable,
#: arbitrary, no script and no new port. A display that knows nothing about it
#: is unaffected — it keeps reading `media-title`, which still says what it
#: always said.
TEXT_PROPERTY = "user-data/agent-media/text"

#: A card is two lines on a phone and one on some head units. Longer than this
#: is not a title, it is the reply pretending to be one.
TEXT_MAX = 120


def set_media_title(title: str, target: Target = DEFAULT_TARGET) -> bool:
    """Name this reply on the speech broker, for anything that shows a title.

    sink-speech plays rendered files, so mpv titles them the only way it can —
    `remote-20260814T190922-18480.mp3`. The phone's speech card fell back to a
    constant ("Sam") rather than show that, which says who is talking but not
    what about, and the car display had the same hole.

    So we say what the popup says: the conversation title captured when the
    reply was queued (`source_window`). One property, set once per response,
    read by every display that already reads `media-title` — no new channel and
    nothing for the app to learn.

    Best-effort like `set_speaking`, and for the same reason: a title is not
    worth delaying a sentence for. An empty title is a no-op rather than a
    write, so a reply with nothing to call itself keeps the last one's name off
    the display by leaving the app's own fallback in charge.
    """
    text = (title or "").strip()
    if not text:
        return False
    try:
        ipc.set_property(_socket_for(target), TITLE_PROPERTY, text)
        return True
    except (ipc.MpvIpcError, OSError) as e:
        log.debug("sink-speech: media title failed: %s", e)
        return False


def set_reply_text(text: str, target: Target = DEFAULT_TARGET) -> bool:
    """Put the reply's own words on the broker, for a display with room for them.

    First line only, and clipped: what a card can show is a phrase, and a
    paragraph arriving in a metadata field is a paragraph scrolling across
    somebody's dashboard. Best-effort like the title beside it — worth having,
    never worth delaying a sentence for.
    """
    line = (text or "").strip().splitlines()
    first = line[0].strip() if line else ""
    if not first:
        return False
    if len(first) > TEXT_MAX:
        first = first[:TEXT_MAX - 1].rstrip() + "…"
    try:
        ipc.set_property(_socket_for(target), TEXT_PROPERTY, first)
        return True
    except (ipc.MpvIpcError, OSError) as e:
        log.debug("sink-speech: reply text failed: %s", e)
        return False


def _clip_uri_for(uri: str, target: Target, prefer_url: bool = False) -> str:
    """Resolve the clip reference the *remote* player should load (Grade B).

    A remote broker (the phone's mpv over a TCP bridge) can't read this host's
    filesystem. Two ways to give it the clip, preferred in order:

      1. ``MEDIA_SPEECH_CLIP_LOCALDIR_<TARGET>`` — the clip was **pre-fetched**
         to this dir on the remote host (see :meth:`SinkSpeech.prefetch`); play
         it as a local file ``<localdir>/<basename>`` — no per-clip network I/O,
         which is what makes long replies reliable.
      2. ``MEDIA_SPEECH_CLIP_BASEURL_<TARGET>`` — fetch ``<baseurl>/<basename>``
         over HTTP (fallback; per-clip fetch, fragile on long replies).

    Already-URL uris and the all-unset case pass through, so local/rooms
    playback is unchanged.
    """
    if uri.startswith(("http://", "https://", "rtsp://")):
        return uri
    localdir = os.environ.get(_env_key("MEDIA_SPEECH_CLIP_LOCALDIR", target.name))
    base = os.environ.get(_env_key("MEDIA_SPEECH_CLIP_BASEURL", target.name))
    # prefer_url: the caller knows the localdir copy is unreliable (its
    # prefetch just failed), so a configured HTTP base beats a local path
    # that may not exist — a per-clip fetch is fragile, silence is worse.
    if localdir and not (prefer_url and base):
        return localdir.rstrip("/") + "/" + Path(uri).name
    if base:
        return base.rstrip("/") + "/" + Path(uri).name
    return uri


def _device_for(target: Target) -> Optional[str]:
    """Map a logical target to an mpv `audio-device` id (decision 1C).

    Returns None to leave the broker on its default device (the `local`
    case unless overridden). Routing is per-clip: `play` sets the device
    before `loadfile`, so one broker can serve `local`, `rooms`, etc.

    Resolution order:
      1. `MEDIA_SPEECH_DEVICE_<TARGET>` — explicit override for any
         target. "" / "auto" / "default" mean "broker default".
      2. `local`  → `MEDIA_SPEECH_LOCAL_DEVICE` (default: broker default).
      3. `rooms`  → `pulse/<MEDIA_ROOMS_SINK>` (default sink name `am`),
         i.e. the whole-house Snapcast feed.
    Unknown targets raise NotImplementedError so misroutes are loud.
    """
    override = os.environ.get(_env_key("MEDIA_SPEECH_DEVICE", target.name))
    if override is not None:
        return None if override.lower() in ("", "auto", "default") else override
    if target.name == "local":
        return os.environ.get("MEDIA_SPEECH_LOCAL_DEVICE") or None
    if target.name == "rooms":
        return f"pulse/{os.environ.get('MEDIA_ROOMS_SINK', 'am')}"
    raise NotImplementedError(
        f"sink-speech target {target.name!r} not configured — set "
        f"{_env_key('MEDIA_SPEECH_DEVICE', target.name)}")


class SinkSpeech:
    """Sink protocol implementation for the speech broker."""

    def __init__(self) -> None:
        # Target names whose last prefetch failed: play/play_playlist then
        # resolve clips to the HTTP base URL (if configured) instead of the
        # remote localdir the clips never reached.
        self._relay_unavailable: set = set()

    def _prefer_url(self, target: Target) -> bool:
        return target.name in self._relay_unavailable

    def play(self, uri: str, target: Target = DEFAULT_TARGET,
             reset_state: bool = True, **_: object) -> None:
        sock = _socket_for(target)
        device = _device_for(target)
        if device is not None:
            try:
                ipc.set_property(sock, "audio-device", device)
            except ipc.MpvIpcError as e:
                # Don't drop the clip over a device-switch hiccup; mpv
                # falls back to its current device.
                log.warning("sink-speech: set audio-device %s failed: %s",
                            device, e)
        # critical: this call IS the speech. A slow phone bridge must delay it,
        # never skip it — the breaker only exists to drop policy chatter.
        ipc.command(sock, "loadfile",
                    _clip_uri_for(uri, target, self._prefer_url(target)),
                    "replace", critical=True)
        # A fresh response must be audible regardless of a lingering
        # pause/mute left on the broker (e.g. a popup Space/m while idle) —
        # otherwise it loads into a paused/muted broker and plays silently.
        # But advancing between sentences of one response must NOT clear a
        # pause the user just made via the popup, or the response "resumes
        # itself". Callers pass reset_state=False for those mid-response
        # clips; only the first clip of a response resets.
        if reset_state:
            for prop in ("pause", "mute"):
                try:
                    ipc.set_property(sock, prop, False)
                except ipc.MpvIpcError:
                    pass

    def prefetch(self, paths: "list", target: Target = DEFAULT_TARGET) -> bool:
        """Copy all of a response's clips to the remote player's local dir up
        front (Grade B reliability).

        When ``MEDIA_SPEECH_CLIP_LOCALDIR_<TARGET>`` is set, the rendered clips
        are tar-piped over SSH into that dir on the remote host, so each
        subsequent `play` is a *local* loadfile instead of a per-clip network
        fetch — the per-sentence HTTP/​bridge fragility that stalled long replies.
        No-op (returns True) when no local dir is configured (local/rooms, or
        HTTP fallback). Best-effort: a failure is logged, remembered per
        target, and this sink's play/play_playlist then resolve clips to the
        HTTP base URL (if one is set) instead of the localdir the clips never
        reached — e.g. ssh to the phone broken while the clip HTTP server is
        fine. Returns False on failure so callers can react too.
        """
        localdir = os.environ.get(_env_key("MEDIA_SPEECH_CLIP_LOCALDIR", target.name))
        if not localdir or not paths:
            return True
        import shlex
        import subprocess
        host = (os.environ.get(_env_key("MEDIA_SPEECH_CLIP_SSH", target.name))
                or os.environ.get("MEDIA_MUSIC_LOCAL_SSH", "p8a"))
        ps = [Path(p) for p in paths]
        srcdir = str(ps[0].parent)
        qnames = " ".join(shlex.quote(p.name) for p in ps)
        opts = ("-o BatchMode=yes -o ConnectTimeout=10 -o ControlMaster=auto "
                "-o ControlPath=/tmp/ssh-am-%r@%h:%p -o ControlPersist=300")
        remote = (f"mkdir -p {shlex.quote(localdir)} && "
                  f"tar -C {shlex.quote(localdir)} -xf -")
        # pipefail (hence bash): a tar-side failure (clip pruned from the
        # local cache) must not read as success just because ssh exited 0.
        cmd = (f"set -o pipefail; "
               f"tar -C {shlex.quote(srcdir)} -cf - {qnames} | "
               f"ssh {opts} {shlex.quote(host)} {shlex.quote(remote)}")
        try:
            rc = subprocess.run(
                cmd, shell=True, executable="/bin/bash",
                timeout=float(os.environ.get("MEDIA_SPEECH_PREFETCH_TIMEOUT", "30")),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            ).returncode
        except Exception as e:  # noqa: BLE001 — best-effort; play has its own fallback
            log.warning("sink-speech: prefetch to %s failed: %s", host, e)
            rc = -1
        if rc != 0:
            log.warning("sink-speech: prefetch to %s failed (rc=%s); "
                        "falling back to clip base URL if configured", host, rc)
            self._relay_unavailable.add(target.name)
            return False
        self._relay_unavailable.discard(target.name)
        return True

    def play_playlist(self, uris: "list", target: Target = DEFAULT_TARGET,
                      gapless: bool = True) -> None:
        """Load all of a response's clips as a gapless playlist and start it.

        The (remote) player then advances through the clips *autonomously* — no
        per-sentence drive from this host, so a bridge hiccup can't stall or cut
        the reply, and clips play back-to-back with no inter-sentence gap. The
        caller monitors `playlist_pos` to follow along (now_playing/highlight).
        """
        sock = _socket_for(target)
        device = _device_for(target)
        # One batched round-trip instead of ~10 (each a ~600ms hop over the
        # bridge). Build the whole playlist BEFORE starting: a `loadfile replace`
        # would play the (~0.5s) first clip *immediately*, and it can END before
        # the rest are appended, leaving mpv idle with unplayed items. So clear,
        # append every clip to the idle player (append does NOT auto-play), then
        # jump to index 0 — from there mpv auto-advances gaplessly.
        cmds: list = []
        if device is not None:
            cmds.append(["set_property", "audio-device", device])
        cmds.append(["set_property", "gapless-audio", "yes" if gapless else "no"])
        cmds.append(["stop"])
        cmds.append(["playlist-clear"])
        prefer_url = self._prefer_url(target)
        for uri in uris:
            cmds.append(["loadfile", _clip_uri_for(str(uri), target, prefer_url),
                         "append"])
        cmds.append(["set_property", "pause", False])
        cmds.append(["set_property", "mute", False])
        cmds.append(["set_property", "playlist-pos", 0])
        try:
            ipc.command_batch(sock, cmds, critical=True)
        except (ipc.MpvIpcError, OSError) as e:
            log.warning("sink-speech: play_playlist batch failed: %s", e)
            # The fallback chain is exhausted — this reply never sounded.
            # Queue a "missed speech" phone notification that retries until
            # the (probably dozed) phone wakes and can show it.
            try:
                from ._miss_notify import record_miss
                record_miss(target.name)
            except Exception:  # noqa: BLE001 — alerting must not break playback
                pass

    def snapshot(self, target: Target = DEFAULT_TARGET) -> dict:
        """One-round-trip read of the state the playlist monitor needs each tick
        (playlist-pos / idle-active / pause / time-pos). Empty dict on failure —
        far cheaper over a bridge than four separate get_property hops.

        Judged by the same budget as `display_properties`, and for the same
        reason: latency is not a fault here. The in-app player on p8a answers
        this exact read in a steady 1.28s — 0.43s of tailnet connect and the
        rest honest work — against a default slow line of 1200ms. Eighty
        milliseconds over, every single time, so the breaker tripped on every
        tick and then skipped the endpoint for twenty seconds:

            1.29s  answered=[idle-active, mute, pause, playlist-pos, speed]
            0.00s  FAILED: skipped, endpoint slow (19s left)
            0.00s  FAILED: skipped, endpoint slow (19s left)

        The breaker's state is shared on disk, so one process tripping it blinds
        every other hook too. A follow loop that cannot read the player bails as
        stalled and then holds the speech token for the reply's whole duration
        on the blind-hold tail — which is how replies queued behind each other
        for ten, twenty, fifty minutes while the phone itself was idle 80% of
        the hour.

        Failure still opens it, briefly: a genuinely dead phone must not make
        every tick wait out the connect timeout. Being slow is not failing.
        """
        try:
            return ipc.get_properties(
                _socket_for(target),
                ["playlist-pos", "idle-active", "pause", "time-pos", "mute",
                 "speed"],
                # Above the 1.28s the app really takes, so a tick under load
                # returns a whole snapshot rather than one missing the very
                # field (idle-active) the loop ends on.
                timeout=3.0, slow_s=0, breaker_s=5)
        except (ipc.MpvIpcError, OSError):
            return {}

    def playlist_pos(self, target: Target = DEFAULT_TARGET) -> Optional[int]:
        """Index of the clip the playlist is currently on (-1/None when idle)."""
        try:
            pos = ipc.get_property(_socket_for(target), "playlist-pos")
        except ipc.MpvIpcError:
            return None
        return int(pos) if pos is not None else None

    def set_playlist_pos(self, pos: int, target: Target = DEFAULT_TARGET) -> None:
        """Jump the playlist to clip `pos` (popup skip/replay over the bridge)."""
        try:
            ipc.set_property(_socket_for(target), "playlist-pos", int(pos))
        except ipc.MpvIpcError:
            pass

    def queue(self, uri: str, target: Target = DEFAULT_TARGET) -> None:
        """Append a clip to mpv's playlist without interrupting what's playing."""
        ipc.command(_socket_for(target), "loadfile",
                    _clip_uri_for(uri, target), "append", critical=True)

    # critical: a person pressed a key. The slow-endpoint breaker exists to drop
    # *observational* chatter (is anything playing? duck it) when a remote
    # endpoint is answering slowly — but the phone lane makes that same endpoint
    # the control path, and silently skipping a pause is indistinguishable from
    # the button being broken. Speech that arrives late is a nuisance; a pause
    # that never happens is a bug the user cannot work around.
    def pause(self, target: Target = DEFAULT_TARGET) -> None:
        ipc.set_property(_socket_for(target), "pause", True, critical=True)

    def resume(self, target: Target = DEFAULT_TARGET) -> None:
        ipc.set_property(_socket_for(target), "pause", False, critical=True)

    def stop(self, target: Target = DEFAULT_TARGET) -> None:
        ipc.command(_socket_for(target), "stop", critical=True)

    # ---- cross-host broker ownership -------------------------------------
    #
    # These are no-ops for a local/rooms (unix-socket) target: only one host
    # drives it, so the playback flock already serializes. They matter only for
    # a shared remote (tcp://) broker, where several hosts can drive the same
    # mpv and the flock — being per-host — can't stop them clobbering each other.

    def _is_remote(self, target: Target) -> bool:
        return str(_socket_for(target)).startswith("tcp://")

    def active_other_owner(self, target: Target = DEFAULT_TARGET) -> Optional[dict]:
        """The claim of *another* host that currently holds this broker, or None.

        None means 'safe to take': local target, unowned, expired, ours, or
        unreadable (a dead bridge — can't coordinate, so don't pretend someone
        holds it). Returns the raw ``{"owner", "deadline"}`` dict otherwise so a
        waiter can watch the deadline advance (a live holder refreshes it)."""
        if not self._is_remote(target):
            return None
        try:
            cur = ipc.get_property(_socket_for(target), _BROKER_OWNER_KEY)
        except (ipc.MpvIpcError, OSError):
            return None
        if not isinstance(cur, dict):
            return None
        owner = cur.get("owner")
        if not owner or owner == _broker_owner_id():
            return None
        try:
            if float(cur.get("deadline", 0)) <= time.time():
                return None  # expired — the holder crashed or stalled
        except (TypeError, ValueError):
            return None
        return cur

    def claim_broker(self, target: Target = DEFAULT_TARGET,
                     ttl: float = BROKER_TTL_S) -> bool:
        """Best-effort claim of a shared remote broker. Returns True once we own
        it — or immediately for a local target, or if the broker is unreachable
        (never block a reply on the token machinery itself)."""
        if not self._is_remote(target):
            return True
        if self.active_other_owner(target) is not None:
            return False  # someone else actively holds it
        sock = _socket_for(target)
        me = _broker_owner_id()
        try:
            ipc.set_property(sock, _BROKER_OWNER_KEY,
                             {"owner": me, "deadline": time.time() + ttl})
        except (ipc.MpvIpcError, OSError):
            return True  # can't reach broker to claim → play anyway, don't wedge
        # Verify after a small per-pid desync so two hosts that raced the read
        # don't both believe they won — the later writer wins and the other sees
        # it isn't the owner and backs off.
        time.sleep(0.05 + (os.getpid() % 10) / 100.0)
        try:
            cur = ipc.get_property(sock, _BROKER_OWNER_KEY)
        except (ipc.MpvIpcError, OSError):
            return True
        return isinstance(cur, dict) and cur.get("owner") == me

    def refresh_broker(self, target: Target = DEFAULT_TARGET,
                       ttl: float = BROKER_TTL_S) -> None:
        """Push our claim's deadline out while we keep playing. No-op unless we
        currently own it (so we never steal a claim from whoever took over)."""
        if not self._is_remote(target):
            return
        sock = _socket_for(target)
        me = _broker_owner_id()
        try:
            cur = ipc.get_property(sock, _BROKER_OWNER_KEY)
            if isinstance(cur, dict) and cur.get("owner") == me:
                ipc.set_property(sock, _BROKER_OWNER_KEY,
                                 {"owner": me, "deadline": time.time() + ttl})
        except (ipc.MpvIpcError, OSError):
            pass

    def release_broker(self, target: Target = DEFAULT_TARGET) -> None:
        """Drop our claim so the next host can take the broker immediately.
        Only clears it if it's still ours."""
        if not self._is_remote(target):
            return
        sock = _socket_for(target)
        me = _broker_owner_id()
        try:
            cur = ipc.get_property(sock, _BROKER_OWNER_KEY)
            if isinstance(cur, dict) and cur.get("owner") == me:
                ipc.set_property(sock, _BROKER_OWNER_KEY,
                                 {"owner": "", "deadline": 0})
        except (ipc.MpvIpcError, OSError):
            pass

    def duck(self, target: Target = DEFAULT_TARGET, level: int = 50) -> None:
        # Clamp to the broker's configured ceiling, not a bare 100 — the broker
        # runs with --volume-max above 100 for the louder default, so the duck
        # level must be free to sit anywhere in that range.
        ipc.set_property(_socket_for(target), "volume",
                         max(0, min(_broker_max_volume(), level)))

    def unduck(self, target: Target = DEFAULT_TARGET) -> None:
        # Restore to the broker's *configured* default, not a hardcoded 100,
        # or every duck/unduck cycle would quietly pull speech below the
        # louder MEDIA_SPEECH_VOLUME the broker launched with.
        ipc.set_property(_socket_for(target), "volume", _broker_default_volume())

    def position(self, target: Target = DEFAULT_TARGET) -> Optional[int]:
        try:
            pos = ipc.get_property(_socket_for(target), "time-pos")
        except ipc.MpvIpcError:
            return None
        if pos is None:
            return None
        return int(pos * 1000)

    def idle(self, target: Target = DEFAULT_TARGET) -> bool:
        """True when nothing is playing — useful for queue-vs-interrupt
        decisions in route/.
        """
        try:
            return bool(ipc.get_property(_socket_for(target), "idle-active"))
        except ipc.MpvIpcError:
            return True

    def paused(self, target: Target = DEFAULT_TARGET) -> bool:
        """True when a clip is loaded but held paused (e.g. popup Space).

        Returns False on IPC error so a transient hiccup can't wedge a
        caller that loops while paused.
        """
        try:
            return bool(ipc.get_property(_socket_for(target), "pause"))
        except ipc.MpvIpcError:
            return False

    def muted(self, target: Target = DEFAULT_TARGET) -> bool:
        """True when the speech broker is muted (e.g. popup `m`).

        Returns False on IPC error so a transient hiccup can't make a
        caller think silent-speech when it isn't.
        """
        try:
            return bool(ipc.get_property(_socket_for(target), "mute"))
        except ipc.MpvIpcError:
            return False
