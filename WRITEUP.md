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

The model is allowed in exactly two places: discovery, and an optional ranking step inside
human-approved repair (off by default, and the replay path never imports it). Never in normal replay.

## Architecture

```
agent/      DiscoveryLoop  -> transcript -> recorder.py -> Artifact   (+ commit gate for irreversible steps)
replay/     ReplayEngine   (artifact + typed inputs -> ReplayResult)
surface/    Surface.perceive()/act()   one chokepoint, Playwright-backed; pool.py = browser workers
safety/     allowlist + risk classifier inside Surface.act(); policy.yaml = approvals, caps, redaction
escalation/ SessionManager (pause / take over / resume) + operator console
runs/       RunStore: runs, idempotency keys, approvals, repair proposals, canary history (SQLite)
repair/     locator repair proposals (heuristic, optional LLM ranker) and their approval
canary.py   scheduled read-only replays
runtime.py  run_replay(): the one path CLI, API and chatbot all call
            policy caps -> idempotency -> approval -> browser pool -> engine -> run record
```

## The clinic target

The platform needs something it can be tested and measured against that it does not control the
behavior of, and that a real browser can reach. `clinic/` is that: a fictional clinic portal with
a legacy server-rendered skin and a modern React skin over one set of business rules
(`clinic/services.py`), so a rule cannot differ between skins.

What makes it a test target rather than just an app:

- **Ground truth.** Every read, review, denial, rejection and state change is written to an audit
  log by the domain layer, not the UI. `effect=1` marks rows where state really changed, so a test
  can assert "exactly one refund was issued" independent of what any screen displayed.
- **Idempotent reset** to a deterministic seed, which also clears sessions, chaos rules and drift.
- **Chaos rules**: latency, 500s, a maintenance page whose Continue link does not go back, session
  expiry, rate limiting, each matchable by method and path.
- **A duplicate-submit guard that can be switched off.** With it on, a second confirm of the same
  transaction is blocked and audited. With it off, a retried refund really posts twice. That lets
  the platform prove it does not depend on the target to stop a double-post.
- **UI drift** in four levels (renamed ids and classes, then labels, then form field names).
- **Supervisor gating at submit time, not view time**, and a refund cap with a two-person approval
  flow, so permission-denied and pending-approval are real business outcomes.

The legacy skin is awkward on purpose: unlabeled inputs (so role locators find nothing and CSS
`name` fallbacks are needed), headerless tables, identical "Claim" / "Refund" links on every row,
and results returned straight from a POST so a refresh resubmits.

## Hardening decisions (phase 2)

**An irreversible step is never retried on a guess.** If a step that commits something was
issued and the run cannot confirm the result (the page errored, the session expired, the
checkpoint never appeared), the result is `needs_review`, not a retry and not a failure. A
`RETRY` rule in the artifact does not override this. It is deliberately conservative: when the
session expired as the commit was submitted, nothing had posted, and the engine still reports
`needs_review` because from the outside it cannot tell (`test_recoverable_page_after_commit_...`).
A person settles it against the target's own records with `cli.py resolve`.

**Idempotency lives in the platform, not the target.** `run_replay(idempotency_key=...)` records
the run before it starts. A second request with the same key gets the first run's stored result
and no browser is launched; if the first run is unresolved the key is blocked until someone
settles it. The tests switch the clinic's own duplicate guard off, so "posted exactly once" is
the platform's doing, and they assert it against the clinic's audit log rather than the page.

**Approvals are recorded, tiered and bound to an identity.** `safety/policy.yaml` says per
capability whether its commit step needs a live confirmation, an operator approval or a
supervisor approval, and sets a numeric cap per parameter and a daily commit cap. An approval
stores who, when and why; the requester cannot approve their own run; and a run approved as one
target account cannot be resumed as another. The approver *roster* is enforced, but identity is
asserted by the caller, not authenticated (see Limitations).

**Commit steps are recorded, not hand-edited.** During discovery the safety policy still blocks
the irreversible click; a commit gate (`agent/commit_gate.py`) then asks a person at the terminal
(`supervised`) or, only on a profile marked `sandbox`, approves automatically (`auto_sandbox`).
The step is executed for real, recorded as irreversible and non-idempotent, and the approver and
mode are written into the artifact's provenance. Lint warns if an irreversible step has no
recorded approval.

**A failed locator produces a proposal, never a patch.** When replay cannot resolve a step's
element, `repair/propose.py` looks at the page the step failed on and ranks same-role elements
against the hints recorded at discovery (position among same-role elements, neighbouring labels,
HTML field name, old name). If one clearly wins it becomes a stored proposal with the candidates,
scores and a screenshot; otherwise the proposal says "no confident match". Approval writes a new
artifact version (parent, approver and reason in provenance), checked by lint, and a repair that
touches an irreversible step needs a supervisor. Rollback restores the previous version.

