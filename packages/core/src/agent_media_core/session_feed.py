"""A conversation, as one episode.

The speech channel already writes everything this needs. A finished turn
records its text, every clip it rendered, each clip's sentence and each
clip's duration (`intake/submit.py`, the history extras). So an episode is not
a synthesis job — it is a concatenation, and the chapter marks come from the
turn boundaries that are already in the data.

    media feed session            the conversation this pane is holding
    media feed session <id>       any conversation, by Claude session id

## Why one episode per conversation

A turn is thirty seconds. Nobody subscribes to that, and a client showing four
hundred of them is worse than useless. The conversation is the unit someone
actually wants back — and `extras.source_session` is the only honest boundary
for it: a tmux session holds several conversations, and one conversation moves
panes when it resumes.

Each turn becomes a chapter titled with its first sentence, so the thing a
client's chapter list gives you is a table of contents for the conversation:
skip to the bit where you asked about the ringer.

## Stream copy, verified

The clips are already mp3 at one engine's settings, so `-c copy` is seconds of
work rather than minutes of re-encoding. But a session that fell back to
another engine mid-conversation (`extras.fallback`) can hold clips at two
sample rates, and concatenating *those* by copy produces a file whose header
lies about its own length — it plays, and every chapter mark after the join
points at the wrong moment.

So the result is measured against the sum of the parts, and re-encoded only
when they disagree. The common case stays fast; the broken case does not ship.

## Publishing is re-publishing

The guid is the session id, so a conversation still going can be published now
and again later: the second run replaces the first with a longer episode. That
is the intended way to use it — there is no need to wait for a conversation to
end, and no way to end up with three overlapping copies of one afternoon.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import feed as feedmod
from .docs import _ffprobe_duration, _write_chapter_metadata

log = logging.getLogger(__name__)

#: How far back to look for a conversation's turns. Speech history is shared
#: by every conversation on the host, so scoping to one means over-fetching:
#: a busy day interleaves hundreds of other clips between this session's.
_FETCH = 4000

#: Chapters listed in the episode description. High enough that the cap is
#: almost never reached: it was 40, and a long afternoon runs past that, so
#: the line saying how many were dropped became a routine sight — and it is
#: text, so there is nothing to click. A chapter line is ~60 bytes; two
#: hundred of them is a feed 12KB larger, which is nothing against a client
#: that polls hourly.
_NOTES_CHAPTERS = 200


@dataclass
class Turn:
    at: float
    text: str
    clips: list = field(default_factory=list)        # list[Path]
    durations: list = field(default_factory=list)    # list[float]
    #: The tmux session this turn was spoken from, when the row recorded one.
    workspace: str = ""
    #: One sentence per clip, when the row recorded the map and it still
    #: lines up with the clips that survived. Empty otherwise — a caller that
    #: wants to name a clip has to cope with not being told.
    sentences: list = field(default_factory=list)
    #: True when the listener typed this turn from the player rather than the
    #: assistant speaking it. A conversation has two sides; this is which.
    listener: bool = False
    #: The reply's dedup key, when the row recorded one. It is what the visual
    #: channel remembers its pushes by, so it is how a turn finds its picture.
    key: str = ""
    #: A multiple-choice question, when this turn was one:
    #: `[{question, options: [{label, description}], multiSelect}]`. The words
    #: were spoken with a location label and the options run together, so a
    #: reader wants the structure rather than the sentence.
    ask: list = field(default_factory=list)

    @property
    def title(self) -> str:
        """The turn's first sentence, as a chapter name."""
        first = " ".join((self.text or "").split())
        for stop in (". ", "? ", "! "):
            i = first.find(stop)
            if 0 < i < 90:
                return first[:i + 1].strip()
        return (first[:87] + "…") if len(first) > 90 else first or "…"


