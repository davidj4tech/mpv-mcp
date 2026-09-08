"""A conversation as a library item that grows: the clips, as they land.

What this replaced built one file per conversation — every clip concatenated,
one chapter per turn — and rebuilt it from scratch each time another turn
landed. Nothing could be published until the conversation had been quiet for an
hour, an 80-minute history was re-concatenated to add two minutes to it, and
anyone holding the old file was holding something that had changed underneath
them. It also cost a second copy of every conversation: 91MB of spool beside
the clips it was made from.

Measured against Audiobookshelf 2.35.1 before any of this was written (see
`docs/proposals/2026-09-02-growing-item-experiment.md`): a scan of a folder
that gained a file keeps the same item id, appends the track at the right
offset, leaves the existing files' inode, index and mtime alone, and preserves
the listener's position — which ABS stores in seconds, so it survives the
duration changing underneath it. The one thing it does not do by itself is
re-open an item the listener had finished; `reopen` below is that, and it is
two calls in an order the API does not advertise.

**The clips are the tracks.** The renderer already splits a reply into a clip
per sentence, and those files already exist — so this writes no audio at all.
Each track is a *hardlink* to the clip the renderer made: one inode with two
names, nothing to re-encode, nothing to keep in step, and a conversation costs
the library zero bytes. Joining each turn into one file was tried first and
undone: it wrote a second copy of every conversation, needed ffmpeg and a
guard against concatenations that lie about their length, and bought only a
shorter list.

That list is the visible trade. Audiobookshelf makes a chapter of every track,
so a conversation is chaptered by *sentence* — 336 of them for this one. Which
is either a lot of rows, or a transcript you can jump around in; the sentence
map the renderer recorded is what makes it the second, because each chapter is
named with the words it is about to say.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from . import session_feed
from ._paths import state_dir

log = logging.getLogger(__name__)


#: A folder name a scanner and three filesystems can all live with. Not the
#: episode title verbatim: those carry `/`, `:` and the em dash that separates
#: workspace from question.
_UNSAFE = re.compile(r"[^\w .,()'’-]+")


def safe_name(text: str, limit: int = 110) -> str:
    name = _UNSAFE.sub(" ", (text or "").replace("·", "-")).strip()
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > limit:
        name = name[:limit].rsplit(" ", 1)[0].rstrip(" .,-")
    return name or "conversation"


def root() -> Path:
    """Where the library lives. `MEDIA_BOOK_TRACKS_ROOT` overrides, and
    `MEDIA_BOOK_EXPORT_ROOT` still does too — it named this tree first, and a
    host that set it meant this tree."""
    raw = (os.environ.get("MEDIA_BOOK_TRACKS_ROOT", "").strip()
           or os.environ.get("MEDIA_BOOK_EXPORT_ROOT", "").strip())
    return Path(raw).expanduser() if raw else Path.home() / "conversations"


def _manifest_path(session: str) -> Path:
    return state_dir() / "book-tracks" / f"{safe_name(session, 80)}.json"


def _read_manifest(session: str) -> dict:
    try:
        d = json.loads(_manifest_path(session).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_manifest(session: str, data: dict) -> None:
    p = _manifest_path(session)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(p)
    except OSError as e:
        log.warning("book-tracks: cannot write %s (%s)", p, e)


def _ffprobe(p: Path) -> float:
    return session_feed._ffprobe_duration(p)


def place_clip(src, out: Path) -> Optional[Path]:
    """Give the clip a second name inside the item. `out`, or None.

    A hardlink, not a copy: the render cache holds the only durable copy of
    this audio, and a byte-for-byte duplicate would double what a conversation
    costs while being able to drift from it. Two names for one inode cannot
    disagree, and deleting either never takes the audio with it. Cross-device
    falls back to copying, because a link that cannot be made is not a reason
    to have no track.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    src = Path(src)
    try:
        os.link(src, out)
    except FileExistsError:
        return out
    except OSError:
        try:
            shutil.copyfile(src, out)
        except OSError as e:
            log.warning("book-tracks: cannot place %s (%s)", src, e)
            return None
    return out


