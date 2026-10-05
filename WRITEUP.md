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

## Runtime and API decisions (phase 3a)

**Async runs, one execution path.** `runtime.prepare_run` does everything that needs no browser (input validation, policy caps, the
idempotency claim, the approval request, the run record) and returns either an early answer or a prepared run; `run_replay` executes it
inline, and the v1 API hands it to `RunExecutor` and returns the run id at once. Concurrency is bounded by the browser pool, a run waits as
`queued`, and `cancel` stops it between actions. A server that dies mid-run recovers on start: a run left running that could have issued an
irreversible step becomes `needs_review`, anything else a failure, so neither blocks its idempotency key for ever.

**Identity from the key, not from the caller.** v1 requests carry an API key (stored as a hash). Its name is the requester or approver and
its role is the authority, so "who approved this" is no longer asserted by the request. A requester still cannot approve their own run.
Forbidden (wrong role) and conflicting (already decided) are different HTTP statuses.

**The caller does not choose where a run goes.** v1 accepts a target *name*, never a URL or credentials. The older synchronous endpoint
refuses URL, credential and evidence-path overrides unless a test environment variable allows them.

**Partial updates are a capability design, not an HTTP verb.** The contact update used to need every field because the engine backfilled any
omitted optional input with an empty string, which would have blanked the field. Steps can now be conditional (`when_present`): an omitted
or null input skips the step and the page keeps what it already holds; `at_least_one_of` demands that something be supplied. The recorder
marks the steps for optional inputs itself, so a rediscovery produces this shape without hand-editing.

## Observability and MCP decisions (phase 3b)

**Metrics are queries over the run store, not a second set of counters.** The run store is already the system of record for every run, so
`runs/metrics.py` derives success, failure, escalation and latency from it and states each definition (a business outcome is a good answer,
`needs_review` is neither success nor failure, runs still waiting or never executed are excluded from rates). It is served as JSON and in
Prometheus text format and printed by `cli.py metrics`.

**One log line per event, tagged with the run, and never with values.** Logs are JSON on stderr. The run id travels in a context variable and
is copied into browser-pool worker threads, so a line written deep in the executor or on a browser thread still carries it. Only an allowlist of
facts is logged (ids, statuses, durations, error codes, parameter *names*); parameter values and API keys are not.

**Traces for failures, on sandboxes, by policy.** A Playwright trace is the best evidence a failed run can leave, but it records everything typed,
including passwords, and cannot be redacted. So the default keeps one only when a run fails and only for a target marked sandbox; both are
settings in `safety/policy.yaml`, the file is created owner-only, and downloading it needs an operator key.

**MCP is a doorway, not a new capability.** `mcp_server.py` is a thin client of `/v1`, so an assistant's call goes through the same validation, caps,
idempotency and approvals as any other run, under the assistant's own key. Only recorded capabilities become tools (discovery is not exposed).
There is no approve tool, so an assistant cannot approve its own request. A tool that commits something requires an `idempotency_key` (assistants
retry); a request that needs an approval returns at once as "pending approval" instead of holding the conversation open; and an unknown outcome is
reported as an error that says not to retry.

## Console decisions (phase 4)

**Task-first, with chat as a supplement.** The home of the app is the catalog: pick a task and fill a form generated from its input schema (enums become
selects, patterns become format hints and are checked in the browser with the same rules the server applies, an optional field says what leaving it
empty means). Forms are clearer and safer than a sentence for anything that commits money. Chat is one tab: it can only start recorded tasks, checks
every argument against the schema and refuses invented ones, shows its run inline with live status and approve buttons, and cannot approve anything.

**Decisions are made where the context is.** The inbox shows each item with who asked, what for and how long it has waited, and approve, reject and
settle all take a reason that is recorded with the person's name. Buttons are disabled with the reason when you are the requester or your role is too low,
instead of failing after a click.

