# Computer-Use Automation System

An LLM figures out how to complete a task on a web UI **once**. That run is recorded as a typed,
parameterized **artifact**. Every run after that is **deterministic replay**: no model in the
loop, no tokens spent, with safety gating, human escalation and a full evidence trail around it.

> **Status: in active development on `dev`.** The core loop works end to end against a bundled mock
> app and against a purpose-built clinic app with fault injection, UI drift and an audit log. Eight
> clinic capabilities have been discovered by the LLM and replay deterministically, with versioned
> artifacts, approvals, idempotency keys, locator repair proposals and canaries around them. An async
> authenticated async API with metrics, structured logs, failure traces and an MCP server are in place, and a single web console (task-first catalog, runs with step timelines and screenshots, an operator inbox, artifact inspector, chat) sits on top of it. A measured benchmark is still to come. See
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
| `ui/` | The web console: a Next.js (React, TypeScript) static export served by the API under `/ui` |
| `mcp_server.py` | MCP server: the recorded capabilities as tools for any MCP-capable assistant (a thin client of `/v1`) |
| `observability.py` | Structured JSON logging with a run id on every line |
| `surface/` | Surface interface and the Playwright implementation, aria parsing, locator resolution, browser pool |
| `safety/` | Allowlist, risk classifier, combined policy, and `policy.yaml` (approvals, caps, redaction) |
| `runs/` | SQLite run store (runs, idempotency keys, approvals, repair proposals, canary history, API keys) and the async run executor |
| `repair/` | Locator repair proposals and their approval |
| `escalation/` | Pause/resume session manager and the operator console |
| `evidence_lib/` | JSONL evidence logger and redaction |
| `api/` | `v1.py` (authenticated async API), the older capability endpoint, chatbot, run dashboard |
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

### The console

One web app over `/v1`, served by the API server itself under `/ui` once it has been built:

```bash
cd ui && npm install && npm run build          # once, and again after UI changes
uv run uvicorn api.app:app --port 8020          # then open http://localhost:8020/ui/
```

Sign in with an API key (create one with `cli.py keys create`). What you see follows your role, and everyone lands on the same place: a search box asking
what you want to do, with tasks grouped by what they do (look something up, change something, needs an approval first).

| Role | Sees |
|---|---|
| operator, supervisor | Tasks, My runs, Inbox (the badge counts only what *you* can decide), Chat |
| viewer | Tasks and Runs, read-only |
| admin | The above, plus a **Manage** section: Overview (metrics), Artifacts, Policy, API keys |

The role decides visibility only; the API enforces every permission itself.

**Watch it run.** On a task form, and in chat, choose *Slow* or *Step by step*. The run is paced (a pause around each action) and the element it is about to
touch is outlined in amber, with a live view on the run page that follows the steps. It never changes what the run does, and sign-on is not slowed. Screenshots
for the live view are kept for sandbox targets only. If the server runs on the machine you are sitting at, start it with `CUA_ALLOW_WINDOW=1` and an option
appears to also open a real browser window; closing the window (or Cmd+Q) releases it.

| Page | What it is for |
|---|---|
| Tasks (home) | A task search. Opening a task shows a form generated from its input schema, with what will happen, what approval it needs, and the watch option |
| Overview (admin) | Success, failure, escalation and latency per capability, and a banner when something needs a person or a canary is failing |
| Runs | Every run with filters. A run shows live progress, each step with the page as it looked afterwards, and for a failed step what was expected against what happened |
| Inbox | Approvals, runs whose commit outcome is unknown, and repair proposals, with who asked and how long each has waited. Decisions need a reason and are recorded with your name |
| Artifacts | A capability's steps, how each element is found, provenance, version history, side-by-side diff, promote and roll back |
| Chat | A shortcut for one-off requests. Starts only recorded tasks, asks when something is missing, can never approve; history is saved per key and can be cleared, and a half-typed message survives switching pages |
| Policy, API keys | The safety policy (read-only) and key management (admin) |

Keyboard: `g` then a letter shown in `?` to jump between pages, `/` to search, `?` for help. It follows your system's light or dark theme and can be switched.
For development, `cd ui && npm run dev` serves it on port 3000 and proxies `/v1` to a running API on 8020.

The older server-rendered chat (`/chat`) and dashboard (`/dashboard`) still work but are superseded by the console.

### The v1 API

`/v1` is the authenticated, asynchronous API (interactive docs at `/docs`). Identity comes from an API key: its name is
recorded as the requester or approver, and its role (`viewer`, `operator`, `supervisor`, `admin`) decides what it may do.

```bash
uv run python cli.py keys create --name alex --role operator       # shown once; only a hash is stored
uv run python cli.py keys create --name dana.okafor --role supervisor
```