def track_name(index: int, said: str, fallback: str = "") -> str:
    """`007 - The sentence this clip says.mp3`.

    The index leads because a scanner orders an item's files by name, and it is
    zero-padded to four because otherwise track 10 sorts before track 2 — which
    corrupts nothing, it just plays the conversation in the wrong order,
    quietly. Four digits, because a long conversation is thousands of
    sentences and the day that wraps is the day every later one is misfiled.
    """
    return f"{index:04d} - {safe_name(said or fallback, 90)}.mp3"


def folder_for(session: str, turns: list, manifest: dict) -> Optional[Path]:
    """The item folder for this conversation, decided once and then kept.

    The title comes from what was asked, so it can change as a conversation
    grows — and a library item that renames itself is a *new* item to
    Audiobookshelf: new id, no progress, and the old one left behind as a
    duplicate. So the first export writes the folder into the manifest and
    every later one uses it, even when a better title exists by then. An item
    that keeps its identity is worth more than an item with the best name.
    """
    kept = manifest.get("folder")
    if kept:
        return Path(kept)
    if not turns:
        return None
    workspace = session_feed.workspace_for(session, turns)
    # The workspace is the folder above, which Audiobookshelf reads as the
    # author — so putting it in the title too says it twice on every shelf.
    title = session_feed.asked_for(session, turns)
    return root() / safe_name(workspace) / safe_name(title)


# --- the listener's own turns -------------------------------------------------
#
# A conversation you can reply to is a conversation with two people in it, and
# only one of them was being recorded. The reply box put words into the session
# and they vanished — the answer came back on the shelf, the question did not,
# and a chapter list of answers to invisible questions is a worse record than
# no record.
#
# So a typed reply is rendered to speech and written to speech history like any
# other turn. It is not *played*: nobody wants their own message read at them
# as they send it. The row is what matters — the exporter reads history, so the
# turn lands in order, in the right item, with a chapter of its own, and the
# audio is there for whoever plays the conversation back later.
#
# In a different voice, deliberately. The whole representation is audio, so the
# only way to hear who is speaking is to hear it.

#: Australian, and not the assistant's. Override with MEDIA_LISTENER_VOICE.
LISTENER_VOICE = "en-AU-WilliamNeural"


def record_listener_turn(session: str, text: str, *, store=None) -> bool:
    """Render a reply the listener typed and add it to the conversation.

    Returns whether the turn was recorded. A failed render is not fatal to the
    reply itself — the words still reached the session, which is the point of
    the feature; they just do not appear on the shelf.
    """
    import time as _time

    text = " ".join((text or "").split())
    if not text or not session:
        return False
    from ._paths import cache_dir
    from .render.engines import render_text
    from .state.store import StateStore

    at = _time.time()
    d = cache_dir() / "audio"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("book-tracks: no clip dir (%s)", e)
        return False
    stamp = _time.strftime("%Y%m%dT%H%M%S", _time.localtime(at))
    out = d / f"{stamp}-listener-{safe_name(session, 8)}.mp3"

    engine = os.environ.get("MEDIA_LISTENER_ENGINE") or "edge"
    voice = os.environ.get("MEDIA_LISTENER_VOICE") or LISTENER_VOICE
    ok, err = render_text(text, out, engine=engine, voice=voice,
                          edge_voice=voice)
    if not ok or not out.is_file():
        log.warning("book-tracks: could not render a listener turn (%s)", err)
        return False
    dur = _ffprobe(out)

    try:
        st = store or StateStore()
        st.add_history(
            sink="speech", uri=str(out), started_at=at, ended_at=at + dur,
            # Never played anywhere: the row exists to be archived, not routed.
            target="none", source="listener", content_type="audio/mpeg",
            # The chapter title comes from this text, so it carries the speaker
            # label — a chapter list is read, not heard, and without it the
            # questions and the answers look alike. The audio says only the
            # reply; the label is not spoken.
            text=f"You: {text}",
            extras={"source_session": session, "listener": True,
                    "engine": engine, "voice": voice,
                    "clip_uris": [str(out)], "clip_sentences": [text],
                    "clip_durations_s": [dur]})
    except Exception as e:  # noqa: BLE001 — the reply already landed
        log.warning("book-tracks: could not record a listener turn (%s)", e)
        return False

    # Put it on the shelf on the same schedule as a spoken turn, rather than
    # waiting for the answer to arm one: a question that appears after its
    # answer is worse than one that appears late.
    try:
        from . import feed_debounce

        feed_debounce.arm()
    except Exception:  # noqa: BLE001 — the poll will catch it
        pass
    return True


