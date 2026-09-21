"""Playwright-backed Surface.

Perception: a screenshot plus a compact accessibility-style tree that we build ourselves
by walking every frame. We do not rely on ids, test ids, or semantic markup — only on
what an operator can see: roles inferred from tags, names from labels/adjacent cells/text,
and bounding boxes in page coordinates. That is what makes the same recipe portable to
legacy web apps and (via a different Surface) to desktop accessibility trees.
"""

from __future__ import annotations

from playwright.sync_api import Dialog, Frame, Page, TimeoutError as PWTimeout, sync_playwright

from cua.surface.base import Action, ActionResult, DialogInfo, Node, Observation

# Runs inside each frame. Tags elements with data-cua-ref and returns a compact node list.
_COLLECT_JS = r"""
(prefix) => {
  const out = [];
  let i = 0;
  const seen = new Set();
  const roleOf = (el) => {
    const t = el.tagName.toLowerCase();
    const ty = (el.getAttribute('type') || '').toLowerCase();
    if (el.getAttribute('role')) return el.getAttribute('role');
    if (t === 'a' && el.hasAttribute('href')) return 'link';
    if (t === 'button' || (t === 'input' && (ty === 'submit' || ty === 'button' || ty === 'reset'))) return 'button';
    if (t === 'input' && ty === 'password') return 'textbox';
    if (t === 'input' && (ty === 'checkbox')) return 'checkbox';
    if (t === 'input' && (ty === 'radio')) return 'radio';
    if (t === 'input' || t === 'textarea') return 'textbox';
    if (t === 'select') return 'combobox';
    if (t === 'th') return 'columnheader';
    if (t === 'td') return 'cell';
    if (/^h[1-6]$/.test(t)) return 'heading';
    if (t === 'label') return 'label';
    return 'text';
  };
  const txt = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const nameOf = (el, role) => {
    const t = el.tagName.toLowerCase();
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    if (t === 'input' && ['submit','button','reset'].includes((el.type||'').toLowerCase())) return el.value || '';
    if (t === 'input' || t === 'textarea' || t === 'select') {
      if (el.id) { const l = el.ownerDocument.querySelector(`label[for="${el.id}"]`); if (l) return txt(l); }
      const pl = el.closest('label'); if (pl) return txt(pl);
      return el.getAttribute('placeholder') || el.getAttribute('name') || '';
    }
    return txt(el).slice(0, 120);
  };
  const hintOf = (el) => {
    // legacy table layout: the label is usually the previous cell in the same row
    const cell = el.closest('td,th');
    if (cell && cell.previousElementSibling) return txt(cell.previousElementSibling).slice(0, 80);
    const row = el.closest('tr');
    if (row && row.previousElementSibling) return txt(row.previousElementSibling).slice(0, 80);
    return '';
  };
  const pathOf = (el) => {
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1 && n.tagName.toLowerCase() !== 'html') {
      const tag = n.tagName.toLowerCase();
      let k = 1, s = n.previousElementSibling;
      while (s) { if (s.tagName === n.tagName) k++; s = s.previousElementSibling; }
      parts.unshift(`${tag}[${k}]`);
      n = n.parentElement;
    }
    return parts.join('>');
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const interactive = 'a[href],button,input,select,textarea,[role],[onclick]';
  const textual = 'th,td,h1,h2,h3,h4,h5,h6,label,p,li,span.err,span.msg,div.err,div.msg,td.err,td.msg';
  const els = Array.from(document.querySelectorAll(interactive + ',' + textual));
  for (const el of els) {
    if (!visible(el)) continue;
    const t = el.tagName.toLowerCase();
    if (t === 'input' && (el.type||'').toLowerCase() === 'hidden') continue;
    const role = roleOf(el);
    // skip container cells that only wrap other collected elements
    if ((role === 'cell' || role === 'text' || role === 'label') && el.querySelector(interactive)) continue;
    if ((role === 'cell' || role === 'text') && txt(el).length === 0) continue;
    if ((role === 'cell' || role === 'text') && el.querySelector('table')) continue;
    const r = el.getBoundingClientRect();
    const ref = prefix + (++i);
    el.setAttribute('data-cua-ref', ref);
    let value = null;
    if (t === 'input' && !['submit','button','reset','password'].includes((el.type||'').toLowerCase())) value = el.value;
    if (t === 'select') value = el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : '';
    if (t === 'input' && (el.type||'').toLowerCase() === 'password') value = el.value ? '••••' : '';
    out.push({ref, role, name: nameOf(el, role), label_hint: (t==='input'||t==='select'||t==='textarea') ? hintOf(el) : '',
              value, tag: t, path: pathOf(el), bbox: [r.x, r.y, r.width, r.height],
              enabled: !el.disabled,
              options: t === 'select' ? Array.from(el.options).map(o => o.text) : undefined});
  }
  const text = (document.body ? (document.body.innerText || '') : '').replace(/[ \t]+/g, ' ').replace(/\n{2,}/g, '\n').trim();
  return {nodes: out, text: text.slice(0, 6000), title: document.title};
}
"""


