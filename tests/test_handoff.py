"""Handoff: pause automation, let a 'human' act on the same live session, record it, resume."""

import socket
import threading
import time
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from cua.artifact.store import load
from cua.evidence.logger import RunLog
from cua.handoff.controller import ControlOwner, HandoffController
from cua.policy.guard import Policy
from cua.policy.redact import Redactor
from cua.replay.engine import ReplayEngine
from cua.surface.base import Action
from cua.surface.playwright_surface import PlaywrightSurface
from mockbank.app import create_app

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "lookup_member.json"
PARAMS = {"username": "teller1", "password": "demo-pass", "member_id": "12345"}


@pytest.fixture(scope="module")
def alpha():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = make_server("127.0.0.1", port, create_app("alpha"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def test_replay_handoff_same_session(alpha, tmp_path):
    if not FIXTURE.exists():
        pytest.skip("fixture missing")
    cap = load(FIXTURE)
    cap.app.entry_url = alpha + "/"
    # Remove the declared dialog recovery so the dialog becomes an *undeclared* blocking condition.
    cap.outcomes = [o for o in cap.outcomes if o.code != "UNEXPECTED_DIALOG"]
    policy = Policy.load(ROOT / "config/policy.yaml")
    log = RunLog(tmp_path, "replay-handoff", Redactor(policy.redact_patterns, policy.redact_param_names, {"password": "demo-pass"}))
    seen = {}

    def human(req, surface):
        # The human sees the request context, then works on the SAME live session.
        seen["req"] = req
        seen["owner_during"] = hc.owner
        assert surface.observe().dialog is not None
        surface.act(Action(type="dismiss_dialog", accept=True))      # clear the compliance notice
        surface.page.frames[-1].click("text=Member Detail")          # a real click, recorded as evidence
        seen["url_after_human"] = surface.page.frames[-1].url

    hc = HandoffController(log, mode="callback", callback=human, timeout_s=30)
    s = PlaywrightSurface(headless=True)
    try:
        s.act(Action(type="navigate", url=alpha + "/fault/dialog"))
        r = ReplayEngine(cap, PARAMS, policy, s, log, handoff=hc).run()
    finally:
        s.close(); log.close()
    assert r.status == "success", r
    # the run finished on the same session the human touched, and returned its declared outputs
    assert r.outputs["member_name"] == "Ada Lovelace"
    assert seen["url_after_human"].endswith("/members/12345")
    assert seen["owner_during"] is ControlOwner.HUMAN and hc.owner is ControlOwner.AUTOMATION
    assert "undeclared blocking dialog" in seen["req"].reason and seen["req"].screenshot
    assert (Path(log.dir) / f"intervention-step{seen['req'].step_index}.json").exists()
    events = (Path(log.dir) / "log.jsonl").read_text()
    for kind in ("handoff.request", "handoff.resume", "human.action", "replay.resume"):
        assert f'"kind": "{kind}"' in events, kind
    assert "demo-pass" not in events
    assert hc.history and hc.history[0]["resumed"] and hc.history[0]["human_actions"] >= 1


def test_handoff_timeout_returns_false(tmp_path):
    policy = Policy.load(ROOT / "config/policy.yaml")
    log = RunLog(tmp_path, "h", Redactor([], []))
    hc = HandoffController(log, mode="callback", callback=None, timeout_s=0.2)
    from cua.agent.loop import HandoffRequest

    class NoSurface:  # no page attribute -> recording is skipped
        pass
    ok = hc(HandoffRequest(run_id="h", goal="g", step_index=0, reason="r", url="u"), NoSurface())
    assert ok is False and hc.owner is ControlOwner.AUTOMATION