def export_session(session: str, *, store=None) -> tuple[Optional[Path], int]:
    """Write any clips this conversation has gained. `(folder, added)`.

    Idempotent and append-only. The turn is the unit that lands — a reply's
    clips all exist by the time it is over — so a turn already exported is
    identified by the `started_at` its history row carries and skipped whole.

    The running number comes from the manifest, never from recounting the
    turns. Speech history outlives the render cache, so a conversation can lose
    an old turn's audio entirely (`session_feed.turns` drops what it cannot
    find) — and a number derived by counting would then shift every track after
    the gap, renaming files that are already on disk and already downloaded.
    What has been written is what decides where the next one goes.
    """
    turns = session_feed.turns(session, store=store)
    manifest = _read_manifest(session)
    folder = folder_for(session, turns, manifest)
    if folder is None:
        return None, 0

    done = {float(t["at"]) for t in manifest.get("turns", [])}
    written = list(manifest.get("turns", []))
    index = sum(len(t.get("files") or []) for t in written)
    added = 0
    for turn in turns:
        at = float(turn.at)
        if at in done:
            continue
        files = []
        for i, clip in enumerate(turn.clips):
            said = turn.sentences[i] if i < len(turn.sentences) else ""
            dest = folder / track_name(index + 1, said, turn.title)
            if dest.exists():
                # Ours already, from a run whose manifest was lost. Adopt it
                # rather than counting it as new: "+12 tracks" for a run that
                # wrote nothing is a report nobody can act on.
                index += 1
                files.append(dest.name)
                continue
            if place_clip(clip, dest) is not None:
                index += 1
                files.append(dest.name)
                added += 1
                continue
            # Stop at the first clip that will not go: the numbering is
            # sequential, so carrying on would file the next sentence under
            # this one's number and leave the conversation out of order.
            log.warning("book-tracks: %s stopped at track %d", session, index + 1)
            break
        if not files:
            break
        written.append({"at": at, "title": turn.title, "files": files})

    if added or not manifest:
        manifest.update({"session": session, "folder": str(folder),
                         "turns": written})
        _write_manifest(session, manifest)
    return folder, added


def export_all(store=None, since_hours: float = 24.0) -> list:
    """Conversations that have said something lately. `[(session, folder, added)]`.

    Bounded on purpose. Speech history goes back months, and an unbounded run
    would build a growing item for every conversation that ever spoke — a
    library of hundreds where the point is the handful still being had. A
    conversation quiet for longer than the window is finished as far as this is
    concerned, and `book_export` already publishes those. `since_hours <= 0`
    means all of them, for the one-off backfill.
    """
    import time as _time

    cutoff = (_time.time() - since_hours * 3600.0) if since_hours > 0 else 0.0
    out = []
    for conv in session_feed.conversations(store=store):
        session = conv.get("session") or ""
        if not session:
            continue
        last = float(conv.get("last") or conv.get("at") or 0.0)
        if cutoff and last and last < cutoff:
            continue
        folder, added = export_session(session, store=store)
        if folder is not None:
            out.append((session, folder, added))
    return out


# --- the item, once the files are there ------------------------------------
#
# Appending a file is not the whole job. Audiobookshelf stores progress in
# seconds, so a listener's position survives the item growing — but `isFinished`
# survives it too. Reach the end of what exists, let a turn land, and the item
# stays finished: the new turn is on the server, correctly placed, and out of
# Continue Listening, which is the one place anyone would look for it. Measured
# on 2.35.1; see the experiment write-up.
#
# Re-opening it is two calls, and the order is not decoration: clearing
# `isFinished` in the same body as a position RESETS `currentTime` to zero,
# because ABS reads un-finishing as starting over. So clear the flag, then put
# the listener back — at the head of the turn they have not heard.