class PlaywrightSurface:
    def __init__(self, headless: bool = True, viewport: tuple[int, int] = (1280, 900), slow_mo: int = 0):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless, slow_mo=slow_mo)
        self._ctx = self._browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
        self.page: Page = self._ctx.new_page()
        self.viewport = viewport
        self._pending_dialog: Dialog | None = None
        self._last_status: int | None = None
        self._last_obs: Observation | None = None
        self._ref_frames: dict[str, Frame] = {}
        self.page.on("dialog", self._on_dialog)
        self.page.on("response", self._on_response)

    # --- events ---------------------------------------------------------------
    def _on_dialog(self, dialog: Dialog) -> None:
        # Do not auto-dismiss: a blocking dialog is a runtime condition the caller must decide on.
        self._pending_dialog = dialog

    def _on_response(self, resp) -> None:
        try:
            if resp.request.resource_type == "document" and resp.frame != self.page.main_frame:
                self._last_status = resp.status
            elif resp.request.resource_type == "document" and resp.frame == self.page.main_frame:
                self._last_status = resp.status
        except Exception:
            pass

    # --- perception -----------------------------------------------------------
    def _frame_offset(self, frame: Frame) -> tuple[float, float]:
        x = y = 0.0
        f = frame
        while f.parent_frame is not None:
            try:
                fe = f.frame_element()
                bb = fe.bounding_box()
                if bb:
                    x += bb["x"]
                    y += bb["y"]
            except Exception:
                pass
            f = f.parent_frame
        return x, y

    def _pump(self, ms: int = 50) -> None:
        # wait_for_timeout is served by the driver, not page JS, so it is safe while a dialog blocks
        # the page. It also dispatches queued events (e.g. "dialog") in the sync API.
        try:
            self.page.wait_for_timeout(ms)
        except Exception:
            pass

    def observe(self) -> Observation:
        self._pump()
        if self._pending_dialog is None:
            try:
                self.page.wait_for_load_state("load", timeout=5000)
            except PWTimeout:
                pass
            self._pump()
        if self._pending_dialog is not None:
            d = self._pending_dialog
            base = self._last_obs
            return Observation(
                url=self.page.url, title=base.title if base else "", frames=base.frames if base else [],
                nodes=base.nodes if base else [], text=base.text if base else "",
                dialog=DialogInfo(type=d.type, message=d.message), http_status=self._last_status,
                viewport=self.viewport, screenshot_png=base.screenshot_png if base else None,
            )
        nodes: list[Node] = []
        frames: list[str] = []
        texts: list[str] = []
        title = self.page.title()
        self._ref_frames = {}
        for idx, frame in enumerate(self.page.frames):
            fname = frame.name or ("" if frame == self.page.main_frame else f"frame{idx}")
            try:
                data = frame.evaluate(_COLLECT_JS, f"{fname or 'm'}_")
            except Exception:
                continue
            if not data["nodes"] and not data["text"]:
                continue
            ox, oy = self._frame_offset(frame)
            frames.append(fname)
            for n in data["nodes"]:
                bx, by, bw, bh = n["bbox"]
                name = n["name"]
                if n.get("options"):
                    name = f"{name} [options: {', '.join(n['options'])}]"
                node = Node(ref=n["ref"], frame=fname, role=n["role"], name=name, label_hint=n["label_hint"],
                            value=n["value"], tag=n["tag"], path=n["path"],
                            bbox=(bx + ox, by + oy, bw, bh), enabled=n["enabled"])
                nodes.append(node)
                self._ref_frames[n["ref"]] = frame
            if data["text"]:
                texts.append(f"[frame:{fname or 'main'}]\n{data['text']}")
            if frame == self.page.main_frame and data["title"]:
                title = data["title"]
        obs = Observation(url=self.page.url, title=title, frames=frames, nodes=nodes, text="\n".join(texts),
                          http_status=self._last_status, viewport=self.viewport, screenshot_png=self.screenshot())
        self._last_obs = obs
        return obs

    def screenshot(self) -> bytes:
        self._pump()
        if self._pending_dialog is not None:
            return self._last_obs.screenshot_png if self._last_obs and self._last_obs.screenshot_png else b""
        try:
            return self.page.screenshot(type="png", timeout=5000)
        except Exception:
            return b""

    # --- action ---------------------------------------------------------------
    def _locator(self, ref: str):
        frame = self._ref_frames.get(ref)
        if frame is None:
            raise ValueError(f"unknown ref {ref}; observe() first")
        return frame.locator(f'[data-cua-ref="{ref}"]').first

    def act(self, action: Action) -> ActionResult:
        t = action.type
        self._pump()
        try:
            if t == "dismiss_dialog":
                if self._pending_dialog is None:
                    return ActionResult(ok=False, detail="no dialog open")
                d, self._pending_dialog = self._pending_dialog, None
                (d.accept() if action.accept else d.dismiss())
                return ActionResult(ok=True, detail=f"dialog {'accepted' if action.accept else 'dismissed'}: {d.message[:80]}")
            if self._pending_dialog is not None:
                return ActionResult(ok=False, detail="a dialog is open; dismiss it first")
            if t == "navigate":
                self.page.goto(action.url, wait_until="commit", timeout=15000)
                return ActionResult(ok=True, detail=f"navigated to {action.url}")
            if t == "click":
                if action.ref:
                    self._locator(action.ref).click(timeout=8000, no_wait_after=True)
                    self._settle()
                    return ActionResult(ok=True, detail=f"clicked {action.ref}")
                if action.x is not None and action.y is not None:
                    self.page.mouse.click(action.x, action.y)
                    self._settle()
                    return ActionResult(ok=True, detail=f"clicked at ({action.x:.0f},{action.y:.0f})")
                return ActionResult(ok=False, detail="click needs ref or x/y")
            if t == "type":
                if action.text is None:
                    return ActionResult(ok=False, detail="type needs text")
                if action.ref:
                    loc = self._locator(action.ref)
                else:
                    self.page.mouse.click(action.x, action.y)
                    loc = None
                if loc is not None:
                    loc.fill(action.text, timeout=8000)
                else:
                    self.page.keyboard.type(action.text)
                return ActionResult(ok=True, detail=f"typed into {action.ref or 'focused'}")
            if t == "select":
                if not action.ref or action.text is None:
                    return ActionResult(ok=False, detail="select needs ref and text")
                self._locator(action.ref).select_option(label=action.text, timeout=8000)
                return ActionResult(ok=True, detail=f"selected {action.text!r}")
            if t == "press":
                if action.ref:
                    self._locator(action.ref).press(action.key or "Enter", timeout=8000, no_wait_after=True)
                else:
                    self.page.keyboard.press(action.key or "Enter")
                self._settle()
                return ActionResult(ok=True, detail=f"pressed {action.key}")
            if t == "extract":
                if not action.ref:
                    return ActionResult(ok=False, detail="extract needs ref")
                val = self._locator(action.ref).inner_text(timeout=5000).strip()
                return ActionResult(ok=True, detail=f"extracted {action.name}", extracted={action.name or "value": val})
            if t == "done":
                return ActionResult(ok=True, detail="done")
            return ActionResult(ok=False, detail=f"unsupported action {t}")
        except PWTimeout as e:
            return ActionResult(ok=False, detail=f"timeout: {str(e).splitlines()[0][:160]}")
        except Exception as e:  # noqa: BLE001
            return ActionResult(ok=False, detail=f"{type(e).__name__}: {str(e).splitlines()[0][:160]}")

    def _settle(self, ms: int = 300) -> None:
        # Give a legacy server-rendered app time to start its navigation; observe() waits for load.
        self._pump(ms)

    # --- handoff support ------------------------------------------------------
    def wait_for_settle(self, timeout_ms: int = 5000) -> None:
        try:
            self.page.wait_for_load_state("load", timeout=timeout_ms)
        except PWTimeout:
            pass

    def close(self) -> None:
        try:
            self._ctx.close()
            self._browser.close()
        finally:
            self._pw.stop()
