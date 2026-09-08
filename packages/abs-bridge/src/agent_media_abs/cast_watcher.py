"""`media-abs-cast-watcher` — the phone's live session, and on request the rooms.

Audiobookshelf plays client-side and the server has no "pause that client"
command, so casting works by substitution: start the same file on the book
channel at the live position, then close the ABS session. The official clients
stop once their session is gone — within a sync cycle, so there is a few
seconds of overlap and then the rooms have it.

Since 2026-09-09 that is an **action, not a reflex**. The phone (Sasonica's
ExoPlayer) is the primary player; the watcher keeps following its session but
only casts when `CAST_AUTO=1`. `media-abs-cast` sends the live session to the
rooms when asked. The old behaviour treated the app as a remote control for
mpv, which meant a Bluetooth listen on the phone was stolen by the speakers.

**An open session is not a playing session.** Idle tabs stay open for hours,
and a seek moves the position without anything playing. So a session only
counts when its position advances in something like real time, twice running.
That test is the whole reason this is safe to leave enabled: it is what stops a
forgotten tab from seizing the speakers.

Nothing loops back: rooms playback is mpv, which opens no ABS session.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
from typing import Optional

from ._abs import Abs, load_env, local_path, log

#: Two consecutive real-time advances before casting. One is a seek; two is
#: somebody listening.
ADVANCES_NEEDED = 2


def device_label(session: dict) -> str:
    d = session.get("deviceInfo") or {}
    return f"{d.get('deviceName') or '?'} / {d.get('clientName') or '?'}"


def device_ok(session: dict, allow: list, deny: list) -> bool:
    """Allow-list wins outright when set; otherwise everything but the deny."""
    label = device_label(session).lower()
    if allow:
        return any(a in label for a in allow)
    return not any(d in label for d in deny)


def is_advancing(delta: float, elapsed: float) -> bool:
    """Did the position move the way playback moves?

    Nothing (paused) fails, and so does a jump far bigger than the wall clock
    (a seek, or a client catching up after a sleep). The upper bound allows for
    speed-up playback and a late poll.
    """
    return 0.3 < delta < elapsed * 3 + 2


def cast(abs_api: Abs, session: dict, *, dry: bool = False) -> bool:
    """Move one session into the rooms. True if the rooms took it."""
    item_id = session.get("libraryItemId")
    ct = session.get("currentTime") or 0
    title = session.get("displayTitle") or item_id
    where = device_label(session)

    try:
        item = abs_api.req("GET", f"/api/items/{item_id}")
    except urllib.error.URLError as e:
        log(f"  cannot look up {item_id}: {e!r}")
        return False
    files = (item.get("media") or {}).get("audioFiles") or []
    container = ((files[0].get("metadata") or {}).get("path") if files else "") or ""
    path = local_path(container)
    if path is None:
        # ABS can catalogue what the rooms cannot reach — another library
        # folder, a file only the container has. Say which, and do not close
        # the session: the phone should go on playing rather than stop dead.
        log(f"  no local file for {title!r} ({container or 'no path'}); not casting")
        return False

    if dry:
        log(f"WOULD CAST -> rooms: {title!r} @ {int(ct)}s (from {where}) [CAST_DRY]")
        return True

    log(f"CAST -> rooms: {title!r} @ {int(ct)}s (from {where})")
    rc = subprocess.call(
        ["media", "book", "play", str(path), "--start-ms", str(int(ct * 1000))],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    if rc != 0:
        # Leave the ABS session alone. Closing it after a failed handover would
        # stop the phone as well, which is the one outcome worse than not
        # casting.
        log(f"  `media book play` failed (rc={rc}); not closing ABS session")
        return False
    try:
        abs_api.req("POST", f"/api/session/{session['id']}/close", {"currentTime": ct})
        log("  ABS session closed (client stops on its next sync)")
    except Exception as e:  # noqa: BLE001
        log(f"  note: could not close ABS session: {e!r}")
    return True


def pick_session(sessions: list, session_id: str = "") -> Optional[dict]:
    """The session an explicit cast means: the named one, else the book
    session touched most recently. Someone asking to hear the phone in the
    rooms has just been listening on it, so recency is the right guess and
    the advancing test is not needed — they said so."""
    books = [s for s in sessions if s.get("mediaType") == "book"]
    if session_id:
        return next((s for s in books if s.get("id") == session_id), None)
    if not books:
        return None
    return max(books, key=lambda s: s.get("updatedAt") or 0)


def cast_main(argv=None) -> int:
    """`media-abs-cast` — send the phone's live session to the rooms, now."""
    import argparse
    ap = argparse.ArgumentParser(prog="media-abs-cast", description=cast_main.__doc__)
    ap.add_argument("--session", default="", help="ABS session id (default: most recent book session)")
    ap.add_argument("--list", action="store_true", help="show open book sessions and exit")
    ap.add_argument("--dry", action="store_true", help="decide, touch nothing")
    a = ap.parse_args(argv)
    load_env()
    abs_api = Abs()
    if not abs_api.token:
        log("ABS_TOKEN not set in ~/.config/agent-media/abs-bridge.env")
        return 2
    sessions = abs_api.req("GET", "/api/sessions/open").get("sessions", [])
    if a.list:
        for s in sessions:
            if s.get("mediaType") == "book":
                print(f"{s.get('id')}  {int(s.get('currentTime') or 0)}s  "
                      f"{device_label(s)}  {s.get('displayTitle')}")
        return 0
    s = pick_session(sessions, a.session)
    if s is None:
        log("no open book session on Audiobookshelf — nothing to cast")
        return 1
    return 0 if cast(abs_api, s, dry=a.dry) else 1