def chapters_from(turns: list, tracks: list) -> list:
    """One chapter per turn, over tracks that are one per sentence.

    The two units are not in conflict, they answer different questions. The
    *tracks* are the clips the renderer made, because those files already exist
    and linking them costs nothing. The *chapters* are the turns, because that
    is what a listener moves around by — 352 rows of one sentence each is a
    transcript, not a table of contents.

    Offsets come from the tracks as the server computed them, never from the
    durations recorded here: the server is the thing that will play it, and if
    the two ever disagree the one that is wrong is this one.
    """
    out, i = [], 0
    for n, turn in enumerate(turns):
        count = len(turn.get("files") or [])
        if not count or i + count > len(tracks):
            break
        start = float(tracks[i].get("startOffset") or 0.0)
        last = tracks[i + count - 1]
        end = float(last.get("startOffset") or 0.0) + float(last.get("duration") or 0.0)
        title = (turn.get("title") or "").strip()
        if not title:
            # A manifest written before titles were kept. The filename holds
            # the sentence, which is the same words the title would have been.
            name = (turn.get("files") or [""])[0]
            title = name.rsplit(".", 1)[0].split(" - ", 1)[-1]
        out.append({"id": n, "start": round(start, 3), "end": round(end, 3),
                    "title": title[:120]})
        i += count
    return out


def _abs_items(url: str, token: str, lib_id: str) -> list:
    import json as _json
    import urllib.request as _u
    req = _u.Request(f"{url}/api/libraries/{lib_id}/items?limit=1000",
                     headers={"Authorization": f"Bearer {token}"})
    with _u.urlopen(req, timeout=15) as r:
        return _json.loads(r.read()).get("results", [])


def _abs_patch(url: str, token: str, path: str, body: dict) -> None:
    import json as _json
    import urllib.request as _u
    req = _u.Request(url + path, data=_json.dumps(body).encode(), method="PATCH",
                     headers={"Authorization": f"Bearer {token}",
                              "Content-Type": "application/json"})
    with _u.urlopen(req, timeout=15):
        return


def _abs_ready(target=None):
    """`(url, token, [libraries])` for the book libraries, or None when there is
    no Audiobookshelf configured on this host — which is not a failure.

    Every book library, not the configured one. A host with two of them —
    "Audiobooks" and "Conversations" here — resolves an unset `ABS_LIBRARY` to
    whichever came first, and looking for a conversation in the audiobooks is a
    silent nothing: no item, no chapters, no complaint. The folder tail is
    unique across libraries anyway, so searching them all cannot be wrong; the
    configured one just goes first.
    """
    from . import library

    url, token, want = library._abs_cfg(target)
    if not url or not token:
        return None
    import json as _json
    import urllib.request as _u
    try:
        req = _u.Request(f"{url}/api/libraries",
                         headers={"Authorization": f"Bearer {token}"})
        with _u.urlopen(req, timeout=15) as r:
            libs = _json.loads(r.read()).get("libraries", [])
    except Exception:  # noqa: BLE001
        return None
    books = [l for l in libs if l.get("mediaType") == "book"]
    if want:
        books.sort(key=lambda l: (l.get("id") != want and l.get("name") != want))
    return (url, token, books) if books else None


def _abs_ready_all(target=None):
    """Every Audiobookshelf that should carry conversation metadata, as a list
    of `(url, token, [book libraries])`.

    The primary (`_abs_ready`) first, then each extra server from `ABS_SERVERS`
    resolved to its own book libraries with its own token. Empty when none is
    configured. A server that will not answer is dropped, not fatal — the same
    posture the single-server path always took.
    """
    from . import library

    out = []
    primary = _abs_ready(target)
    if primary:
        out.append(primary)
    import json as _json
    import urllib.request as _u
    for url, token in library._abs_extra_servers(target):
        try:
            req = _u.Request(f"{url}/api/libraries",
                             headers={"Authorization": f"Bearer {token}"})
            with _u.urlopen(req, timeout=15) as r:
                libs = _json.loads(r.read()).get("libraries", [])
        except Exception:  # noqa: BLE001
            continue
        books = [l for l in libs if l.get("mediaType") == "book"]
        if books:
            out.append((url, token, books))
    return out


