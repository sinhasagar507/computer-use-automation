"""Deterministic replay: execute a Capability with parameters, no LLM in the loop.

Per step:
  1. observe; detect declared runtime conditions (business -> return; recoverable -> recover;
     failure -> stop with a debuggable error)
  2. resolve the target via the locator ladder (record which rung hit -> drift signal)
  3. act through the Surface
  4. wait, then verify the step's post-conditions (polling up to the step timeout to absorb
     transient slowness); on failure re-run outcome detection so a "record not found"
     page after a click is reported as an outcome, not as a missing element
Finally verify checkpoints and return declared outputs.
"""

from __future__ import annotations

import re
import time
from typing import Callable

from cua.agent.loop import HandoffRequest, substitute
from cua.artifact.schema import Capability, Outcome, Step
from cua.evidence.logger import RunLog
from cua.policy.guard import Policy, PolicyViolation
from cua.replay.detectors import describe, detect_outcome, evaluate as _evaluate, resolve
from cua.replay.result import Drift, ReplayResult, StepReport
from cua.surface.base import Action, Observation, Surface

HandoffFn = Callable[[HandoffRequest, Surface], bool]


def _bind(e, params):
    return e.model_copy(update={"value": substitute(e.value, params)}) if e.value else e


class _Redo(Exception):
    """Raised after a restart so the current step is executed again from scratch."""


class _Stop(Exception):
    def __init__(self, result: ReplayResult):
        self.result = result


def validate_params(cap: Capability, params: dict[str, str]) -> list[str]:
    errs = []
    for p in cap.params:
        v = params.get(p.name)
        if p.required and (v is None or v == ""):
            errs.append(f"missing required param {p.name!r}")
            continue
        if v is None:
            continue
        if p.type == "integer" and not re.fullmatch(r"-?\d+", v):
            errs.append(f"param {p.name!r} must be an integer")
        if p.pattern and not re.fullmatch(p.pattern, v):
            errs.append(f"param {p.name!r} does not match {p.pattern}")
    return errs


