"""The follow-up: a one-line next prompt filed per session (intake/_followup.py)."""

from agent_media_core.intake import _followup as fu
from agent_media_core.intake import _summary


def _state(monkeypatch, tmp_path):
    monkeypatch.setattr(fu, "state_dir", lambda: tmp_path)


def test_suggest_files_the_cleaned_line(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path)
    seen = {}
    def chat(p, t, timeout, model=None):
        seen["model"] = model
        return '"Next: go with projects, do the relabel."\n'
    monkeypatch.setattr(_summary, "_chat", chat)
    monkeypatch.setenv("MEDIA_FOLLOWUP_MODEL", "tiny")
    out = fu.suggest_followup("A long reply.", "sess-1", "k1")
    assert out == "go with projects, do the relabel."
    assert seen["model"] == "tiny"
    assert fu.load_followup("sess-1") == {"text": out, "key": "k1", "at": fu.load_followup("sess-1")["at"]}


def test_a_failed_call_files_nothing_and_keeps_the_last(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path)
    fu.save_followup("sess-1", "earlier", "k0")
    monkeypatch.setattr(_summary, "_chat", lambda p, t, timeout, model=None: None)
    assert fu.suggest_followup("A reply.", "sess-1", "k1") is None
    assert fu.load_followup("sess-1")["key"] == "k0"


def test_an_essay_is_not_a_prompt(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path)
    monkeypatch.setattr(_summary, "_chat", lambda p, t, timeout, model=None: "x" * 300)
    assert fu.suggest_followup("A reply.", "sess-1", "k1") is None


def test_switched_off_spawns_nothing(monkeypatch):
    monkeypatch.setenv("MEDIA_FOLLOWUP", "1")
    assert fu.followup_enabled() is True
    monkeypatch.setenv("MEDIA_FOLLOWUP", "0")
    called = []
    monkeypatch.setattr(fu.threading, "Thread", lambda *a, **k: called.append(1))
    fu.spawn_followup("reply", "sess-1", "k1")
    assert not called
    assert fu.followup_enabled() is False


def test_no_session_no_file(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path)
    assert fu.load_followup("") is None
    fu.save_followup("", "text", "k")
    assert not list(tmp_path.glob("**/*.json"))