def _find_item(url: str, token: str, libs: list, folder: Path):
    """The scanned item for this folder, or None.

    Match on the tail of the path rather than the whole of it: the server sees
    its mount ("/conversations/..."), this process sees the host's, and nothing
    here is told how one maps onto the other. <author>/<title> is the part both
    agree on.
    """
    tail = "/".join(folder.parts[-2:])
    for lib in libs:
        for i in _abs_items(url, token, lib["id"]):
            if str(i.get("path", "")).replace("\\", "/").endswith(tail):
                return i
    return None


def _abs_item(url: str, token: str, item_id: str) -> Optional[dict]:
    import json as _json
    import urllib.request as _u
    req = _u.Request(f"{url}/api/items/{item_id}",
                     headers={"Authorization": f"Bearer {token}"})
    try:
        with _u.urlopen(req, timeout=10) as r:
            return _json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def _track_count(item: dict) -> int:
    """How many audio files ABS holds for an item.

    Two shapes: the library listing summarises (`media.numTracks`), while
    `/api/items/<id>` expands (`media.audioFiles`) and carries no count at all.
    Read whichever is there.
    """
    media = item.get("media") or {}
    files = media.get("audioFiles")
    if isinstance(files, list):
        return len(files)
    return int(media.get("numTracks") or 0)


def wait_for_tracks(folder: Path, want: int, *, target=None,
                    timeout_s: float = 25.0) -> bool:
    """Block until Audiobookshelf's item for `folder` holds `want` tracks.

    This replaces sleeping a fixed ten seconds after asking for a scan. The
    sleep was a guess in both directions: too long when the scan finished in
    two, and too short when it did not, in which case the chapters were written
    against the durations the item had *before* the turn landed. Asking the
    item how many tracks it has now is the actual question.

    False on timeout, which is not fatal — the caller writes chapters anyway
    and the next turn corrects them.
    """
    import time as _time

    ready = _abs_ready(target)
    if not ready:
        return False
    url, token, libs = ready
    item = _find_item(url, token, libs, folder)
    deadline = _time.monotonic() + timeout_s
    while True:
        if item:
            fresh = _abs_item(url, token, item["id"]) or item
            if _track_count(fresh) >= want:
                return True
        if _time.monotonic() >= deadline:
            return False
        _time.sleep(0.4)
        if not item:
            # The item did not exist yet at all — a conversation's first turn.
            item = _find_item(url, token, libs, folder)


def publish_chapters(session: str, folder: Path, *, target=None) -> int:
    """Give the item a chapter per turn. Number written, or 0.

    Audiobookshelf makes one chapter per audio file when it first scans a
    multi-track item, which for clips-as-tracks means a chapter per sentence —
    and on an item it later *updates*, it does not redo them at all: the item
    grows to 352 tracks and keeps the 32 chapters it was born with, pointing at
    moments that have moved. (That staleness is #525 upstream, and it is not
    worth waiting for.) So the chapters are written here rather than inferred
    there: the turn structure is in the manifest, the offsets are in the tracks
    the server just scanned, and the API takes the list.
    """
    ready = _abs_ready(target)
    if not ready:
        return 0
    url, token, libs = ready
    item = _find_item(url, token, libs, folder)
    if not item:
        return 0
    try:
        import json as _json
        import urllib.request as _u
        req = _u.Request(f"{url}/api/items/{item['id']}?expanded=1",
                         headers={"Authorization": f"Bearer {token}"})
        with _u.urlopen(req, timeout=20) as r:
            full = _json.loads(r.read())
        chapters = chapters_from(_read_manifest(session).get("turns", []),
                                 full.get("media", {}).get("tracks") or [])
        if not chapters:
            return 0
        body = _json.dumps({"chapters": chapters}).encode()
        req = _u.Request(f"{url}/api/items/{item['id']}/chapters", data=body,
                         method="POST",
                         headers={"Authorization": f"Bearer {token}",
                                  "Content-Type": "application/json"})
        with _u.urlopen(req, timeout=20):
            pass
        return len(chapters)
    except Exception as e:  # noqa: BLE001
        log.warning("book-tracks: chapters failed (%s)", e)
        return 0