def turns(session: str, *, store=None) -> list[Turn]:
    """This conversation's spoken turns, oldest first.

    Three kinds of row are left out, and none of them is a judgement call:

    - **alerts** (`extras.kind == "notif"`) — "Claude is waiting" is not part
      of the conversation, and the popup's own traversal excludes them for the
      same reason. A *question* is the exception: it rides the same alert lane
      (it is spoken by the PreToolUse hook, not by a reply) but it is the
      assistant's own turn and the answer that follows makes no sense without
      it, so a row carrying `extras.ask` stays;
    - **rows with no audio** — a `silenced:` row records that something was
      *not* said aloud, and there is nothing to concatenate;
    - **clips the cache has swept** — the row outlives the file (that is the
      whole reason this feature exists), so every path is checked and a turn
      whose audio is entirely gone is dropped rather than faked.

    A turn that was rendered but never heard — muted pane, flushed by a
    barge-in — *is* included. The words were written and spoken by the
    renderer; whether the room was listening is not what the archive is about.
    """
    from .state.store import StateStore

    st = store or StateStore()
    out: list[Turn] = []
    for row in st.recent_history(sink="speech", limit=_FETCH):
        ex = row.get("extras")
        if not isinstance(ex, dict) or ex.get("source_session") != session:
            continue
        # `ask` was a bare True before the question kept its options, and a
        # row from then has no structure to show and an audio clip the shelf
        # never placed — letting those in appends a months-old question to the
        # end of an item. Only a structured one is a turn.
        if ex.get("kind") == "notif" and not isinstance(ex.get("ask"), list):
            continue
        uris = ex.get("clip_uris") or ([row["uri"]] if row.get("uri") else [])
        durs = list(ex.get("clip_durations_s") or [])
        said = list(ex.get("clip_sentences") or [])
        clips, kept, lines = [], [], []
        for i, u in enumerate(uris):
            p = Path(str(u))
            if not p.is_file():
                continue
            clips.append(p)
            kept.append(durs[i] if i < len(durs) else _ffprobe_duration(p))
            # Carried per surviving clip, not per original: a clip the cache
            # swept must take its sentence with it, or every later line names
            # the wrong audio.
            lines.append(said[i] if i < len(said) else "")
        if not clips:
            continue
        out.append(Turn(at=float(row.get("started_at") or 0),
                        text=(row.get("text") or ""),
                        clips=clips, durations=kept,
                        sentences=(lines if any(lines) else []),
                        listener=bool(ex.get("listener")),
                        key=str(ex.get("dedup_key") or ""),
                        ask=(ex.get("ask") if isinstance(ex.get("ask"), list) else []),
                        workspace=(ex.get("source_tmux_session") or "").strip()))
    out.sort(key=lambda t: t.at)
    return out


def workspace_for(session: str, ts: list[Turn]) -> str:
    """Which tmux session this conversation belongs to, or "".

    A day's conversations are not a flat list: they happen in workspaces —
    `p-agent-media`, `org-alert`, `scratch` — and several run at once. A client
    showing twenty episodes titled only by their opening question makes you
    read all twenty to find the one from the project you were in.

    The commonest name across the turns, not the first: a conversation resumed
    in another pane carries the new session for its later turns, and the
    workspace it mostly lived in is the truer label.

    Falls back to the transcript's project directory, which is the *cwd* rather
    than the tmux session but is the same idea and survives rows that recorded
    no tmux name at all (everything the phone rendered before 2026-07).
    """
    from collections import Counter

    names = Counter(t.workspace for t in ts if t.workspace)
    if names:
        return names.most_common(1)[0][0]

    from .conversation import transcript

    path = transcript(session)
    if path is None:
        return ""
    # `~/.claude/projects/-home-ryer-projects-agent-media/<id>.jsonl`: the cwd
    # with every `/` turned into `-`, which cannot be decoded exactly (project
    # names contain hyphens too). The tail after `projects-` is right far more
    # often than it is wrong, and a wrong guess here costs a label, not a file.
    #
    # The leading `-` is what makes this recognisable as an encoded path rather
    # than some other directory a transcript happens to sit in — without that
    # check any parent directory becomes a workspace name.
    dirname = path.parent.name
    if not dirname.startswith("-"):
        return ""
    _, sep, tail = dirname.partition("projects-")
    # Only a project earns a label. Without that segment the encoded path is a
    # home directory or a scratch cwd, and "home-ryer · " on the front of every
    # episode is a prefix that distinguishes nothing.
    return tail.strip() if sep and tail else ""