**A run explains itself.** The timeline joins the artifact's steps to the evidence log: each step's outcome, a screenshot of the page after it (kept
for sandbox targets by `evidence.screenshots` in the policy), and for the failed step what was expected against what actually happened. Progress is live
over server-sent events read with `fetch`, because `EventSource` cannot send the API key.

**One door for everyone, with the role deciding what else you see.** After the first version the console showed every role the same eight pages, which buried
the point of the product for the person who just wants to get a job done. Everyone now lands on a task search; an operator or supervisor sees four items, a viewer
two, and only an admin gets a Manage section (metrics, artifacts, policy, keys). The inbox badge counts only what the person can decide, so it never nags about
something they are not allowed to act on. This is visibility, not security: the API enforces every role.

**Watching a run is our own pacing, not Playwright's `slow_mo`.** `slow_mo` delays every internal call (resolving, polling, settling), so a run would crawl and
risk its own timeouts. Watch mode instead pauses a set time around each action and outlines the element about to be touched, and the outline stays for the
screenshot taken afterwards, so the live view shows what was just done. Sign-on is not paced. Two ways to see it: a live view in the console (works against any
server, built on the per-step screenshots, sandbox targets only) and, only when the server runs on the person's own machine and says so (`CUA_ALLOW_WINDOW=1`), a
real browser window. The choice is stored with the run, so it survives the wait for an approval. A smooth video of a remote browser, with the person able to
click in it, was deliberately not attempted.

**Next.js, exported statically.** The plan recommended Next.js; the app is a client-rendered tool behind a key, so it is built as a static export and
served by the API process under `/ui`: one process, one port, nothing to deploy separately. Routes use query strings (`/run/?id=...`) because a static
export cannot pre-render unknown ids.

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

Found by exercising the finished system by hand (each pinned in `tests/test_qa_regressions.py` and `tests/test_edge_cases.py`):

15. **One approval could be executed several times.** Four concurrent resumes of a single approved
    refund each launched a browser. Resume now claims the approved run atomically; exactly one wins.
16. **A connection error left a run "running" for ever and blocked its idempotency key.** An
    unreachable target now closes the run as a `runner_error` failure and frees the key. An unexpected
    crash after an irreversible step was issued is reported as `needs_review`, not a plain failure.
17. **Settling an ambiguous commit, and promoting or rolling back a version, needed no roster.** Any
    name worked. Both now require someone on the roster at the capability's tier.
18. **`--resume` ignored `--capability`**, so resuming a refund run under the name of another
    capability ran the refund. It now refuses a mismatch.
19. **An idempotency key reused with different parameters returned the first request's result.** It is
    now a conflict.
20. **A pending-approval or policy-refused run showed as "running" on the dashboard.** Also: a
    capability could be run against the wrong app (it hit an allowlist block instead of saying so), a
    failed login reported nothing useful, and the CLI printed Python tracebacks for an unknown
    capability or a bad resume. All fixed.

Found by a second QA pass over every capability with edge cases and injected faults (about 100 cases; the money flows held up, including a
lost response, a lost request, a 12s stall on the commit and a session expiring mid-run, always with exactly the expected state changes):

21. **An input's allowed values were never enforced.** A reason that was not one of the listed options passed validation, then waited 8
    seconds for a dropdown option that did not exist. Validation now checks `enum`, length and range, and selecting a missing option
    fails at once and lists what is offered.
22. **A request that could not run still reached an approver's queue.** An amount sent as a number was queued for supervisor approval and
    only then failed validation. Validation now runs before policy, approval and the idempotency claim.
23. **The chatbot invented values and the run went ahead.** Asked to change only a phone number, the model filled email and address with
    "unknown", which can pass the target's own validation and be written to a record. Arguments are now checked against the schema and
    placeholder words are refused with a question. (The root cause, no partial update, is fixed above.)
24. **The chat page still described the previous target.** Its "required fields" table listed capabilities that do not exist here. It is now
    built from the real artifacts.
25. **Re-sending a field's current value read as a hard failure.** The target says "no changes were needed", which failed the success
    checkpoint. It is now a `no_change` business outcome.
