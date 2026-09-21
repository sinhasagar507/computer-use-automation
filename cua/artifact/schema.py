"""Capability artifact schema (v1).

The artifact is the contract between the discovering model and the production caller.
It is *not* a transcript: it stores what to do, how to find each control, what the caller
must supply, what it gets back, what "success" means, and which runtime conditions are
legitimate outcomes rather than crashes.

Design notes
- Targets carry a *locator ladder*: ordered strategies from most semantic (role+name) to
  most geometric (bbox fraction). Replay tries them in order and records which rung hit;
  a lower rung than recorded is a drift signal, not a failure.
- Values may reference params as {{params.<name>}} so one recording serves many invocations.
- Outcomes are declared per capability so the caller can branch on codes, not on prose.
- Tenant overlays override names/routes only; the flow is shared across tenants.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

RiskClass = Literal["read", "reversible_write", "irreversible"]
StepAction = Literal["navigate", "click", "type", "select", "press", "extract", "dismiss_dialog"]


class Locator(BaseModel):
    strategy: Literal["role_name", "label", "text", "path", "bbox"]
    frame: str = ""
    role: str | None = None
    name: str | None = None          # role_name: accessible name; label: label/adjacent cell text; text: exact text
    path: str | None = None          # structural path inside frame
    bbox_frac: tuple[float, float, float, float] | None = None  # x,y,w,h as fraction of viewport
    exact: bool = True


class Target(BaseModel):
    description: str                 # human-readable: "Member ID textbox"
    locators: list[Locator]          # ladder, best first
    recorded_rung: int = 0           # index in ladder that identified it at discovery


class Param(BaseModel):
    name: str
    type: Literal["string", "integer", "number", "boolean"] = "string"
    required: bool = True
    description: str = ""
    example: str | None = None
    pattern: str | None = None
    sensitive: bool = False          # masked in logs/evidence


class Expect(BaseModel):
    kind: Literal["url_contains", "text_visible", "text_absent", "node_visible", "dialog_open", "http_status"]
    value: str | None = None
    target: Target | None = None
    frame: str | None = None


class Wait(BaseModel):
    kind: Literal["load", "text_visible", "node_visible", "none"] = "load"
    value: str | None = None
    target: Target | None = None
    timeout_ms: int = 8000


class Step(BaseModel):
    index: int
    action: StepAction
    target: Target | None = None
    value: str | None = None         # typed text / option label; may contain {{params.x}}
    url: str | None = None           # navigate; may contain {{params.x}}
    key: str | None = None
    accept: bool = True              # dismiss_dialog
    output: str | None = None        # extract: output name
    wait: Wait = Field(default_factory=Wait)
    expect: list[Expect] = Field(default_factory=list)   # post-conditions checked after the step
    risk: RiskClass = "read"
    note: str = ""                   # the model's stated reason at discovery (redacted)


class Output(BaseModel):
    name: str
    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str = ""
    target: Target                   # where to read it
    regex: str | None = None         # optional post-processing capture group 1
    after_step: int


class Outcome(BaseModel):
    """A declared runtime condition and how replay must treat it."""

    code: str                                    # e.g. MEMBER_NOT_FOUND
    kind: Literal["business", "recoverable", "failure"]
    description: str = ""
    detect: Expect                               # condition that identifies it
    recovery: Literal["none", "dismiss_dialog", "retry", "relogin"] = "none"
    max_attempts: int = 1


class Checkpoint(BaseModel):
    name: str
    after_step: int
    expect: list[Expect]


class AppRef(BaseModel):
    vendor: str
    product: str
    version: str
    tenant: str
    entry_url: str


class Provenance(BaseModel):
    discovered_by: str                           # model id
    run_id: str
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    transcript_sha256: str
    steps_taken: int
    surface: str = "playwright-chromium"


class Capability(BaseModel):
    schema_version: str = SCHEMA_VERSION
    id: str                                      # slug, stable across versions
    name: str
    version: str = "1.0.0"
    status: Literal["draft", "approved"] = "draft"
    description: str
    goal: str                                    # the original natural-language goal (redacted)
    app: AppRef
    params: list[Param]
    outputs: list[Output]
    steps: list[Step]
    checkpoints: list[Checkpoint]
    outcomes: list[Outcome]
    risk_max: RiskClass = "read"
    provenance: Provenance

    def param_names(self) -> set[str]:
        return {p.name for p in self.params}


class TenantOverlay(BaseModel):
    """Per-tenant specialization of a shared capability. Overrides only surface details."""

    tenant: str
    app_version: str | None = None
    entry_url: str | None = None
    route_map: dict[str, str] = Field(default_factory=dict)   # "/members/search" -> "/member/lookup"
    name_map: dict[str, str] = Field(default_factory=dict)    # "Member ID" -> "Member Number"
