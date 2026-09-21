# Design write-up

## 1. Architecture

Five components, one process, synchronous. The boundaries matter more than the topology, so I kept
the topology boring: no queue, no services, no database. Everything that would need to scale later is
an interface today.

```
            ┌──────────────┐         ┌───────────────────┐
 goal ─────►│ Discovery    │ decides │ LLM (open weights,│
            │ loop         │◄────────│ OpenAI-compatible)│
            └──────┬───────┘         └───────────────────┘
                   │ trace                     ┌──────────┐
            ┌──────▼───────┐  artifact  ┌──────▼───────┐  │ Policy   │
            │ Recorder     ├───────────►│ Replay engine│◄─┤ (allow-  │
            └──────────────┘            └──────┬───────┘  │ list,    │
                                               │          │ risk,    │
            ┌──────────────┐                   │          │ redact)  │
            │ Handoff      │◄──────────────────┤          └──────────┘
            │ controller   │   escalate        │
            └──────┬───────┘                   │
                   │            ┌──────────────▼───────┐
                   └───────────►│ Surface (Playwright) │──► the application
                     same          observe() / act()
                    session
```

**Surface** is the load-bearing abstraction. It exposes exactly two things: `observe() -> Observation`
(a screenshot, plus a flat list of `Node`s with role, accessible name, label hint, structural path and
page-coordinate bounding box, plus per-frame URLs and text) and `act(Action)` (navigate, click, type,
select, press, extract, dismiss_dialog). Nothing above it knows about Playwright, CSS, or the DOM.
Both the discovery loop and the replay engine are written against `Surface` alone.

**Perception is screenshot + accessibility-style tree, not DOM selectors.** I build the node list by
walking every frame and inferring roles from tags, names from labels, `value` attributes or adjacent
table cells, and geometry from bounding boxes. It never reads ids or test ids, because the target
class of applications does not have them. This is deliberately the same information a desktop
accessibility API exposes, which is what makes the design portable off the browser.

**Key trade-offs.**
- *Sync over async.* A computer-use loop is inherently serial: observe, then decide, then act. Async
  would add concurrency I do not need and make the handoff — which is literally "block and wait for a
  human" — harder to reason about.
- *JSON-in-text instead of provider tool-calling.* One action per turn, parsed from the reply. It
  behaves identically on OpenRouter, vLLM and Ollama, so the "open weights, swap the endpoint" promise
  is real rather than aspirational.
- *One action per LLM turn.* More round trips, but every step gets a fresh observation, and the
  trace maps one-to-one onto artifact steps. The real discovery run took 8 model calls.
- *Dialogs are never auto-dismissed.* The surface surfaces them as state. Silently accepting a modal
  in a banking app is exactly the class of bug that must not exist.

## 2. Artifact schema

The artifact is a **capability contract**, not a transcript. A calling agent needs to know what it
does, what to pass, what it gets back, and what might legitimately come back instead. So the schema
carries those four things explicitly (`cua/artifact/schema.py`):

```jsonc
{
  "schema_version": "1.0", "id": "lookup_member_balance", "version": "1.0.0", "status": "draft",
  "app": { "vendor": "MockCore", "product": "Teller Workstation", "version": "1.0", "tenant": "alpha" },
  "params":  [{ "name": "member_id", "type": "string", "required": true, "example": "12345" },
              { "name": "password",  "sensitive": true, "example": null }],
  "outputs": [{ "name": "savings_balance", "type": "string", "target": { … }, "after_step": 6 }],
  "steps":   [{ "index": 4, "action": "click", "risk": "read",
                "target": { "description": "button 'Search' in frame main",
                            "locators": [ {"strategy":"role_name","role":"button","name":"Search"},
                                          {"strategy":"path","path":"body[1]>form[1]>…>input[1]"},
                                          {"strategy":"bbox","bbox_frac":[0.28,0.117,0.042,0.022]} ],
                            "recorded_rung": 0 },
                "wait": { "kind": "load", "timeout_ms": 8000 },
                "expect": [{ "kind": "url_contains", "value": "/members/{{params.member_id}}" }] }],
  "checkpoints": [{ "name": "goal_reached", "after_step": 6,
                    "expect": [{ "kind": "text_visible", "value": "Member Detail" }, …] }],
  "outcomes": [{ "code": "MEMBER_NOT_FOUND", "kind": "business",
                 "detect": { "kind": "text_visible", "value": "No member found for" } }, …],
  "risk_max": "read",
  "provenance": { "discovered_by": "qwen/qwen3-vl-235b-a22b-instruct", "run_id": "…",
                  "transcript_sha256": "…", "steps_taken": 8 }
}
```

