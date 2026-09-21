"""CLI: serve-mock | discover | replay."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich import print as rprint

app = typer.Typer(add_completion=False, help="Computer-use automation: discover with an LLM, replay without one.")
load_dotenv()

ROOT = Path(__file__).resolve().parents[1]


def _parse_params(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items:
        if "=" not in it:
            raise typer.BadParameter(f"--param must be name=value, got {it!r}")
        k, v = it.split("=", 1)
        if v.startswith("env:"):
            v = os.environ.get(v[4:], "")
            if not v:
                raise typer.BadParameter(f"environment variable {it.split('env:')[1]} is empty")
        out[k.strip()] = v
    return out


def _policy(path: Path):
    from cua.policy.guard import Policy
    return Policy.load(path)


def _redactor(policy, params: dict[str, str], sensitive: set[str]):
    from cua.policy.redact import Redactor
    r = Redactor(policy.redact_patterns, policy.redact_param_names)
    for k in sensitive:
        if k in params:
            r.add_sensitive(k, params[k])
    return r


def _handoff(mode: str, log, timeout: float, port: int):
    from cua.handoff.controller import HandoffController
    return HandoffController(log, mode=mode, timeout_s=timeout, web_port=port)


@app.command("serve-mock")
def serve_mock(tenant: str = typer.Option("alpha", help="alpha | beta (same vendor app, different labels/routes)"),
               port: int = 5050):
    """Run the mock legacy core-banking app."""
    from mockbank.app import run
    rprint(f"[green]mock bank[/] tenant={tenant} at http://127.0.0.1:{port}/  (user teller1 / demo-pass)")
    run(tenant, port)


@app.command()
def discover(
    goal: str = typer.Option(..., help="Natural-language goal"),
    entry: str = typer.Option("http://127.0.0.1:5050/", help="Entry URL of the target app"),
    param: list[str] = typer.Option([], help="name=value or name=env:VAR; typed as {{params.name}} by the model"),
    sensitive: str = typer.Option("username,password", help="Comma-separated param names never persisted"),
    cap_id: str = typer.Option("capability", "--id", help="Capability id (slug)"),
    name: str = typer.Option("", help="Capability display name"),
    out: Path = typer.Option(Path("artifacts/capability.json"), help="Where to save the artifact"),
    evidence: Path = typer.Option(Path("evidence"), help="Evidence root folder"),
    policy: Path = typer.Option(Path("config/policy.yaml")),
    outcomes: Path = typer.Option(Path("config/outcomes.yaml")),
    model: Optional[str] = typer.Option(None, help="Override CUA_LLM_MODEL"),
    max_steps: int = 25,
    timeout: float = 420,
    headed: bool = typer.Option(False, help="Show the browser (required for a real human handoff)"),
    handoff: str = typer.Option("cli", help="web | cli | none"),
    handoff_timeout: float = 600,
    operator_port: int = 5055,
    confirm_risky: bool = typer.Option(False, help="Escalate irreversible actions to a human instead of blocking"),
    tenant: str = "alpha", vendor: str = "MockCore", product: str = "Teller Workstation", app_version: str = "1.0",
):
    """Run one LLM-driven discovery run and record it as a capability artifact."""
    from cua.agent.loop import run_discovery
    from cua.artifact.recorder import record, slugify
    from cua.artifact.schema import AppRef
    from cua.artifact.store import save
    from cua.evidence.logger import RunLog, new_run_id
    from cua.llm.client import OpenAICompatLLM
    from cua.surface.playwright_surface import PlaywrightSurface

    params = _parse_params(param)
    sens = {s.strip() for s in sensitive.split(",") if s.strip()}
    pol = _policy(policy)
    red = _redactor(pol, params, sens)
    run_id = new_run_id("discovery")
    log = RunLog(evidence, run_id, red)
    llm = OpenAICompatLLM(model=model)
    rprint(f"[cyan]discovery[/] run={run_id} model={llm.model} endpoint={llm.base_url}")
    hc = _handoff(handoff, log, handoff_timeout, operator_port)
    surface = PlaywrightSurface(headless=not headed)
    try:
        res = run_discovery(goal, entry, params, sens, pol, surface, llm, log, handoff=hc,
                            max_steps=max_steps, timeout_s=timeout, confirm_risky=confirm_risky)
        log.json("discovery-result.json", res.model_dump(exclude={"trace": {"__all__": {"target": {"bbox"}}}}))
        rprint(f"[bold]{res.status}[/] {res.detail}  steps={len(res.trace)} llm_calls={res.llm_calls} outputs={res.outputs}")
        if res.status != "success":
            rprint(f"evidence: {log.dir}")
            raise typer.Exit(2)
        cid = cap_id if cap_id != "capability" else slugify(goal)
        cap = record(res, goal=goal, cap_id=cid, cap_name=name or cid.replace("_", " ").title(), entry_url=entry,
                     params=params, sensitive=sens, policy=pol, redactor=red,
                     app=AppRef(vendor=vendor, product=product, version=app_version, tenant=tenant, entry_url=entry),
                     model=llm.model, run_id=run_id, outcomes_path=outcomes)
        save(cap, out)
        log.json("artifact.json", json.loads(cap.model_dump_json()))
        rprint(f"[green]artifact saved[/] {out}  ({len(cap.steps)} steps, {len(cap.outputs)} outputs, risk_max={cap.risk_max})")
        rprint(f"evidence: {log.dir}")
    finally:
        surface.close()
        log.close()


@app.command()
def replay(
    artifact: Path = typer.Argument(..., help="Capability artifact JSON"),
    param: list[str] = typer.Option([], help="name=value or name=env:VAR"),
    sensitive: str = typer.Option("username,password"),
    evidence: Path = typer.Option(Path("evidence")),
    policy: Path = typer.Option(Path("config/policy.yaml")),
    overlay: Optional[Path] = typer.Option(None, help="Tenant overlay JSON to specialize the artifact"),
    entry: Optional[str] = typer.Option(None, help="Override the entry URL"),
    fault: Optional[str] = typer.Option(None, help="Mock-only: inject a runtime condition before replay "
                                                    "(not_found|validation|denied|dialog|timeout|slow|error500)"),
    headed: bool = False,
    handoff: str = typer.Option("none", help="web | cli | none"),
    handoff_timeout: float = 600,
    operator_port: int = 5055,
    confirm_risky: bool = False,
    label: str = typer.Option("", help="Suffix for the evidence run id"),
):
    """Replay a capability deterministically (no LLM) and print the structured result."""
    from cua.artifact.store import apply_overlay, load, load_overlay
    from cua.evidence.logger import RunLog, new_run_id
    from cua.replay.engine import ReplayEngine
    from cua.surface.base import Action
    from cua.surface.playwright_surface import PlaywrightSurface

    cap = load(artifact)
    if overlay:
        cap = apply_overlay(cap, load_overlay(overlay))
    if entry:
        cap.app.entry_url = entry
    params = _parse_params(param)
    sens = {s.strip() for s in sensitive.split(",") if s.strip()}
    pol = _policy(policy)
    red = _redactor(pol, params, sens)
    run_id = new_run_id("replay" + (f"-{label}" if label else ""))
    log = RunLog(evidence, run_id, red)
    hc = _handoff(handoff, log, handoff_timeout, operator_port)
    surface = PlaywrightSurface(headless=not headed)
    try:
        if fault:
            surface.act(Action(type="navigate", url=cap.app.entry_url.rstrip("/") + f"/fault/{fault}"))
            log.event("fault.injected", fault=fault)
        res = ReplayEngine(cap, params, pol, surface, log, handoff=hc, confirm_risky=confirm_risky).run()
        log.json("replay-result.json", res.model_dump())
        color = {"success": "green", "outcome": "yellow", "failed": "red", "escalated": "magenta"}[res.status]
        rprint(f"[{color} bold]{res.status.upper()}[/] {res.outcome_code or ''} {res.message}")
        print(json.dumps(res.model_dump(exclude={"steps"}), indent=2, default=str))
        raise typer.Exit(0 if res.status in ("success", "outcome") else 1)
    finally:
        surface.close()
        log.close()


if __name__ == "__main__":
    app()
