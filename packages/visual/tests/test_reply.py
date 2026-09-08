"""Replying to a conversation from the Audiobookshelf player."""

import json

import pytest

from agent_media_visual import reply


# --- who may type -------------------------------------------------------------

def test_root_may_reply_without_configuration(monkeypatch):
    monkeypatch.delenv("MEDIA_REPLY_USERS", raising=False)
    monkeypatch.delenv("MEDIA_REPLY_ROOT", raising=False)
    ok, who = reply.may_reply({"username": "david", "type": "root"})
    assert (ok, who) == (True, "david")


def test_root_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("MEDIA_REPLY_ROOT", "0")
    monkeypatch.delenv("MEDIA_REPLY_USERS", raising=False)
    ok, _ = reply.may_reply({"username": "david", "type": "root"})
    assert ok is False


def test_admin_is_not_enough(monkeypatch):
    # A library-management role is not a keyboard: admins are named or nothing.
    monkeypatch.delenv("MEDIA_REPLY_USERS", raising=False)
    ok, why = reply.may_reply({"username": "sam", "type": "admin"})
    assert ok is False and "not allowed" in why


def test_named_user_may_reply(monkeypatch):
    monkeypatch.setenv("MEDIA_REPLY_USERS", " sam , cece ")
    assert reply.may_reply({"username": "cece", "type": "user"})[0] is True
    assert reply.may_reply({"username": "guest", "type": "user"})[0] is False


def test_no_identity_is_a_refusal():
    assert reply.may_reply(None)[0] is False


# --- identity comes from ABS, and is cached -----------------------------------

def test_identity_asks_abs_once_per_ttl(monkeypatch):
    calls = []

    def fake_get(url, bearer, path, method="GET"):
        calls.append((path, method, bearer))
        return {"user": {"username": "david", "type": "root"}}, 200

    monkeypatch.setattr(reply, "_abs_url", lambda: "http://abs")
    monkeypatch.setattr(reply, "_abs_get", fake_get)
    reply._IDENT.clear()
    assert reply.abs_identity("tok")[0]["username"] == "david"
    assert reply.abs_identity("tok")[0]["username"] == "david"
    assert calls == [("/api/authorize", "POST", "tok")]


def test_identity_of_a_bad_token_is_none(monkeypatch):
    monkeypatch.setattr(reply, "_abs_url", lambda: "http://abs")
    monkeypatch.setattr(reply, "_abs_get", lambda *a, **k: (None, 401))
    reply._IDENT.clear()
    assert reply.abs_identity("nope") == (None, 401)
    assert reply.abs_identity("") == (None, 401)


def test_a_refusal_is_never_cached(monkeypatch):
    # One transient failure used to refuse every reply for the next minute.
    answers = [(None, 0), ({"user": {"username": "d", "type": "root"}}, 200)]
    monkeypatch.setattr(reply, "_abs_url", lambda: "http://abs")
    # One server, so the two canned answers line up with the two calls. Without
    # this the test reads the dev machine's real abs-bridge.env: an extra server
    # there consumes an answer of its own and the assertion desyncs.
    monkeypatch.setattr(reply, "abs_urls", lambda: ["http://abs"])
    monkeypatch.setattr(reply, "_abs_get", lambda *a, **k: answers.pop(0))
    reply._IDENT.clear()
    assert reply.abs_identity("tok") == (None, 0)
    assert reply.abs_identity("tok")[0]["username"] == "d"


def test_an_unreachable_abs_is_not_a_401(monkeypatch):
    # A 401 sends the app off to refresh its token, and a failed refresh logs
    # the user out — an outage must never do that.
    assert reply._identity_error(0)["status"] == 503
    assert reply._identity_error(503)["status"] == 503
    assert reply._identity_error(401)["status"] == 401
    assert reply._identity_error(500)["status"] == 502


# --- item → session -----------------------------------------------------------

def _manifests(tmp_path, monkeypatch, rows):
    d = tmp_path / "book-tracks"
    d.mkdir()
    for sid, folder in rows:
        (d / f"{sid}.json").write_text(json.dumps({"session": sid, "folder": folder}))
    monkeypatch.setattr(reply, "_manifest_dir", lambda: d)


def test_item_resolves_to_its_session(tmp_path, monkeypatch):
    _manifests(tmp_path, monkeypatch,
               [("abc-1", "/home/ryer/conversations/scratch/scratch - Drones")])
    monkeypatch.setattr(reply, "_abs_url", lambda: "http://abs")
    # ABS reports its own mount; only the <author>/<title> tail is shared.
    monkeypatch.setattr(reply, "_abs_get",
                        lambda *a, **k: ({"path": "/conversations/scratch/scratch - Drones"}, 200))
    assert reply.session_for_item("item1", "tok") == ("abc-1", "")


