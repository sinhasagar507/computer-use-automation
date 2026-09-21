"""Evaluate expectations and detect declared runtime conditions against an Observation."""

from __future__ import annotations

from cua.artifact.schema import Expect, Locator, Outcome, Target
from cua.surface.base import Node, Observation


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def resolve(target: Target, obs: Observation) -> tuple[Node | None, int, tuple[float, float] | None]:
    """Walk the locator ladder. Returns (node, rung, coords). coords is set only for a bbox hit
    with no node underneath (pure coordinate fallback)."""
    vw, vh = obs.viewport
    for rung, loc in enumerate(target.locators):
        cands = [n for n in obs.nodes if n.frame == loc.frame]
        hit = _match(loc, cands, vw, vh)
        if hit is not None:
            return hit, rung, None
        if loc.strategy == "bbox" and loc.bbox_frac:
            x, y, w, h = loc.bbox_frac
            return None, rung, (x * vw + w * vw / 2, y * vh + h * vh / 2)
    return None, -1, None


def _match(loc: Locator, cands: list[Node], vw: int, vh: int) -> Node | None:
    s = loc.strategy
    if s == "role_name" and loc.name:
        exact = [n for n in cands if n.role == loc.role and n.name == loc.name]
        if len(exact) >= 1:
            return exact[0]
        loose = [n for n in cands if n.role == loc.role and _norm(loc.name) in _norm(n.name)]
        return loose[0] if len(loose) == 1 else None
    if s == "label" and loc.name:
        exact = [n for n in cands if n.label_hint == loc.name and (loc.role is None or n.role == loc.role)]
        if exact:
            return exact[0]
        loose = [n for n in cands if _norm(loc.name) in _norm(n.label_hint) and (loc.role is None or n.role == loc.role)]
        return loose[0] if len(loose) == 1 else None
    if s == "text" and loc.name:
        exact = [n for n in cands if n.name == loc.name]
        return exact[0] if exact else None
    if s == "path" and loc.path:
        exact = [n for n in cands if n.path == loc.path]
        return exact[0] if exact else None
    if s == "bbox" and loc.bbox_frac:
        x, y, w, h = loc.bbox_frac
        cx, cy = x * vw + w * vw / 2, y * vh + h * vh / 2
        inside = [n for n in cands if n.bbox[0] <= cx <= n.bbox[0] + n.bbox[2] and n.bbox[1] <= cy <= n.bbox[1] + n.bbox[3]
                  and n.role not in ("text", "cell")]
        if inside:
            return min(inside, key=lambda n: n.bbox[2] * n.bbox[3])
    return None


def evaluate(e: Expect, obs: Observation) -> tuple[bool, str]:
    """Returns (satisfied, observed-description)."""
    if e.kind == "url_contains":
        urls = obs.all_urls()
        return any((e.value or "") in u for u in urls), f"urls={urls}"
    if e.kind == "text_visible":
        ok = _norm(e.value or "") in _norm(obs.text)
        return ok, f"text[:200]={obs.text[:200]!r}"
    if e.kind == "text_absent":
        return _norm(e.value or "") not in _norm(obs.text), f"text[:200]={obs.text[:200]!r}"
    if e.kind == "node_visible" and e.target:
        node, rung, _ = resolve(e.target, obs)
        return node is not None, f"target {e.target.description} {'found' if node else 'not found'}"
    if e.kind == "dialog_open":
        return obs.dialog is not None, f"dialog={obs.dialog.message!r}" if obs.dialog else "no dialog"
    if e.kind == "http_status":
        return str(obs.http_status) == (e.value or ""), f"http_status={obs.http_status}"
    return False, f"unsupported expect {e.kind}"


def detect_outcome(outcomes: list[Outcome], obs: Observation) -> Outcome | None:
    """First declared condition whose detector matches the current screen."""
    for oc in outcomes:
        ok, _ = evaluate(oc.detect, obs)
        if ok:
            return oc
    return None


def describe(e: Expect) -> str:
    if e.kind in ("url_contains", "text_visible", "text_absent", "http_status"):
        return f"{e.kind} {e.value!r}"
    if e.kind == "node_visible" and e.target:
        return f"node_visible {e.target.description}"
    return e.kind
