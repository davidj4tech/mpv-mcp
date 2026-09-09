"""One address, two answers: the picture, and the page that shows it.

Sasonica draws the picture under a message as an ``<img>`` and opens the very
same URL in the browser when it is tapped. So ``/img/<name>`` has to return
bytes to the chat and a viewer page to the tap, with neither end knowing which
it is asking for — the browser's own ``Sec-Fetch-Dest`` is what tells them
apart. Everything ambiguous falls to the bytes: the viewer is an improvement on
a tap, never a condition of the picture loading.
"""

import http.client
import threading
from http.server import ThreadingHTTPServer

import pytest

from agent_media_visual import canvas


@pytest.fixture()
def spool(tmp_path, monkeypatch):
    monkeypatch.setattr(canvas, "spool_dir", lambda: tmp_path)
    (tmp_path / "fig.png").write_bytes(b"\x89PNG\r\n\x1a\nnot really")
    (tmp_path / "fig.svg").write_text("<svg/>")
    return tmp_path


@pytest.fixture()
def server(spool):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), canvas.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address
    srv.shutdown()
    srv.server_close()


def _get(addr, path, headers=None):
    conn = http.client.HTTPConnection(*addr, timeout=5)
    conn.request("GET", path, headers=headers or {})
    res = conn.getresponse()
    body = res.read()
    conn.close()
    return res, body


IMG = {"Sec-Fetch-Dest": "image"}
DOC = {"Sec-Fetch-Dest": "document"}


def test_an_img_tag_still_gets_the_picture(server):
    res, body = _get(server, "/img/fig.png", IMG)
    assert res.status == 200
    assert res.getheader("Content-Type") == "image/png"
    assert body.startswith(b"\x89PNG")


def test_a_tap_gets_the_viewer(server):
    res, body = _get(server, "/img/fig.png", DOC)
    assert res.status == 200
    assert res.getheader("Content-Type").startswith("text/html")
    assert b'id="pic"' in body and b'id="full"' in body


def test_the_viewer_asks_for_its_own_picture_raw(server):
    # ?raw=1 is how the page declines the viewer it was just served, so it must
    # come back as bytes even on a navigation-shaped request.
    _, page = _get(server, "/img/fig.png", DOC)
    assert b"'?raw=1'" in page
    res, body = _get(server, "/img/fig.png?raw=1", DOC)
    assert res.getheader("Content-Type") == "image/png"
    assert body.startswith(b"\x89PNG")


def test_view_1_asks_for_the_viewer_without_a_header(server):
    # So a link can be deliberate rather than relying on the browser's word.
    res, body = _get(server, "/img/fig.png?view=1")
    assert res.getheader("Content-Type").startswith("text/html")
    assert b'id="pic"' in body


def test_an_unmarked_client_gets_the_picture(server):
    # curl, an older browser, the app's native HTTP plugin: no Sec-Fetch-Dest,
    # so the answer is the behaviour this route has always had.
    for headers in ({}, {"Sec-Fetch-Dest": ""}, {"Accept": "*/*"}):
        res, body = _get(server, "/img/fig.png", headers)
        assert res.getheader("Content-Type") == "image/png", headers
        assert body.startswith(b"\x89PNG")


def test_a_missing_picture_is_404_before_any_viewer(server):
    # A viewer page for a picture that does not exist is a blank screen with a
    # fullscreen button on it — say 404 instead, whoever is asking.
    for headers in (DOC, IMG):
        res, _ = _get(server, "/img/nope.png", headers)
        assert res.status == 404, headers


def test_the_viewer_carries_the_landscape_lock(server):
    _, page = _get(server, "/img/fig.png", DOC)
    assert b"requestFullscreen" in page
    assert b"orientation.lock('landscape')" in page
    # ...and withholds it on e-ink, where nothing else on these pages moves.
    assert b"eink()" in page


def test_svg_is_marked_inkable_by_its_extension(server):
    # The viewer decides from its own address (there is no state to consult),
    # so the rule has to survive being read off location.pathname.
    _, page = _get(server, "/img/fig.svg", DOC)
    assert b"inkable" in page
