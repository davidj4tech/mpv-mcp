"""A conversation laid out as an item that grows.

The property everything else rests on: **nothing already written is ever
rewritten**. That is what lets Audiobookshelf keep the item's id, keep the
existing files' inodes, and keep the listener's position while the conversation
carries on — measured against 2.35.1 in
`docs/proposals/2026-09-02-growing-item-experiment.md`, which is what this
module was written against.
"""

import json

import pytest

from agent_media_core import book_tracks, session_feed


@pytest.fixture(autouse=True)
def tree(tmp_path, monkeypatch):
    """A book tree and a state dir of our own."""
    monkeypatch.setenv("MEDIA_BOOK_TRACKS_ROOT", str(tmp_path / "books"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def _clip(tmp_path, name: str, body: bytes = b"audio") -> str:
    p = tmp_path / "clips" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return str(p)


def _turn(tmp_path, at: float, text: str, clips: int = 1, said=None):
    return session_feed.Turn(
        at=at, text=text, workspace="p-agent-media",
        clips=[_clip(tmp_path, f"{at}-{i}.mp3", f"clip {at}.{i}".encode())
               for i in range(clips)],
        durations=[3.0] * clips,
        sentences=list(said or []))


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    """Three turns that can be added to, one clip each (no ffmpeg needed)."""
    turns = [_turn(tmp_path, 100.0, "First thing said.", clips=2,
                   said=["First thing said.", "And a second sentence."]),
             _turn(tmp_path, 200.0, "Second thing said.", clips=1,
                   said=["Second thing said."]),
             _turn(tmp_path, 300.0, "Third thing said.", clips=1,
                   said=["Third thing said."])]
    state = {"turns": turns[:2]}
    monkeypatch.setattr(session_feed, "turns",
                        lambda session, store=None: list(state["turns"]))
    monkeypatch.setattr(session_feed, "workspace_for",
                        lambda session, ts: "p-agent-media")
    # asked_for, not title_for: the shelf files under the workspace already,
    # so the folder is named for the conversation alone.
    monkeypatch.setattr(session_feed, "asked_for",
                        lambda session, ts: "How the growing item works")
    return state, turns


def test_the_clips_are_the_tracks_named_by_what_they_say(conversation):
    """No audio is written: each track is a second name for the clip the
    renderer already made, and it is named with the sentence it says."""
    folder, added = book_tracks.export_session("sess-1")
    assert added == 3                      # two clips in turn one, one in turn two
    assert sorted(p.name for p in folder.iterdir()) == [
        "0001 - First thing said.mp3",
        "0002 - And a second sentence.mp3",
        "0003 - Second thing said.mp3"]
    for track in folder.iterdir():
        assert track.stat().st_nlink == 2  # linked, never copied


def test_the_folder_is_workspace_over_title(conversation):
    """What a book library reads off a path: the author is the workspace, the
    title is the conversation."""
    folder, _ = book_tracks.export_session("sess-1")
    assert folder.parent.name == "p-agent-media"
    assert folder.name == "How the growing item works"


def test_a_new_turn_appends_and_touches_nothing_else(conversation):
    state, turns = conversation
    folder, _ = book_tracks.export_session("sess-1")
    before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns)
              for p in folder.iterdir()}

    state["turns"] = turns                      # the conversation goes on
    folder2, added = book_tracks.export_session("sess-1")

    assert (folder2, added) == (folder, 1)
    after = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns)
             for p in folder.iterdir()}
    assert "0004 - Third thing said.mp3" in after
    for name, was in before.items():
        assert after[name] == was, f"{name} was rewritten"


def test_running_again_with_nothing_new_writes_nothing(conversation):
    book_tracks.export_session("sess-1")
    folder, added = book_tracks.export_session("sess-1")
    assert added == 0


def test_the_folder_survives_a_better_title(conversation, monkeypatch):
    """A conversation's title comes from what was asked and can improve as it
    goes. Renaming the folder would hand ABS a new item — new id, no progress,
    the old one left behind — so the first answer is the one that is kept."""
    folder, _ = book_tracks.export_session("sess-1")
    monkeypatch.setattr(session_feed, "asked_for",
                        lambda session, ts: "A much better title")
    folder2, _ = book_tracks.export_session("sess-1")
    assert folder2 == folder


