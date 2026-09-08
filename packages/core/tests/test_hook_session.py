"""The Claude Code hook forwards its session_id into the event metadata.

That id is what lets the popup's `goto-pane` resume a conversation whose
source pane has since been closed (`claude --resume <session>`).
"""

from agent_media_core.intake import hook_claude_code as H


def _capture_submit(monkeypatch):
    seen = {}

    def fake_submit(event, **_):
        seen["event"] = event
        return "rid-1"

    monkeypatch.setattr(H, "submit_event", fake_submit)
    # The Stop path detaches playback into a forked child where an in-process
    # mock can't be observed; run inline so the captured call is visible.
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")
    return seen


def test_stop_forwards_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    seen = _capture_submit(monkeypatch)
    monkeypatch.setattr(H, "_latest_assistant_text", lambda tp: "hello there")
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: False)
    monkeypatch.setattr(H, "_session_name", lambda: "")

    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    assert H._handle_stop({"transcript_path": str(transcript),
                           "session_id": "stop-sess"}) == 0
    assert (seen["event"].metadata or {}).get("session") == "stop-sess"


def test_notification_forwards_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    seen = _capture_submit(monkeypatch)
    monkeypatch.setattr(H, "_client_focused_recently", lambda within: False)
    monkeypatch.setattr(H, "_session_name", lambda: "")
    monkeypatch.setattr(H, "_notif_label", lambda sess: "")

    assert H._handle_notification({"message": "Claude is waiting",
                                   "session_id": "notif-sess"}) == 0
    assert (seen["event"].metadata or {}).get("session") == "notif-sess"


def test_missing_session_id_is_empty_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    seen = _capture_submit(monkeypatch)
    monkeypatch.setattr(H, "_latest_assistant_text", lambda tp: "hi")
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: False)
    monkeypatch.setattr(H, "_session_name", lambda: "")

    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    assert H._handle_stop({"transcript_path": str(transcript)}) == 0
    assert (seen["event"].metadata or {}).get("session") == ""


def test_claim_once_is_exclusive_within_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert H._claim_once("same question", ttl_seconds=300) is True
    # A second, concurrent read of the same question is refused...
    assert H._claim_once("same question", ttl_seconds=300) is False
    # ...but a different question is independent.
    assert H._claim_once("other question", ttl_seconds=300) is True


def test_claim_once_reclaims_when_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # ttl=0 means the prior claim is always considered stale, so a genuine
    # re-ask after the window is spoken again rather than swallowed forever.
    assert H._claim_once("q", ttl_seconds=0) is True
    assert H._claim_once("q", ttl_seconds=0) is True