26. **The unauthenticated endpoint would send a profile's credentials to any URL it was given, and write evidence to any path.** Both
    overrides are now refused unless a test environment variable allows them.


Found while adding watch mode and role-based navigation:

29. **Watch mode slowed the sign-on, not just the task.** The first watched run sat for ten seconds before the task's first step because the four sign-on
    actions were paced too. Pacing now starts after sign-on.
30. **A live view could freeze while its tab was open.** Polling was skipped whenever the tab reported itself hidden, which some embedded browsers do while
    someone is watching. Background tabs now poll a fifth as often instead of stopping.
31. **A test run leaked into the next test.** A chat test started a paced run and did not wait for it, so its pauses were counted by an unrelated test. Tests
    that start background runs now wait for them.

Found while building the console:

27. **A sign-on failure was blamed on the task's own step.** Sign-on is itself a replayed capability whose steps are numbered s1, s2 ... like any other,
    so the failing sign-on step s4 was matched to the requested task's s4 ("Click Select" shown as failed) and not-reached steps showed the sign-on's
    screenshots. Recorded actions now carry the capability they belong to and the timeline shows sign-on as its own row.
28. **The test suite could run recovery against the developer's real run database.** The in-process API server is session-scoped and starts before any
    per-test environment is set, so its startup recovery (which closes runs a dead process left running) read `data/runs.db`. A test run beside a live
    server could have marked its in-flight runs interrupted. The suite now points at a temporary database before anything is imported.

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
| Platform behavior | 312 offline tests: schema, replay engine, safety, sessions, operator console, API, dashboard, web surface, artifact versioning/lint/diff, replay hardening, run store and policy, commit recording, browser pool, clinic capabilities (incl. partial updates), repair and canary, the v1 API, edge cases, observability, MCP |
| Clinic target behavior | 62 tests: business rules, audit and exactly-once semantics, JSON API, both skins, test kit, and 16 real-browser tests |
| A retried commit posts exactly once | `tests/test_clinic_capabilities.py`: same-key retry, response lost after commit, request lost before commit; all asserted on the clinic audit log with its own duplicate guard off |
| The console works for each role, in a real browser | `tests/ui/test_console.py`: sign-in and revoked keys, catalog filters, form validation, a live run with screenshots, a partial update, a refund held for approval then approved by a supervisor with a reason (the requester and a viewer cannot), unknown-outcome settling, drift to repair to rollback, chat history and drafts, key admin, shortcuts and theme, empty and error states |
| Endpoints behind the console | `tests/test_ui_backend.py`: timeline, screenshot access and path safety, inbox, filters, policy, key admin, chat against a scripted model |
| Metrics, structured logs and traces behave as documented | `tests/test_observability.py`: definitions checked against hand-built runs, run id present on every line including browser threads, no values or keys in logs, trace kept only for failures on sandbox targets |
| An assistant can use capabilities over MCP but cannot approve | `tests/test_mcp_server.py`: a real MCP client over memory and over real stdio, against the real API, browser and clinic; refund waits for a human approval, retry with the same key posts once, lost response says do not retry |
| An irreversible step is not retried on a guess | `tests/test_replay_hardening.py` (needs_review, RETRY rule ignored, session expiry at commit) |
| Every clinic capability replays | `tests/test_clinic_capabilities.py` replays the 8 checked-in LLM-discovered artifacts with different inputs than discovery |
| Drift yields a repair proposal; approval makes a new version; rollback works | `tests/test_repair_and_canary.py`, `scripts/drift_report.py` |
| Commit steps can be recorded from a run | `tests/test_commit_recording.py` (scripted model, real browser, real clinic) and the 4 checked-in artifacts whose provenance records an `auto_sandbox` approval |
| LLM discovery works end to end | `tests/test_discovery_live.py` (skips without `GEMINI_API_KEY`) |
| Measured success and recovery rates | **Not yet measured.** A benchmark harness is planned. |

## Limitations

These are verified against the code as of this writing.

