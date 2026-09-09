"""Cross-origin access to the canvas.

The web client (Audiobookshelf's React client, forked as sasonica-web) is
served from the ABS port and talks to the canvas on 8781, so every one of the
conversation calls is cross-origin. The Capacitor app never was — a native
HTTP client ignores the same-origin policy — so this is the one thing the
browser needs that the phone did not.
"""

import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

from agent_media_visual import canvas


@pytest.fixture()
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), canvas.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


def _request(addr, method, path, headers=None):
    conn = http.client.HTTPConnection(*addr, timeout=5)
    conn.request(method, path, headers=headers or {})
    res = conn.getresponse()
    res.read()
    conn.close()
    return res


def test_preflight_allows_the_conversation_routes(server):
    res = _request(server, "OPTIONS", "/reply", {"Origin": "http://red5:13379"})
    assert res.status == 204
    assert res.getheader("Access-Control-Allow-Origin") == "*"
    assert "Authorization" in res.getheader("Access-Control-Allow-Headers")
    assert "POST" in res.getheader("Access-Control-Allow-Methods")


def test_preflight_refuses_the_token_guarded_routes(server):
    # /input, /show, /ctl spend OUR credential, not the caller's: a page the
    # browser happens to be visiting must not be able to preflight its way in.
    for path in ("/input", "/show", "/ctl", "/say", "/play"):
        res = _request(server, "OPTIONS", path, {"Origin": "http://evil.example"})
        assert res.status == 405, path
        assert res.getheader("Access-Control-Allow-Origin") is None, path


def test_answer_carries_the_header_even_when_it_refuses(server):
    # No bearer, so this is a 401/404 — and the browser still has to be
    # allowed to READ it, or the client shows a network error instead of the
    # server's own words about why it said no.
    res = _request(server, "GET", "/conversation?item=nope")
    assert res.status in (401, 403, 404)
    assert res.getheader("Access-Control-Allow-Origin") == "*"


def test_other_routes_stay_same_origin(server):
    res = _request(server, "GET", "/healthz")
    assert res.getheader("Access-Control-Allow-Origin") is None
