"""Goal-driven discovery loop: observe -> decide (LLM) -> act, under policy, with evidence.

Stop conditions: goal met (done), max steps, timeout, dead end (repeated failures), policy
block, or escalation. Escalation goes through the HandoffController seam so a human can
take the live session and hand it back; the loop then resumes with a fresh observation.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Callable, Literal

from pydantic import BaseModel, Field

from cua.evidence.logger import RunLog
from cua.llm.client import LLM
from cua.policy.guard import Policy, PolicyViolation
from cua.surface.base import Action, ActionResult, Node, Observation, Surface

PARAM_RE = re.compile(r"\{\{\s*params\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class TraceStep(BaseModel):
    index: int
    url: str
    title: str
    reason: str
    action: Action                       # as decided (placeholders intact, never substituted values)
    target: Node | None = None
    result: ActionResult
    screenshot: str | None = None
    url_after: str | None = None
    dialog_before: bool = False
    risk: str = "read"


class DiscoveryResult(BaseModel):
    status: Literal["success", "max_steps", "timeout", "dead_end", "blocked", "escalated", "llm_error"]
    detail: str = ""
    trace: list[TraceStep] = Field(default_factory=list)
    outputs: dict[str, str] = Field(default_factory=dict)
    evidence_text: str | None = None
    final_url: str | None = None
    transcript_sha256: str = ""
    llm_calls: int = 0


class HandoffRequest(BaseModel):
    run_id: str
    goal: str
    step_index: int
    reason: str
    url: str
    screenshot: str | None = None


# Seam: implemented by cua.handoff.controller. Returns True when a human handed control back.
HandoffFn = Callable[[HandoffRequest, Surface], bool]


def substitute(value: str | None, params: dict[str, str]) -> str | None:
    if value is None:
        return None
    return PARAM_RE.sub(lambda m: params.get(m.group(1), m.group(0)), value)


def describe_params(params: dict[str, str], sensitive: set[str]) -> str:
    if not params:
        return "(none)"
    out = []
    for k, v in params.items():
        out.append(f"- {{{{params.{k}}}}}" + ("  (sensitive; value hidden)" if k in sensitive else f"  = {v}"))
    return "\n".join(out)


def run_discovery(
    goal: str, entry_url: str, params: dict[str, str], sensitive: set[str],
    policy: Policy, surface: Surface, llm: LLM, log: RunLog,
    handoff: HandoffFn | None = None, max_steps: int = 25, timeout_s: float = 300, confirm_risky: bool = False,
) -> DiscoveryResult:
    t0 = time.time()
    trace: list[TraceStep] = []
    history: list[str] = []
    outputs: dict[str, str] = {}
    consecutive_failures = 0
    recent_actions: list[str] = []
    llm_calls = 0
    extra = ""
    transcript = hashlib.sha256()

    policy.check_url(entry_url)
    log.event("discovery.start", goal=goal, entry_url=entry_url, params=list(params), model=getattr(llm, "model", "?"))
    surface.act(Action(type="navigate", url=entry_url))

    def finish(status, detail="", evidence=None):
        final_url = None
        if status == "success":
            o = surface.observe()
            final_url = next((o.url_of(f) for f, t in o.frame_texts.items() if evidence and evidence in t), o.url)
        res = DiscoveryResult(status=status, detail=detail, trace=trace, outputs=outputs, evidence_text=evidence,
                              final_url=final_url,
                              transcript_sha256=transcript.hexdigest(), llm_calls=llm_calls)
        log.event("discovery.end", status=status, detail=detail, steps=len(trace), outputs=outputs, llm_calls=llm_calls)
        return res

    for i in range(max_steps):
        if time.time() - t0 > timeout_s:
            return finish("timeout", f"exceeded {timeout_s}s")
        obs = surface.observe()
        shot = log.screenshot(obs.screenshot_png or b"", f"step{i:02d}")
        try:
            decision = llm.decide(goal, describe_params(params, sensitive), history, obs, extra)
            llm_calls += 1
        except ValueError as e:                     # unparseable reply: tell the model once, then give up
            log.event("llm.parse_error", step=i, error=str(e))
            extra = f"Your previous reply was invalid: {e}. Reply with exactly one JSON object."
            llm_calls += 1
            if extra and consecutive_failures >= 2:
                return finish("llm_error", str(e))
            consecutive_failures += 1
            continue
        except Exception as e:  # noqa: BLE001  (network, auth)
            log.event("llm.error", step=i, error=f"{type(e).__name__}: {e}")
            return finish("llm_error", f"{type(e).__name__}: {e}")
        extra = ""
        action = decision.action
        transcript.update(json.dumps({"i": i, "url": obs.url, "a": action.model_dump()}, sort_keys=True).encode())
        node = obs.find(action.ref) if action.ref else None
        log.event("llm.decision", step=i, reason=decision.reason, action=action.model_dump(exclude_none=True),
                  target=node.model_dump() if node else None, usage=getattr(llm, "last_usage", {}))

        # --- policy -----------------------------------------------------------
        try:
            policy.check_action(action.type)
            if action.type == "navigate" and action.url:
                policy.check_url(substitute(action.url, params) or "")
        except PolicyViolation as e:
            log.event("policy.block", step=i, error=str(e))
            history.append(f"{i}: {action.type} BLOCKED by policy: {e}")
            extra = f"Policy blocked your last action: {e}. Choose another action or escalate."
            consecutive_failures += 1
            if consecutive_failures >= 3:
                return finish("blocked", str(e))
            continue
        risk = policy.classify(action.type, node.name if node else None, action.text)
        if risk == "irreversible" and not confirm_risky:
            log.event("policy.risk_block", step=i, risk=risk, target=node.name if node else None)
            history.append(f"{i}: {action.type} on {node.name if node else action.ref} BLOCKED: irreversible action")
            extra = "That action is classified irreversible and is blocked. Escalate if the goal requires it."
            consecutive_failures += 1
            if consecutive_failures >= 3:
                return finish("blocked", "irreversible action required")
            continue

        # --- terminal actions ---------------------------------------------------
        if action.type == "done":
            # The run is only successful if the model can point at text that is really on the
            # screen. This is what turns "the model says it worked" into a replayable checkpoint.
            ev = (action.evidence or "").strip()
            if ev and " ".join(ev.split()).lower() not in " ".join(obs.text.split()).lower():
                log.event("done.evidence_missing", step=i, evidence=ev)
                history.append(f"{i}: done rejected: evidence text {ev!r} is not on screen")
                extra = (f"Your evidence {ev!r} does not appear on the screen as one contiguous phrase, so the goal "
                         f"is not verified. Copy ONE short phrase exactly as it appears (a heading or a status line), "
                         f"or take another action first. Do not join several values together.")
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    return finish("dead_end", "could not verify success evidence")
                continue
            trace.append(TraceStep(index=i, url=obs.url, title=obs.title, reason=decision.reason, action=action,
                                   result=ActionResult(ok=True, detail="done"), screenshot=shot, url_after=obs.url))
            return finish("success", decision.reason, ev)
        if action.type == "escalate" or (risk == "irreversible" and confirm_risky):
            reason = decision.reason or "model requested escalation"
            if risk == "irreversible":
                reason = f"irreversible action needs human confirmation: {node.name if node else action.ref}"
            req = HandoffRequest(run_id=log.run_id, goal=goal, step_index=i, reason=reason, url=obs.url, screenshot=shot)
            log.event("escalate", **req.model_dump())
            if handoff is None or not handoff(req, surface):
                return finish("escalated", reason)
            history.append(f"{i}: escalated to human ({reason}); human handed control back. Re-observe.")
            consecutive_failures = 0
            continue

        # --- act ------------------------------------------------------------------
        exec_action = action.model_copy(update={"text": substitute(action.text, params),
                                                "url": substitute(action.url, params)})
        result = surface.act(exec_action)
        after = surface.observe()
        if result.extracted:
            outputs.update(result.extracted)
        frame = node.frame if node else None
        step = TraceStep(index=i, url=obs.url_of(frame), title=obs.title, reason=decision.reason, action=action, target=node,
                         result=result, screenshot=shot, url_after=after.url_of(frame), dialog_before=obs.dialog is not None, risk=risk)
        trace.append(step)
        log.event("action", step=i, action=action.model_dump(exclude_none=True), ok=result.ok, detail=result.detail,
                  url_after=after.url, extracted=result.extracted)
        history.append(f"{i}: {action.type} {action.ref or action.url or ''} {('text=' + repr(action.text)) if action.text else ''}"
                       f" -> {'ok' if result.ok else 'FAILED'}: {result.detail}")

        # --- stuck detection ------------------------------------------------------
        sig = f"{action.type}:{action.ref}:{action.text}:{obs.url}"
        recent_actions.append(sig)
        if not result.ok:
            consecutive_failures += 1
        else:
            consecutive_failures = 0
        if consecutive_failures >= 3 or recent_actions[-3:].count(sig) == 3:
            reason = "three consecutive failures" if consecutive_failures >= 3 else "repeating the same action"
            req = HandoffRequest(run_id=log.run_id, goal=goal, step_index=i, reason=reason, url=after.url, screenshot=shot)
            log.event("escalate", **req.model_dump())
            if handoff is None or not handoff(req, surface):
                return finish("dead_end", reason)
            history.append(f"{i}: stuck ({reason}); human intervened and handed back. Re-observe.")
            consecutive_failures = 0
            recent_actions.clear()
    return finish("max_steps", f"{max_steps} steps")
