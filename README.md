# Computer-Use Automation System

An LLM figures out how to complete a task on a web UI **once**. That run is recorded as a typed,
parameterized **artifact**. Every run after that is **deterministic replay**: no model in the
loop, no tokens spent, with safety gating, human escalation and a full evidence trail around it.

> **Status: in active development on `dev`.** The core loop works end to end against a bundled mock
> app and against a purpose-built clinic app with fault injection, UI drift and an audit log. Eight
> clinic capabilities have been discovered by the LLM and replay deterministically, with versioned
> artifacts, approvals, idempotency keys, locator repair proposals and canaries around them. An async
> API with auth, a unified UI and a measured benchmark are still to come. See
> [WRITEUP.md](WRITEUP.md) for the design, the incidents, the measured drift results and an honest
> list of what is not done.

## How it works

```
 goal + typed contract ──► DISCOVER (LLM observes, decides, acts) ──► ARTIFACT (typed JSON)
                                  │                                          │
                                  ▼                                          ▼
                          evidence log + screenshots              REPLAY (no LLM, deterministic)
                                                                             │
                              ┌──────────────────────────────────────────────┤
                              ▼                      ▼                       ▼
                         safety gate          three-way result         human escalation
                  (allowlist + risk class)   success / business        (pause, take over the
                  on every single action     outcome / failure          same live session)
```

- **Discovery** reads the page as an accessibility-tree element list (not pixels) and drives a
  real Playwright browser one action at a time toward a stated goal.
- **Recording** is deterministic code, not the model: it turns the transcript into steps with
  locator fallback chains (role, CSS, XPath, text), parameter placeholders, per-step checkpoints
  and risk tags.
- **Replay** classifies every page state against the artifact's declared signals: a known
  *business outcome* (for example "no such member") is a result, a *recoverable* condition is
  handled and re-checked, anything else is a hard failure that can escalate to a human.
- **Safety** lives in one chokepoint, `Surface.act()`, which both discovery and replay pass
  through. An irreversible action is blocked unless a human explicitly confirms it.
- **Front doors** (CLI, HTTP capability API, chatbot) all call the same `runtime.run_replay()`,
  so none of them can skip safety, evidence or escalation.

## Layout

| Path | What it is |
|---|---|
| `artifacts_lib/` | Artifact schema (Pydantic), versioned storage, lint (`validate`) and structured diff |
| `agent/` | Discovery loop, tool set, Gemini client, recorder, capability catalog |
| `replay/` | Deterministic replay engine, templating, input validation, result types |
| `surface/` | Surface interface and the Playwright implementation, aria parsing, locator resolution, browser pool |
| `safety/` | Allowlist, risk classifier, combined policy, and `policy.yaml` (approvals, caps, redaction) |
| `runs/` | SQLite run store: runs, idempotency keys, approvals, repair proposals, canary history |
| `repair/` | Locator repair proposals and their approval |
| `escalation/` | Pause/resume session manager and the operator console |
| `evidence_lib/` | JSONL evidence logger and redaction |
| `api/` | Capability API, chatbot, run dashboard |
| `mockbank/` | Bundled legacy-style bank app used as a test target |
| `clinic/` | Larkspur Clinic Ops: the self-hosted test target (legacy and React skins, JSON API, test kit) |
| `artifacts/` | Saved capability artifacts, one directory per capability with every version and a promotion history |
| `scripts/` | `discover_clinic.sh` (discover all clinic capabilities), `drift_report.py` (measure drift recovery) |
| `tests/` | Offline test suite |

## Quickstart

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run playwright install chromium
cp .env.example .env        # add GEMINI_API_KEY only if you want to run discovery or the chatbot
```

Start the bundled target and the API (separate terminals):

```bash
uv run uvicorn mockbank.app:app --port 8000
uv run uvicorn api.app:app --port 8020
```

Then:

- Chatbot: <http://127.0.0.1:8020/chat>
- Run dashboard: <http://127.0.0.1:8020/dashboard>
- Operator console (starts with the API): <http://127.0.0.1:8010/operator>
- Capability catalog: `GET /capabilities`, invoke with `POST /capabilities/{id}/invoke`

CLI:

```bash
# replay a saved capability, no LLM involved
uv run python cli.py replay --capability mockbank.member_balance_lookup --param member_id=10001

# run LLM-driven discovery (needs GEMINI_API_KEY); saves a NEW version of the artifact as a candidate
# (the first version of a capability becomes current automatically), then you promote it
uv run python cli.py discover --capability mockbank.member_balance_lookup --param member_id=10001
uv run python cli.py artifact diff mockbank.member_balance_lookup 2.0.0 2.0.1
uv run python cli.py artifact promote mockbank.member_balance_lookup 2.0.1 --by YOU --reason "reviewed"

