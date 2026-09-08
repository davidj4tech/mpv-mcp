"""The phone's own player comes first for the book channel (2026-09-09)."""
from pathlib import Path

import pytest

from agent_media_core import mcp_server, phone_player
from agent_media_core.types import Target


PHONE = Target(name="phone")


def test_match_item_decides_by_basename_not_title():
    hits = [
        {"libraryItem": {"id": "a", "media": {"audioFiles": [{"metadata": {"filename": "Part 1.m4b"}}]}}},
        {"libraryItem": {"id": "b", "media": {"audioFiles": [{"metadata": {"path": "/audiobooks/Part 1 (2).m4b"}}]}}},
    ]
    assert phone_player.match_item(hits, "Part 1 (2).m4b") == "b"
    assert phone_player.match_item(hits, "Part 1.m4b") == "a"
    assert phone_player.match_item(hits, "nothing.m4b") is None


def test_app_server_falls_back_to_the_bridge_env(tmp_path, monkeypatch):
    env = tmp_path / "abs-bridge.env"
    env.write_text("ABS_URL=http://127.0.0.1:13378\nABS_SERVERS=http://red5:13379|tok1,http://x|tok2\n")
    monkeypatch.delenv("MEDIA_PHONE_PLAYER_ABS", raising=False)
    monkeypatch.setenv("MEDIA_ABS_BRIDGE_ENV", str(env))
    assert phone_player.app_server() == ("http://red5:13379", "tok1")
    monkeypatch.setenv("MEDIA_PHONE_PLAYER_ABS", "http://a|b")
    assert phone_player.app_server() == ("http://a", "b")


def test_local_target_never_reaches_for_a_phone(monkeypatch):
    monkeypatch.setenv("MEDIA_PHONE_PLAYER_SSH_PHONE", "p8a")
    assert phone_player.reachable(Target(name="local")) is False
    assert phone_player.reachable(PHONE) is True
    monkeypatch.setenv("MEDIA_PHONE_PLAYER", "0")
    assert phone_player.reachable(PHONE) is False


def test_ssh_transport_thaws_then_curls_loopback(monkeypatch):
    monkeypatch.setenv("MEDIA_PHONE_PLAYER_SSH_PHONE", "p8a")
    monkeypatch.delenv("MEDIA_PHONE_PLAYER_URL", raising=False)
    seen = {}

    class P:
        returncode, stdout = 0, '{"source":"sasonica","item":"i1","t":5.0}'

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return P()
    monkeypatch.setattr(phone_player.subprocess, "run", run)
    s = phone_player.request(PHONE, "/play", {"item": "i1", "t": 5.0})
    assert s["item"] == "i1"
    assert seen["cmd"][:1] == ["ssh"] and seen["cmd"][-2] == "p8a"
    remote = seen["cmd"][-1]
    assert "MediaButtonReceiver" in remote            # the thaw comes first
    assert "127.0.0.1:8772/play?item=i1&t=5.0" in remote


def test_an_error_answer_or_dead_ssh_means_not_taken(monkeypatch):
    monkeypatch.setenv("MEDIA_PHONE_PLAYER_SSH_PHONE", "p8a")

    class Dead:
        returncode, stdout = 255, ""
    monkeypatch.setattr(phone_player.subprocess, "run", lambda *a, **k: Dead())
    assert phone_player.request(PHONE, "/state") is None

    class Err:
        returncode, stdout = 0, '{"error":"no such route"}'
    monkeypatch.setattr(phone_player.subprocess, "run", lambda *a, **k: Err())
    assert phone_player.request(PHONE, "/state") is None


# --- book_play goes to the phone first -----------------------------------------

class _Store:
    def __init__(self):
        self.now = None
        self.last = None
    def get_resume_pos(self, uri): return 42000
    def set_now_playing(self, **kw): self.now = kw
    def set_book_last(self, uri, title=None): self.last = (uri, title)
    def clear_playlist_active(self): pass


class _Mpv:
    played = None
    def play(self, uri, target, start_ms=0): _Mpv.played = (uri, start_ms)
    def idle(self, target): return True
    def pause(self, target): pass


def _wire(monkeypatch, store, taken):
    monkeypatch.setattr(mcp_server, "_book", lambda: _Mpv())
    monkeypatch.setattr(mcp_server, "_state", lambda: store)
    monkeypatch.setattr(mcp_server, "_book_target", lambda name="": PHONE)
    monkeypatch.setattr(mcp_server, "_save_book_bookmark", lambda *a, **k: None)
    monkeypatch.setattr(phone_player, "play", lambda p, t, start_ms=0, rate=None: taken)


def test_book_play_stops_at_the_phone_when_it_takes_the_file(tmp_path, monkeypatch):
    f = tmp_path / "Hounded.m4b"; f.write_bytes(b"x")
    store = _Store(); _Mpv.played = None
    _wire(monkeypatch, store, {"item": "i1", "title": "Hounded", "t": 42.0, "item_id": "i1"})
    r = mcp_server.book_play(str(f), target="phone")
    assert r["ok"] and r["player"] == "sasonica" and r["resumed_from_ms"] == 42000
    assert _Mpv.played is None                      # mpv never touched
    assert store.now["extras"]["player"] == "sasonica"
    assert store.last == (str(f), "Hounded")


def test_book_play_falls_back_to_mpv_when_the_phone_declines(tmp_path, monkeypatch):
    f = tmp_path / "Hounded.m4b"; f.write_bytes(b"x")
    store = _Store(); _Mpv.played = None
    _wire(monkeypatch, store, None)
    monkeypatch.setattr(mcp_server, "_note_media_title", lambda *a, **k: None)
    import agent_media_core.sinks.book as book
    monkeypatch.setattr(book, "remote_cached_path", lambda p, t: "cached", raising=False)
    r = mcp_server.book_play(str(f), target="phone")
    assert r["ok"] and "player" not in r
    assert _Mpv.played == (str(f), 42000)


def test_transport_commands_go_to_the_app_while_it_holds_a_session(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_server, "_book_target", lambda name="": PHONE)
    monkeypatch.setattr(phone_player, "has_item", lambda t: True)
    monkeypatch.setattr(phone_player, "request",
                        lambda t, route, params=None, timeout=50.0: calls.append((route, params)) or {"t": 130.0, "rate": 1.6})
    assert mcp_server.book_pause()["player"] == "sasonica"
    assert mcp_server.book_skip(-10)["ok"]
    assert mcp_server.book_seek(130)["position_ms"] == 130000
    assert mcp_server.book_speed(1.6)["speed"] == 1.6
    assert [c[0] for c in calls] == ["/pause", "/jump", "/seek", "/speed"]
