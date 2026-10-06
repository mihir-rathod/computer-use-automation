# Reference

The detail behind the [README](../README.md): the API, metrics and traces, MCP, the command line, and the clinic target app. Design decisions, incidents and limitations are in [WRITEUP.md](../WRITEUP.md).

- [The v1 API](#the-v1-api)
- [Metrics, logs and traces](#metrics-logs-and-traces)
- [Using the platform from an AI assistant (MCP)](#using-the-platform-from-an-ai-assistant-mcp)
- [Operating the clinic capabilities (CLI)](#operating-the-clinic-capabilities)
- [The clinic target app](#the-clinic-target-app)
- [The console pages](#the-console-pages)

## The v1 API

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
URL or credentials; the target profile decides. There is no other way in: the older unauthenticated, synchronous `/capabilities/{id}/invoke`, with its own chat page and dashboard, was removed (it is on the `mockbank` branch).

**Partial updates.** An update capability can mark individual fields optional (`when_present` steps and `at_least_one_of` inputs):
`clinic.update_patient_contact` takes an MRN plus any of phone, email, address, and leaves the fields you do not send exactly as they were.

## Metrics, logs and traces

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

## Using the platform from an AI assistant (MCP)

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
`CUA_TARGETS` maps a system to a target profile (default `{"clinic": "clinic"}`).

## Operating the clinic capabilities

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
authenticated: see the limitations in [WRITEUP.md](../WRITEUP.md).

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
requires it. See [WRITEUP.md](../WRITEUP.md) for what the test kit is for and what it does not cover.

## The console pages

| Page | What it is for |
|---|---|
| Tasks (home) | A task search. Opening a task shows a form generated from its input schema, with what will happen, what approval it needs, and the watch option |
| Overview (admin) | Success, failure, escalation and latency per capability, and a banner when something needs a person or a canary is failing |
| Runs | Every run with filters. A run shows live progress, each step with the page as it looked afterwards, and for a failed step what was expected against what happened |
| Inbox | Approvals, runs whose commit outcome is unknown, and repair proposals, with who asked and how long each has waited. Decisions need a reason and are recorded with your name |
| Artifacts | A capability's steps, how each element is found, provenance, version history, side-by-side diff, promote and roll back |
| Chat | A shortcut for one-off requests. Starts only recorded tasks, asks when something is missing, can never approve; history is saved per key and can be cleared, and a half-typed message survives switching pages |
| Discover (supervisor) | Describe a new task in words, watch the model work it out once, answer its one commit question, review the recorded draft and make it available. See the [README](../README.md#discovering-a-new-task) |
| Policy, API keys | The safety policy (read-only) and key management (admin) |
