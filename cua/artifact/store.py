"""Load/save capability artifacts and apply tenant overlays."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from cua.artifact.schema import Capability, TenantOverlay


def save(cap: Capability, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cap.model_dump_json(indent=2))
    return path


def load(path: Path) -> Capability:
    return Capability.model_validate_json(Path(path).read_text())


def content_hash(cap: Capability) -> str:
    data = cap.model_dump(mode="json", exclude={"provenance"})
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def load_overlay(path: Path) -> TenantOverlay:
    return TenantOverlay.model_validate_json(Path(path).read_text())


def apply_overlay(cap: Capability, ov: TenantOverlay) -> Capability:
    """Return a specialized copy: routes and control names substituted, flow unchanged."""

    def sub_route(s: str | None) -> str | None:
        if not s:
            return s
        for old, new in ov.route_map.items():
            s = s.replace(old, new)
        return s

    def sub_name(s: str | None) -> str | None:
        if not s:
            return s
        for old, new in ov.name_map.items():
            s = s.replace(old, new)
        return s

    c = copy.deepcopy(cap)
    c.app.tenant = ov.tenant
    if ov.app_version:
        c.app.version = ov.app_version
    if ov.entry_url:
        c.app.entry_url = ov.entry_url

    def fix_target(t):
        if t is None:
            return
        t.description = sub_name(t.description)
        for loc in t.locators:
            loc.name = sub_name(loc.name)

    def fix_expect(e):
        e.value = sub_name(sub_route(e.value))
        fix_target(e.target)

    for s in c.steps:
        s.url = sub_route(s.url)
        fix_target(s.target)
        for e in s.expect:
            fix_expect(e)
        fix_target(s.wait.target)
        s.wait.value = sub_name(s.wait.value)
    for o in c.outputs:
        fix_target(o.target)
    for cp in c.checkpoints:
        for e in cp.expect:
            fix_expect(e)
    for oc in c.outcomes:
        fix_expect(oc.detect)
    return c