def test_an_item_with_no_manifest_is_not_a_conversation(tmp_path, monkeypatch):
    _manifests(tmp_path, monkeypatch, [("abc-1", "/x/scratch/scratch - Drones")])
    monkeypatch.setattr(reply, "_abs_url", lambda: "http://abs")
    monkeypatch.setattr(reply, "_abs_get", lambda *a, **k: ({"path": "/books/Tolkien/Hobbit"}, 200))
    sid, why = reply.session_for_item("item1", "tok")
    assert sid is None and "not a conversation" in why


def test_an_item_abs_will_not_show_us_is_refused(monkeypatch):
    # The caller's own bearer does the lookup, so ABS's library permissions
    # decide this for us: no item, no reply.
    monkeypatch.setattr(reply, "_abs_url", lambda: "http://abs")
    monkeypatch.setattr(reply, "_abs_get", lambda *a, **k: (None, 404))
    assert reply.session_for_item("item1", "tok") == (None, "no such item")


# --- the message itself --------------------------------------------------------

def test_quote_and_reply_land_on_one_line():
    out = reply.compose("try the second one", quote="Short answer: no,\n nothing does")
    assert out == 'Re: "Short answer: no, nothing does" — try the second one'
    assert "\n" not in out


def test_a_long_quote_is_clipped():
    out = reply.compose("ok", quote="x" * 500)
    assert len(out) < 200 and out.endswith('…" — ok')


def test_no_quote_is_just_the_text():
    assert reply.compose("hello", quote="  ") == "hello"


# --- refusals before anything is typed ----------------------------------------

def test_reply_refuses_an_empty_message():
    ok, detail = reply.reply("item1", "   ", "tok")
    assert ok is False and detail["error"] == "empty reply"


def test_reply_refuses_a_user_who_may_not_type(monkeypatch):
    monkeypatch.setattr(reply, "abs_identity", lambda b: ({"username": "guest", "type": "user"}, 200))
    monkeypatch.delenv("MEDIA_REPLY_USERS", raising=False)
    typed = []
    monkeypatch.setattr(reply, "session_for_item",
                        lambda *a, **k: typed.append("looked up") or ("s", ""))
    ok, detail = reply.reply("item1", "hi", "tok")
    assert ok is False and "not allowed" in detail["error"]
    assert typed == []  # refused before we go anywhere near a pane