def title_for(session: str, ts: list[Turn]) -> str:
    """What to call the episode: the workspace, then what was asked in it.

    `p-agent-media · I wonder how calibre would go` — the tmux session someone
    would recognise, then the Claude conversation inside it. Both, because
    either alone is ambiguous in a client's episode list: the workspace repeats
    across a dozen episodes, and the question does not say where it happened.

    The question itself is the first thing the person asked, which is what they
    will remember about the conversation — the reply's opening sentence is a
    worse name, and a session id is no name at all.

    Read straight from the transcript rather than from any index: the file is
    named for the session, so there is one place to look and a hit in it is
    proof (`conversation.transcript`).
    """
    from .conversation import transcript

    return _joined(workspace_for(session, ts), _asked(session, ts))


#: How much of the question survives into a title. Shorter than the old cap:
#: the workspace now takes room, and a client's episode list is one line.
_ASK_CHARS = 64


def _joined(workspace: str, asked: str) -> str:
    return f"{workspace} · {asked}" if workspace else asked


def _trim(text: str) -> str:
    """A label, not a paragraph. Breaks at a word: a title that stops
    mid-word reads as a truncation bug rather than a summary."""
    text = " ".join((text or "").split())
    if len(text) <= _ASK_CHARS + 3:
        return text
    cut = text[:_ASK_CHARS].rsplit(" ", 1)[0] or text[:_ASK_CHARS]
    return cut.rstrip(" ,.;:") + "…"


def asked_for(session: str, ts: list[Turn]) -> str:
    """What this conversation is *about*, with no workspace attached.

    `title_for` glues the workspace on because a podcast client shows one flat
    list and the question alone does not say where it happened. A library that
    files items under the workspace already answers that, and repeating it in
    the title is just noise — so that shelf asks for this instead.
    """
    return _asked(session, ts)


def _asked(session: str, ts: list[Turn]) -> str:
    """What to call this conversation.

    **Claude Code's own name for it, when it has one.** The transcript carries
    `ai-title` records — the name shown in the resume list — and a conversation
    called "Calibre speech channel interface" is the one you would look for. The
    opening question is a poor substitute: it is whatever was typed before
    anybody knew where the afternoon would go, and it is long.

    The *last* `ai-title`, because the name is rewritten as the conversation
    turns out to be about something else. Falling back to the first thing the
    person asked, then to the first thing that was said.

    One pass, and a substring check before parsing each line: these transcripts
    run to tens of megabytes, most of it attachments this never looks at.
    """
    from .conversation import transcript

    path = transcript(session)
    title = prompt = ""
    if path is not None:
        try:
            with path.open(errors="replace") as fh:
                for line in fh:
                    if '"ai-title"' in line:
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        title = (d.get("aiTitle") or "").strip() or title
                        continue
                    # `"user"` rather than `"type":"user"`: the transcripts
                    # this reads are written without spaces after the colons,
                    # but nothing promises that, and a fast path that depends
                    # on JSON formatting is a fast path that silently stops
                    # finding anything.
                    if prompt or '"user"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("type") != "user":
                        continue
                    content = (d.get("message") or {}).get("content")
                    if isinstance(content, list):      # blocks, not a string
                        content = next((b.get("text") for b in content
                                        if isinstance(b, dict)
                                        and b.get("type") == "text"), None)
                    if not isinstance(content, str):
                        continue
                    text = " ".join(content.split())
                    # Tool results, hook injections and system reminders are
                    # user-role messages too, and none of them is a question
                    # anybody asked.
                    if not text or text.startswith(("<", "Caveat:", "[media ")):
                        continue
                    prompt = text
        except OSError as e:
            log.debug("transcript unreadable for %s: %s", session, e)
    for candidate in (title, prompt, ts[0].title if ts else ""):
        if candidate:
            return _trim(candidate)
    return f"Conversation {session[:8]}"