```bash
# submit: returns 202 and a run id at once; follow it with GET /v1/runs/{id} or the SSE stream
curl -s -X POST localhost:8020/v1/runs -H "Authorization: Bearer $ALEX" -H "Idempotency-Key: refund-INV-30001-1" \
  -H 'content-type: application/json' \
  -d '{"capability_id":"clinic.issue_refund","target":"clinic","params":{"invoice":"INV-30001","amount":"25.00","reason":"duplicate_payment"}}'
curl -N localhost:8020/v1/runs/RUN_ID/events -H "Authorization: Bearer $ALEX"
# a supervisor (a different key from the requester) approves; the run starts straight away
curl -s -X POST localhost:8020/v1/runs/RUN_ID/approve -H "Authorization: Bearer $DANA" -H 'content-type: application/json' -d '{"reason":"invoice checked"}'
```

Other endpoints: `/v1/capabilities` (schemas, risk metadata, policy tier, canary state), `/v1/approvals`, `/v1/runs/{id}/cancel|reject|resolve`,
`/v1/repairs`, `/v1/artifacts/{id}/versions|diff|promote|rollback`, `/v1/targets`, `/v1/me`, `/v1/health`. The caller never supplies a
URL or credentials; the target profile decides. The older `POST /capabilities/{id}/invoke` (used by the chatbot and dashboard until
the unified UI replaces them) is unauthenticated and synchronous, and now refuses URL, credential and evidence-path overrides.

**Partial updates.** An update capability can mark individual fields optional (`when_present` steps and `at_least_one_of` inputs):
`clinic.update_patient_contact` takes an MRN plus any of phone, email, address, and leaves the fields you do not send exactly as they were.

### Metrics, logs and traces

```bash
uv run python cli.py metrics --hours 24       # per capability: runs, success, failure, needs_review, escalation, p50/p95 latency
curl -s localhost:8020/v1/metrics -H "Authorization: Bearer $VIC"        # the same as JSON
curl -s localhost:8020/v1/metrics.prom -H "Authorization: Bearer $VIC"   # Prometheus text format, for anything that scrapes
```

The figures come from the run store, and `runs/metrics.py` states exactly how each is defined (a business outcome counts as a good answer,
`needs_review` is counted separately, pending approvals are excluded from the rates).

The API server logs one JSON line per event to stderr (`run.accepted`, `run.started`, `run.finished`, `approval.decided`, `repair.proposed`,
`http.request`, `auth.rejected` ...), each tagged with its `run_id` and the key's name. Parameter values and keys are never logged, only
parameter names. `LOG_FORMAT=text` and `LOG_LEVEL=DEBUG` change the format and level. The CLI stays quiet unless you set `LOG_LEVEL`.

A failed run against a sandbox target keeps a Playwright trace: `evidence/<run>/trace.zip` (also `GET /v1/runs/{id}/trace`, operators and above).
Open it with `uv run playwright show-trace evidence/<run>/trace.zip` for a timeline of DOM snapshots, network calls and screenshots. Traces record
whatever was typed, passwords included, and cannot be redacted, so they are off for non-sandbox targets and kept for failures only; the `tracing`
section of `safety/policy.yaml` changes that.

### Using the platform from an AI assistant (MCP)

`mcp_server.py` exposes every recorded capability as an MCP tool, with its input schema and risk metadata, so an assistant such as Claude Desktop
can use a legacy system without a custom integration. It is a thin client of `/v1`: a call is an ordinary run with the same validation, caps,
idempotency and approvals, made under the assistant's own API key, so the key's role is its ceiling. There is deliberately no tool to approve:
a request that needs approval returns "pending approval" and a person approves it elsewhere. A tool that commits something requires an
`idempotency_key`, so a retry cannot post twice. Discovery is not exposed; only recorded capabilities can be called.

```bash
uv run python cli.py keys create --name my-assistant --role operator
```

Claude Desktop (`claude_desktop_config.json`); the API server and the target must be running:

```json
{
  "mcpServers": {
    "capability-platform": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/computer-use-automation-system", "run", "python", "mcp_server.py"],
      "env": {"CUA_API_URL": "http://127.0.0.1:8020", "CUA_API_KEY": "cua_..."}
    }
  }
}
```

Only the stdio transport is implemented. `CUA_WAIT_SECONDS` (default 60) is how long a call waits for a run before returning its run id, and
`CUA_TARGETS` maps a system to a target profile (default `{"clinic": "clinic", "mockbank": "mockbank"}`).

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
At the time of writing: 374 passing, 4 skipped without a key. The suite takes about five minutes
because it drives a real browser, including a full drift-repair loop.

## What is not done yet

Read [WRITEUP.md](WRITEUP.md) for the full list. The short version: approver identity is asserted
rather than authenticated, the supervised commit prompt has not been used by a person yet, repair
proposals are one step at a time and were tuned on the clinic's own drift modes, runs are synchronous
over an unauthenticated API, and there is no measured benchmark of discovery cost or reliability yet.
