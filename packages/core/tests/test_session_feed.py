"""A conversation, as one episode.

Nothing here synthesises anything: the clips, their durations and the turn
boundaries are already in speech history. So the tests are about what gets
left out (alerts, rows with no audio, clips the cache has swept), what the
turn boundaries become (chapters), and the one thing a stream copy can get
wrong (a join between two sample rates).
"""

import json
import shutil
import subprocess

import pytest

from agent_media_core import feed, session_feed
from agent_media_core.session_feed import Turn

SESSION = "1111-2222"


class _Store:
    """Just enough StateStore to answer the one question this asks."""

    def __init__(self, rows):
        self._rows = rows

    def recent_history(self, *, sink=None, limit=0):
        return list(self._rows)


def _row(clips, *, at=100.0, text="Something was said.", session=SESSION,
         durs=None, **extras):
    return {"uri": str(clips[0]) if clips else "", "started_at": at,
            "text": text,
            "extras": {"source_session": session,
                       "clip_uris": [str(c) for c in clips],
                       "clip_durations_s": durs if durs is not None
                       else [1.0] * len(clips),
                       **extras}}


@pytest.fixture
def clip(tmp_path):
    def _make(name):
        p = tmp_path / name
        p.write_bytes(b"ID3fake")
        return p
    return _make


# --- which turns are in it -------------------------------------------------

def test_turns_come_back_oldest_first_with_their_clips(clip):
    a, b = clip("a.mp3"), clip("b.mp3")
    store = _Store([_row([b], at=200.0, text="Second."),
                    _row([a], at=100.0, text="First.")])
    ts = session_feed.turns(SESSION, store=store)
    assert [t.text for t in ts] == ["First.", "Second."]
    assert ts[0].clips == [a]


def test_another_conversation_is_not_in_this_episode(clip):
    store = _Store([_row([clip("a.mp3")]),
                    _row([clip("b.mp3")], session="other")])
    assert len(session_feed.turns(SESSION, store=store)) == 1


def test_alerts_are_not_part_of_the_conversation(clip):
    """"Claude is waiting" is not something anyone wants back."""
    store = _Store([_row([clip("a.mp3")]),
                    _row([clip("b.mp3")], kind="notif")])
    assert len(session_feed.turns(SESSION, store=store)) == 1


def test_a_row_with_no_audio_is_skipped(clip):
    """A silenced alert records that something was *not* said aloud."""
    store = _Store([{"uri": "silenced:phone", "started_at": 1.0, "text": "x",
                     "extras": {"source_session": SESSION}},
                    _row([clip("a.mp3")])])
    assert len(session_feed.turns(SESSION, store=store)) == 1


def test_clips_the_cache_has_swept_are_dropped(clip, tmp_path):
    """The row outliving its audio is the whole reason the feed exists."""
    a = clip("a.mp3")
    gone = tmp_path / "swept.mp3"
    store = _Store([_row([a, gone], durs=[1.0, 2.0])])
    ts = session_feed.turns(SESSION, store=store)
    assert ts[0].clips == [a] and ts[0].durations == [1.0]


def test_a_turn_with_no_surviving_audio_is_dropped_not_faked(tmp_path):
    store = _Store([_row([tmp_path / "swept.mp3"])])
    assert session_feed.turns(SESSION, store=store) == []


def test_a_turn_nobody_heard_is_still_in_the_archive(clip):
    """Muted or flushed: the words were written and rendered. Whether the room
    was listening is not what the archive is about."""
    store = _Store([_row([clip("a.mp3")], muted=True),
                    _row([clip("b.mp3")], at=200.0, flushed=True)])
    assert len(session_feed.turns(SESSION, store=store)) == 2


# --- chapters and notes ----------------------------------------------------

@pytest.mark.parametrize("text,want", [
    ("Pushed — b93255d..5968656 on main. Then more.", "Pushed — b93255d..5968656 on main."),
    ("Is that right? Yes.", "Is that right?"),
    ("no punctuation at all here", "no punctuation at all here"),
    ("x" * 200, "x" * 87 + "…"),
])
def test_a_chapter_is_named_by_the_turn_s_first_sentence(text, want):
    assert Turn(at=0, text=text).title == want