def conversation_log(session: str, folder: Path, *, target=None) -> list:
    """The conversation as lines you can read. `[{start, end, who, text}]`.

    A chapter title is one sentence, because a table of contents is for finding
    your place. Reading what was actually said needs the whole turn, and the
    whole turn is in speech history — the manifest keeps only what it needs to
    name a file. So this joins the two: the manifest and the server's tracks
    give each turn its position in the item, history gives it its words.

    Read-only, and derived on demand. It is not a second copy of the
    conversation to be kept in step with the audio; it is the same rows,
    rendered.
    """
    turns = _read_manifest(session).get("turns", [])
    if not turns:
        return []

    positions = []
    ready = _abs_ready(target)
    if ready:
        url, token, libs = ready
        item = _find_item(url, token, libs, folder)
        if item:
            try:
                import json as _json
                import urllib.request as _u
                req = _u.Request(f"{url}/api/items/{item['id']}?expanded=1",
                                 headers={"Authorization": f"Bearer {token}"})
                with _u.urlopen(req, timeout=20) as r:
                    full = _json.loads(r.read())
                positions = chapters_from(
                    turns, (full.get("media") or {}).get("tracks") or [])
            except Exception as e:  # noqa: BLE001 — a log without times still reads
                log.warning("book-tracks: no track offsets for the log (%s)", e)

    # History keyed by the same `at` the manifest recorded, so a turn whose
    # audio has been swept from the cache still has its words here.
    said = {}
    for t in session_feed.turns(session):
        said[round(float(t.at), 3)] = t

    out = []
    for n, turn in enumerate(turns):
        at = round(float(turn.get("at") or 0.0), 3)
        spoken = said.get(at)
        text = (spoken.text if spoken else turn.get("title") or "").strip()
        who = "you" if (spoken and spoken.listener) else "agent"
        if who == "you" and text.startswith("You: "):
            # The label belongs to the chapter title, where there is no other
            # way to tell the sides apart. Here the side is its own field.
            text = text[len("You: "):]
        pos = positions[n] if n < len(positions) else {}
        out.append({"start": pos.get("start"), "end": pos.get("end"),
                    "who": who, "text": text})
    return out


#: Who a conversation is by. The workspace used to end up here, because it is
#: the folder above and that is where Audiobookshelf looks — which filled the
#: Authors shelf with tmux session names. Override with MEDIA_CONVERSATION_AUTHOR.
CONVERSATION_AUTHOR = "Claude"


