"""The listener's typed reply becomes a turn in the conversation."""
import json
from pathlib import Path

from agent_media_core import book_tracks


def test_render_failure_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(book_tracks, "render_text", None, raising=False)
    import agent_media_core.render.engines as eng
    monkeypatch.setattr(eng, "render_text", lambda *a, **k: (False, "no engine"))
    assert book_tracks.record_listener_turn("s1", "hello") is False


def test_empty_text_is_not_a_turn():
    assert book_tracks.record_listener_turn("s1", "   ") is False
    assert book_tracks.record_listener_turn("", "hello") is False


class _Store:
    def __init__(self, rows):
        self.rows = rows
        self.added = []

    def recent_history(self, *, sink=None, limit=20):
        return self.rows

    def add_history(self, **row):
        self.added.append(row)


def _row(session, text, at):
    return {"text": f"You: {text}", "started_at": at,
            "extras": {"listener": True, "source_session": session}}


def test_the_same_words_twice_are_one_turn(tmp_path, monkeypatch):
    """A reply from the player is recorded by the canvas as it sends and by
    the prompt hook as Claude Code receives it; the second is dropped."""
    import time
    import agent_media_core.render.engines as eng
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    # Rendering would fail — proving the repeat never gets that far.
    monkeypatch.setattr(eng, "render_text", lambda *a, **k: (False, "no"))
    store = _Store([_row("s1", "hello there", time.time() - 1)])
    assert book_tracks.record_listener_turn("s1", "hello  there", store=store) is True
    assert store.added == []


def test_the_same_words_later_or_elsewhere_are_a_new_turn(tmp_path, monkeypatch):
    import time
    import agent_media_core.render.engines as eng
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(eng, "render_text", lambda *a, **k: (False, "no"))
    old = _Store([_row("s1", "hello", time.time() - book_tracks.LISTENER_REPEAT_S - 5)])
    other = _Store([_row("s2", "again", time.time() - 1)])
    # Not a repeat, so it goes on to render — and the failed render says False.
    assert book_tracks.record_listener_turn("s1", "hello", store=old) is False
    assert book_tracks.record_listener_turn("s1", "again", store=other) is False


def test_two_recorders_at_once_make_one_turn(tmp_path, monkeypatch):
    """The race the history check cannot catch: both look before either has
    written. The claim is taken before the render, so the second is refused."""
    import time
    import agent_media_core.render.engines as eng
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(eng, "render_text", lambda *a, **k: (False, "no"))
    now = time.time()
    assert book_tracks._claim_listener_turn("s1", "hello", now) is True
    assert book_tracks._claim_listener_turn("s1", "hello", now) is False
    assert book_tracks._claim_listener_turn("s1", "hello again", now) is True
    # The second recorder, history still empty, is told it is already a turn.
    assert book_tracks.record_listener_turn("s1", "hello", store=_Store([])) is True


def test_a_stale_claim_is_swept(tmp_path, monkeypatch):
    import os, time
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    now = time.time()
    assert book_tracks._claim_listener_turn("s1", "hello", now) is True
    d = tmp_path / "state" / "agent-media" / "listener-claims"
    for p in d.iterdir():
        os.utime(p, (now - 400, now - 400))
    assert book_tracks._claim_listener_turn("s1", "hello", now) is True
