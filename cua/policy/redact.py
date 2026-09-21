"""Redaction for logs, prompts, and artifacts. Secrets and sensitive param values never persist."""

from __future__ import annotations

import os
import re
from typing import Any


class Redactor:
    def __init__(self, patterns: list[str], param_names: list[str], sensitive_values: dict[str, str] | None = None):
        self._res = [re.compile(p) for p in patterns]
        self._param_names = {p.lower() for p in param_names}
        self._values: dict[str, str] = {}
        # Never let the LLM key or mock credentials reach disk, even by accident.
        for env in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "MOCKBANK_PASSWORD"):
            v = os.environ.get(env)
            if v and len(v) >= 6:
                self._values[v] = f"<{env}>"
        for k, v in (sensitive_values or {}).items():
            if v and len(v) >= 3:
                self._values[v] = f"<{k}>"

    def add_sensitive(self, name: str, value: str) -> None:
        if value and len(value) >= 3:
            self._values[value] = f"<{name}>"

    def is_sensitive_param(self, name: str) -> bool:
        return name.lower() in self._param_names

    def text(self, s: str) -> str:
        if not s:
            return s
        for v, tag in self._values.items():
            s = s.replace(v, tag)
        for r in self._res:
            s = r.sub("<redacted>", s)
        return s

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: ("<redacted>" if isinstance(k, str) and self.is_sensitive_param(k) and v else self.obj(v)) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(x) for x in o]
        return o