def notes(ts: list[Turn], limit: int = _NOTES_CHAPTERS) -> str:
    """The episode description: a timestamped table of contents, in HTML.

    A client shows one text field, and the useful thing to put in it for a
    conversation is where in the hour each part is — the same information as
    the chapter marks, for the clients that don't read them.

    One paragraph per chapter, because a description is rendered as HTML:
    newlines collapse, and a list joined by them arrives in Audiobookshelf as
    one unbroken wall of text with the timestamps buried inside it.
    """
    from xml.sax.saxutils import escape

    lines, clock = [], 0.0
    for t in ts:
        lines.append(f"<p>{feedmod.hms(clock)} — {escape(t.title)}</p>")
        clock += sum(t.durations)
    # Newest first. A conversation is read forwards but *scanned* backwards:
    # what you are looking for in one you had this afternoon is near the end,
    # and that is also the half a cap should never be the one to drop.
    lines.reverse()
    if len(lines) > limit:
        dropped = len(lines) - limit
        lines = lines[:limit] + [f"<p>… and {dropped} earlier</p>"]
    return "\n".join(lines)


def build(ts: list[Turn], out: Path) -> Optional[Path]:
    """Concatenate the turns into `out`, one chapter each. None if it can't.

    The source clips are the render cache's, and are left exactly where they
    are: other things replay them, and this is a copy of the conversation, not
    a move of it.
    """
    if not ts:
        return None
    chapters, clock = [], 0.0
    for t in ts:
        dur = sum(t.durations) or sum(_ffprobe_duration(c) for c in t.clips)
        chapters.append((t.title, clock, clock + dur))
        clock += dur
    expected = clock

    with tempfile.TemporaryDirectory(prefix="media-episode-") as tmp:
        work = Path(tmp)
        listing = work / "parts.txt"
        listing.write_text("".join(f"file '{c.as_posix()}'\n"
                                   for t in ts for c in t.clips))
        meta = work / "chapters.txt"
        _write_chapter_metadata(meta, chapters)

        def _run(codec: list[str]) -> bool:
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-f", "concat", "-safe", "0", "-i", str(listing),
                     "-i", str(meta), "-map_metadata", "1", *codec, str(out)],
                    check=True, capture_output=True, timeout=1800)
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("episode: ffmpeg failed (%s)", e)
                return False
            return out.exists() and out.stat().st_size > 0

        if not _run(["-c", "copy"]):
            return None
        # Mixed sample rates concatenate into a file that plays and lies about
        # its length; every chapter after the join then points at the wrong
        # moment. Measuring is the only way to tell, and it is one ffprobe.
        got = _ffprobe_duration(out)
        if expected > 0 and abs(got - expected) > max(2.0, expected * 0.02):
            log.info("episode: %s ≠ %s expected — re-encoding", got, expected)
            if not _run(["-c:a", "libmp3lame", "-q:a", "4"]):
                return None
    return out


def conversations(store=None) -> list[dict]:
    """Every conversation speech history knows about, newest last turn first.

    One pass over the same rows `turns` reads, grouped — the listing and the
    auto-publisher both want "what is there", and neither wants it per-session
    (which would be one full scan per conversation).
    """
    from collections import Counter

    from .state.store import StateStore

    st = store or StateStore()
    seen: dict = {}
    for row in st.recent_history(sink="speech", limit=_FETCH):
        ex = row.get("extras")
        if not isinstance(ex, dict) or ex.get("kind") == "notif":
            continue
        sess = ex.get("source_session")
        if not sess:
            continue
        at = float(row.get("started_at") or 0)
        cur = seen.setdefault(sess, {"session": sess, "turns": 0,
                                     "first": at, "last": at,
                                     "workspaces": Counter()})
        cur["turns"] += 1
        cur["first"] = min(cur["first"], at)
        cur["last"] = max(cur["last"], at)
        ws = (ex.get("source_tmux_session") or "").strip()
        if ws:
            cur["workspaces"][ws] += 1
    out = []
    for c in seen.values():
        c["workspace"] = (c["workspaces"].most_common(1)[0][0]
                          if c["workspaces"] else "")
        c.pop("workspaces")
        out.append(c)
    return sorted(out, key=lambda c: -c["last"])


