# Design, incidents and limitations

This is a living document. Every claim below is backed by code in this repository or by a test,
and anything not yet true is listed under [Limitations](#limitations) rather than implied.

## The problem

Web UIs that have no API (legacy back-office screens, vendor portals) are usually automated by
hand-written scripts that break whenever the page changes, or by an LLM agent that drives the UI
live on every run: slow, expensive, and non-deterministic. This project takes a middle path.

- **Discovery.** An LLM drives the UI once, toward a stated goal, against the real page.
- **Artifact.** The run is turned into a typed, parameterized, reviewable JSON artifact.
- **Replay.** Every later run executes the artifact deterministically. The replay path imports
  nothing from the LLM code (`replay/`, `surface/`, `safety/`, `escalation/` and `evidence_lib/`
  have no import of `agent/` or any model SDK), so a replay cannot spend a token.

The model is allowed in exactly two places: discovery, and (planned) a human-approved repair
mode. Never in normal replay.

## Architecture

```
agent/      DiscoveryLoop  -> transcript -> recorder.py -> Artifact
replay/     ReplayEngine   (artifact + typed inputs -> ReplayResult)
surface/    Surface.perceive()/act()   one chokepoint, Playwright-backed
safety/     allowlist + risk classifier, consulted inside Surface.act()
escalation/ SessionManager (pause / take over / resume) + operator console
runtime.py  run_replay(): the one path CLI, API and chatbot all call
```

## Decisions and why

**The model discovers; code structures.** `agent/loop.py` only decides what to click, type or
extract. `agent/recorder.py` is deterministic: it assigns step ids, builds locator chains,
replaces concrete values with `{{param}}` placeholders, synthesizes per-step checkpoints and tags
risk. The contract (capability id, typed input and output, success signal, known error signals)
is written by a human in `agent/catalog.py`; the model only works out *how*.

**Perception is an accessibility tree, not pixels.** The loop is handed a numbered element list
(role, accessible name, value). It is cheaper and more stable than screenshots, and it is the
same vocabulary replay later resolves locators against. Screenshots are captured as evidence only.

**One locator mechanism.** A `Target` is an ordered fallback chain (role+name, then CSS, then
XPath, then text). The same shape is used to act and to check. The resolver treats an ambiguous
match as no match rather than guessing, so a locator that hits two elements falls through to the
next strategy instead of clicking the wrong one.

**One `Signal` shape, three purposes.** The same structure answers "did this step work" (step
checkpoint), "was the goal reached" (artifact success checkpoint) and "does this page match a
known condition" (error rules).

**Three-way result.** Replay returns `success`, `business_outcome` or `hard_failure`. A page that
says "no such member" is a correct answer, not an error, so collapsing it into either side is
the classic mistake. Classification order is business outcome, then recoverable condition, then
hard failure, and it runs after every action, not only on checkpoint failure, because a weak
checkpoint can pass on a broken page.

**Safety is a chokepoint, not a convention.** `Surface.act()` consults the allowlist and the risk
classifier for every action from both discovery and replay. The classifier re-classifies live and
does not trust the `risk_level` an artifact claims, so a stale or hand-edited artifact cannot
downgrade a step. An irreversible action is blocked unless a human confirms it.

**Escalation acts on the same live session.** The automation thread owns the Playwright page.
The operator console runs on another thread and may only enqueue intents; the automation thread
drains them inside `pause()`, because Playwright's sync API is not safe across threads. A step
escalates at most once. On resume the engine checks the step's checkpoint before redoing the
action, so a human who already performed it is not double-submitted.

**One execution path.** The CLI, the HTTP API and the chatbot all call `runtime.run_replay()`.
None of them reimplements it, so none of them can skip safety, evidence or escalation.

## Incidents

Things that broke when the system met reality, and what changed.

1. **A commit button slipped past the risk classifier.** During unsupervised discovery against an
   external legacy app, a button whose label carried none of the classifier's keywords committed
   a real, irreversible action with no confirmation. This is why keyword matching is documented
   as a limitation, why risk is also declared per capability, and why the planned fix is policy
   configuration rather than a bigger word list. (Earlier milestone; not part of this branch.)
2. **Playwright state leaked across pooled threads.** Running a replay on a shared API worker
   thread left that thread's Playwright event-loop state dangling, which broke the next unrelated
   request on the same thread. Each run now gets its own throwaway thread.
3. **A parameter value corrupted an artifact.** The recorder replaces concrete values with
   placeholders by substring match, so a value that was a literal substring of another recorded
   string rewrote the wrong text. It was fixed by hand in the one affected artifact; the
   substring approach itself is still in `agent/recorder.py` and is on the fix list.
4. **The chatbot silently substituted a different capability.** Given an incomplete request, the
   model picked a capability it could fully satisfy instead of asking what was missing. Three
   prompt rewrites failed to stop it, and one made it invent placeholder values. A code-level
   keyword guard now declines instead. It is a stopgap, not a solution.
5. **A discovered artifact had no starting step.** Replayed from a different page, it failed at
   step one. Discovery now records an explicit navigation to `start_path` first.
6. **Wrong values looked like hangs.** Playwright's 30s default made a stale locator value look
   like a frozen run. The default action timeout is 8s.

## Evidence for each claim

| Claim | Backed by |
|---|---|
| Replay path cannot call a model | No import of `agent/` or a model SDK anywhere in the replay path (checked by grep) |
| Core behavior | 96 offline tests (schema, replay engine, safety, session manager, operator console, API, dashboard, web surface) |
| LLM discovery works end to end | `tests/test_discovery_live.py` (skips without `GEMINI_API_KEY`) |
| Measured success and recovery rates | **Not yet measured.** A benchmark harness is planned. |

## Limitations

These are verified against the code as of this writing.

- **Irreversible steps are hand-authored.** Discovery stops at the confirmation gate, so the
  final commit step of a state-changing artifact is written by hand. Some artifacts are
  `reviewed: false`.
- **No stability waits.** Replay relies on Playwright's auto-waiting and an 8s action timeout.
  There is no DOM-stability or network-idle wait, no whole-run timeout, and cancellation only
  works while paused.
- **Retries are not fully idempotent-safe.** `Step.idempotent` prevents an automatic restart
  after re-login once a commit step ran, but a `RETRY` recovery rule re-runs the step blindly,
  and there are no idempotency keys or server-side ground truth to confirm "exactly once".
- **Safety rules live in code.** The risk classifier is a keyword list. There are no per-capability
  limits, dry-run mode or approver identity.
- **Redaction is narrow.** Only password-like fields are redacted; evidence otherwise contains
  whatever the page shows.
- **No locator healing and no versioning.** A UI change fails the artifact. Storage keeps one
  file per capability, overwritten on each discovery, with no history or rollback.
- **Synchronous, in-memory runtime.** `/invoke` blocks until the run finishes. Sessions, chat
  history and settings are process-global memory, and run history is rebuilt by scanning the
  evidence directory. Nothing survives a restart.
- **The API is unauthenticated** and accepts `evidence_dir`, `base_url` and credentials from the
  request body. It is for local use only. The operator console also starts at import time on a
  fixed port.
- **One browser per run.** Each run launches Chromium and logs in again. Headed runs deliberately
  leave the browser open.
- **The chatbot is minimal.** Gemini only, no conversation memory, one capability per message,
  a hand-maintained keyword guard, and a server-rendered page that polls.
- **One test target.** The offline suite only exercises the bundled MockBank app, so results
  say little yet about generalization to other apps.

## What is next

A self-hosted target app with an audit-log API as ground truth, fault and drift injection, and a
reset endpoint; supervised capture of irreversible steps; locator repair proposals that a human
approves; replay stability waits and idempotency; async, persisted runs with API auth; one
unified UI; and a benchmark that reports success rate, recovery rate and discovery cost against
zero-token replay. None of these are claimed until they are built and measured.
