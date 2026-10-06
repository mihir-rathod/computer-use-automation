# Computer-Use Automation System

An LLM works out how to do a task on a web UI **once**. That run is recorded as a typed, parameterized **artifact**. Every run after that is a **deterministic
replay**: no model, no tokens, with safety gating, human approval and a full evidence trail around it.

> **Status: in active development on `dev`.** Discovery, replay, approvals, repair proposals, an authenticated async API, an MCP server and a web console work end to
> end against a purpose-built clinic app that has fault injection, UI drift and an audit log. Eight clinic tasks were discovered by the model; more can be added from
> the console. A measured benchmark, CI and the hosted demo are still to come. [WRITEUP.md](WRITEUP.md) has the design, the incidents and an honest list of what is not done.

## What you can do

- **Run a task** from a form generated from its input schema. A task that commits something (a refund, a claim) waits for a supervisor's approval, and a retry can never post twice.
- **Watch it run**, with a pause around each step and the element about to be touched outlined, or just read the step timeline and screenshots afterwards.
- **Discover a new task** by describing it in a sentence. The model works it out on a practice copy, you review the recording, and from then on it replays with no model.
- **Handle what needs a person** in one inbox: approvals, runs whose outcome is unknown, and proposed repairs after a screen changed.
- **Use it from anywhere else**: an authenticated HTTP API, a command line, or any MCP-capable AI assistant.

## Run it

Needs Python 3.12, [uv](https://docs.astral.sh/uv/) and Node 20+.

```bash
uv sync
uv run playwright install chromium
cp .env.example .env                              # add GEMINI_API_KEY only for discovery and chat
(cd ui && npm install && npm run build)           # the web console, built once
```

Start the practice system and the platform in two terminals:

```bash
uv run uvicorn clinic.app:app --port 8100
uv run uvicorn api.app:app --port 8020
```

Create a key, shown once, and sign in at <http://localhost:8020/ui/> with it:

```bash
uv run python cli.py keys create --name dana --role supervisor
```

The practice clinic signs in as `frontdesk` / `desk-demo-123` (or `supervisor` / `super-demo-123`); the platform does this for you. All data is synthetic.

## The console

What you see follows your key's role. The role decides visibility only; the API enforces every permission itself.

| Role | Sees |
|---|---|
| viewer | Tasks and Runs, read-only |
| operator | Tasks, My runs, Inbox (the badge counts only what *you* can decide), Chat |
| supervisor | The above, plus **Discover** |
| admin | The above, plus **Manage**: Overview (metrics), Artifacts, Policy, API keys |

Keyboard: `g` then a letter to jump between pages, `/` to search, `?` for help. Light and dark themes follow your system. Page-by-page details are in
[docs/reference.md](docs/reference.md#the-console-pages). While developing the UI, `cd ui && npm run dev` serves it on port 3000 and proxies `/v1` to the API.

**Watch it run.** On a task form, choose *Slow* or *Step by step*. The element about to be touched is outlined in amber and the run page follows along. It never changes what the
run does. If the server runs on the machine you are sitting at, start it with `CUA_ALLOW_WINDOW=1` to also open a real browser window.

### Discovering a new task

Supervisors: **Discover → Discover a task**.

1. Say what you want, with a real example: *"Look up appointment A-20002 and tell me the patient and the provider."*
2. **Draft the task.** The model looks at the system's main page and proposes the name, inputs, what it reads back and whether it only reads. If an example is missing it asks
   instead of inventing one. You confirm the draft; the one thing that matters most is whether the task changes anything.
3. **Start discovery.** The model drives a practice copy using only clicks and typing, and you watch each step. Detours and failed actions are pruned from the recording.
4. The result is saved as a **draft** that nobody can run. The system decides what text shows success, then replays the draft once with no model to prove it stands on its own.
   A recording that never uses its input, or reads the wrong things, is not offered as ready.
5. You look over the recorded steps and make it available (the same approval tier as any promotion), or discard it.

If the model reaches the one irreversible step it stops and asks you; a task declared read-only can never commit. If it gets stuck, nothing is saved and *Try again with a hint*
reopens the draft. *More details* lets you set everything by hand. Passwords, secrets and tokens are refused as inputs. One session runs at a time, and it needs
`GEMINI_API_KEY` (running tasks does not). Limits are under `teaching:` in `safety/policy.yaml`.

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

- **Discovery** reads the page as an accessibility-tree element list (not pixels) and drives a real Playwright browser one action at a time toward a goal.
- **Recording** is deterministic code, not the model: steps with locator fallback chains, parameter placeholders, per-step checkpoints and risk tags.
- **Replay** classifies every page state: a known *business outcome* ("no such record") is a result, a *recoverable* condition is handled, anything else is a hard failure that can escalate.
- **Safety** lives in one chokepoint, `Surface.act()`, which discovery and replay both pass through. An irreversible action is blocked unless a human confirms it.
- **Every front door** (console, API, CLI, chat, MCP) goes through the same `runtime` code, so none of them can skip safety, evidence or approval.

## Command line

```bash
uv run python cli.py replay --target clinic --capability clinic.patient_lookup --param mrn=LK-100002          # replay, no model
uv run python cli.py replay --target clinic --capability clinic.patient_lookup --param mrn=LK-100002 --headed --slow-mo 600   # watch it
uv run python cli.py discover --target clinic --capability clinic.patient_lookup --param mrn=LK-100001        # discovery from a catalog spec (needs GEMINI_API_KEY)
uv run python cli.py artifact list                                                                           # versions, drafts, what is current
uv run python cli.py metrics --hours 24
```

Approvals, repairs, canaries, keys and the rest are in [docs/reference.md](docs/reference.md).

## Where things are

| Area | Paths |
|---|---|
| **Discovery and replay** | `agent/` (discovery loop, recorder, catalog) · `replay/` (engine) · `surface/` (Playwright, browser pool) · `artifacts_lib/` (schema, versions, lint, diff) · `teach/` (discovery from the console) |
| **Safety and runs** | `safety/` (allowlist, risk, `policy.yaml`) · `runs/` (SQLite store, async executor) · `repair/` · `escalation/` · `evidence_lib/` |
| **Interfaces** | `api/` (the `/v1` API) · `ui/` (web console, Next.js) · `mcp_server.py` · `cli.py` · `observability.py` |
| **Practice systems** | `clinic/` (the target we ship: legacy and React skins, JSON API, audit log, chaos, drift). The original MockBank sample, and the older chat and dashboard, live on the `mockbank` branch; the engine tests still use a small copy of the MockBank site under `tests/support/`. |
| **Data** | `artifacts/` (one folder per task, every version) · `data/` (run database) · `evidence/` (screenshots, logs, traces) |
| **Everything else** | `tests/` · `scripts/` · `docs/reference.md` · `WRITEUP.md` |

## Tests

```bash
uv run pytest
```

430 tests pass. The suite starts the clinic in-process and drives a real Chromium, so it takes about 12 minutes. Eight tests call the real model (discovery from a sentence,
chat, escalation) and skip without `GEMINI_API_KEY`. The modern-skin browser tests skip until `clinic/modern` is built (`cd clinic/modern && npm install && npm run build`).

## Not done yet

The short version: a measured benchmark and CI; the hosted demo; taking over a paused run; approver identity on the CLI is asserted, not authenticated; repair proposals are one
step at a time. [WRITEUP.md](WRITEUP.md) has the full list, the design decisions and the incidents.
