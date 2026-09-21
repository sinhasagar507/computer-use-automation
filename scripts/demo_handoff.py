"""Reproducible evidence for the human-in-the-loop handoff.

Scenario: the capability meets a runtime condition it does NOT declare (a compliance dialog
that this capability was never taught about). Replay cannot proceed safely, so it raises an
intervention request and cedes control. A *mock operator* (the `operator` function below,
standing in for a person at the operator console) works on the SAME live session, clears the
condition, and hands control back. Replay resumes on that session and finishes.

The only thing mocked here is the person. The request, the ownership transfer, the recording
of what the human did, and the resume are the real code paths used by the CLI.

    uv run python scripts/demo_handoff.py [--headed]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cua.artifact.store import load  # noqa: E402
from cua.evidence.logger import RunLog, new_run_id  # noqa: E402
from cua.handoff.controller import HandoffController  # noqa: E402
from cua.policy.guard import Policy  # noqa: E402
from cua.policy.redact import Redactor  # noqa: E402
from cua.replay.engine import ReplayEngine  # noqa: E402
from cua.surface.base import Action  # noqa: E402
from cua.surface.playwright_surface import PlaywrightSurface  # noqa: E402

ARTIFACT = ROOT / "artifacts" / "lookup_member_balance.json"
PARAMS = {"username": "teller1", "password": "demo-pass", "member_id": "12345"}
UNDECLARED = "UNEXPECTED_DIALOG"


def operator(req, surface) -> None:
    """Stands in for the human at the operator console. Acts on the live session."""
    print(f"  [operator] intervention received: {req.reason}")
    print(f"  [operator] context: step {req.step_index}, url {req.url}, screenshot {req.screenshot}")
    obs = surface.observe()
    if obs.dialog:
        print(f"  [operator] clearing dialog: {obs.dialog.message[:60]}...")
        surface.act(Action(type="dismiss_dialog", accept=True))
    surface.page.frames[-1].click("text=Member Detail")     # a real click on the live page
    print("  [operator] handing control back to automation")


def main() -> int:
    headed = "--headed" in sys.argv
    cap = load(ARTIFACT)
    cap.outcomes = [o for o in cap.outcomes if o.code != UNDECLARED]
    print(f"capability {cap.id} v{cap.version}; dropped declared outcome {UNDECLARED} to force an escalation")

    policy = Policy.load(ROOT / "config" / "policy.yaml")
    red = Redactor(policy.redact_patterns, policy.redact_param_names, {"password": PARAMS["password"]})
    log = RunLog(ROOT / "evidence", new_run_id("replay-handoff"), red)
    hc = HandoffController(log, mode="callback", callback=operator, timeout_s=60)
    surface = PlaywrightSurface(headless=not headed)
    try:
        surface.act(Action(type="navigate", url=cap.app.entry_url.rstrip("/") + "/fault/dialog"))
        log.event("fault.injected", fault="dialog")
        res = ReplayEngine(cap, PARAMS, policy, surface, log, handoff=hc).run()
        log.json("replay-result.json", res.model_dump())
    finally:
        surface.close()
        log.close()

    print(f"\nresult   : {res.status.upper()} {res.message}")
    print(f"outputs  : {res.outputs}")
    print(f"handoffs : {hc.history}")
    print(f"evidence : {log.dir}")
    return 0 if res.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