# watch a run in a visible browser
uv run python cli.py replay --capability mockbank.member_balance_lookup --param member_id=10001 --headed --slow-mo 600
```

MockBank's demo login is `operator` / `bankdemo123` (a fixture credential, not a secret).

### Operating the clinic capabilities

```bash
uv run uvicorn clinic.app:app --port 8100 &     # the target
scripts/discover_clinic.sh                       # optional: re-discover all 8 (uses your Gemini quota)

# a read: replay a capability, optionally with a key so a retry cannot run twice
uv run python cli.py replay --target clinic --capability clinic.patient_lookup --param mrn=LK-100002

# a commit: policy says who must approve (safety/policy.yaml); nothing runs until someone does
uv run python cli.py replay --target clinic --capability clinic.issue_refund \
    --param invoice=INV-30001 --param amount=25.00 --param reason=duplicate_payment \
    --idempotency-key refund-INV-30001-1 --requested-by alex
uv run python cli.py runs pending
uv run python cli.py approve RUN_ID --by dana.okafor --reason "checked the invoice"
uv run python cli.py replay --target clinic --capability clinic.issue_refund --resume RUN_ID
uv run python cli.py replay --target clinic --capability clinic.issue_refund --dry-run \
    --param invoice=INV-30001 --param amount=25.00 --param reason=duplicate_payment   # stops before the commit

# when the outcome of a commit is unknown the run is needs_review and its key stays blocked until:
uv run python cli.py resolve RUN_ID --outcome committed --by dana.okafor --reason "refund is in the ledger"

# drift: a failed replay leaves a repair proposal; a person approves it; a new version is promoted
uv run python cli.py repair list --status pending
uv run python cli.py repair show REPAIR_ID
uv run python cli.py repair approve REPAIR_ID --by sam.reyes --reason "button was relabelled"
uv run python cli.py artifact rollback clinic.patient_lookup --by sam.reyes

# canaries: known-good read-only replays, to find drift before real traffic does
uv run python cli.py canary run --target clinic
uv run python cli.py canary history
```

Approver names come from the roster in `safety/policy.yaml`. Identity is asserted by the caller, not
authenticated: see the limitations in [WRITEUP.md](WRITEUP.md).

## The clinic target app

`clinic/` is a fictional clinic front-desk and billing portal built to be automated and measured.
It has two skins over one set of business rules, plus a JSON API and a test kit.

| Surface | Where | What it is |
|---|---|---|
| Legacy skin | `/legacy` | Server-rendered tables, unlabeled inputs, headerless tables, results returned from POSTs |
| Modern skin | `/app` | React single-page app with async loading, modals, toasts, a session-expired dialog and a CSV download |
| JSON API | `/api` | Every flow, with stable error codes (`/docs` for the OpenAPI page) |
| Test kit | `/_test` | Audit-log API, idempotent reset, chaos rules, duplicate-guard switch, UI drift, a control panel at `/_test/panel` |

Flows: patient search and detail, contact update, reschedule, and review -> confirm -> receipt for
cancelling an appointment (with a late-cancellation fee), submitting an insurance claim, issuing a
refund (above $200 needs supervisor approval) and writing off a balance (supervisor only).
Roles are `frontdesk` / `desk-demo-123` and `supervisor` / `super-demo-123`. All data is synthetic.

```bash
uv run uvicorn clinic.app:app --port 8100          # legacy skin works immediately
(cd clinic/modern && npm install && npm run build)  # needed once for the /app skin
docker compose up --build                           # or: everything in one container
```

Set `CLINIC_TEST_TOKEN` on any deployment that is not purely local; every `/_test` endpoint then
requires it. See [WRITEUP.md](WRITEUP.md) for what the test kit is for and what it does not cover.

## Tests

```bash
uv run pytest
```

The suite is offline: it starts MockBank and the clinic app in-process and drives a real Chromium
against them. The live-model tests (discovery, chatbot) skip automatically unless `GEMINI_API_KEY`
is set, and the modern-skin browser tests skip until `clinic/modern` has been built.
At the time of writing: 262 passing, 4 skipped without a key. The suite takes about five minutes
because it drives a real browser, including a full drift-repair loop.

## What is not done yet

Read [WRITEUP.md](WRITEUP.md) for the full list. The short version: approver identity is asserted
rather than authenticated, the supervised commit prompt has not been used by a person yet, repair
proposals are one step at a time and were tuned on the clinic's own drift modes, runs are synchronous
over an unauthenticated API, and there is no measured benchmark of discovery cost or reliability yet.