- **Identity is authenticated on `/v1` only.** There, the API key supplies the name and role. The CLI still trusts the name you type
  (checked against the roster in `safety/policy.yaml`), and the older `/capabilities/{id}/invoke`, the chatbot and the dashboard have no
  authentication at all and take `requested_by` from the caller. They are replaced by the unified UI (phase 4), which will use `/v1`. Keys
  are created from the CLI; there is no key rotation or expiry.
- **The supervised commit gate has not been used by a person at a keyboard.** It is a terminal prompt;
  it was run once live against Gemini with the answers piped in (the approver landed in the artifact's
  provenance) and is otherwise tested with an injected answer function. The checked-in clinic artifacts were recorded with
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
- **Metrics, logs and traces are local.** Metrics reset with the database and nothing alerts on them; logs go to stderr and are not shipped
  anywhere; a trace holds whatever was typed and exists only for failed runs on sandbox targets. There is no retention policy for evidence or
  traces.
- **MCP is minimal.** Stdio transport only; the server uses one API key for its whole life; the tool list is fetched when asked, with no
  notification when a capability is added or an approval completes (the assistant has to poll `get_run_status`); a long run returns its run id
  after `CUA_WAIT_SECONDS` rather than streaming progress. The tool descriptions and annotations are advice to an assistant, not enforcement:
  enforcement is the platform behind it.
- **Redaction is partial.** Evidence (`log.jsonl`, `result.json`) and the stored parameters in the
  evidence trail are scrubbed by pattern, field name and known secrets. Screenshots are not, and the run
  store keeps parameters and results unredacted because replaying an approved run and answering a
  duplicate request need them. Protect `data/runs.db` like the target's own data.
- **The browser pool does not cover everything.** Headed and slow-mo runs use a dedicated thread, a job
  that blocks forever holds its worker, and a paused escalation holds one until a human resumes.
  Tests print Playwright teardown noise at interpreter exit.
- **The console is the least finished part.** The live view is a sequence of screenshots, not video, and exists for sandbox targets only; watching a real window works
  only when the server runs on the machine you are using; there is no take-over of a run paused mid-way (v1 runs do not enable
  the operator console, so a stuck run fails instead of pausing); an approver sees the request's inputs but not a preview of what the target will show
  (a rehearsal run could supply that); policy is read-only in the UI; the API key sits in the browser's local storage, so it is as safe as the origin
  is from script injection; and nothing has been checked with a screen reader beyond semantic markup, labels, focus handling and native dialogs.
  Screenshots shown in it cannot be redacted, which is why they are kept for sandbox targets only.
- **The older API and the chat are still in-process and synchronous.** Chat history is one global list in memory (every visitor shares
  it and a restart clears it), the page reloads itself while a run is pending, which discards a half-typed message, and the chatbot runs
  every clinic task as the front-desk account. The operator console starts at import time on a fixed port. v1 has no rate limiting.
- **The chatbot is minimal.** Gemini only, no conversation memory, one capability per message, a keyword guard for the credit-union
  capabilities plus the new schema and placeholder check, and a server-rendered page that polls. It cannot approve a run it submits.
- **Clinic coverage.** Capabilities target the legacy skin only; the modern skin, the CSV export and the
  approvals queue have no capability. Browser tests cover search, contact update, reschedule, cancel,
  CSV download, session expiry, drift, duplicate submit and maintenance; the claim, refund, write-off and
  approval flows are covered at the API and server-rendered level by the clinic's own tests and through
  replay by the platform's. Chaos applies to `/legacy` and `/api` only. Drift covers all four levels on
  the legacy skin but only labels on the modern skin. State is in memory unless `CLINIC_DB_PATH` is set.
  The Render blueprint (`render.yaml`) has not been deployed.

## What is next

A benchmark that reports success rate, recovery rate and discovery cost against zero-token replay (phase 6, with CI and a demo);
running the supervised gate and the LLM repair ranker live; the console's missing pieces (an approver's preview, paused-run take-over, a live
browser view); deploying the clinic. None of these are claimed until they are built and measured.