def test_notes_are_a_timestamped_table_of_contents():
    ts = [Turn(at=1, text="First thing.", durations=[65.0]),
          Turn(at=2, text="Second thing.", durations=[10.0, 5.0])]
    # One paragraph per chapter (a description is rendered as HTML, so a list
    # joined by newlines arrives as one wall of text), newest first (a
    # conversation is read forwards but scanned backwards).
    assert session_feed.notes(ts) == ("<p>0:01:05 — Second thing.</p>\n"
                                      "<p>0:00:00 — First thing.</p>")


def test_a_long_conversation_s_notes_are_capped():
    ts = [Turn(at=i, text=f"Turn {i}.", durations=[1.0]) for i in range(10)]
    out = session_feed.notes(ts, limit=3)
    assert out.count("<p>") == 4
    # The cap drops the OLDEST, and says so: the recent end of a long
    # conversation is the half worth keeping.
    assert out.startswith("<p>0:00:09 — Turn 9.</p>")
    assert out.endswith("<p>… and 7 earlier</p>")


# --- the title -------------------------------------------------------------

def _transcript(tmp_path, monkeypatch, lines):
    p = tmp_path / f"{SESSION}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    monkeypatch.setattr(session_feed, "turns", session_feed.turns)
    monkeypatch.setattr("agent_media_core.conversation.transcript",
                        lambda s: p)
    return p


def test_the_title_is_the_first_thing_the_person_asked(tmp_path, monkeypatch):
    _transcript(tmp_path, monkeypatch, [
        {"type": "mode", "mode": "normal"},
        {"type": "user", "message": {"content": "I wonder how calibre would go"}},
        {"type": "user", "message": {"content": "and then something else"}},
    ])
    assert session_feed.title_for(SESSION, []) == "I wonder how calibre would go"


def test_injected_user_messages_are_not_the_title(tmp_path, monkeypatch):
    """Tool results, hook injections and system reminders are user-role
    messages too, and none of them is a question anybody asked."""
    _transcript(tmp_path, monkeypatch, [
        {"type": "user", "message": {"content": "<system-reminder>hi</system-reminder>"}},
        {"type": "user", "message": {"content": "Caveat: the messages below"}},
        {"type": "user", "message": {"content": "[media ask] what is playing"}},
        {"type": "user", "message": {"content": "the real question"}},
    ])
    assert session_feed.title_for(SESSION, []) == "the real question"


def test_a_block_shaped_message_still_yields_a_title(tmp_path, monkeypatch):
    _transcript(tmp_path, monkeypatch, [
        {"type": "user", "message": {"content": [
            {"type": "image"}, {"type": "text", "text": "look at this"}]}},
    ])
    assert session_feed.title_for(SESSION, []) == "look at this"


def test_no_transcript_falls_back_to_the_first_thing_said(monkeypatch):
    monkeypatch.setattr("agent_media_core.conversation.transcript",
                        lambda s: None)
    assert session_feed.title_for(SESSION, [Turn(at=1, text="Right, so.")]) \
        == "Right, so."
    assert session_feed.title_for("abcdef123456", []) == "Conversation abcdef12"


# --- building --------------------------------------------------------------

def test_a_stream_copy_that_lies_about_its_length_is_re_encoded(
        tmp_path, clip, monkeypatch):
    """Two sample rates concatenate into a file that plays and mis-times every
    chapter after the join. Measuring is the only way to tell."""
    runs = []

    def _fake_run(cmd, **kw):
        runs.append(cmd)
        out = tmp_path / "out.mp3"
        out.write_bytes(b"joined")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(session_feed.subprocess, "run", _fake_run)
    monkeypatch.setattr(session_feed, "_ffprobe_duration", lambda p: 3.0)
    ts = [Turn(at=1, text="One.", clips=[clip("a.mp3")], durations=[60.0])]
    assert session_feed.build(ts, tmp_path / "out.mp3") is not None
    assert len(runs) == 2
    assert "copy" in runs[0] and "libmp3lame" in runs[1]


def test_a_stream_copy_that_adds_up_is_left_alone(tmp_path, clip, monkeypatch):
    runs = []
    monkeypatch.setattr(session_feed.subprocess, "run",
                        lambda cmd, **kw: (runs.append(cmd),
                                           (tmp_path / "out.mp3").write_bytes(b"x"),
                                           subprocess.CompletedProcess(cmd, 0))[-1])
    monkeypatch.setattr(session_feed, "_ffprobe_duration", lambda p: 60.4)
    ts = [Turn(at=1, text="One.", clips=[clip("a.mp3")], durations=[60.0])]
    assert session_feed.build(ts, tmp_path / "out.mp3") is not None
    assert len(runs) == 1


