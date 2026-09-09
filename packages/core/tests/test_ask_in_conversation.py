"""A question rides the alert lane but belongs to the conversation."""
from dataclasses import dataclass, field

from agent_media_core import book_tracks, session_feed


class _Store:
    def __init__(self, rows): self.rows = rows
    def recent_history(self, sink="speech", limit=200): return self.rows


def _row(at, text, extras):
    return {"started_at": at, "text": text, "uri": "", "extras": extras}


def test_an_alert_is_dropped_but_a_question_is_kept(tmp_path, monkeypatch):
    clip = tmp_path / "c.mp3"; clip.write_bytes(b"x")
    ask = [{"question": "Which case?", "options": [{"label": "A", "description": ""}],
            "multiSelect": False}]
    rows = [
        _row(1.0, "Claude is waiting for your input",
             {"source_session": "s1", "kind": "notif", "clip_uris": [str(clip)]}),
        _row(2.0, "red5 / pane: Which case? Option 1: A.",
             {"source_session": "s1", "kind": "notif", "ask": ask,
              "clip_uris": [str(clip)]}),
    ]
    monkeypatch.setattr(session_feed, "_ffprobe_duration", lambda p: 1.0, raising=False)
    got = session_feed.turns("s1", store=_Store(rows))
    assert [t.at for t in got] == [2.0]
    assert got[0].ask == ask


def test_the_line_a_reader_gets_is_the_question_not_the_announcement(monkeypatch):
    ask = [{"question": "Which case do you mean?",
            "options": [{"label": "In the app", "description": "red5 started it here"}],
            "multiSelect": False}]

    @dataclass
    class T:
        at: float
        text: str
        listener: bool = False
        key: str = ""
        ask: list = field(default_factory=list)

    monkeypatch.setattr(book_tracks, "_read_manifest", lambda s: {"turns": []})
    monkeypatch.setattr(book_tracks, "_abs_ready", lambda target=None: None)
    monkeypatch.setattr(book_tracks, "_live_turn", lambda s: None)
    monkeypatch.setattr(book_tracks.session_feed, "turns",
                        lambda s: [T(at=2.0, text="red5 / pane: Which case do you mean? Option 1: In the app.", ask=ask)])
    lines = book_tracks.conversation_log("s1", __import__("pathlib").Path("/tmp/x"))
    assert len(lines) == 1
    line = lines[0]
    assert line["who"] == "agent"
    assert line["text"] == "Which case do you mean?"     # not the spoken sentence
    assert line["ask"][0]["options"][0]["label"] == "In the app"