def _series_position(session: str, folder: Path) -> str:
    """Where this conversation comes in its workspace, by date. 1-based.

    Recomputed rather than stored: a conversation exported out of order would
    otherwise keep a number that no longer describes it, and renumbering costs
    nothing because the number is only ever read as an ordering.
    """
    workspace = folder.parent.name
    rows = []
    for p in sorted((state_dir() / "book-tracks").glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        other = Path(str(data.get("folder") or ""))
        if other.parent.name != workspace:
            continue
        turns = data.get("turns") or []
        rows.append((float((turns[0].get("at") if turns else 0) or 0),
                     str(data.get("session") or p.stem)))
    rows.sort()
    for n, (_at, sid) in enumerate(rows, 1):
        if sid == session:
            return str(n)
    return ""


def set_metadata(session: str, folder: Path, *, target=None) -> str:
    """Describe the item the way a library should. "" if nothing changed.

    Three fields, and the reason for each:

    * **title** — the conversation, without the workspace. The workspace is the
      folder above, which Audiobookshelf reads as the author, so a title of
      "<workspace> - <question>" said it twice on every shelf.
    * **series** — the workspace, numbered by date. A workspace is an ordered
      set of related items, which is what a series *is*; a collection would
      have to be maintained by hand and would not sort.
    * **author** — Claude, rather than a tmux session name. Deriving the author
      from the folder filled the Authors shelf with things that are not
      authors, and left the field saying nothing true.

    Folders are never renamed for this — a renamed folder is a new item, with
    no progress and the old one stranded — so all of it is metadata, re-applied
    on every publish, which means a rescan that reverts it corrects itself on
    the next turn.
    """
    servers = _abs_ready_all(target)
    if not servers:
        return ""
    turns = session_feed.turns(session)
    title = session_feed.asked_for(session, turns) if turns else ""
    if not title:
        return ""
    workspace = folder.parent.name
    sequence = _series_position(session, folder)
    author = os.environ.get("MEDIA_CONVERSATION_AUTHOR") or CONVERSATION_AUTHOR

    # Fan out: each server is its own item, its own token. One that lacks the
    # item yet, or will not answer, is skipped rather than failing the rest, so
    # a freshly-added second instance catches up on its next scan.
    described = False
    for url, token, libs in servers:
        item = _find_item(url, token, libs, folder)
        if not item:
            continue
        md = ((_abs_item(url, token, item["id"]) or {}).get("media") or {}).get("metadata") or {}
        have_series = [(x.get("name"), str(x.get("sequence") or ""))
                       for x in (md.get("series") or [])]
        have_authors = [a.get("name") for a in (md.get("authors") or [])]
        if (md.get("title") == title
                and have_series == [(workspace, sequence)]
                and have_authors == [author]):
            described = True
            continue
        try:
            _abs_patch(url, token, f"/api/items/{item['id']}/media",
                       {"metadata": {
                           "title": title,
                           # Arrays, not the seriesName/authorName fields: those
                           # are the read side of the same thing and a write to
                           # them is accepted and ignored.
                           "series": [{"name": workspace, "sequence": sequence}],
                           "authors": [{"name": author}]}})
            described = True
        except Exception as e:  # noqa: BLE001 — metadata is not worth a failure
            log.warning("book-tracks: could not describe %s on %s (%s)",
                        folder.name, url, e)
    return title if described else ""


def reopen(folder: Path, *, target=None) -> Optional[str]:
    """Bring a grown item back into Continue Listening. Item id, or None.

    None covers every ordinary case as well as failure: no Audiobookshelf
    configured, no item scanned for this folder yet, or a listener who had not
    finished it — that last one needs nothing done, because a position mid-item
    already survives the append on its own.
    """
    import json as _json
    import urllib.request as _u

    ready = _abs_ready(target)
    if not ready:
        return None
    url, token, libs = ready
    try:
        item = _find_item(url, token, libs, folder)
        if not item:
            return None

        req = _u.Request(f"{url}/api/me/progress/{item['id']}",
                         headers={"Authorization": f"Bearer {token}"})
        try:
            with _u.urlopen(req, timeout=15) as r:
                prog = _json.loads(r.read() or b"{}")
        except _u.HTTPError as e:
            if e.code == 404:
                # Nobody has played it. The commonest answer of all, and the
                # one that used to be logged as a failure every half hour.
                return None
            raise
        if not prog.get("isFinished"):
            return None

        at = float(prog.get("currentTime") or 0.0)
        duration = float(item.get("media", {}).get("duration") or 0.0)
        _abs_patch(url, token, f"/api/me/progress/{item['id']}",
                   {"isFinished": False})
        body = {"currentTime": at}
        if duration > 0:
            body["duration"] = duration
            body["progress"] = min(1.0, at / duration) if duration else 0.0
        _abs_patch(url, token, f"/api/me/progress/{item['id']}", body)
        log.info("book-tracks: reopened %s at %.0fs of %.0fs",
                 folder.name, at, duration)
        return item["id"]
    except Exception as e:  # noqa: BLE001 - a library that will not answer is not this job's problem
        log.warning("book-tracks: reopen failed (%s)", e)
        return None