**Canaries find drift before traffic does.** An artifact may carry a known-good invocation and the
outputs it must produce. `cli.py canary run` replays those against the live target; only
read-only capabilities may have one (lint enforces it), and a failure records its repair proposal.

**Replay waits for the page, then asks.** After an action that can change the page the surface
waits for the network and DOM to go quiet (bounded). Checkpoints are polled for up to 1.5s rather
than checked once, and a known error banner is looked for again after that wait. A whole-run
deadline and a cancel event are checked before every action and inside every backoff sleep.

**A browser pool instead of a throwaway thread.** Headless runs execute on a fixed set of worker
threads that each own one Chromium, with a fresh isolated context per run. That keeps Playwright
off request threads (incident 2) without launching a browser per run, and bounds concurrency.

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

Found while building phase 2 (each has a test):

7. **UI drift broke sign-on before anything else.** The first drift test failed at login: an
   unlabeled field was recorded with only a bare role and its `id`, and renaming ids (the mildest
   drift level) removed the only usable fallback. The HTML `name` is now always recorded as well.
8. **A password reached the evidence log.** Discovery against the clinic's unlabeled password field
   logged the typed text in clear, because redaction keyed on the accessible name and a legacy form
   has none. A second leak was the model's own commentary ("I typed the password ..."). Redaction
   now also uses the form field name and scrubs the literal value of any password parameter from
   free text.
9. **The model selected an option by its label and an input went unused.** Discovery recorded the
   literal label (`Weather`) instead of the `{{reason}}` parameter, so cancel and write-off ignored
   their reason input. The new `unused-input` lint caught it; the surface now reports the value it
   actually applied and the recorder parameterizes that.
10. **A URL checkpoint pinned one patient.** `**/legacy/patients/1` was recorded from discovery and
    failed for every other patient. Record ids in URL checkpoints are now wildcards, and lint warns
    about any that remain.
11. **Value cells were located by their own text.** A phone number cell recorded as
    `cell[name='(206) 555-0111']` either fails on the next patient or matches an unrelated cell. Cells
    are now anchored to the label beside them.
12. **An approved run could be resumed as a different account.** Writing the write-off test showed
    that approval was not bound to the target identity. The run record now stores the target and a
    resume under another one is refused.
13. **A repaired step kept a broken checkpoint.** The drift report showed repairs stalling: the step's
    own checkpoint read the same element through the old locators. Repair now updates it too.
14. **The first repair heuristic was too strict.** The initial measurement (below) recovered 3 of 7
    capabilities at drift level 2 and 0 of 7 at level 3 because unlabeled fields give no name to match
    on. The scoring was then changed to use position as the base signal. See the caveat under the table.

## Measured: drift and repair

`scripts/drift_report.py` replays each clinic capability under each UI drift level against a running
clinic, approves the platform's repair proposal whenever replay fails on an unresolved locator, and
replays again. State-changing capabilities run through the full approval flow and the table counts the
real state changes from the clinic's audit log. Last run (7 capabilities, sign-on repaired inside each):

| Drift level | What changes | Recovered with approvals | Proposals needed per capability | State changes per commit run |
|---|---|---|---|---|
| 1 | ids and css classes | 7 of 7, no repair needed | 0 | 1 |
| 2 | + button, link and heading labels | 7 of 7 | 3 to 5 | 1 |
| 3 | + form field names | 7 of 7 | 6 to 11 | 1 |

Every proposal in the final run was confident and approved without edits. Before the scoring change
the same script recovered 3 of 7 at level 2 and 0 of 7 at level 3.

Read this carefully. The heuristic was tuned against this clinic's own drift generator, so the table
shows the mechanism works on a target whose changes are known, not that it will survive a real vendor
release. A proposal is one step at a time: a capability with 11 broken steps takes 11 approvals, because
later pages cannot be inspected until earlier steps work. "Confident" means the heuristic found a clear
winner, not that it is right; the end-state checks (success plus exactly one audit-logged state change)
are what show the repaired flows did the right thing. Drift that changes a flow (a new step, a moved
page) or the text of a checkpoint or business-outcome signal is not covered at all and fails plainly.

## Evidence for each claim