class ReplayEngine:
    def __init__(self, cap: Capability, params: dict[str, str], policy: Policy, surface: Surface, log: RunLog,
                 handoff: HandoffFn | None = None, confirm_risky: bool = False):
        self.cap, self.params, self.policy, self.surface, self.log = cap, params, policy, surface, log
        self.handoff, self.confirm_risky = handoff, confirm_risky
        self.reports: list[StepReport] = []
        self.recoveries: list[str] = []
        self.outputs: dict[str, str] = {}
        self.drift = Drift()
        self._attempts: dict[str, int] = {}
        self._t0 = time.time()
        self._restarted = False

    # ------------------------------------------------------------------ result helpers
    def _base(self, status, **kw) -> ReplayResult:
        return ReplayResult(status=status, capability=self.cap.id, version=self.cap.version, run_id=self.log.run_id,
                            outputs=self.outputs, recoveries=self.recoveries, drift=self.drift, steps=self.reports,
                            duration_ms=int((time.time() - self._t0) * 1000), evidence_dir=str(self.log.dir), **kw)

    def _fail(self, step: Step | None, expected: str, obs: Observation | None, message: str, code: str | None = None):
        shot = self.log.screenshot(obs.screenshot_png or b"", f"fail-step{step.index if step else 'x'}") if obs else None
        if obs:
            self.log.json(f"fail-step{step.index if step else 'x'}-observation.json", obs.model_dump())
        observed = f"url={obs.url}; dialog={obs.dialog.message if obs and obs.dialog else None}; text[:300]={obs.text[:300]!r}" if obs else None
        self.log.event("replay.failed", step=step.index if step else None, expected=expected, observed=observed,
                       message=message, code=code, screenshot=shot)
        raise _Stop(self._base("failed", failed_step=step.index if step else None, expected=expected, observed=observed,
                               message=message, outcome_code=code, outcome_kind="failure" if code else None))

    def _outcome(self, oc: Outcome, step: Step | None, obs: Observation):
        shot = self.log.screenshot(obs.screenshot_png or b"", f"outcome-{oc.code}")
        self.log.event("replay.outcome", step=step.index if step else None, code=oc.code, outcome_kind=oc.kind, screenshot=shot)
        raise _Stop(self._base("outcome", outcome_code=oc.code, outcome_kind=oc.kind, message=oc.description,
                               failed_step=step.index if step else None))

    # ------------------------------------------------------------------ conditions
    def _handle_conditions(self, step: Step | None, obs: Observation) -> Observation:
        """Apply declared outcomes to the current screen. Returns a (possibly new) observation."""
        for _ in range(3):
            oc = detect_outcome(self.cap.outcomes, obs)
            if oc is None:
                return obs
            if oc.kind == "business":
                self._outcome(oc, step, obs)
            if oc.kind == "failure":
                self._fail(step, "no app error", obs, f"declared failure condition {oc.code}: {oc.description}", oc.code)
            # recoverable
            n = self._attempts.get(oc.code, 0)
            if n >= oc.max_attempts:
                self._fail(step, f"{oc.code} not to recur (max {oc.max_attempts} recoveries)", obs,
                           f"recoverable condition {oc.code} exceeded max_attempts", oc.code)
            self._attempts[oc.code] = n + 1
            self.log.event("replay.recover", step=step.index if step else None, code=oc.code, recovery=oc.recovery, attempt=n + 1)
            self.recoveries.append(f"{oc.code}:{oc.recovery}@step{step.index if step else '-'}")
            if oc.recovery == "dismiss_dialog":
                self.surface.act(Action(type="dismiss_dialog", accept=True))
            elif oc.recovery == "relogin":
                self._restart(step)
                raise _Redo()
            elif oc.recovery == "retry":
                self.surface.act(Action(type="navigate", url=obs.url))
            obs = self.surface.observe()
        return obs

    def _restart(self, upto: Step | None) -> None:
        """Re-run the flow from the entry point up to (not including) the current step, once."""
        if self._restarted:
            self._fail(upto, "session to stay valid", self.surface.observe(), "session expired again after re-login", "SESSION_EXPIRED")
        self._restarted = True
        self.surface.act(Action(type="navigate", url=substitute(self.cap.app.entry_url, self.params)))
        for s in self.cap.steps:
            if upto is not None and s.index >= upto.index:
                break
            self._run_step(s, nested=True)

    # ------------------------------------------------------------------ steps
    def _resolve(self, step: Step, obs: Observation):
        node, rung, coords = resolve(step.target, obs)
        if node is None and coords is None:
            return None, rung, None
        if rung > step.target.recorded_rung:
            self.drift.rung_regressions += 1
            self.log.event("replay.drift", step=step.index, target=step.target.description,
                           recorded_rung=step.target.recorded_rung, used_rung=rung,
                           strategy=step.target.locators[rung].strategy)
        return node, rung, coords

    def _run_step(self, step: Step, nested: bool = False, after_human: bool = False) -> None:
        try:
            self._run_step_inner(step, nested, after_human)
        except _Redo:
            if nested:
                raise
            self._run_step_inner(step, nested, after_human)

    def _run_step_inner(self, step: Step, nested: bool, after_human: bool) -> None:
        t = time.time()
        obs = self.surface.observe()
        obs = self._handle_conditions(step, obs)
        # A modal dialog that no declared outcome claims is a blocked state: page scripts are
        # frozen, so every later step fails with a confusing error. Ask a human instead.
        if obs.dialog is not None and step.action != "dismiss_dialog":
            reason = f"undeclared blocking dialog: {obs.dialog.message[:120]!r}"
            if not after_human and self._escalate(step, obs, reason):
                return self._after_human(step)
            self._fail(step, "no blocking dialog, or a declared outcome that handles it", obs, reason,
                       "UNDECLARED_DIALOG")
        self.policy.check_action(step.action)
        if step.action == "navigate":
            url = substitute(step.url, self.params) or ""
            self.policy.check_url(url)
        if step.risk == "irreversible":
            if not self.confirm_risky:
                self._fail(step, "policy to permit this action", obs, "irreversible step blocked (run with --confirm-risky to escalate to a human)")
            if not self._escalate(step, obs, f"irreversible step needs human confirmation: {step.target.description if step.target else step.action}"):
                raise _Stop(self._base("escalated", failed_step=step.index, message="human did not confirm irreversible step"))

        node = coords = None
        rung = None
        if step.target is not None:
            node, rung, coords = self._resolve(step, obs)
            if node is None and coords is None:
                self.drift.unresolved += 1
                ladder = "; ".join(f"{i}:{l.strategy}={l.name or l.path or l.bbox_frac}" for i, l in enumerate(step.target.locators))
                if not after_human and self._escalate(step, obs, f"could not find {step.target.description}"):
                    return self._after_human(step)
                self._fail(step, f"find {step.target.description} via [{ladder}]", obs, "target not found by any locator strategy")

        action = Action(type=step.action, ref=node.ref if node else None,
                        x=coords[0] if coords else None, y=coords[1] if coords else None,
                        text=substitute(step.value, self.params), url=substitute(step.url, self.params),
                        key=step.key, accept=step.accept, name=step.output)
        res = self.surface.act(action)
        if not res.ok:
            after = self.surface.observe()
            after = self._handle_conditions(step, after)
            if not after_human and self._escalate(step, after, f"action failed: {res.detail}"):
                return self._after_human(step)
            self._fail(step, f"{step.action} on {step.target.description if step.target else step.url} to succeed", after, res.detail)
        if res.extracted:
            for k, v in res.extracted.items():
                out = next((o for o in self.cap.outputs if o.name == k), None)
                if out and out.regex:
                    m = re.search(out.regex, v)
                    v = m.group(1) if m else v
                self.outputs[k] = v

        # wait + verify (poll to absorb transient slowness)
        deadline = time.time() + step.wait.timeout_ms / 1000
        after = self.surface.observe()
        unmet = None
        while True:
            after = self._handle_conditions(step, after)
            unmet = [(e, ob) for e, ob in ((e, _evaluate(_bind(e, self.params), after)) for e in step.expect) if not ob[0]]
            if not unmet or time.time() > deadline:
                break
            time.sleep(0.5)
            after = self.surface.observe()
        if unmet:
            e, (_, observed) = unmet[0]
            if not after_human and self._escalate(step, after, f"post-condition not met: {describe(_bind(e, self.params))}"):
                return self._after_human(step)
            self._fail(step, describe(_bind(e, self.params)), after, f"post-condition not met after {step.wait.timeout_ms} ms")

        rep = StepReport(index=step.index, action=step.action, target=step.target.description if step.target else None,
                         rung_recorded=step.target.recorded_rung if step.target else None, rung_used=rung,
                         strategy_used=step.target.locators[rung].strategy if step.target and rung is not None and rung >= 0 else None,
                         ok=True, detail=res.detail, duration_ms=int((time.time() - t) * 1000))
        if not nested:
            self.reports.append(rep)
        self.log.event("replay.step", **rep.model_dump())

    # ------------------------------------------------------------------ handoff
    def _escalate(self, step: Step, obs: Observation, reason: str) -> bool:
        if self.handoff is None:
            return False
        shot = self.log.screenshot(obs.screenshot_png or b"", f"escalate-step{step.index}")
        req = HandoffRequest(run_id=self.log.run_id, goal=self.cap.goal, step_index=step.index, reason=reason,
                             url=obs.url, screenshot=shot)
        self.log.event("escalate", **req.model_dump())
        return self.handoff(req, self.surface)

    def _after_human(self, step: Step) -> None:
        """Human handed control back. If the step's post-conditions already hold (the human did the
        step), record it as completed by human; otherwise re-run the step once, without escalating again."""
        after = self.surface.observe()
        unmet = [describe(_bind(e, self.params)) for e in step.expect if not _evaluate(_bind(e, self.params), after)[0]]
        self.log.event("replay.resume", step=step.index, unmet=unmet, will_rerun=bool(unmet) or step.action == "extract")
        if unmet or step.action == "extract":
            return self._run_step(step, after_human=True)
        self.reports.append(StepReport(index=step.index, action=step.action, ok=True, detail="completed by human",
                                       target=step.target.description if step.target else None))

    # ------------------------------------------------------------------ entry
    def run(self) -> ReplayResult:
        self.log.event("replay.start", capability=self.cap.id, version=self.cap.version, params=list(self.params),
                       tenant=self.cap.app.tenant)
        try:
            errs = validate_params(self.cap, self.params)
            if errs:
                self._fail(None, "valid params", None, "; ".join(errs))
            entry = substitute(self.cap.app.entry_url, self.params) or ""
            self.policy.check_url(entry)
            self.surface.act(Action(type="navigate", url=entry))
            for step in self.cap.steps:
                self._run_step(step)
            obs = self.surface.observe()
            obs = self._handle_conditions(None, obs)
            for cp in self.cap.checkpoints:
                for e in cp.expect:
                    ok, observed = _evaluate(_bind(e, self.params), obs)
                    if not ok:
                        self._fail(self.cap.steps[cp.after_step], f"checkpoint {cp.name}: {describe(_bind(e, self.params))}", obs, "checkpoint failed")
            missing = [o.name for o in self.cap.outputs if o.name not in self.outputs]
            if missing:
                self._fail(None, f"outputs {missing}", obs, "declared outputs were not extracted")
            total = max(1, sum(1 for s in self.cap.steps if s.target))
            self.drift.score = round((self.drift.rung_regressions + 2 * self.drift.unresolved) / (2 * total), 3)
            self.log.screenshot(obs.screenshot_png or b"", "final")
            res = self._base("success", message="checkpoint verified")
            self.log.event("replay.end", status="success", outputs=self.outputs, drift=self.drift.model_dump())
            return res
        except _Stop as s:
            self.log.event("replay.end", status=s.result.status, code=s.result.outcome_code, message=s.result.message)
            return s.result
        except PolicyViolation as e:
            self.log.event("replay.end", status="failed", message=str(e))
            return self._base("failed", message=f"policy: {e}", expected="action within allowlist", observed=str(e))