def test_a_session_with_no_transcript_is_not_revived(monkeypatch):
    monkeypatch.setattr(reply, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.delenv("MEDIA_REPLY_ROOT", raising=False)
    monkeypatch.setattr(reply, "session_for_item", lambda *a, **k: ("gone-1", ""))
    monkeypatch.setattr(reply, "live_sessions", dict)
    monkeypatch.setattr(reply, "session_exists", lambda s: False)
    opened = []
    monkeypatch.setattr(reply, "open_window", lambda *a, **k: opened.append(1) or ("%1", ""))
    ok, detail = reply.reply("item1", "hi", "tok")
    assert ok is False and "no transcript" in detail["error"]
    assert opened == []


# --- the live path, and the revive path ---------------------------------------

@pytest.fixture
def _allowed(monkeypatch):
    # Stub the shelving for EVERY test that gets as far as a successful send.
    # Left real, it renders speech and writes a history row against whatever
    # session id the test invented — on a background thread, so it outlives the
    # tmp-dir monkeypatching — and "sess-1" duly appeared in the real library as
    # a conversation called "You: hi". Tests that care about it override this.
    monkeypatch.setattr(reply, "_record_turn", lambda s, t: None)
    monkeypatch.setattr(reply, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.delenv("MEDIA_REPLY_ROOT", raising=False)
    monkeypatch.setattr(reply, "session_for_item", lambda *a, **k: ("sess-1", ""))
    monkeypatch.setattr(reply, "session_exists", lambda s: True)
    monkeypatch.setattr(reply, "transcript_cwd", lambda s: "/home/ryer/projects/x")


def test_a_live_session_is_typed_into_directly(monkeypatch, _allowed):
    from agent_media_visual import canvas
    sent = []
    monkeypatch.setattr(reply, "live_sessions", lambda: {"sess-1": "%7"})
    monkeypatch.setattr(canvas, "_pane_alive", lambda p: True)
    monkeypatch.setattr(canvas, "_send_to_pane", lambda p, t: sent.append((p, t)) or "")
    monkeypatch.setattr(reply, "open_window", lambda *a, **k: pytest.fail("should not revive"))
    ok, detail = reply.reply("item1", "hi", "tok", quote="a turn")
    assert ok is True
    assert detail == {"session": "sess-1", "pane": "%7", "opened": False}
    assert sent == [("%7", 'Re: "a turn" — hi')]


def test_a_dead_session_is_revived_in_a_window(monkeypatch, _allowed):
    from agent_media_visual import canvas
    opened, sent = [], []

    def fake_open(session, cwd, *, resume):
        opened.append((session, cwd, resume))
        return "%9", ""

    monkeypatch.setattr(reply, "live_sessions", dict)
    monkeypatch.setattr(reply, "open_window", fake_open)
    monkeypatch.setattr(canvas, "_send_to_pane", lambda p, t: sent.append((p, t)) or "")
    ok, detail = reply.reply("item1", "hi", "tok")
    assert ok is True and detail["opened"] is True and detail["pane"] == "%9"
    assert opened == [("sess-1", "/home/ryer/projects/x", True)]
    assert sent == [("%9", "hi")]


def test_a_stale_pane_id_falls_through_to_a_revive(monkeypatch, _allowed):
    # Pane ids get recycled, so a live_sessions hit is still probed.
    from agent_media_visual import canvas
    monkeypatch.setattr(reply, "live_sessions", lambda: {"sess-1": "%7"})
    monkeypatch.setattr(canvas, "_pane_alive", lambda p: False)
    monkeypatch.setattr(reply, "open_window", lambda *a, **k: ("%9", ""))
    monkeypatch.setattr(canvas, "_send_to_pane", lambda p, t: "")
    ok, detail = reply.reply("item1", "hi", "tok")
    assert ok is True and detail["pane"] == "%9"


def test_a_window_that_never_comes_up_is_reported(monkeypatch, _allowed):
    monkeypatch.setattr(reply, "live_sessions", dict)
    monkeypatch.setattr(reply, "open_window", lambda *a, **k: ("%9", "%9 did not come up"))
    ok, detail = reply.reply("item1", "hi", "tok")
    assert ok is False and "did not come up" in detail["error"]


def test_branch_never_resumes_and_seeds_a_fresh_session(monkeypatch, _allowed):
    from agent_media_visual import canvas
    opened, sent = [], []
    monkeypatch.setattr(reply, "live_sessions", lambda: {"sess-1": "%7"})
    monkeypatch.setattr(reply, "open_window",
                        lambda s, cwd, *, resume: opened.append(resume) or ("%9", ""))
    monkeypatch.setattr(canvas, "_send_to_pane", lambda p, t: sent.append(t) or "")
    ok, detail = reply.reply("item1", "go deeper", "tok", quote="a turn", mode="branch")
    assert ok is True and detail["branched"] is True
    assert opened == [False]                       # a branch is not a resume
    assert sent == ['Re: "a turn" — go deeper']    # ... but it carries the quote


# --- focus --------------------------------------------------------------------

def test_focus_refuses_a_pane_that_is_not_claude(monkeypatch):
    from agent_media_visual import canvas
    monkeypatch.setattr(canvas, "_tmux_cc_panes", lambda: [{"pane": "%7"}])
    ok, why = reply.focus("%3")
    assert ok is False and "not a live claude pane" in why


def test_focus_walks_the_client_to_the_pane(monkeypatch):
    from agent_media_visual import canvas
    calls = []
    monkeypatch.setattr(canvas, "_tmux_cc_panes", lambda: [{"pane": "%7"}])
    monkeypatch.setattr(reply, "_tmux",
                        lambda a, **k: calls.append(a) or ("work" if "session_name" in a[-1]
                                                          else "@2" if "window_id" in a[-1] else ""))
    ok, detail = reply.focus("%7")
    assert (ok, detail) == (True, "%7")
    assert ["switch-client", "-t", "work"] in calls
    assert ["select-pane", "-t", "%7"] in calls


# --- "should the app draw a reply box here?" -----------------------------------

def test_conversation_says_yes_for_a_live_one(monkeypatch):
    monkeypatch.setattr(reply, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.delenv("MEDIA_REPLY_ROOT", raising=False)
    monkeypatch.setattr(reply, "session_for_item", lambda *a, **k: ("sess-1", ""))
    monkeypatch.setattr(reply, "live_sessions", lambda: {"sess-1": "%7"})
    monkeypatch.setattr(reply, "session_exists", lambda s: True)
    ok, detail = reply.conversation("item1", "tok")
    assert ok is True
    assert detail == {"session": "sess-1", "live": True, "pane": "%7", "resumable": True}


def test_conversation_says_no_to_someone_who_may_not_reply(monkeypatch):
    # The box must not appear where the send would be refused.
    monkeypatch.setattr(reply, "abs_identity", lambda b: ({"username": "guest", "type": "user"}, 200))
    monkeypatch.delenv("MEDIA_REPLY_USERS", raising=False)
    ok, detail = reply.conversation("item1", "tok")
    assert ok is False and "not allowed" in detail["error"]


def test_conversation_says_no_for_an_ordinary_audiobook(monkeypatch):
    monkeypatch.setattr(reply, "abs_identity", lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.delenv("MEDIA_REPLY_ROOT", raising=False)
    monkeypatch.setattr(reply, "session_for_item", lambda *a, **k: (None, "not a conversation"))
    ok, _ = reply.conversation("item1", "tok")
    assert ok is False


def test_an_unreachable_abs_does_not_read_as_a_bad_login(monkeypatch):
    monkeypatch.setattr(reply, "abs_identity", lambda b: (None, 0))
    ok, detail = reply.reply("item1", "hi", "tok")
    assert ok is False and detail["status"] == 503
    assert "did not answer" in detail["error"]


def test_a_rejected_token_reads_as_401(monkeypatch):
    monkeypatch.setattr(reply, "abs_identity", lambda b: (None, 401))
    ok, detail = reply.conversation("item1", "tok")
    assert ok is False and detail["status"] == 401


# --- the listener's own words go on the shelf too -------------------------------

def test_a_sent_reply_is_recorded_as_a_turn(monkeypatch, _allowed):
    from agent_media_visual import canvas
    recorded = []
    monkeypatch.setattr(reply, "live_sessions", lambda: {"sess-1": "%7"})
    monkeypatch.setattr(canvas, "_pane_alive", lambda p: True)
    monkeypatch.setattr(canvas, "_send_to_pane", lambda p, t: "")
    monkeypatch.setattr(reply, "_record_turn", lambda s, t: recorded.append((s, t)))
    ok, _ = reply.reply("item1", "try the second one", "tok", quote="a turn")
    assert ok is True
    # The words as typed, not the quoted line that went into the pane: the
    # quote is context for the agent, not something the listener said.
    assert recorded == [("sess-1", "try the second one")]


def test_nothing_is_recorded_when_the_send_fails(monkeypatch, _allowed):
    from agent_media_visual import canvas
    recorded = []
    monkeypatch.setattr(reply, "live_sessions", lambda: {"sess-1": "%7"})
    monkeypatch.setattr(canvas, "_pane_alive", lambda p: True)
    monkeypatch.setattr(canvas, "_send_to_pane", lambda p, t: "pane is gone")
    monkeypatch.setattr(reply, "_record_turn", lambda s, t: recorded.append(s))
    ok, _ = reply.reply("item1", "hi", "tok")
    assert ok is False and recorded == []


def test_a_multi_line_reply_is_flattened():
    # send-keys types literally then presses Enter, so an embedded newline
    # would submit half a message and strand the rest in the composer.
    out = reply.compose("first line\nsecond line")
    assert out == "first line second line"
    assert reply.compose("a\nb", quote="q") == 'Re: "q" — a b'


def test_recording_a_turn_never_touches_the_real_library(monkeypatch):
    """The guard for the mistake above, stated as a test.

    `_record_turn` spawns a thread, so a test that leaves it real escapes the
    fixture teardown that redirected the state store and the clip cache. It
    wrote four conversations' worth of "hi" into the actual shelf before anyone
    noticed. Everything it needs is resolved *inside* the thread, so the only
    safe rule is that no test calls it for real.
    """
    started = []
    monkeypatch.setattr(reply.threading, "Thread",
                        lambda **kw: started.append(kw) or type(
                            "T", (), {"start": lambda self: None})())
    reply._record_turn("s", "hi")
    assert started and started[0]["daemon"] is True


# --- more than one Audiobookshelf ---------------------------------------------

def test_one_server_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("MEDIA_ABS_URLS", raising=False)
    monkeypatch.setattr(reply.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(reply, "_abs_url", lambda: "http://one.example/")
    assert reply.abs_urls() == ["http://one.example"]


def test_extra_servers_come_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_ABS_URLS", " http://two.example , http://one.example/ ,, ")
    monkeypatch.setattr(reply.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(reply, "_abs_url", lambda: "http://one.example")
    # Publishing server first, no duplicates, no empties.
    assert reply.abs_urls() == ["http://one.example", "http://two.example"]


def test_extra_servers_can_live_beside_the_abs_config(monkeypatch, tmp_path):
    monkeypatch.delenv("MEDIA_ABS_URLS", raising=False)
    cfg = tmp_path / ".config" / "agent-media"
    cfg.mkdir(parents=True)
    (cfg / "abs-bridge.env").write_text('ABS_URL=http://one.example\nABS_URLS="http://two.example"\n')
    monkeypatch.setattr(reply.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(reply, "_abs_url", lambda: "http://one.example")
    assert reply.abs_urls() == ["http://one.example", "http://two.example"]


def test_identity_tries_each_server_and_remembers_which(monkeypatch):
    reply._IDENT.clear()
    asked = []

    def fake_get(url, bearer, path, method="GET"):
        asked.append(url)
        if url == "http://two.example":
            return {"user": {"username": "david", "type": "root"}}, 200
        return None, 401

    monkeypatch.setattr(reply, "abs_urls",
                        lambda: ["http://one.example", "http://two.example"])
    monkeypatch.setattr(reply, "_abs_get", fake_get)
    user, status = reply.abs_identity("tok")
    assert (user["username"], status) == ("david", 200)
    assert asked == ["http://one.example", "http://two.example"]
    # And the item lookups that follow go to the server that knew them.
    assert reply.abs_home("tok") == "http://two.example"


def test_a_token_no_server_knows_is_a_401(monkeypatch):
    reply._IDENT.clear()
    monkeypatch.setattr(reply, "abs_urls",
                        lambda: ["http://one.example", "http://two.example"])
    monkeypatch.setattr(reply, "_abs_get", lambda *a, **k: (None, 401))
    assert reply.abs_identity("tok") == (None, 401)


def test_a_server_being_down_does_not_hide_a_refusal(monkeypatch):
    # One unreachable, one refusing: the token is still the reason, and 401 is
    # what the app must be told rather than "the server did not answer".
    reply._IDENT.clear()
    seq = iter([(None, 0), (None, 401)])
    monkeypatch.setattr(reply, "abs_urls",
                        lambda: ["http://down.example", "http://two.example"])
    monkeypatch.setattr(reply, "_abs_get", lambda *a, **k: next(seq))
    assert reply.abs_identity("tok") == (None, 401)


def test_abs_home_falls_back_to_the_publishing_server(monkeypatch):
    reply._IDENT.clear()
    monkeypatch.setattr(reply, "_abs_url", lambda: "http://one.example")
    assert reply.abs_home("never-seen") == "http://one.example"


# --- the transcript log flags a reply in flight ---------------------------------

def _log_ready(monkeypatch, tmp_path, lines):
    """Everything log_for_item needs, with a canned set of transcript lines."""
    from agent_media_core import book_tracks
    _manifests(tmp_path, monkeypatch,
               [("sess-1", "/home/ryer/conversations/scratch/A talk")])
    monkeypatch.setattr(reply, "abs_identity",
                        lambda b: ({"username": "d", "type": "root"}, 200))
    monkeypatch.setattr(reply, "session_for_item", lambda *a, **k: ("sess-1", ""))
    monkeypatch.setattr(book_tracks, "conversation_log", lambda *a, **k: lines)


def test_log_is_pending_when_the_listener_had_the_last_word(monkeypatch, tmp_path):
    _log_ready(monkeypatch, tmp_path,
               [{"who": "agent", "text": "Hi"}, {"who": "you", "text": "A question"}])
    ok, detail = reply.log_for_item("item1", "tok")
    assert ok is True
    assert detail["pending"] is True


def test_log_is_not_pending_once_the_answer_has_landed(monkeypatch, tmp_path):
    _log_ready(monkeypatch, tmp_path,
               [{"who": "you", "text": "A question"}, {"who": "agent", "text": "An answer"}])
    ok, detail = reply.log_for_item("item1", "tok")
    assert ok is True
    assert detail["pending"] is False


def test_an_empty_log_is_not_pending(monkeypatch, tmp_path):
    _log_ready(monkeypatch, tmp_path, [])
    ok, detail = reply.log_for_item("item1", "tok")
    assert ok is True and detail["pending"] is False