| Claim | Backed by |
|---|---|
| Replay path cannot call a model | No import of `agent/` or a model SDK anywhere in the replay path (checked by grep) |
| Platform behavior | 200 offline tests: schema, replay engine, safety, sessions, operator console, API, dashboard, web surface, artifact versioning/lint/diff, replay hardening, run store and policy, commit recording, browser pool, clinic capabilities, repair and canary |
| Clinic target behavior | 62 tests: business rules, audit and exactly-once semantics, JSON API, both skins, test kit, and 16 real-browser tests |
| A retried commit posts exactly once | `tests/test_clinic_capabilities.py`: same-key retry, response lost after commit, request lost before commit; all asserted on the clinic audit log with its own duplicate guard off |
| An irreversible step is not retried on a guess | `tests/test_replay_hardening.py` (needs_review, RETRY rule ignored, session expiry at commit) |
| Every clinic capability replays | `tests/test_clinic_capabilities.py` replays the 8 checked-in LLM-discovered artifacts with different inputs than discovery |
| Drift yields a repair proposal; approval makes a new version; rollback works | `tests/test_repair_and_canary.py`, `scripts/drift_report.py` |
| Commit steps can be recorded from a run | `tests/test_commit_recording.py` (scripted model, real browser, real clinic) and the 4 checked-in artifacts whose provenance records an `auto_sandbox` approval |
| LLM discovery works end to end | `tests/test_discovery_live.py` (skips without `GEMINI_API_KEY`) |
| Measured success and recovery rates | **Not yet measured.** A benchmark harness is planned. |

## Limitations

These are verified against the code as of this writing.

- **Approver identity is asserted, not authenticated.** The roster in `safety/policy.yaml` is enforced
  (right tier, not the requester), but nothing proves the caller is the person they name. Real
  authentication is phase 3.
- **The supervised commit gate has not been exercised by a person in this build.** It is a terminal
  prompt, tested with an injected answer function. The checked-in clinic artifacts were recorded with
  `auto_sandbox`, which only a profile marked `sandbox` permits. `mockbank.open_subaccount` is still
  hand-written, and the `unreviewed` lint warning stands on every discovered artifact.
- **Recording is only as good as the contract and the model.** All 8 clinic discoveries finished on
  their final attempt, but they were run repeatedly while platform bugs were being found (incidents 7
  to 11), so that is not a success rate. No benchmark of discovery cost or reliability exists yet.
- **`needs_review` is conservative.** Any doubt after an issued irreversible step stops the run, even
  when nothing posted. A commit step that returns a business outcome (for example a refund queued for
  supervisor approval) reports `committed: false` although the request was recorded by the clinic.
- **Repair is narrow.** One proposal per broken step, so a badly drifted capability needs many
  approvals. The heuristic only considers elements of the same role and was tuned on the clinic's own
  drift generator. It cannot handle a moved or removed element, a changed flow, or a changed checkpoint
  or business-outcome text. The LLM ranker (`--repair-llm`) exists and is unit-tested with a fake
  client; it has not been run against the live model.
- **Canaries are minimal.** Only `clinic.patient_lookup` has one, only read-only capabilities may, and
  the scheduler is a plain loop (`canary schedule`), not a daemon with alerting.
- **Timeouts cannot interrupt an action in flight.** The run deadline and cancel are checked between
  actions; an action itself is bounded by the 8s page timeout. The maintenance-page chaos rule is not
  recoverable by any artifact (there is no restart-from-the-top recovery action).
- **Redaction is partial.** Evidence (`log.jsonl`, `result.json`) and the stored parameters in the
  evidence trail are scrubbed by pattern, field name and known secrets. Screenshots are not, and the run
  store keeps parameters and results unredacted because replaying an approved run and answering a
  duplicate request need them. Protect `data/runs.db` like the target's own data.
- **The browser pool does not cover everything.** Headed and slow-mo runs use a dedicated thread, a job
  that blocks forever holds its worker, and a paused escalation holds one until a human resumes.
  Tests print Playwright teardown noise at interpreter exit.
- **Synchronous API, unauthenticated.** `/invoke` blocks until the run finishes, accepts `base_url`,
  `evidence_dir` and credentials from the request body, and is for local use only. Chat history and
  sessions are process memory. The operator console starts at import time on a fixed port.
- **The chatbot is minimal.** Gemini only, no conversation memory, one capability per message, a
  hand-maintained keyword guard, and a server-rendered page that polls.
- **Clinic coverage.** Capabilities target the legacy skin only; the modern skin, the CSV export and the
  approvals queue have no capability. Browser tests cover search, contact update, reschedule, cancel,
  CSV download, session expiry, drift, duplicate submit and maintenance; the claim, refund, write-off and
  approval flows are covered at the API and server-rendered level by the clinic's own tests and through
  replay by the platform's. Chaos applies to `/legacy` and `/api` only. Drift covers all four levels on
  the legacy skin but only labels on the modern skin. State is in memory unless `CLINIC_DB_PATH` is set.
  The Render blueprint (`render.yaml`) has not been deployed.

## What is next

Async, persisted runs with API-key auth and one unified UI (phase 3); a benchmark that reports success
rate, recovery rate and discovery cost against zero-token replay; running the supervised gate and the LLM
repair ranker live; deploying the clinic. None of these are claimed until they are built and measured.