Five decisions worth defending:

1. **Every target is a locator ladder, not a locator.** Ordered from most semantic to most geometric:
   `role+name` → `label` (the adjacent cell, which is how legacy table forms are actually labelled) →
   `text` → `structural path` → `bbox fraction`. The order is chosen per control type — a text input
   is identified by its label first, a button by its own name first. `recorded_rung` remembers which
   rung identified it at discovery, which turns locator resolution into a drift measurement (§3).
2. **Values are parameterized at record time, never at prompt time.** The model is told to type the
   literal `{{params.member_id}}`; the loop substitutes the real value only in the call to
   `Surface.act`. So a sensitive value is never in the prompt, never in the trace, and never in the
   artifact. Post-conditions and checkpoints are parameterized too, by substituting recorded values
   back out (`/members/12345` → `/members/{{params.member_id}}`) — without that the capability would
   silently only work for the member it was recorded on. That is a tested invariant.
3. **Outcomes are part of the contract.** `kind` is `business`, `recoverable` or `failure`. They are
   seeded per vendor product in `config/outcomes.yaml` and copied into the artifact, so a capability
   is self-contained and reviewable. "No such member" being a *result* rather than an exception is a
   schema-level decision, not a convention the caller has to remember.
4. **Success is a checkpoint, not the model's word.** The discovery loop refuses `done` unless the
   model quotes a contiguous phrase that really is on the screen; that phrase becomes the replay
   checkpoint. On the first real run the model tried to finish with `"Ada Lovelace, 4,312.57"` — two
   values joined — and the loop rejected it three times and ended in `dead_end`. I fixed the prompt,
   not the check.
5. **Provenance is separate from behaviour.** `content_hash()` excludes it, so two recordings of the
   same flow compare equal. Review and approval (`status: draft → approved`) can hang off that.

## 3. Determinism and error handling

Replay calls no model. It resolves each target through the ladder, acts, waits, then verifies the
step's declared post-conditions before moving on — the "assume the click worked" failure mode cannot
happen. Timing is handled by polling the post-condition until the step's timeout rather than by
sleeping, which is what absorbs the injected 3–6 second load without a fixed delay.

**The taxonomy is the point.** After every step, and after every failed expectation, the engine runs
the declared detectors against the current screen and classifies:

| Class | Example | What replay does | What the caller sees |
| --- | --- | --- | --- |
| Business outcome | `MEMBER_NOT_FOUND`, `VALIDATION_ERROR`, `PERMISSION_DENIED` | stops cleanly | `status: outcome`, `outcome_code`, exit 0 |
| Recoverable | `UNEXPECTED_DIALOG` → dismiss; `SESSION_EXPIRED` → re-login and re-run prior steps; slow load → retry | recovers, bounded by `max_attempts`, and records what it did | `status: success`, `recoveries: [...]` |
| Hard failure | `APP_ERROR` (HTTP 500), unresolvable target, unmet post-condition | stops, screenshots, dumps the observation | `status: failed` + `failed_step`, `expected`, `observed` |
| Blocked | an undeclared modal dialog | escalates to a human (§5) | `status: escalated`, or success after the human |

