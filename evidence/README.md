# Evidence

Every folder is one real run. Each holds `log.jsonl` (structured events), numbered screenshots, and a
result JSON. Nothing here is edited by hand. No credential or key appears in any file.

| Folder | What it shows |
| --- | --- |
| `discovery-20260921T185259Z-2c6d6b/` | **The real LLM run.** `qwen/qwen3-vl-235b-a22b-instruct` via OpenRouter drives the live app: sign in, search, read the member, extract two outputs. 8 steps, 8 model calls. Holds one screenshot per step, `discovery-result.json` (the full trace with each decision and its reason) and `artifact.json` (the recorded capability). |
| `replay-success-*/` | Deterministic replay of that artifact, no model. `success`, checkpoint verified, both outputs returned. |
| `replay-other-member-*/` | Same artifact, `member_id=67890`. Returns Grace Hopper / 15,004.00 — the capability is parameterized, not member-specific. Reports `rung_regressions: 1, drift: 0.071`: the balance cell's label rung no longer matches, so resolution fell through to the structural path. The run still succeeds; the number is a review signal. |
| `replay-not-found-*/` | `member_id=99999`. **Business outcome**, not a crash: `status: outcome`, `outcome_code: MEMBER_NOT_FOUND`, exit 0. |
| `replay-app-error-*/` | Injected HTTP 500. **Hard failure** with `failed_step: 4`, `expected`, `observed`, plus `fail-step4-observation.json` and a screenshot. |
| `replay-dialog-*/` | Injected compliance modal that the capability *does* declare. **Recovered** automatically: `recoveries: ["UNEXPECTED_DIALOG:dismiss_dialog@step4"]`, then `success`. |
| `replay-handoff-*/` | The same modal with that outcome removed, so nothing declares it. Replay **escalates at step 5** (`intervention-step5.json` + screenshot), a human clears it on the same live session (`human.action` events), hands control back, and the run completes with both outputs. |
| `replay-beta-tenant-*/` | The artifact recorded on tenant *alpha* replayed against tenant *beta* — different brand, different control labels, different route — through `config/tenants/beta.json`. `success`, zero drift, no re-recording. |

Reproduce any of them with the commands in [../README.md](../README.md).

```bash
# read a run
python3 -c "import json;[print(json.loads(l)['kind']) for l in open('evidence/<folder>/log.jsonl')]"
```