def test_ask_read_once_across_pretooluse_and_notification(tmp_path, monkeypatch):
    """The two hooks a single AskUserQuestion modal fires (PreToolUse, then a
    Notification) must speak the question exactly once, not twice."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    calls = []

    def fake_submit(event, **_):
        calls.append(event)
        return "rid"

    monkeypatch.setattr(H, "submit_event", fake_submit)
    # Isolate _claim_once from history dedup and environment noise.
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: False)
    monkeypatch.setattr(H, "_session_name", lambda: "")
    monkeypatch.setattr(H, "_ask_location_label", lambda: "")
    monkeypatch.setattr(H, "_client_pane_focused", lambda: False)

    tool_input = {"questions": [{"question": "Ship it?",
                                 "options": [{"label": "Yes"}, {"label": "No"}]}]}
    # PreToolUse path (fires as the modal appears).
    H._handle_pretooluse({"tool_name": "AskUserQuestion",
                          "tool_input": tool_input, "session_id": "s1"})
    # Notification path (the same modal, transcript now flushed) reads the
    # live last turn as an AskUserQuestion and would re-emit it.
    monkeypatch.setattr(H, "_latest_ask_question",
                        lambda tp: H._format_ask_question(tool_input))
    tp = tmp_path / "t.jsonl"
    tp.write_text("{}\n")
    H._handle_notification({"transcript_path": str(tp),
                            "message": "waiting", "session_id": "s1"})

    assert len(calls) == 1  # spoken once, not twice


# ---- where it was said ----------------------------------------------------
#
# The pane's tmux session and window name go in the metadata too, resolved
# here. The submitter used to ask tmux itself, and by then it can be too late:
# a reply is rendered and queued before a word of it is spoken, and a
# conversation that has just said goodbye closes its window in that gap. tmux
# answers about a pane that has gone with a successful empty string, so the
# last clip of a conversation was the one most likely to arrive with nowhere
# recorded — and the phone's list filed all of them under "no session".

def _tmux_says(monkeypatch, answer):
    monkeypatch.setattr(H, "_tmux", lambda args, **k: answer)


def test_the_pane_and_its_names_ride_along(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("TMUX_PANE", "%155")
    seen = _capture_submit(monkeypatch)
    monkeypatch.setattr(H, "_latest_assistant_text", lambda tp: "goodbye")
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: False)
    _tmux_says(monkeypatch, "work\tthe conversation")

    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    assert H._handle_stop({"transcript_path": str(transcript),
                           "session_id": "s"}) == 0
    md = seen["event"].metadata or {}
    assert md["pane"] == "%155"
    assert md["tmux"] == "work"
    assert md["window"] == "the conversation"


def test_outside_tmux_it_claims_nothing(tmp_path, monkeypatch):
    # A missing key means "ask tmux yourself"; an empty string would mean "the
    # answer is nothing", which is a different and wrong claim.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    seen = _capture_submit(monkeypatch)
    monkeypatch.setattr(H, "_latest_assistant_text", lambda tp: "hello")
    monkeypatch.setattr(H, "_dedup_seen", lambda *a, **k: False)
    monkeypatch.setattr(H, "_session_name", lambda: "")

    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    assert H._handle_stop({"transcript_path": str(transcript),
                           "session_id": "s"}) == 0
    md = seen["event"].metadata or {}
    assert "pane" not in md and "tmux" not in md and "window" not in md


def test_a_pane_with_no_window_name_sends_only_what_it_has(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%1")
    _tmux_says(monkeypatch, "work\t")
    assert H._source_place() == {"pane": "%1", "tmux": "work"}


# --- UserPromptSubmit: the listener's typed words reach the transcript ---------

def _capture_record(monkeypatch):
    seen = []

    class _BT:
        @staticmethod
        def record_listener_turn(session, text):
            seen.append((session, text))
            return True

    import agent_media_core
    monkeypatch.setattr(agent_media_core, "book_tracks", _BT, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "agent_media_core.book_tracks", _BT)
    monkeypatch.setenv("MEDIA_HOOK_NO_DETACH", "1")
    return seen


def test_a_typed_prompt_is_recorded_as_a_listener_turn(monkeypatch):
    seen = _capture_record(monkeypatch)
    assert H._handle_user_prompt(
        {"prompt": "  this chat  doesn't\nshow up ", "session_id": "s-1"}) == 0
    assert seen == [("s-1", "this chat doesn't show up")]


def test_slash_commands_and_empties_are_not_conversation(monkeypatch):
    seen = _capture_record(monkeypatch)
    for prompt in ["/loop 5m foo", "", "   ", None]:
        H._handle_user_prompt({"prompt": prompt, "session_id": "s-1"})
    H._handle_user_prompt({"prompt": "hello"})   # no session: nowhere to file it
    assert seen == []


def test_a_paste_is_skipped_not_truncated(monkeypatch):
    seen = _capture_record(monkeypatch)
    H._handle_user_prompt({"prompt": "x" * (H.PROMPT_RECORD_LIMIT + 1), "session_id": "s-1"})
    assert seen == []


def test_main_routes_the_event(monkeypatch):
    seen = _capture_record(monkeypatch)
    import io, json
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"hook_event_name": "UserPromptSubmit", "prompt": "hi", "session_id": "s-2"})))
    monkeypatch.setattr(H, "load_env_file", lambda name: None)
    assert H.main() == 0
    assert seen == [("s-2", "hi")]