Re-running earlier steps after a session timeout is bounded to one attempt: an app that expires the
session twice is a real failure, not something to retry into. Recovery attempts are counted per
outcome code, so nothing loops.

A failure is debuggable without a rerun. Example, from `evidence/replay-app-error-*`:

```
FAILED  APP_ERROR  declared failure condition APP_ERROR: The host system returned an internal error.
failed_step: 4   expected: "no app error"
observed:    url=…; dialog=None; text[:300]='System Error HTTP 500 - An internal error occurred…'
```
plus `002-fail-step4.png` and `fail-step4-observation.json` in the same folder.

**Drift, secondarily.** Because each target records which rung identified it, resolving by a *weaker*
rung than recorded is measurable. Replaying the artifact against a different member does exactly that
— the balance cell's label rung (`S-0001`) no longer matches, so it falls through to the structural
path and the run reports `rung_regressions: 1, drift: 0.071`. The run still succeeds; the number is a
signal for review, not an error. Unresolvable targets weigh double in the score.

## 4. Heterogeneity and multi-tenant

**Other surfaces.** The seam is `Surface`: `observe()` returns roles, names, geometry and text;
`act()` takes semantic actions. Nothing else in the system knows how those are obtained. A legacy web
app needs no change at all — the current implementation already walks framesets and table layouts and
reads no ids. A desktop app needs a new `Surface` over the OS accessibility API (AX on macOS, UIA on
Windows): the same roles and names, the same bounding boxes, `act()` implemented with synthetic input
and the artifact schema untouched, because `Locator` strategies are already surface-neutral
(`role_name`, `label`, `text`, `path`, `bbox`). The one strategy that needs a per-surface definition
is `path`, which is why it sits *below* the semantic rungs rather than above them.

**Many tenants, one vendor app.** An artifact is keyed by `app.vendor/product/version`, and the
tenant is a field, not part of the flow. Per-tenant differences are expressed as a `TenantOverlay` —
a `route_map` and a `name_map` applied to a copy of the artifact — so one recording serves many
institutions and the specialization is small, reviewable and diffable. This is demonstrated, not
hypothesized: the artifact recorded against *Summit Federal Credit Union* replays unchanged against
*Riverbend Community Bank*, which brands differently, renames "Member ID" to "Member Number" and
"Search" to "Find member", and serves the search screen from `/member/lookup`, using a 10-line
overlay (`config/tenants/beta.json`) — with zero drift.

At real scale I would add, in this order: (a) store artifacts keyed by `vendor@version` with tenant
overlays alongside, so a vendor upgrade is one re-record plus N overlay reviews rather than N
re-records; (b) run the drift score continuously per tenant, since a rung regression is an early
warning that a tenant's UI has moved; (c) promote a tenant overlay to a full fork only when its drift
stays high, which keeps the fork decision data-driven.

## 5. Escalation and handoff

**Detecting "stuck" is explicit, not a timeout.** The loop escalates when the model asks to, when
three consecutive actions fail, when the same action repeats three times, or when the next action is
irreversible. Replay escalates when no locator rung resolves a target, when an action fails, when a
post-condition stays unmet past its timeout, when a modal dialog appears that no declared outcome
claims, or before an irreversible step. Each escalation carries the capability, the goal, the step
index, the reason, the URL and a screenshot, written to the run folder as
`intervention-step<N>.json`.

**Control transfer is a state machine over one session.** `ControlOwner` is `AUTOMATION` or `HUMAN`,
never both. Automation cedes control by *blocking inside the handoff call* — it issues no commands
while the human owns the session — and the human works in the very browser window Playwright opened,
so cookies, frames and page state are all preserved. There is no second session anywhere in the
design. While the human owns the session, the surface records what they do (clicks, field changes
with password fields masked *in the page*, submits, navigations) and those become `human.action`
events in the evidence log. The human returns control through the operator console, the terminal, or
a callback; the engine then re-observes, and either accepts the step (its post-conditions now hold —
recorded as "completed by human") or re-runs it once without escalating again. An unanswered request
times out and the run ends as `escalated` rather than hanging.

