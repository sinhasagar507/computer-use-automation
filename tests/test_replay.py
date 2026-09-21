"""Deterministic replay against the live mock app, including every injected runtime condition."""

import socket
import threading
import time
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from cua.artifact.schema import Capability
from cua.artifact.store import apply_overlay, load
from cua.artifact.schema import TenantOverlay
from cua.evidence.logger import RunLog
from cua.policy.guard import Policy
from cua.policy.redact import Redactor
from cua.replay.engine import ReplayEngine
from cua.surface.base import Action
from cua.surface.playwright_surface import PlaywrightSurface
from mockbank.app import create_app

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "lookup_member.json"
PARAMS = {"username": "teller1", "password": "demo-pass", "member_id": "12345"}


def _serve(tenant):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = make_server("127.0.0.1", port, create_app(tenant))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    return srv, f"http://127.0.0.1:{port}"


@pytest.fixture(scope="module")
def alpha():
    srv, url = _serve("alpha"); yield url; srv.shutdown()


@pytest.fixture(scope="module")
def beta():
    srv, url = _serve("beta"); yield url; srv.shutdown()


@pytest.fixture(scope="module")
def surface():
    s = PlaywrightSurface(headless=True); yield s; s.close()


def _cap(url) -> Capability:
    if not FIXTURE.exists():
        pytest.skip("run tests/test_loop_recorder.py first to produce the fixture")
    cap = load(FIXTURE)
    cap.app.entry_url = url + "/"
    return cap


def _run(cap, surface, tmp_path, params=PARAMS, fault=None, base=None, handoff=None):
    policy = Policy.load(ROOT / "config/policy.yaml")
    red = Redactor(policy.redact_patterns, policy.redact_param_names, {"password": params.get("password", "")})
    surface.act(Action(type="navigate", url=(base or cap.app.entry_url.rstrip('/')) + f"/fault/{fault or 'none'}"))
    surface.act(Action(type="navigate", url=(base or cap.app.entry_url.rstrip('/')) + "/logout"))
    log = RunLog(tmp_path, f"replay-{fault or 'ok'}", red)
    try:
        return ReplayEngine(cap, params, policy, surface, log, handoff=handoff).run()
    finally:
        log.close()


def test_success_with_outputs(alpha, surface, tmp_path):
    r = _run(_cap(alpha), surface, tmp_path)
    assert r.status == "success", r
    assert r.outputs == {"member_name": "Ada Lovelace", "savings_balance": "4,312.57"}
    assert r.drift.rung_regressions == 0 and r.drift.score == 0
    assert all(s.ok for s in r.steps) and len(r.steps) == 7


def test_business_outcome_not_found(alpha, surface, tmp_path):
    r = _run(_cap(alpha), surface, tmp_path, params={**PARAMS, "member_id": "99999"})
    assert r.status == "outcome" and r.outcome_code == "MEMBER_NOT_FOUND" and r.outcome_kind == "business"
    assert r.failed_step == 4  # the Search click


def test_permission_denied_is_business_outcome(alpha, surface, tmp_path):
    r = _run(_cap(alpha), surface, tmp_path, fault="denied")
    assert r.status == "outcome" and r.outcome_code == "PERMISSION_DENIED"


def test_hard_failure_app_error(alpha, surface, tmp_path):
    r = _run(_cap(alpha), surface, tmp_path, fault="error500")
    assert r.status == "failed" and r.outcome_code == "APP_ERROR"
    assert r.failed_step == 4 and "System Error" in (r.observed or "")
    assert (Path(r.evidence_dir) / "fail-step4-observation.json").exists()
    assert any(p.name.startswith("00") and p.suffix == ".png" for p in Path(r.evidence_dir).iterdir())


def test_unexpected_dialog_is_recovered(alpha, surface, tmp_path):
    r = _run(_cap(alpha), surface, tmp_path, fault="dialog")
    assert r.status == "success", r
    assert any(x.startswith("UNEXPECTED_DIALOG:dismiss_dialog") for x in r.recoveries)


def test_slow_load_is_absorbed(alpha, surface, tmp_path):
    r = _run(_cap(alpha), surface, tmp_path, fault="slow")
    assert r.status == "success", r


def test_session_expiry_relogin_bounded(alpha, surface, tmp_path):
    r = _run(_cap(alpha), surface, tmp_path, fault="timeout")
    # the fault persists, so one re-login is attempted and then it stops with a clear code
    assert r.status == "failed" and r.outcome_code == "SESSION_EXPIRED"
    assert any(x.startswith("SESSION_EXPIRED:relogin") for x in r.recoveries)


def test_missing_param_is_rejected(alpha, surface, tmp_path):
    r = _run(_cap(alpha), surface, tmp_path, params={"username": "teller1", "password": "demo-pass"})
    assert r.status == "failed" and "member_id" in r.message


def test_escalation_when_target_missing(alpha, surface, tmp_path):
    cap = _cap(alpha)
    cap.steps[3].target.locators = [cap.steps[3].target.locators[0].model_copy(update={"name": "Nonexistent Label"})]
    calls = []
    def handoff(req, s):
        calls.append(req); return False
    r = _run(cap, surface, tmp_path, handoff=handoff)
    assert r.status == "failed" and r.failed_step == 3 and "not found" in r.message
    assert calls and "could not find" in calls[0].reason and calls[0].screenshot


def test_tenant_overlay_replays_on_beta(alpha, beta, surface, tmp_path):
    cap = _cap(alpha)
    ov = TenantOverlay(tenant="beta", entry_url=beta + "/",
                       route_map={"/members/search": "/member/lookup"},
                       name_map={"Member ID": "Member Number", "Search": "Find member"})
    b = apply_overlay(cap, ov)
    r = _run(b, surface, tmp_path, base=beta)
    assert r.status == "success", r
    assert r.outputs["member_name"] == "Ada Lovelace"
