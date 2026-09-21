"""Turn a successful discovery trace into a Capability artifact.

The artifact is decoupled from the transcript: no model prose except a redacted one-line
note per step, no screenshots, no raw values for sensitive params. Every acted-on element
becomes a Target with a locator ladder built from what the surface perceived.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import yaml

from cua.agent.loop import DiscoveryResult, TraceStep
from cua.artifact.schema import (AppRef, Capability, Checkpoint, Expect, Locator, Outcome, Output, Param, Provenance,
                                 Step, Target, Wait)
from cua.policy.guard import Policy, max_risk
from cua.policy.redact import Redactor
from cua.surface.base import Node

GENERIC_NAMES = {"", "-", "|", "select"}


def build_target(node: Node, viewport: tuple[int, int] = (1280, 900)) -> Target:
    """Locator ladder: semantic first, geometric last. Order depends on the control type."""
    ladder: list[Locator] = []
    f = node.frame
    role_name = Locator(strategy="role_name", frame=f, role=node.role, name=node.name) if node.name and node.name not in GENERIC_NAMES else None
    label = Locator(strategy="label", frame=f, role=node.role, name=node.label_hint) if node.label_hint else None
    text = Locator(strategy="text", frame=f, name=node.name) if node.name and node.role in ("link", "cell", "text", "heading") else None
    path = Locator(strategy="path", frame=f, path=node.path) if node.path else None
    x, y, w, h = node.bbox
    bbox = Locator(strategy="bbox", frame=f, bbox_frac=(x / viewport[0], y / viewport[1], w / viewport[0], h / viewport[1]))

    if node.role in ("textbox", "combobox", "checkbox", "radio"):
        order = [label, role_name, path, bbox]
    elif node.role in ("cell", "text"):
        order = [label, text, path, bbox]
    else:  # link, button, heading
        order = [role_name, text, path, bbox]
    for loc in order:
        if loc is not None:
            ladder.append(loc)
    desc = f"{node.role} '{node.label_hint or node.name}'" + (f" in frame {f}" if f else "")
    return Target(description=desc, locators=ladder, recorded_rung=0)


def _url_path(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).path or "/"


def _parameterize(s: str | None, params: dict[str, str]) -> str | None:
    """Replace concrete param values with placeholders (longest values first to avoid partial hits)."""
    if not s:
        return s
    for name, val in sorted(params.items(), key=lambda kv: -len(kv[1] or "")):
        if val and len(val) >= 3 and val in s:
            s = s.replace(val, f"{{{{params.{name}}}}}")
    return s


def load_outcomes(vendor: str, path: Path | str = "config/outcomes.yaml") -> list[Outcome]:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return [Outcome(**o) for o in raw.get(vendor, [])]


def record(result: DiscoveryResult, *, goal: str, cap_id: str, cap_name: str, entry_url: str,
           params: dict[str, str], sensitive: set[str], policy: Policy, redactor: Redactor, app: AppRef,
           model: str, run_id: str, outcomes_path: Path | str = "config/outcomes.yaml") -> Capability:
    assert result.status == "success", "only successful runs are recorded"
    steps: list[Step] = []
    outputs: list[Output] = []
    risk_max = "read"
    used_params: set[str] = set()
    acted = [t for t in result.trace if t.action.type not in ("done", "escalate")]

    for k, t in enumerate(acted):
        a = t.action
        nxt = acted[k + 1] if k + 1 < len(acted) else None
        target = build_target(t.target) if t.target else None
        value = a.text
        url = a.url
        for name in params:
            if value and f"{{{{params.{name}}}}}" in value:
                used_params.add(name)
            if url and params[name] and params[name] in url:
                url = url.replace(params[name], f"{{{{params.{name}}}}}")
                used_params.add(name)
        expect: list[Expect] = []
        moved = t.url_after and _url_path(t.url_after) != _url_path(t.url)
        if moved and a.type in ("click", "navigate", "press"):
            expect.append(Expect(kind="url_contains", value=_url_path(t.url_after)))
        wait = Wait(kind="load" if a.type in ("click", "navigate", "press") else "none")
        risk = t.risk if a.type == "click" else "read"
        risk_max = max_risk(risk_max, risk)
        steps.append(Step(index=k, action=a.type, target=target, value=value, url=url, key=a.key, accept=a.accept,
                          output=a.name if a.type == "extract" else None, wait=wait, expect=expect, risk=risk,
                          note=redactor.text(t.reason)[:160]))
        if a.type == "extract" and target and a.name:
            outputs.append(Output(name=a.name, target=target, after_step=k,
                                  description=f"Extracted from {target.description}"))
        _ = nxt

    # Success checkpoint from the model's verified evidence text + final route.
    cp_expect: list[Expect] = []
    if result.evidence_text:
        cp_expect.append(Expect(kind="text_visible", value=result.evidence_text))
    if result.final_url:
        cp_expect.append(Expect(kind="url_contains", value=_parameterize(_url_path(result.final_url), params)))
    checkpoints = [Checkpoint(name="goal_reached", after_step=len(steps) - 1, expect=cp_expect)] if cp_expect else []

    plist = [Param(name=n, required=True, sensitive=(n in sensitive),
                   example=None if n in sensitive else params.get(n),
                   description=("credential; supply via env:VAR" if n in sensitive else ""))
             for n in params if n in used_params or n in sensitive]

    return Capability(
        id=cap_id, name=cap_name, description=redactor.text(goal), goal=redactor.text(goal), app=app,
        params=plist, outputs=outputs, steps=steps, checkpoints=checkpoints,
        outcomes=load_outcomes(app.vendor, outcomes_path), risk_max=risk_max,
        provenance=Provenance(discovered_by=model, run_id=run_id, transcript_sha256=result.transcript_sha256,
                              steps_taken=len(result.trace)),
    )


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:48]
