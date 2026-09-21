"""Agent loop + recorder with a scripted LLM (no network). Produces the artifact fixture used by replay tests."""

import socket
import threading
import time
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from cua.agent.loop import run_discovery
from cua.artifact.recorder import record
from cua.artifact.schema import AppRef
from cua.artifact.store import save
from cua.evidence.logger import RunLog
from cua.llm.client import Decision, parse_decision
from cua.policy.guard import Policy
from cua.policy.redact import Redactor
from cua.surface.base import Action, Observation
from cua.surface.playwright_surface import PlaywrightSurface
from mockbank.app import create_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def mock_url():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = make_server("127.0.0.1", port, create_app("alpha"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


class ScriptedLLM:
    """Plays the role of the model: picks refs from the live observation like a model would."""
    model = "scripted"
    last_usage = {}

    def __init__(self):
        self.phase = 0

    def _ref(self, obs: Observation, **match):
        for n in obs.nodes:
            if all(getattr(n, k) == v for k, v in match.items()):
                return n.ref
        raise AssertionError(f"no node matching {match}")

    def decide(self, goal, params_desc, history, obs, extra=""):
        p = self.phase
        self.phase += 1
        if p == 0: return Decision(reason="sign in", action=Action(type="type", ref=self._ref(obs, label_hint="User ID"), text="{{params.username}}"))
        if p == 1: return Decision(reason="password", action=Action(type="type", ref=self._ref(obs, label_hint="Password"), text="{{params.password}}"))
        if p == 2: return Decision(reason="submit", action=Action(type="click", ref=self._ref(obs, role="button", name="Sign In")))
        if p == 3: return Decision(reason="enter id", action=Action(type="type", ref=self._ref(obs, label_hint="Member ID"), text="{{params.member_id}}"))
        if p == 4: return Decision(reason="search", action=Action(type="click", ref=self._ref(obs, name="Search")))
        if p == 5: return Decision(reason="read name", action=Action(type="extract", ref=self._ref(obs, label_hint="Name"), name="member_name"))
        if p == 6: return Decision(reason="read balance", action=Action(type="extract", ref=self._ref(obs, name="4,312.57"), name="savings_balance"))
        return Decision(reason="goal met", action=Action(type="done", evidence="Member Detail"))


def test_parse_decision_variants():
    d = parse_decision('```json\n{"reason":"x","action":{"type":"click","ref":"m_1"}}\n```')
    assert d.action.type == "click" and d.action.ref == "m_1"
    d = parse_decision('Sure: {"type":"done","evidence":"OK","reason":"r"}')
    assert d.action.type == "done"
    with pytest.raises(ValueError):
        parse_decision("no json here")


def test_discovery_and_record(mock_url, tmp_path):
    policy = Policy.load(ROOT / "config/policy.yaml")
    params = {"username": "teller1", "password": "demo-pass", "member_id": "12345"}
    sensitive = {"username", "password"}
    red = Redactor(policy.redact_patterns, policy.redact_param_names)
    for k in sensitive:
        red.add_sensitive(k, params[k])
    log = RunLog(tmp_path, "disc-test", red)
    s = PlaywrightSurface(headless=True)
    try:
        res = run_discovery("Look up member 12345 and read name and savings balance", mock_url + "/", params, sensitive,
                            policy, s, ScriptedLLM(), log, max_steps=12)
    finally:
        s.close(); log.close()
    assert res.status == "success", res.detail
    assert res.outputs == {"member_name": "Ada Lovelace", "savings_balance": "4,312.57"}

    # evidence never contains the password
    text = (tmp_path / "disc-test" / "log.jsonl").read_text()
    assert "demo-pass" not in text and "{{params.password}}" in text

    app = AppRef(vendor="MockCore", product="Teller Workstation", version="1.0", tenant="alpha", entry_url=mock_url + "/")
    cap = record(res, goal="Look up member and read balance", cap_id="lookup_member", cap_name="Lookup member",
                 entry_url=mock_url + "/", params=params, sensitive=sensitive, policy=policy, redactor=red, app=app,
                 model="scripted", run_id="disc-test", outcomes_path=ROOT / "config/outcomes.yaml")
    assert [s.action for s in cap.steps] == ["type", "type", "click", "type", "click", "extract", "extract"]
    assert cap.steps[1].value == "{{params.password}}"
    assert cap.steps[3].target.locators[0].strategy == "label" and cap.steps[3].target.locators[0].name == "Member ID"
    assert cap.steps[4].expect[0].kind == "url_contains" and cap.steps[4].expect[0].value == "/members/12345"
    assert {o.name for o in cap.outputs} == {"member_name", "savings_balance"}
    assert {p.name for p in cap.params} == {"username", "password", "member_id"}
    assert next(p for p in cap.params if p.name == "password").example is None
    assert any(o.code == "MEMBER_NOT_FOUND" for o in cap.outcomes)
    dumped = cap.model_dump_json()
    assert "demo-pass" not in dumped and "teller1" not in dumped
    save(cap, ROOT / "tests" / "fixtures" / "lookup_member.json")
