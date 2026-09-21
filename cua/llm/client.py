"""OpenAI-compatible vision LLM client. Default endpoint: OpenRouter, model: Qwen3-VL (open weights).

The model sees a screenshot plus a compact node list and answers with ONE JSON action.
We use JSON-in-text rather than provider tool-calling because it behaves identically
across OpenRouter, vLLM, and Ollama.
"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Protocol

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from cua.surface.base import Action, Observation

SYSTEM_PROMPT = """You are a computer-use agent operating a legacy back-office banking application on behalf of a bank employee.
You see a screenshot and a list of on-screen elements with refs like m_3 or main_12. Frames are listed by name.
You act ONE step at a time. Respond with exactly one JSON object and nothing else:

{"reason": "<short why>", "action": {"type": "<type>", ...fields}}

Action types and fields:
- navigate: {"url": "..."}                      (only within the allowed site)
- click:    {"ref": "<ref>"}                     or {"x": <px>, "y": <px>} if no ref fits
- type:     {"ref": "<ref>", "text": "..."}      (replaces the field content)
- select:   {"ref": "<ref>", "text": "<option label>"}
- press:    {"ref": "<ref>", "key": "Enter"}
- extract:  {"ref": "<ref>", "name": "<output_name>"}   (read the text of an element as a named output)
- dismiss_dialog: {"accept": true|false}          (when a blocking dialog is open)
- done:     {"evidence": "<one short phrase copied verbatim from the current screen>"}
- escalate: {"reason": "..."}                    (you are stuck, blocked, or the next step is risky/irreversible)

Rules:
- Use the ref of an element from the CURRENT element list. Prefer refs over coordinates.
- For parameters, type the literal placeholder {{params.<name>}} (e.g. {{params.member_id}}) instead of the value. The system substitutes it. Never invent parameter values.
- Extract every output the goal asks for with an `extract` action BEFORE `done`.
- `evidence` must be ONE contiguous phrase that appears on the screen exactly as you write it, such as a
  page heading ("Member Detail") or a confirmation line ("The new sub-account was opened successfully").
  Never join two values together, and never summarise. This phrase becomes the replay checkpoint.
- Do not perform irreversible actions (posting transactions, closing accounts, wires). Escalate instead.
- If the same action fails twice, try a different element or escalate.
- Legacy layouts: field labels are usually in the cell to the left (shown as hint=...).
"""


class Decision(BaseModel):
    reason: str = ""
    action: Action


class LLM(Protocol):
    model: str
    def decide(self, goal: str, params_desc: str, history: list[str], obs: Observation, extra: str = "") -> Decision: ...


def format_observation(obs: Observation, max_nodes: int = 120) -> str:
    lines = [f"URL: {obs.url}", f"Title: {obs.title}", f"Frames: {', '.join(f or 'main' for f in obs.frames)}"]
    if obs.http_status and obs.http_status >= 400:
        lines.append(f"Last HTTP status: {obs.http_status}")
    if obs.dialog:
        lines.append(f"!! BLOCKING DIALOG OPEN ({obs.dialog.type}): {obs.dialog.message!r}. Use dismiss_dialog.")
    lines.append("Elements:")
    for n in obs.nodes[:max_nodes]:
        if n.role in ("text", "cell", "columnheader", "heading", "label") and len(n.name) > 90:
            continue
        parts = [f"{n.ref} [{n.role}]"]
        if n.name:
            parts.append(f'name="{n.name}"')
        if n.label_hint and n.label_hint != n.name:
            parts.append(f'hint="{n.label_hint}"')
        if n.value:
            parts.append(f'value="{n.value}"')
        parts.append(f"frame={n.frame or 'main'}")
        x, y, w, h = n.bbox
        parts.append(f"at=({x:.0f},{y:.0f},{w:.0f}x{h:.0f})")
        if not n.enabled:
            parts.append("disabled")
        lines.append("  " + " ".join(parts))
    if len(obs.nodes) > max_nodes:
        lines.append(f"  ... {len(obs.nodes) - max_nodes} more")
    lines.append("Visible text:")
    lines.append(obs.text[:2500])
    return "\n".join(lines)


_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_decision(raw: str) -> Decision:
    txt = raw.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.S)
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        m = _JSON_RE.search(txt)
        if not m:
            raise ValueError(f"no JSON in model reply: {raw[:200]!r}")
        data = json.loads(m.group(0))
    if "action" not in data and "type" in data:
        data = {"reason": data.pop("reason", ""), "action": data}
    act = data.get("action") or {}
    if isinstance(act, str):
        act = {"type": act}
    if act.get("type") == "escalate" and "reason" in act and not data.get("reason"):
        data["reason"] = act["reason"]
    act.setdefault("reason", data.get("reason", ""))
    try:
        return Decision(reason=data.get("reason", ""), action=Action(**act))
    except ValidationError as e:
        raise ValueError(f"invalid action: {e.errors()[0]['msg']} in {act}") from e


class OpenAICompatLLM:
    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None,
                 temperature: float = 0.1):
        self.model = model or os.environ.get("CUA_LLM_MODEL", "qwen/qwen3-vl-235b-a22b-instruct")
        self.base_url = base_url or os.environ.get("CUA_LLM_BASE_URL", "https://openrouter.ai/api/v1")
        key = api_key or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY") or "none"
        self._client = OpenAI(base_url=self.base_url, api_key=key,
                              default_headers={"HTTP-Referer": "https://github.com/cua-takehome", "X-Title": "cua-takehome"})
        self.temperature = temperature
        self.last_usage: dict = {}

    def decide(self, goal: str, params_desc: str, history: list[str], obs: Observation, extra: str = "") -> Decision:
        user_text = (
            f"GOAL: {goal}\n\nPARAMETERS AVAILABLE (type the placeholder, not the value):\n{params_desc}\n\n"
            f"HISTORY (most recent last):\n" + ("\n".join(history[-8:]) or "(none)") + "\n\n"
            f"{extra}\n\nCURRENT SCREEN:\n{format_observation(obs)}\n\nRespond with one JSON object."
        )
        content: list[dict] = [{"type": "text", "text": user_text}]
        if obs.screenshot_png:
            b64 = base64.b64encode(obs.screenshot_png).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
        resp = self._client.chat.completions.create(
            model=self.model, temperature=self.temperature, max_tokens=400,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
        )
        self.last_usage = resp.usage.model_dump() if resp.usage else {}
        raw = resp.choices[0].message.content or ""
        return parse_decision(raw)