#: The feed a conversation with no workspace goes to. Everything else gets a
#: feed named for its tmux session, so a podcast client's subscription list is
#: the project list — `p-agent-media`, `scratch` — rather than one long stream.
DEFAULT_FEED = "talks"


def feed_for(workspace: str) -> str:
    """The feed name for a workspace.

    Sanitised to what a feed directory and a URL path segment may contain, so a
    tmux session someone named `~work/thing` cannot become a path.
    """
    safe = "".join(c if c in feedmod._SAFE_NAME else "-"
                   for c in (workspace or "").strip().lower())
    safe = "-".join(part for part in safe.split("-") if part)
    return safe or DEFAULT_FEED


#: Below this, a conversation is not an episode. Two seconds of "reply with
#: exactly: local backup works" in a client's list is noise around the things
#: worth finding — and `media feed session <id>` still publishes one by hand.
MIN_EPISODE_S = 30.0


def publish_quiet(*, name: Optional[str] = None, quiet_s: float = 3600.0,
                  now: Optional[float] = None, store=None,
                  limit: int = 0, min_s: float = MIN_EPISODE_S
                  ) -> list[feedmod.Episode]:
    """Publish every conversation that has finished and isn't on the feed yet.

    "Finished" is silence: no turn for `quiet_s`. There is no event for a
    conversation ending — a session id stays valid, a pane stays open, and
    people come back to yesterday's — so quiet is the only signal, and an hour
    of it is a long time in a conversation.

    A session already published is republished only if it has *grown* since.
    That keeps the spool honest about the conversation that actually happened;
    subscribers that already downloaded the shorter episode keep it, because
    every client matches on guid and none re-fetches. The alternative — never
    republishing — loses the tail of any conversation that revives, which is
    worse and silent.
    """
    now = time.time() if now is None else now
    out: list[feedmod.Episode] = []
    # Across every feed, not just one: an episode lives in its workspace's
    # feed, so asking "have I published this" of a single feed would republish
    # everything else on every run.
    published = {e.guid: e for f in feedmod.feeds() for e in feedmod.episodes(f)}
    for conv in conversations(store=store):
        if now - conv["last"] < quiet_s:
            continue                       # still going, or paused mid-thought
        have = published.get(f"session:{conv['session']}")
        if have is not None and have.published >= conv["last"]:
            continue                       # already on the feed, unchanged
        ep = publish(conv["session"], name=name, store=store)
        if ep is None:
            continue                       # no turns whose audio survives
        if min_s and ep.duration_s and ep.duration_s < min_s:
            feedmod.remove(_feed_of(ep, name), ep.guid)
            log.info("skipped %s (%.0fs)", ep.title, ep.duration_s)
            continue
        log.info("published %s (%s)", ep.title, conv["session"])
        out.append(ep)
        if limit and len(out) >= limit:
            break
    return out


def _feed_of(ep: feedmod.Episode, requested: Optional[str]) -> str:
    """Which feed an episode actually landed in — not necessarily the one
    asked for, now that the workspace chooses."""
    if requested:
        return requested
    for f in feedmod.feeds():
        if any(e.guid == ep.guid for e in feedmod.episodes(f)):
            return f
    return DEFAULT_FEED


def publish(session: str, *, name: Optional[str] = None,
            store=None) -> Optional[feedmod.Episode]:
    """Build this conversation and put it on the feed. None if there is none.

    Published at the conversation's *last* turn, not now: that is when the
    episode became what it is, and it puts an afternoon you are archiving
    tonight where you would look for it rather than at the top of the list.
    """
    ts = turns(session, store=store)
    if not ts:
        return None
    workspace = workspace_for(session, ts)
    name = name or feed_for(workspace)
    with tempfile.TemporaryDirectory(prefix="media-episode-") as tmp:
        built = build(ts, Path(tmp) / f"{session}.mp3")
        if built is None:
            return None
        return feedmod.publish(
            name, built, guid=f"session:{session}",
            title=title_for(session, ts), description=notes(ts),
            published=max(t.at for t in ts), source=session)
