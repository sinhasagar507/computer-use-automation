"""Surface test against the live mock app (spawned in-process on a free port)."""

import socket
import threading
import time

import pytest
from werkzeug.serving import make_server

from cua.surface.base import Action
from cua.surface.playwright_surface import PlaywrightSurface
from mockbank.app import create_app


@pytest.fixture(scope="module")
def mock_url():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = make_server("127.0.0.1", port, create_app("alpha"))
    th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
    time.sleep(0.3)
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def test_observe_frames_and_act(mock_url):
    s = PlaywrightSurface(headless=True)
    try:
        s.act(Action(type="navigate", url=mock_url + "/"))
        obs = s.observe()
        assert "nav" in obs.frames and "main" in obs.frames
        uid = next(n for n in obs.nodes if n.role == "textbox" and n.label_hint == "User ID")
        pwd = next(n for n in obs.nodes if n.role == "textbox" and n.label_hint == "Password")
        btn = next(n for n in obs.nodes if n.role == "button" and n.name == "Sign In")
        assert uid.frame == "main" and uid.bbox[0] > 150  # offset by the nav frame width
        s.act(Action(type="type", ref=uid.ref, text="teller1"))
        s.act(Action(type="type", ref=pwd.ref, text="demo-pass"))
        s.act(Action(type="click", ref=btn.ref))
        obs = s.observe()
        assert any(n.label_hint == "Member ID" for n in obs.nodes)
        assert obs.screenshot_png and obs.screenshot_png[:4] == b"\x89PNG"
    finally:
        s.close()


def test_dialog_is_surfaced_not_swallowed(mock_url):
    s = PlaywrightSurface(headless=True)
    try:
        s.act(Action(type="navigate", url=mock_url + "/login"))
        obs = s.observe()
        s.act(Action(type="type", ref=next(n.ref for n in obs.nodes if n.label_hint == "User ID"), text="teller1"))
        s.act(Action(type="type", ref=next(n.ref for n in obs.nodes if n.label_hint == "Password"), text="demo-pass"))
        s.act(Action(type="click", ref=next(n.ref for n in obs.nodes if n.name == "Sign In")))
        s.act(Action(type="navigate", url=mock_url + "/fault/dialog"))
        s.act(Action(type="navigate", url=mock_url + "/members/12345"))
        obs = s.observe()
        assert obs.dialog is not None and "Compliance notice" in obs.dialog.message
        r = s.act(Action(type="click", ref="x"))
        assert not r.ok and "dialog" in r.detail
        assert s.act(Action(type="dismiss_dialog", accept=True)).ok
        obs = s.observe()
        assert obs.dialog is None and "Ada Lovelace" in obs.text
    finally:
        s.close()