def test_a_lost_manifest_adopts_what_is_on_disk(conversation, tmp_path):
    """The manifest is a record of the tree, not its owner. If it goes and the
    tree stays, the export must recognise its own work — not write every turn
    again beside itself, which is an item that plays the conversation twice."""
    folder, _ = book_tracks.export_session("sess-1")
    book_tracks._manifest_path("sess-1").unlink()

    folder2, added = book_tracks.export_session("sess-1")
    assert (folder2, added) == (folder, 0)     # adopted, not re-linked
    assert len(list(folder.iterdir())) == 3
    kept = json.loads(book_tracks._manifest_path("sess-1").read_text())
    assert [f for t in kept["turns"] for f in t["files"]] == [
        "0001 - First thing said.mp3", "0002 - And a second sentence.mp3",
        "0003 - Second thing said.mp3"]


def test_a_clip_that_will_not_go_stops_the_run(conversation, monkeypatch):
    """The numbering is sequential, so carrying on past a failure would file
    the next sentence under this one's number — a conversation out of order."""
    state, turns = conversation
    state["turns"] = turns
    monkeypatch.setattr(book_tracks, "place_clip", lambda src, out: None)
    folder, added = book_tracks.export_session("sess-1")
    assert added == 0
    assert not folder.exists() or not list(folder.iterdir())


def test_track_numbers_are_padded_so_ten_follows_nine():
    names = [book_tracks.track_name(i, "A sentence") for i in (2, 10, 1000)]
    assert names[:2] == ["0002 - A sentence.mp3", "0010 - A sentence.mp3"]
    assert sorted(names) == names          # what a scanner will do with them


def test_a_clip_with_no_recorded_sentence_falls_back_to_the_turn():
    assert book_tracks.track_name(1, "", "The turn") == "0001 - The turn.mp3"


def test_a_track_is_a_second_name_not_a_second_copy(tmp_path):
    src = tmp_path / "one.mp3"
    src.write_bytes(b"the only copy")
    out = tmp_path / "item" / "0001 - one.mp3"
    assert book_tracks.place_clip(str(src), out) == out
    assert out.stat().st_ino == src.stat().st_ino


def test_a_workspace_numbers_its_conversations_by_date(tmp_path, monkeypatch):
    """A series reads in the order things happened, and only counts its own
    workspace."""
    import json as _json

    from agent_media_core import book_tracks as bt

    d = tmp_path / "book-tracks"
    d.mkdir()
    rows = [("late", "/c/work/Third", 300.0),
            ("early", "/c/work/First", 100.0),
            ("mid", "/c/work/Second", 200.0),
            ("elsewhere", "/c/other/Only", 50.0)]
    for session, folder, at in rows:
        (d / f"{session}.json").write_text(_json.dumps(
            {"session": session, "folder": folder,
             "turns": [{"at": at, "title": "t", "files": ["a.mp3"]}]}))
    monkeypatch.setattr(bt, "state_dir", lambda: tmp_path)

    from pathlib import Path
    assert bt._series_position("early", Path("/c/work/First")) == "1"
    assert bt._series_position("mid", Path("/c/work/Second")) == "2"
    assert bt._series_position("late", Path("/c/work/Third")) == "3"
    # A different workspace is a different series, numbered from one.
    assert bt._series_position("elsewhere", Path("/c/other/Only")) == "1"


def test_a_conversation_with_no_manifest_has_no_position(tmp_path, monkeypatch):
    from pathlib import Path

    from agent_media_core import book_tracks as bt

    (tmp_path / "book-tracks").mkdir()
    monkeypatch.setattr(bt, "state_dir", lambda: tmp_path)
    assert bt._series_position("nobody", Path("/c/work/Whatever")) == ""


# --- the transcript shows a turn before the manifest catches up -----------------

