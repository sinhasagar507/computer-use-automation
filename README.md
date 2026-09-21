# Computer-use automation: discover once, replay forever

An LLM drives a legacy back-office banking UI once to accomplish a goal. That run is recorded as a
typed, versioned **capability artifact**. After that, an AI agent invokes the capability through a
**deterministic replay** that never calls the model, returns typed outputs, separates business
outcomes from failures, and escalates to a human on the *same live session* when it cannot proceed.

```
goal + target ──► LLM discovery loop ──► capability artifact ──► deterministic replay ──► outputs
                  (observe/decide/act)    (steps, locators,       (no LLM, checkpoints,   or a business
                         │                 params, outputs,        error taxonomy)         outcome
                         │                 outcomes)                     │
                         └──────────── human handoff on the same live session ─────────────┘
```

See [REPORT.md](REPORT.md) for the design and the trade-offs, and [evidence/](evidence/) for a real
discovery run plus seven replay runs.

## Setup

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), and an OpenRouter API key for the
discovery run (replay needs no key).

```bash
uv sync --extra dev
uv run playwright install chromium
cp .env.example .env        # then put your key in .env, or export it
export OPENROUTER_API_KEY=sk-or-v1-...
export MOCKBANK_PASSWORD=demo-pass
```

The default model is `qwen/qwen3-vl-235b-a22b-instruct` — open weights (Apache-2.0), served through
OpenRouter. The client speaks the OpenAI-compatible protocol, so any other endpoint works without a
code change:

```bash
# local weights instead of a hosted endpoint
export CUA_LLM_BASE_URL=http://localhost:11434/v1
export CUA_LLM_MODEL=qwen3-vl:8b
export OPENROUTER_API_KEY=ollama        # any non-empty value
```

## Demo path

**1. Start the target application.** It is a mock legacy core-banking app: a frameset, table-based
layouts, no ids and no test ids, server-rendered forms, and injectable runtime faults.

```bash
uv run cua serve-mock --port 5050              # tenant "alpha" (Summit Federal Credit Union)
```

**2. Discovery — the LLM run.** This is the only step that calls a model.

```bash
uv run cua discover \
  --goal "Sign in to the teller workstation, look up the member whose Member ID is the member_id parameter, and read that member's details. Extract the member's name as output member_name and the Regular Share (Savings) current balance as output savings_balance." \
  --entry http://127.0.0.1:5050/ \
  --param username=teller1 --param password=env:MOCKBANK_PASSWORD --param member_id=12345 \
  --id lookup_member_balance --name "Look up member savings balance" \
  --out artifacts/lookup_member_balance.json
```

It writes the artifact to `artifacts/` and an evidence folder to `evidence/discovery-<id>/`
(structured JSONL log, one screenshot per step, the redacted result).

**3. Replay — the production path.** No model is involved.

```bash
A=artifacts/lookup_member_balance.json
C="--param username=teller1 --param password=env:MOCKBANK_PASSWORD"

uv run cua replay $A $C --param member_id=12345                 # success + outputs
uv run cua replay $A $C --param member_id=67890                 # same capability, different member
uv run cua replay $A $C --param member_id=99999                 # business outcome: MEMBER_NOT_FOUND
uv run cua replay $A $C --param member_id=12345 --fault error500  # hard failure, with debug detail
uv run cua replay $A $C --param member_id=12345 --fault dialog     # recovered automatically
```

Exit code is `0` for `success` and for a known business outcome, `1` for a failure or an escalation.

**4. Human handoff.** A capability meets a condition it does not declare, cedes control, and resumes
after a person fixes it on the same live session:

```bash
uv run python scripts/demo_handoff.py            # scripted operator, reproducible evidence
uv run python scripts/demo_handoff.py --headed   # watch it happen in a real browser window
```

To take control yourself, run any replay with a real operator console and a visible browser:

```bash
uv run cua replay $A $C --param member_id=12345 --fault dialog --headed --handoff web
# open http://127.0.0.1:5055/ -> read the request -> act in the browser -> "Hand control back"
```

**5. Cross-tenant reuse.** The same artifact, recorded on tenant `alpha`, runs on a second tenant
whose vendor app is branded and routed differently — through an overlay, with no re-recording:

```bash
uv run cua serve-mock --tenant beta --port 5051   # Riverbend Community Bank
uv run cua replay $A $C --param member_id=12345 --overlay config/tenants/beta.json
```

## Tests

```bash
uv run pytest -q          # 22 tests; they drive a real browser against the mock app
```

They cover the artifact schema and overlays, the allowlist and redaction, the perception layer, the
discovery loop and recorder (with a scripted model, so no network), and every replay path: success,
parameter reuse, each business outcome, dialog recovery, slow loads, bounded session recovery, hard
failure, escalation, and the handoff.

## Faults you can inject (mock app only)

`not_found`, `validation`, `denied`, `dialog`, `timeout` (session expiry), `slow` (3–6 s), `error500`.
Pass `--fault <name>` to `cua replay`, or visit `http://127.0.0.1:5050/fault/<name>` in a browser.
`--fault none` clears it.

## Layout

| Path | What it is |
| --- | --- |
| `cua/surface/` | The seam between perceiving/acting on a surface and the recorded flow |
| `cua/agent/loop.py` | Goal-driven discovery loop, stop conditions, escalation |
| `cua/artifact/` | Capability schema, recorder (trace → artifact), store, tenant overlays |
| `cua/replay/` | Deterministic engine, locator ladder resolution, outcome detectors, result contract |
| `cua/policy/` | Allowlist, risk classes, redaction |
| `cua/handoff/` | Control-transfer state machine and the mock operator console |
| `cua/evidence/` | Run folders: JSONL events, screenshots, JSON dumps |
| `mockbank/` | The legacy target app, in two tenant variants |
| `config/` | `policy.yaml`, `outcomes.yaml`, `tenants/beta.json` |

Secrets never enter the repo, the artifacts, or the logs: sensitive parameters are passed as
`name=env:VAR`, referenced in artifacts only as `{{params.name}}`, and redacted from every evidence
file. `.env` and generated artifacts are git-ignored; the committed artifact under `evidence/` is the
redacted copy.