def test_nothing_to_build_is_none(tmp_path):
    assert session_feed.build([], tmp_path / "out.mp3") is None


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="needs ffmpeg")
def test_the_turn_boundaries_really_become_chapters(tmp_path):
    parts = []
    for i, secs in enumerate((1.0, 2.0)):
        p = tmp_path / f"{i}.mp3"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"anullsrc=r=24000:cl=mono", "-t", str(secs),
                        str(p)], check=True, capture_output=True)
        parts.append(p)
    ts = [Turn(at=1, text="First turn.", clips=[parts[0]], durations=[1.0]),
          Turn(at=2, text="Second turn.", clips=[parts[1]], durations=[2.0])]
    out = session_feed.build(ts, tmp_path / "episode.mp3")
    assert out is not None
    chapters = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True).stdout
    assert "First turn." in chapters and "Second turn." in chapters
    assert len(chapters.strip().splitlines()) == 2


# --- publishing ------------------------------------------------------------

def test_publishing_uses_the_session_as_its_guid(tmp_path, clip, monkeypatch):
    monkeypatch.setenv("MEDIA_FEED_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.setattr("agent_media_core.conversation.transcript", lambda s: None)
    monkeypatch.setattr(feed, "_probe_duration", lambda p: 3.0)

    built = tmp_path / "episode.mp3"

    def _fake_build(ts, out):
        out.write_bytes(b"episode audio")
        return out

    monkeypatch.setattr(session_feed, "build", _fake_build)
    store = _Store([_row([clip("a.mp3")], at=100.0, text="First."),
                    _row([clip("b.mp3")], at=900.0, text="Last.")])
    ep = session_feed.publish(SESSION, store=store)
    assert ep is not None
    assert ep.guid == f"session:{SESSION}"
    # Dated when the conversation last spoke, not when it was archived.
    assert ep.published == 900.0
    assert ep.title == "First."
    assert not built.exists()          # the working copy does not survive


def test_publishing_a_session_with_nothing_left_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_FEED_SPOOL", str(tmp_path / "spool"))
    assert session_feed.publish(SESSION, store=_Store([])) is None


# --- publishing what has finished -------------------------------------------

def _conv_rows(clipmaker, session, at, n=1):
    return [_row([clipmaker(f"{session}-{i}.mp3")], at=at + i, session=session)
            for i in range(n)]


def test_only_conversations_that_have_gone_quiet_are_published(tmp_path, clip,
                                                               monkeypatch):
    monkeypatch.setenv("MEDIA_FEED_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.setattr("agent_media_core.conversation.transcript", lambda s: None)
    monkeypatch.setattr(feed, "_probe_duration", lambda p: 300.0)
    monkeypatch.setattr(session_feed, "build",
                        lambda ts, out: (out.write_bytes(b"x"), out)[1])
    now = 1_000_000.0
    store = _Store(_conv_rows(clip, "done", now - 7200)
                   + _conv_rows(clip, "live", now - 60))

    eps = session_feed.publish_quiet(now=now, store=store)
    assert [e.guid for e in eps] == ["session:done"]


def test_a_published_conversation_is_not_published_again(tmp_path, clip,
                                                         monkeypatch):
    monkeypatch.setenv("MEDIA_FEED_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.setattr("agent_media_core.conversation.transcript", lambda s: None)
    monkeypatch.setattr(feed, "_probe_duration", lambda p: 300.0)
    monkeypatch.setattr(session_feed, "build",
                        lambda ts, out: (out.write_bytes(b"x"), out)[1])
    now = 1_000_000.0
    rows = _conv_rows(clip, "done", now - 7200)
    assert len(session_feed.publish_quiet(now=now, store=_Store(rows))) == 1
    assert session_feed.publish_quiet(now=now, store=_Store(rows)) == []


def test_a_conversation_that_grew_is_published_again(tmp_path, clip, monkeypatch):
    """A session that revives and then goes quiet again would otherwise keep
    the shorter episode forever — losing the tail, silently."""
    monkeypatch.setenv("MEDIA_FEED_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.setattr("agent_media_core.conversation.transcript", lambda s: None)
    monkeypatch.setattr(feed, "_probe_duration", lambda p: 300.0)
    monkeypatch.setattr(session_feed, "build",
                        lambda ts, out: (out.write_bytes(b"x"), out)[1])
    now = 1_000_000.0
    rows = _conv_rows(clip, "done", now - 7200)
    session_feed.publish_quiet(now=now, store=_Store(rows))
    rows += _conv_rows(clip, "done", now - 5400, n=2)
    eps = session_feed.publish_quiet(now=now, store=_Store(rows))
    assert [e.guid for e in eps] == ["session:done"]
    assert len(feed.episodes("talks")) == 1        # replaced, not duplicated


def test_conversations_are_grouped_newest_last_turn_first(clip):
    store = _Store(_conv_rows(clip, "old", 100.0, n=2)
                   + _conv_rows(clip, "new", 900.0))
    convs = session_feed.conversations(store=store)
    assert [c["session"] for c in convs] == ["new", "old"]
    assert convs[1]["turns"] == 2


def test_an_alert_does_not_make_a_conversation(clip):
    store = _Store([_row([clip("a.mp3")], session="alerts-only", kind="notif")])
    assert session_feed.conversations(store=store) == []


# --- naming an episode so a client's list is scannable ----------------------
# A day's conversations are not a flat list: they happen in tmux workspaces,
# several at once. Twenty episodes titled only by their opening question means
# reading all twenty to find the one from the project you were in.


def test_the_title_names_the_workspace_then_the_question(monkeypatch, tmp_path):
    p = tmp_path / f"{SESSION}.jsonl"
    p.write_text(json.dumps(
        {"type": "user", "message": {"content": "why is the ringer loud"}}) + "\n")
    monkeypatch.setattr("agent_media_core.conversation.transcript", lambda s: p)
    ts = [Turn(at=1, text="x", workspace="p-agent-media")]
    assert session_feed.title_for(SESSION, ts) == \
        "p-agent-media · why is the ringer loud"


def test_no_workspace_leaves_the_question_alone(monkeypatch, tmp_path):
    p = tmp_path / f"{SESSION}.jsonl"
    p.write_text(json.dumps(
        {"type": "user", "message": {"content": "why is the ringer loud"}}) + "\n")
    monkeypatch.setattr("agent_media_core.conversation.transcript", lambda s: p)
    assert session_feed.title_for(SESSION, []) == "why is the ringer loud"


def test_the_commonest_workspace_wins_not_the_first():
    """A conversation resumed in another pane carries the new session for its
    later turns; the workspace it mostly lived in is the truer label."""
    ts = [Turn(at=1, text="a", workspace="scratch"),
          Turn(at=2, text="b", workspace="p-agent-media"),
          Turn(at=3, text="c", workspace="p-agent-media")]
    assert session_feed.workspace_for(SESSION, ts) == "p-agent-media"


def test_a_history_with_no_tmux_name_falls_back_to_the_project(monkeypatch,
                                                               tmp_path):
    """Everything the phone rendered before 2026-07 recorded no tmux session.
    The transcript's directory is the cwd — a different thing, the same idea."""
    d = tmp_path / "-home-ryer-projects-agent-media"
    d.mkdir()
    p = d / f"{SESSION}.jsonl"
    p.write_text("")
    monkeypatch.setattr("agent_media_core.conversation.transcript", lambda s: p)
    assert session_feed.workspace_for(SESSION, []) == "p-agent-media"


def test_a_long_question_is_cut_at_a_word(monkeypatch, tmp_path):
    p = tmp_path / f"{SESSION}.jsonl"
    p.write_text(json.dumps({"type": "user", "message": {"content":
        "I wonder how calibre would go as an interface for the speech channel "
        "and several other things besides"}}) + "\n")
    monkeypatch.setattr("agent_media_core.conversation.transcript", lambda s: p)
    title = session_feed.title_for(SESSION, [Turn(at=1, text="x",
                                                  workspace="p-agent-media")])
    assert title.startswith("p-agent-media · I wonder how calibre")
    assert title.endswith("…")
    assert " " not in title[-3:-1]          # cut at a word, not mid-word
    assert len(title) < 90


def test_conversations_carry_their_workspace(clip):
    rows = [_row([clip("a.mp3")], at=1.0, source_tmux_session="scratch"),
            _row([clip("b.mp3")], at=2.0, source_tmux_session="p-agent-media"),
            _row([clip("c.mp3")], at=3.0, source_tmux_session="p-agent-media")]
    conv = session_feed.conversations(store=_Store(rows))[0]
    assert conv["workspace"] == "p-agent-media"
    assert conv["turns"] == 3


def test_a_home_directory_is_not_a_workspace(monkeypatch, tmp_path):
    """`home-ryer · ` on the front of every episode distinguishes nothing."""
    d = tmp_path / "-home-ryer"
    d.mkdir()
    p = d / f"{SESSION}.jsonl"
    p.write_text("")
    monkeypatch.setattr("agent_media_core.conversation.transcript", lambda s: p)
    assert session_feed.workspace_for(SESSION, []) == ""


def test_a_two_second_conversation_is_not_an_episode(tmp_path, clip, monkeypatch):
    monkeypatch.setenv("MEDIA_FEED_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.setattr("agent_media_core.conversation.transcript", lambda s: None)
    monkeypatch.setattr(session_feed, "build",
                        lambda ts, out: (out.write_bytes(b"x"), out)[1])
    monkeypatch.setattr(feed, "_probe_duration", lambda p: 2.0)
    now = 1_000_000.0
    rows = [_row([clip("a.mp3")], at=now - 7200, session="tiny")]

    assert session_feed.publish_quiet(now=now, store=_Store(rows)) == []
    # And nothing is left behind in the spool from the attempt.
    assert feed.episodes("talks") == []


def test_a_short_conversation_can_still_be_published_by_hand(tmp_path, clip,
                                                             monkeypatch):
    monkeypatch.setenv("MEDIA_FEED_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.setattr("agent_media_core.conversation.transcript", lambda s: None)
    monkeypatch.setattr(session_feed, "build",
                        lambda ts, out: (out.write_bytes(b"x"), out)[1])
    monkeypatch.setattr(feed, "_probe_duration", lambda p: 2.0)
    rows = [_row([clip("a.mp3")], at=1.0, session="tiny")]
    assert session_feed.publish("tiny", store=_Store(rows)) is not None


# --- one feed per workspace -------------------------------------------------
# A podcast client's subscription list becomes the project list, rather than
# one stream holding every conversation of every kind.


@pytest.mark.parametrize("workspace,want", [
    ("p-agent-media", "p-agent-media"),
    ("scratch", "scratch"),
    ("", "talks"),                       # no workspace: the catch-all
    ("Org Alert", "org-alert"),
    ("~work/thing", "work-thing"),       # a name is not a path
    ("../etc", "etc"),
])
def test_the_feed_is_named_for_the_workspace(workspace, want):
    assert session_feed.feed_for(workspace) == want
    assert feed.valid_name(session_feed.feed_for(workspace))


def _publishable(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_FEED_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("MEDIA_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.setattr("agent_media_core.conversation.transcript", lambda s: None)
    monkeypatch.setattr(feed, "_probe_duration", lambda p: 300.0)
    monkeypatch.setattr(session_feed, "build",
                        lambda ts, out: (out.write_bytes(b"x"), out)[1])


def test_a_conversation_lands_in_its_workspace_feed(tmp_path, clip, monkeypatch):
    _publishable(tmp_path, monkeypatch)
    rows = [_row([clip("a.mp3")], at=1.0, session="s1",
                 source_tmux_session="p-agent-media")]
    ep = session_feed.publish("s1", store=_Store(rows))
    assert ep is not None
    assert [e.guid for e in feed.episodes("p-agent-media")] == ["session:s1"]
    assert feed.episodes("talks") == []


def test_a_conversation_with_no_workspace_lands_in_talks(tmp_path, clip,
                                                         monkeypatch):
    _publishable(tmp_path, monkeypatch)
    rows = [_row([clip("a.mp3")], at=1.0, session="s1")]
    session_feed.publish("s1", store=_Store(rows))
    assert [e.guid for e in feed.episodes("talks")] == ["session:s1"]


def test_an_explicit_feed_still_wins(tmp_path, clip, monkeypatch):
    _publishable(tmp_path, monkeypatch)
    rows = [_row([clip("a.mp3")], at=1.0, session="s1",
                 source_tmux_session="p-agent-media")]
    session_feed.publish("s1", name="talks", store=_Store(rows))
    assert [e.guid for e in feed.episodes("talks")] == ["session:s1"]
    assert feed.feeds() == ["talks"]


def test_publish_quiet_sorts_conversations_into_their_own_feeds(tmp_path, clip,
                                                                monkeypatch):
    _publishable(tmp_path, monkeypatch)
    now = 1_000_000.0
    rows = [_row([clip("a.mp3")], at=now - 7200, session="s1",
                 source_tmux_session="p-agent-media"),
            _row([clip("b.mp3")], at=now - 7200, session="s2",
                 source_tmux_session="scratch"),
            _row([clip("c.mp3")], at=now - 7200, session="s3")]
    eps = session_feed.publish_quiet(now=now, store=_Store(rows))
    assert len(eps) == 3
    assert feed.feeds() == ["p-agent-media", "scratch", "talks"]


def test_already_published_is_asked_of_every_feed(tmp_path, clip, monkeypatch):
    """Asking only the feed being written to would republish every other
    workspace's conversations on every run."""
    _publishable(tmp_path, monkeypatch)
    now = 1_000_000.0
    rows = [_row([clip("a.mp3")], at=now - 7200, session="s1",
                 source_tmux_session="p-agent-media"),
            _row([clip("b.mp3")], at=now - 7200, session="s2",
                 source_tmux_session="scratch")]
    assert len(session_feed.publish_quiet(now=now, store=_Store(rows))) == 2
    assert session_feed.publish_quiet(now=now, store=_Store(rows)) == []


def test_a_project_feed_inherits_the_talks_retention():
    """A directory per project that nothing ever prunes is the alternative."""
    assert feed.default_policy("p-agent-media") == feed.DEFAULT_POLICIES["talks"]
    assert feed.default_policy("docs") == feed.DEFAULT_POLICIES["docs"]


# --- the name Claude Code gives a conversation ------------------------------
# The transcript carries `ai-title` records — the name in the resume list. A
# conversation called "Calibre speech channel interface" is the one you would
# look for; the opening question is whatever was typed before anybody knew
# where the afternoon would go.


def _jsonl(tmp_path, monkeypatch, records):
    p = tmp_path / f"{SESSION}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    monkeypatch.setattr("agent_media_core.conversation.transcript", lambda s: p)
    return p


def test_the_session_name_beats_the_opening_question(tmp_path, monkeypatch):
    _jsonl(tmp_path, monkeypatch, [
        {"type": "user", "message": {"content": "I wonder how calibre would go"}},
        {"type": "ai-title", "aiTitle": "Calibre speech channel interface"},
    ])
    assert session_feed.title_for(SESSION, []) == "Calibre speech channel interface"


def test_the_latest_name_wins(tmp_path, monkeypatch):
    """The name is rewritten as the conversation turns out to be about
    something else."""
    _jsonl(tmp_path, monkeypatch, [
        {"type": "ai-title", "aiTitle": "First guess"},
        {"type": "user", "message": {"content": "hello"}},
        {"type": "ai-title", "aiTitle": "What it became"},
    ])
    assert session_feed.title_for(SESSION, []) == "What it became"


def test_without_a_name_it_falls_back_to_the_question(tmp_path, monkeypatch):
    _jsonl(tmp_path, monkeypatch, [
        {"type": "user", "message": {"content": "why is the ringer loud"}},
    ])
    assert session_feed.title_for(SESSION, []) == "why is the ringer loud"


def test_an_empty_name_does_not_win(tmp_path, monkeypatch):
    _jsonl(tmp_path, monkeypatch, [
        {"type": "ai-title", "aiTitle": "   "},
        {"type": "user", "message": {"content": "the real question"}},
    ])
    assert session_feed.title_for(SESSION, []) == "the real question"


def test_a_long_name_is_still_trimmed_at_a_word(tmp_path, monkeypatch):
    _jsonl(tmp_path, monkeypatch, [
        {"type": "ai-title", "aiTitle": "A name so long " + "and on " * 20},
    ])
    title = session_feed.title_for(SESSION, [])
    assert title.startswith("A name so long") and title.endswith("…")
    assert len(title) < 80
