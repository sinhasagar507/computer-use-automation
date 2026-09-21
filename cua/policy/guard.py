"""Allowlist and risk classification. Enforced by both the agent loop and the replay engine."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field

from cua.artifact.schema import RiskClass


class PolicyViolation(Exception):
    pass


class Policy(BaseModel):
    hosts: list[str]
    routes: list[str]
    actions: list[str]
    irreversible_patterns: list[str] = Field(default_factory=list)
    reversible_write_patterns: list[str] = Field(default_factory=list)
    redact_param_names: list[str] = Field(default_factory=list)
    redact_patterns: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path | str = "config/policy.yaml") -> "Policy":
        raw = yaml.safe_load(Path(path).read_text())
        return cls(
            hosts=raw["allowlist"]["hosts"],
            routes=raw["allowlist"]["routes"],
            actions=raw["allowlist"]["actions"],
            irreversible_patterns=raw.get("risk", {}).get("irreversible_patterns", []),
            reversible_write_patterns=raw.get("risk", {}).get("reversible_write_patterns", []),
            redact_param_names=raw.get("redact", {}).get("param_names", []),
            redact_patterns=raw.get("redact", {}).get("patterns", []),
        )

    # --- allowlist ---------------------------------------------------------
    def url_allowed(self, url: str) -> bool:
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            return False
        if u.hostname not in self.hosts:
            return False
        path = u.path or "/"
        for r in self.routes:
            if r == "/":
                if path == "/":
                    return True
                continue
            if path == r or path.startswith(r.rstrip("/") + "/"):
                return True
        return False

    def check_url(self, url: str) -> None:
        if not self.url_allowed(url):
            raise PolicyViolation(f"URL outside allowlist: {url}")

    def check_action(self, action_type: str) -> None:
        if action_type not in self.actions:
            raise PolicyViolation(f"action type not allowed: {action_type}")

    # --- risk ----------------------------------------------------------------
    def classify(self, action_type: str, target_name: str | None, value: str | None = None) -> RiskClass:
        if action_type in ("extract", "navigate", "type", "select", "dismiss_dialog", "press"):
            # typing/selecting never commits anything by itself; the commit is the click.
            if action_type != "press":
                return "read"
        hay = f"{target_name or ''} {value or ''}".lower()
        if any(p in hay for p in self.irreversible_patterns):
            return "irreversible"
        if any(p in hay for p in self.reversible_write_patterns):
            return "reversible_write"
        return "read"


RISK_ORDER: dict[RiskClass, int] = {"read": 0, "reversible_write": 1, "irreversible": 2}


def max_risk(a: RiskClass, b: RiskClass) -> RiskClass:
    return a if RISK_ORDER[a] >= RISK_ORDER[b] else b
