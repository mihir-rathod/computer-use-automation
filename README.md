# Computer-Use Automation System

An LLM figures out how to complete a task on a web UI **once**. That run is recorded as a typed,
parameterized **artifact**. Every run after that is **deterministic replay**: no model in the
loop, no tokens spent, with safety gating, human escalation and a full evidence trail around it.

> **Status: in active development on `dev`.** The core loop works end to end against a bundled
> mock app. A self-hosted target, a unified UI and a measured benchmark are in progress. See
> [WRITEUP.md](WRITEUP.md) for the design, the incidents, and an honest list of what is not done.

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
| `artifacts_lib/` | Artifact schema (Pydantic) and JSON storage |
| `agent/` | Discovery loop, tool set, Gemini client, recorder, capability catalog |
| `replay/` | Deterministic replay engine, templating, input validation, result types |
| `surface/` | Surface interface and the Playwright implementation, aria parsing, locator resolution |
| `safety/` | Allowlist, risk classifier, combined policy |
| `escalation/` | Pause/resume session manager and the operator console |
| `evidence_lib/` | JSONL evidence logger and redaction |
| `api/` | Capability API, chatbot, run dashboard |
| `mockbank/` | Bundled legacy-style bank app used as a test target |
| `artifacts/` | Saved capability artifacts |
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

# run LLM-driven discovery (needs GEMINI_API_KEY); saves artifacts/<id>.json,
# overwriting the checked-in artifact of the same id
uv run python cli.py discover --capability mockbank.member_balance_lookup --param member_id=10001

# watch a run in a visible browser
uv run python cli.py replay --capability mockbank.member_balance_lookup --param member_id=10001 --headed --slow-mo 600
```

MockBank's demo login is `operator` / `bankdemo123` (a fixture credential, not a secret).

## Tests

```bash
uv run pytest
```

The suite is offline: it starts MockBank in-process and drives a real Chromium against it. The
live-model tests (discovery, chatbot) skip automatically unless `GEMINI_API_KEY` is set.
At the time of writing: 96 passing, 4 skipped without a key.

## What is not done yet

Read [WRITEUP.md](WRITEUP.md) for the full list. The short version: irreversible steps are still
hand-authored in artifacts, replay has no stability waits, runs are synchronous and not
persisted, the API has no authentication, and there is no measured benchmark yet.
