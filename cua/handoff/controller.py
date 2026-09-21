"""Human-in-the-loop handoff on the SAME live session.

Control-transfer model
- Exactly one party owns the session at a time: ControlOwner.AUTOMATION or ControlOwner.HUMAN.
- Automation cedes control by *blocking* inside this call. It issues no commands while the
  human owns the session. The human works in the very browser window Playwright opened
  (run headed), so cookies, frames, scroll position and page state are all preserved.
- While the human owns the session, the Surface records what they do (clicks, field changes
  with passwords masked in the page, submits, navigations). That record is evidence.
- The human returns control through the operator page (web), the terminal (cli), or a
  callback (tests). The caller then re-observes and resumes on the same session.
- An unanswered request returns False after `timeout_s`; the run ends as escalated.

Mocked deliberately: the operator page is a minimal local console, not a real co-browsing
product. The seam it sits on -- request, ownership, recording, hand back -- is real.
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Callable, Literal

from cua.agent.loop import HandoffRequest
from cua.evidence.logger import RunLog
from cua.surface.base import Surface


class ControlOwner(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"


class HandoffController:
    """Implements the HandoffFn seam used by the agent loop and the replay engine."""

    def __init__(self, log: RunLog, mode: Literal["web", "cli", "callback", "none"] = "cli",
                 timeout_s: float = 600, callback: Callable[[HandoffRequest, Surface], None] | None = None,
                 web_port: int = 5055):
        self.log = log
        self.mode = mode
        self.timeout_s = timeout_s
        self.callback = callback
        self.owner = ControlOwner.AUTOMATION
        self.request: HandoffRequest | None = None
        self.human_actions: list[dict] = []
        self.history: list[dict] = []
        self.web_port = web_port
        self._resume = threading.Event()
        self._surface: Surface | None = None
        if mode == "web":
            from cua.handoff.operator_ui import start_operator_ui
            start_operator_ui(self, web_port)

    # --- API used by the operator UI -----------------------------------------
    def state(self) -> dict:
        live = self.human_actions
        if self.owner is ControlOwner.HUMAN and hasattr(self._surface, "drain_human_events"):
            live = self._surface.drain_human_events()
        return {"owner": self.owner.value, "request": self.request.model_dump() if self.request else None,
                "human_actions": live, "history": self.history, "evidence_dir": str(self.log.dir)}

    def hand_back(self) -> None:
        """Called by the human. Ends the wait; automation resumes on the same session."""
        self._resume.set()

    # --- the HandoffFn seam ----------------------------------------------------
    def __call__(self, req: HandoffRequest, surface: Surface) -> bool:
        if self.mode == "none":
            self.log.event("handoff.unavailable", reason=req.reason)
            return False
        self._surface = surface
        self.request = req
        self.human_actions = []
        self._resume.clear()
        self.owner = ControlOwner.HUMAN
        self.log.json(f"intervention-step{req.step_index}.json", req.model_dump())
        self.log.event("handoff.request", owner=self.owner.value, **req.model_dump())
        if hasattr(surface, "start_human_recording"):
            surface.start_human_recording()

        t0 = time.time()
        if self.mode == "callback":
            if self.callback:
                self.callback(req, surface)
                self._resume.set()
        elif self.mode == "cli":
            print(f"\n=== HUMAN INTERVENTION REQUESTED (step {req.step_index}) ===\n"
                  f"reason:     {req.reason}\nurl:        {req.url}\nscreenshot: {self.log.dir}/{req.screenshot}\n"
                  f"You now control the browser window. Do the manual steps, then press Enter to hand back.")
            threading.Thread(target=lambda: (input(), self._resume.set()), daemon=True).start()
        elif self.mode == "web":
            print(f"\n=== HUMAN INTERVENTION REQUESTED: open http://127.0.0.1:{self.web_port}/ ===")

        resumed = self._resume.wait(self.timeout_s)

        if hasattr(surface, "stop_human_recording"):
            self.human_actions = surface.stop_human_recording()
        self.owner = ControlOwner.AUTOMATION
        wait = getattr(surface, "wait_for_settle", None)
        if wait:
            wait(3000)
        for a in self.human_actions:
            self.log.event("human.action", **a)
        rec = {"step": req.step_index, "reason": req.reason, "resumed": resumed,
               "seconds": round(time.time() - t0, 1), "human_actions": len(self.human_actions)}
        self.history.append(rec)
        self.log.event("handoff.resume" if resumed else "handoff.timeout", owner=self.owner.value, **rec)
        self.request = None
        return resumed