def test_conversation_log_includes_the_live_tail(tmp_path, monkeypatch):
    """A turn in speech history but not yet in the manifest still appears —
    with no position — so a reply lands in the transcript within a poll of
    being spoken instead of waiting out the debounced publish."""
    from agent_media_core import book_tracks as bt, session_feed
    folder = tmp_path / "p-agent-media" / "A talk"
    # The manifest is one turn behind: it has the agent's turn at 100 only.
    monkeypatch.setattr(bt, "_read_manifest",
                        lambda s: {"turns": [{"at": 100.0, "title": "First answer"}]})
    # No Audiobookshelf, so no positions are fetched (and none are needed).
    monkeypatch.setattr(bt, "_abs_ready", lambda target=None: None)
    # Speech history has the published turn plus a newer listener turn (200)
    # and the agent's fresh reply (300) that has not been published yet.
    hist = [
        session_feed.Turn(at=100.0, text="First answer", workspace="p-agent-media",
                          clips=[], durations=[], sentences=[], listener=False),
        session_feed.Turn(at=200.0, text="You: a question", workspace="p-agent-media",
                          clips=[], durations=[], sentences=[], listener=True),
        session_feed.Turn(at=300.0, text="The fresh reply", workspace="p-agent-media",
                          clips=[], durations=[], sentences=[], listener=False),
    ]
    monkeypatch.setattr(session_feed, "turns", lambda s, store=None: list(hist))

    lines = bt.conversation_log("sess-1", folder)
    assert [(l["who"], l["text"], l["start"]) for l in lines] == [
        ("agent", "First answer", None),   # from the manifest turn
        ("you", "a question", None),        # live tail: "You: " stripped
        ("agent", "The fresh reply", None), # live tail: the reply, shown at once
    ]


def test_conversation_log_shows_the_turn_being_spoken_now(tmp_path, monkeypatch):
    """The in-flight turn (in now_playing, not yet in history or the manifest)
    shows at once, so a reply appears while it is being spoken."""
    from agent_media_core import book_tracks as bt, session_feed
    from agent_media_core.state.store import StateStore
    folder = tmp_path / "p-agent-media" / "A talk"
    monkeypatch.setattr(bt, "_read_manifest",
                        lambda s: {"turns": [{"at": 100.0, "title": "Earlier"}]})
    monkeypatch.setattr(bt, "_abs_ready", lambda target=None: None)
    monkeypatch.setattr(session_feed, "turns", lambda s, store=None: [
        session_feed.Turn(at=100.0, text="Earlier", workspace="p-agent-media",
                          clips=[], durations=[], sentences=[], listener=False)])
    # now_playing: this session's reply is mid-flight, writer alive (our pid).
    import os
    monkeypatch.setattr(StateStore, "get_now_playing", lambda self, sink: {
        "started_at": 300.0,
        "extras": {"source_session": "sess-1", "text": "Speaking this now",
                   "writer_pid": os.getpid(), "listener": False,
                   "clip_sentences": ["Speaking this now.", "And this next."],
                   "current_sentence_idx": 1,
                   # Local lane: durations, no offsets. Paused 4.5s in.
                   "clip_durations_s": [2.0, 3.0],
                   "play_started_at": 1000.0, "paused_at": 1004.5}})
    lines = bt.conversation_log("sess-1", folder)
    assert lines[-1] == {"start": None, "end": None, "at": 300.0, "key": "",
                         "who": "agent", "text": "Speaking this now",
                         # Live, which sentence the voice is on, and the
                         # timeline so a reader can move it on its own clock.
                         "live": True, "sentence": 1,
                         "sentences": ["Speaking this now.", "And this next."],
                         "offsets": [0.0, 2.0], "elapsed": 4.5, "paused": True}
    # Still playing: elapsed runs on the wall clock from play_started_at.
    # (The first time this path ran for real it raised NameError — `time`
    # was never imported — and every transcript asked for while a reply was
    # audible came back as a 500 the app showed as "Nothing said yet.")
    monkeypatch.setattr(StateStore, "get_now_playing", lambda self, sink: {
        "started_at": 300.0,
        "extras": {"source_session": "sess-1", "text": "Speaking this now",
                   "writer_pid": os.getpid(), "listener": False,
                   "clip_sentences": ["Speaking this now.", "And this next."],
                   "current_sentence_idx": 0,
                   "clip_durations_s": [2.0, 3.0],
                   "play_started_at": 1000.0}})
    monkeypatch.setattr(bt.time, "time", lambda: 1001.25)
    live = bt.conversation_log("sess-1", folder)[-1]
    assert live["live"] and live["paused"] is False
    assert live["elapsed"] == 1.25 and live["sentence"] == 0


def test_conversation_log_ignores_a_live_turn_from_another_session(tmp_path, monkeypatch):
    from agent_media_core import book_tracks as bt, session_feed
    from agent_media_core.state.store import StateStore
    import os
    folder = tmp_path / "p-agent-media" / "A talk"
    monkeypatch.setattr(bt, "_read_manifest",
                        lambda s: {"turns": [{"at": 100.0, "title": "Earlier"}]})
    monkeypatch.setattr(bt, "_abs_ready", lambda target=None: None)
    monkeypatch.setattr(session_feed, "turns", lambda s, store=None: [
        session_feed.Turn(at=100.0, text="Earlier", workspace="p-agent-media",
                          clips=[], durations=[], sentences=[], listener=False)])
    monkeypatch.setattr(StateStore, "get_now_playing", lambda self, sink: {
        "started_at": 300.0,
        "extras": {"source_session": "OTHER", "text": "not this conversation",
                   "writer_pid": os.getpid()}})
    lines = bt.conversation_log("sess-1", folder)
    assert [l["text"] for l in lines] == ["Earlier"]