def main(argv=None) -> int:
    load_env()
    abs_api = Abs()
    poll_s = float(os.environ.get("CAST_POLL_S", "4"))
    cooldown_s = float(os.environ.get("CAST_COOLDOWN_S", "90"))
    deny = [s.strip().lower() for s in os.environ.get("CAST_DEVICE_DENY", "").split(",") if s.strip()]
    allow = [s.strip().lower() for s in os.environ.get("CAST_DEVICE_ALLOW", "").split(",") if s.strip()]
    dry = os.environ.get("CAST_DRY", "0") == "1"
    #: Off by default: the phone is the player. On, the watcher moves every
    #: live session to the rooms the way it did before 2026-09-09.
    auto = os.environ.get("CAST_AUTO", "0") == "1"

    if not abs_api.token:
        log("ABS_TOKEN not set in ~/.config/agent-media/abs-bridge.env — idle.")
        return 0
    log(f"cast watcher up: ABS={abs_api.url} poll={poll_s}s cooldown={cooldown_s}s "
        f"auto={'on' if auto else 'off (media-abs-cast sends a session to the rooms)'} "
        f"allow={allow or '-'} deny={deny or '-'}")

    state: dict = {}
    recent_cast: dict = {}
    while True:
        try:
            now = time.time()
            sessions = abs_api.req("GET", "/api/sessions/open").get("sessions", [])
            live = set()
            for s in sessions:
                if s.get("mediaType") != "book":
                    continue
                sid = s.get("id")
                live.add(sid)
                ct = s.get("currentTime") or 0
                st = state.get(sid)
                if st is None:
                    # Seed without judging: a session already open and stale
                    # when this daemon started must never look like playback.
                    state[sid] = {"last_ct": ct, "last_t": now, "advancing": 0}
                    continue
                elapsed, delta = now - st["last_t"], ct - st["last_ct"]
                st["last_ct"], st["last_t"] = ct, now
                st["advancing"] = st["advancing"] + 1 if is_advancing(delta, elapsed) else 0

                item_id = s.get("libraryItemId")
                cooled = now - recent_cast.get(item_id, 0) < cooldown_s
                if st["advancing"] >= ADVANCES_NEEDED and not st.get("cast") and not cooled:
                    if not device_ok(s, allow, deny):
                        log(f"skip (device filtered): {device_label(s)}")
                        st["cast"] = True      # decided; stop re-deciding
                        continue
                    if not auto:
                        log(f"live on {device_label(s)}: {s.get('displayTitle')!r} "
                            f"@ {int(ct)}s — leaving it there (CAST_AUTO=0)")
                        st["cast"] = True      # noted once; the phone keeps it
                        continue
                    if cast(abs_api, s, dry=dry):
                        st["cast"] = True
                        recent_cast[item_id] = now

            for sid in [k for k in state if k not in live]:
                del state[sid]
        except urllib.error.HTTPError as e:
            log(f"ABS HTTP {e.code}: {e.reason}")
            if e.code in (401, 403):
                time.sleep(60)
        except Exception as e:  # noqa: BLE001
            log(f"error: {e!r}")
        time.sleep(poll_s)


if __name__ == "__main__":
    sys.exit(main())
