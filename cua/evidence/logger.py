"""Evidence: one folder per run with a structured JSONL log, screenshots, and JSON dumps.

Everything written here passes through the Redactor first.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cua.policy.redact import Redactor


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"


class RunLog:
    def __init__(self, root: Path, run_id: str, redactor: Redactor):
        self.run_id = run_id
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._log = (self.dir / "log.jsonl").open("a")
        self._redactor = redactor
        self._shots = 0

    def event(self, kind: str, **fields: Any) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "run_id": self.run_id, "kind": kind}
        rec.update(self._redactor.obj(fields))
        self._log.write(json.dumps(rec, default=str) + "\n")
        self._log.flush()

    def screenshot(self, png: bytes, label: str) -> str | None:
        if not png:
            return None
        self._shots += 1
        name = f"{self._shots:03d}-{label}.png"
        (self.dir / name).write_bytes(png)
        return name

    def json(self, name: str, obj: Any) -> Path:
        p = self.dir / name
        p.write_text(json.dumps(self._redactor.obj(obj), indent=2, default=str))
        return p

    def text(self, name: str, s: str) -> Path:
        p = self.dir / name
        p.write_text(self._redactor.text(s))
        return p

    def close(self) -> None:
        self._log.close()