# --- the live tag -----------------------------------------------------------------

def test_tags_for_adds_and_removes_only_the_live_tag():
    from agent_media_core import book_tracks as b
    assert b._tags_for("s1", ["keep"], live={"s1"}) == ["keep", "live"]
    assert b._tags_for("s1", ["keep", "live"], live=set()) == ["keep"]
    assert b._tags_for("s1", ["live", "live"], live={"s1"}) == ["live"]


def test_sync_live_tags_patches_only_what_changed(tmp_path, monkeypatch):
    import json
    from agent_media_core import book_tracks as b
    from agent_media_core import _paths
    monkeypatch.setattr(_paths, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(b, "state_dir", lambda: tmp_path)
    d = tmp_path / "book-tracks"
    d.mkdir()
    (d / "s-live.json").write_text(json.dumps({"session": "s-live", "folder": "/c/p-x/Alive one"}))
    (d / "s-dead.json").write_text(json.dumps({"session": "s-dead", "folder": "/c/p-x/Ended one"}))
    (d / "s-same.json").write_text(json.dumps({"session": "s-same", "folder": "/c/p-x/Already right"}))
    items = [
        {"id": "i1", "path": "/conversations/p-x/Alive one", "media": {"tags": []}},
        {"id": "i2", "path": "/conversations/p-x/Ended one", "media": {"tags": ["live", "other"]}},
        {"id": "i3", "path": "/conversations/p-x/Already right", "media": {"tags": ["live"]}},
        {"id": "i4", "path": "/conversations/p-x/Not ours", "media": {"tags": ["live"]}},
    ]
    monkeypatch.setattr(b, "_abs_ready_all", lambda target=None: [("http://abs", "tok", [{"id": "lib"}])])
    monkeypatch.setattr(b, "_abs_items", lambda url, token, lib: items)
    patched = []
    monkeypatch.setattr(b, "_abs_patch", lambda url, token, path, body: patched.append((path, body)))
    n = b.sync_live_tags(live={"s-live", "s-same"})
    assert n == 2
    assert patched == [("/api/items/i1/media", {"tags": ["live"]}),
                       ("/api/items/i2/media", {"tags": ["other"]})]   # i3 right already, i4 not a conversation


def test_live_session_ids_reads_the_registry_for_real(tmp_path, monkeypatch):
    # Not mocked past the tmux call: the import it needs was once missing and
    # every other test had stubbed the function away.
    import os
    from agent_media_core import book_tracks as b
    monkeypatch.setenv("MEDIA_PANE_REGISTRY_DIR", str(tmp_path))
    (tmp_path / "1").write_text("11111111-2222-3333-4444-555555555555")   # legacy row, no pid
    (tmp_path / "2").write_text(f"22222222-2222-3333-4444-555555555555 {os.getpid()}")
    (tmp_path / "3").write_text("33333333-2222-3333-4444-555555555555 999999999")  # dead pid
    monkeypatch.setattr(b.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "%1 %2 %3"})())
    assert b.live_session_ids() == {"11111111-2222-3333-4444-555555555555", "22222222-2222-3333-4444-555555555555"}


def test_live_turn_carries_the_playout_delay_for_its_target(monkeypatch):
    import json, time
    from agent_media_core import book_tracks as b
    ex = {"source_session": "s1", "text": "One. Two.", "clip_sentences": ["One.", "Two."],
          "clip_offsets_s": [0.0, 1.5], "play_started_at": time.time() - 1.0}
    row = {"started_at": 100.0, "target": "phone", "extras": json.dumps(ex)}

    class Store:
        def get_now_playing(self, sink):
            return row

    monkeypatch.setattr("agent_media_core.state.store.StateStore", lambda: Store())
    monkeypatch.setenv("MEDIA_SPEECH_PLAYOUT_MS_PHONE", "1200")
    turn = b._live_turn("s1")
    assert turn["delay"] == 1.2 and turn["offsets"] == [0.0, 1.5] and 0.9 < turn["elapsed"] < 1.3
