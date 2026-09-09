"""A slash command is a turn when it is work, and only once when it repeats."""
import pytest

from agent_media_core import slash


class _Store:
    def __init__(self, rows=()): self.rows = list(rows)
    def recent_history(self, sink="speech", limit=400): return self.rows


# --- reading one ------------------------------------------------------------

def test_the_line_somebody_typed():
    assert slash.parse("/code-review high") == {
        "name": "code-review", "args": "high", "text": "/code-review high"}
    assert slash.parse("/simplify")["text"] == "/simplify"
    assert slash.parse("/plugin:skill do a thing")["name"] == "plugin:skill"


def test_the_tags_the_harness_sends_when_it_ran_it_itself():
    raw = ("<local-command-caveat>ignore me</local-command-caveat>"
           "<command-name>/model</command-name>"
           "<command-message>model</command-message>"
           "<command-args>opus</command-args>"
           "<local-command-stdout>Set model</local-command-stdout>")
    assert slash.parse(raw) == {"name": "model", "args": "opus", "text": "/model opus"}


@pytest.mark.parametrize("text", [
    "ordinary words",
    "/home/ryer/projects/notes.md is the file",   # a path, not a command
    "/",
    "",
    "and/or",
])
def test_what_is_not_a_command(text):
    assert slash.parse(text) is None


def test_a_multi_line_prompt_keeps_its_arguments_on_one_line():
    got = slash.parse("/loop 5m\n  /babysit-prs")
    assert got["args"] == "5m /babysit-prs"


# --- deciding ---------------------------------------------------------------

def test_a_settings_command_changes_the_tool_not_the_conversation():
    assert slash.is_settings("model") and slash.is_settings("Clear")
    assert not slash.is_settings("code-review")
    # An unknown one is work: a project's own commands are the point.
    assert not slash.is_settings("babysit-prs")


def test_turn_for_drops_the_tool_and_keeps_the_work():
    assert slash.turn_for("/model opus", "s1", store=_Store()) is None
    assert slash.turn_for("just talking", "s1", store=_Store()) is None
    assert slash.turn_for("/code-review high", "s1", store=_Store())["name"] == "code-review"


def _row(session, cmd):
    return {"started_at": 1.0, "text": "You: " + cmd["text"], "uri": "",
            "extras": {"source_session": session, "listener": True, "command": cmd}}


def test_a_loop_is_recorded_once_however_often_it_fires():
    cmd = {"name": "loop", "args": "5m /babysit-prs", "text": "/loop 5m /babysit-prs"}
    store = _Store([_row("s1", cmd)])
    assert slash.already_recorded("s1", cmd, store=store) is True
    assert slash.turn_for("/loop 5m /babysit-prs", "s1", store=store) is None
    # Another conversation, and the same command with other arguments, are new.
    assert slash.already_recorded("s2", cmd, store=store) is False
    assert slash.already_recorded("s1", {"name": "loop", "args": "10m"}, store=store) is False


def test_a_broken_history_read_still_lets_the_turn_through():
    class Boom(_Store):
        def recent_history(self, sink="speech", limit=400): raise RuntimeError("no db")
    assert slash.already_recorded("s1", {"name": "x", "args": ""}, store=Boom()) is False
