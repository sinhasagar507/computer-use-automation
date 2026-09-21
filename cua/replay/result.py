"""Replay result contract.

status:
  success   - all steps ran, checkpoint verified, outputs returned
  outcome   - a declared *business* outcome was detected (e.g. MEMBER_NOT_FOUND); not a failure
  failed    - a hard failure; failed_step/expected/observed say where and why
  escalated - a human was asked to intervene and did not (or could not) hand back a good state
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class StepReport(BaseModel):
    index: int
    action: str
    target: str | None = None
    rung_recorded: int | None = None
    rung_used: int | None = None          # which locator strategy resolved the target this time
    strategy_used: str | None = None
    ok: bool
    detail: str = ""
    duration_ms: int = 0
    screenshot: str | None = None


class Drift(BaseModel):
    rung_regressions: int = 0             # targets resolved by a weaker strategy than recorded
    unresolved: int = 0
    score: float = 0.0                    # 0 = identical to recording, 1 = nothing matched


class ReplayResult(BaseModel):
    status: Literal["success", "outcome", "failed", "escalated"]
    capability: str
    version: str
    run_id: str
    outcome_code: str | None = None       # for status=outcome (and for failed when a declared failure matched)
    outcome_kind: str | None = None
    message: str = ""
    outputs: dict[str, str] = Field(default_factory=dict)
    failed_step: int | None = None
    expected: str | None = None
    observed: str | None = None
    recoveries: list[str] = Field(default_factory=list)
    drift: Drift = Field(default_factory=Drift)
    steps: list[StepReport] = Field(default_factory=list)
    duration_ms: int = 0
    evidence_dir: str | None = None