**What is mocked.** The operator console is a minimal local page: it shows the ownership state, the
request with its screenshot and reason, the human's recorded actions, and one "Hand control back"
button. A real product needs streaming co-browsing, authentication, queueing and audit. The seam it
sits on is real and unchanged by that work — which is why `scripts/demo_handoff.py` can drive the
exact same code path with a scripted operator and produce genuine evidence
(`evidence/replay-handoff-*`: escalate at step 5 on an undeclared compliance dialog → human clears it
→ hand back → run completes with both outputs).

## 6. Safety

Four layers, all enforced on both paths (discovery and replay), because a capability recorded safely
can still be invoked unsafely:

1. **Allowlist** (`config/policy.yaml`): permitted hosts, route prefixes and action types. Every
   navigation and every action type is checked before execution; a blocked action is fed back to the
   model as a policy message rather than silently dropped, and three blocks end the run.
2. **Risk classes.** Actions are classified `read` / `reversible_write` / `irreversible` from the
   target's name and value. Typing and selecting are always `read` — a field edit commits nothing; the
   click that submits is what carries risk. Irreversible actions ("post transaction", "close account",
   "wire", "transfer funds") are **blocked by default** on both paths. With `--confirm-risky` they do
   not execute either — they escalate to a human for confirmation. The artifact records `risk_max`, so
   a caller can refuse to invoke a capability above its authority without reading the steps.
3. **Redaction.** Sensitive parameters are supplied as `name=env:VAR`, appear in artifacts only as
   `{{params.name}}`, and are masked in every evidence file along with the API key and anything
   matching the configured patterns (SSN, bearer tokens). Password fields are masked inside the page
   before the value crosses the boundary. A test asserts the credential is absent from the log and
   from the artifact, and `grep` over the committed evidence finds no secret.
4. **Verified success.** The model cannot declare victory; it must quote on-screen text that is then
   re-verified as a checkpoint on every replay.

**Limits, stated plainly.** Risk classification is pattern-matching on visible labels — it will miss a
destructive action behind an ambiguous label, so the pattern list is a policy artifact that needs
curation per vendor app, not a solved problem. Redaction protects logs and artifacts, not the screen:
screenshots may contain PII, which is why they stay in the run folder and never enter an artifact.
The allowlist constrains routes, not intent — a capability allowed to click "Submit" on an allowed
route can still submit the wrong thing, which is what the checkpoint and the risk class exist to
bound. And a human who takes control can do anything the application permits; the system records what
they did, it does not constrain them.

## 7. Cuts

**Cut deliberately, with the seam left real.**
- *The operator console* is a local mock page (§5). Real co-browsing, auth and queueing are out.
- *The desktop surface* is designed, not built (§4). No second `Surface` implementation exists.
- *Multi-tenant storage* is a directory of JSON files and an overlay; no registry, no versioned store,
  no approval workflow. `status: draft|approved` exists in the schema and nothing enforces it yet.
- *Assisted recovery.* On replay failure I escalate to a human rather than letting a bounded LLM step
  improvise. With more time I would add it behind the policy check, because it is the natural next
  rung, but "deterministic replay" should not quietly become "a model again" without being asked.
- *Concurrency.* One run per process. The artifact and the engine hold no global state, so a worker
  pool is additive.

**What I would build next, in order.** (1) A capability catalog that exposes saved artifacts as typed,
callable tools with their params, outputs and `risk_max`, since that is the actual product surface for
the agents. (2) Confidence scoring: replay each capability N times on a schedule, track drift and
flakiness, and gate unattended invocation on `approved` plus a stability threshold. (3) A column-aware
locator rung for table cells — the one weak rung in the current ladder, visible as the single drift
regression when the artifact runs against a different member. (4) The desktop `Surface`, to prove the
seam holds where it matters most.
