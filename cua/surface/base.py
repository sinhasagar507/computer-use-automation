"""Surface seam.

A Surface is *how we perceive and act on* an application. The recorded flow (artifact)
never references a concrete surface. Anything that can produce an Observation and
execute an Action can back the agent loop and the replay engine: a browser today,
an OS accessibility API for a desktop app tomorrow.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, Field

ActionType = Literal["navigate", "click", "type", "select", "press", "extract", "dismiss_dialog", "done"]


class Node(BaseModel):
    """One perceivable control or text element, in page coordinates."""

    ref: str                      # stable within one observation, e.g. "e12"
    frame: str                    # frame name ("" for main)
    role: str                     # link | button | textbox | combobox | cell | heading | text ...
    name: str                     # accessible name (label, text, value, placeholder)
    label_hint: str = ""          # text of the nearest preceding cell/label (legacy table layouts)
    value: str | None = None
    tag: str = ""
    path: str = ""                # structural path inside the frame, e.g. "table[1]>tr[3]>td[2]>input[1]"
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)  # x, y, w, h in page px
    enabled: bool = True


class DialogInfo(BaseModel):
    type: str
    message: str


class Observation(BaseModel):
    url: str
    title: str
    frames: list[str]
    nodes: list[Node]
    text: str                                   # visible text digest, per frame
    dialog: DialogInfo | None = None            # a blocking dialog is open
    http_status: int | None = None              # last main-resource status if known
    viewport: tuple[int, int] = (1280, 900)
    screenshot_png: bytes | None = Field(default=None, exclude=True)

    def find(self, ref: str) -> Node | None:
        return next((n for n in self.nodes if n.ref == ref), None)


class Action(BaseModel):
    type: ActionType
    ref: str | None = None          # node ref from the current observation
    x: float | None = None          # or page coordinates (fallback when no ref)
    y: float | None = None
    text: str | None = None         # for type / select / extract(label)
    url: str | None = None          # for navigate
    key: str | None = None          # for press
    accept: bool = True             # for dismiss_dialog
    name: str | None = None         # for extract: output name
    reason: str = ""


class ActionResult(BaseModel):
    ok: bool
    detail: str = ""
    extracted: dict[str, str] = Field(default_factory=dict)


class Surface(Protocol):
    def observe(self) -> Observation: ...
    def act(self, action: Action) -> ActionResult: ...
    def screenshot(self) -> bytes: ...
    def close(self) -> None: ...
