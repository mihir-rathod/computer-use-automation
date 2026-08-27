# Adaptation Write-up: MERIDIAN CORE

This covers adapting the take-home core (discover → typed capability artifact → deterministic
replay, with safety/evidence/escalation) to MERIDIAN CORE (`web-sample.interface-hiring.com`),
and wrapping it as a callable API, a chatbot, and a dashboard.

## What adapting actually took

Recording all seven MERIDIAN capabilities (sign-on, balance inquiry, funds transfer, open
share, update member, place hold, plus member inquiry folded into balance inquiry) needed **no
schema or replay-engine rewrite**. The per-transaction hidden token the brief calls out rides
along automatically through real browser form submission — a genuine live browser driving a
real page doesn't need to "know" a token exists, any more than a human teller does. That's the
adaptation-quality signal working as intended: MERIDIAN's review→post flow, and the
supervisor-gated Place Hold, fell out of the existing `Step`/`ReplayEngine` shape unchanged.

Six small, targeted changes to the core were needed. Each is here because something concrete
broke when this ran against MERIDIAN's real markup — not something anticipated in advance.

1. **Unlabeled login fields.**
   MERIDIAN's Operator ID and Password inputs have no accessible name and no `id` — the
   attributes the locator strategy relied on. *Fix:* `compute_target()` gained a fallback that
   locates by CSS `[name=...]` when those attributes are missing.

2. **Risk classifier flagged a safe click as irreversible.**
   MERIDIAN's routes bake the transaction type into the URL for the whole flow, not just the
   final step — e.g. `/members/{id}/transfer` is the URL on both the entry page and the safe
   review page. The classifier matched risk keywords against the URL as well as the description,
   so it would have blocked the harmless "Continue" click on the review page, before a human ever
   saw it. *Fix:* split the keyword list into two tiers — commit-verb keywords (`confirm`,
   `post`, `delete`) still match against the URL, but domain-noun keywords (`transfer`,
   `withdraw`, `hold`) now only match against the step's own description, never the URL.

3. **A real safety gap, caught by actually running it.**
   The "Open Share" commit button matched neither keyword tier, so it wasn't flagged as
   irreversible at all — a real $50 share opened live with zero confirmation prompt before I
   caught it. *Fix:* added "open share" to the domain-noun list. Flagging this here as a genuine
   incident that happened, not a hypothetical risk.

4. **Extraction broke on a different member's data.**
   The locators used to read values off the page baked in one specific record's structure, and
   MERIDIAN's tables have no `<th>` header cells to anchor against — so an extractor that worked
   during recording silently failed the moment it ran against a different member. *Fix:* switched
   extraction to XPath-anchored locators instead of role/name locators.

5. **No way to say "succeeded, but only after a human stepped in."**
   The original result schema only distinguished success / business outcome / hard failure — it
   had no way to record that a run succeeded *after* escalation, which the API and dashboard both
   need to report honestly. *Fix:* added `escalated` and `recovered` fields to `ReplayResult`, set
   once, at the engine's single exit point.

6. **Almost hand-wrote the login capability instead of discovering it.**
   Partway through, I started to hand-write `meridian.signon` the way a human might assume "just
   script the login form" — until re-checking the brief's own §3.1 made clear login itself has to
   go through the same discovery flow as everything else, since you can't discover how to use the
   other capabilities before you can log in to see them. *Fix:* a one-line special case so
   discovery runs for sign-on first, before any other capability.

## The capability API

**One process, three routers.** The capability API, the chatbot, and the dashboard are separate
FastAPI routers on the same app — deliberate, not default. Every invocation is one synchronous
replay against one external target; nothing here needs independent scaling or a queue.

**A real concurrency bug, found by running it, not by review.** Like any web server, the API can
serve multiple separate, unrelated requests in parallel — different callers hitting it at the
same time, not one request chaining into several capability calls. It does that by pulling
workers from a shared pool and returning them when each request is done. Browser automation has
one rule: whichever worker opens a browser session has to be the one that closes it — you
can't hand it off mid-session. A demo run deliberately leaves the browser window open so it can
actually be watched, skipping that cleanup on purpose — and if that worker went back into the
shared pool afterward, the next, completely unrelated request could get handed that same
half-cleaned-up worker and break for no reason of its own. *Fix:* every replay now gets its own
private, one-time-use worker instead of borrowing from the shared pool, so even when a demo run
leaves a mess behind, that worker just gets thrown away rather than handed to anyone else.

**Two endpoints.**
- `GET /capabilities` — the catalog, read straight from what's already on disk under
  `/artifacts/`: capability_id, typed input/output JSON Schema, safety metadata. No separate
  registry to keep in sync.
- `POST /capabilities/{id}/invoke` — takes typed `params` plus `target` (`mockbank` or
  `meridian`), returns a structured result: `status` (`success` / `business_outcome` /
  `hard_failure`), `outputs`, `business_outcome`, `error`, `escalated`, `recovered`,
  `evidence_dir`.

**Same execution path as the CLI.** Every invocation runs through `runtime.run_replay()` — the
exact function `cli.py replay` itself calls. The API can never become a second implementation of
"how do I run a capability" — structurally, not by convention.

**HTTP status means "was the call well-formed," not "did it succeed."** 404/422 are reserved for
problems with the request itself (unknown capability, unknown target, bad body). A business
outcome — or even a hard failure — is still a *successful* API call: 200, with the outcome in the
response body, matching the brief's own three-way result contract instead of collapsing it into
HTTP status codes.

## Locators on a legacy UI

Same stack as the take-home: Playwright, an accessibility-tree-driven `WebSurface`, no
coordinates, no model in the replay decision loop — the LLM only runs at discovery time, never
during replay.

MERIDIAN's HTML doesn't give the automation clean things to grab onto: some form fields have no
label at all, and its tables have no header cells to anchor a "read this column" locator against.
Concretely, that's the same fallback-locator and XPath-extraction fixes already listed under
"what adapting took" (items 1 and 4) — this is just the "why this category of bug kept showing
up" framing. Found the way most of these were found: by running discovery live, then watching
replay silently fail against a *different* member's data than the one it was recorded on — the
exact failure mode a UI with no stable identifiers produces.

## Handling the six injected fault conditions

The brief lets six fake error states be injected into MERIDIAN (`?inject=notfound`, etc.) to
prove the system handles failures gracefully, not just the happy path. Each one gets sorted into
one of the three outcome types the take-home's engine already understands:

| Injected condition | Sorted as | Recognized by |
|---|---|---|
| `notfound` | expected result, not an error (`not_found`) | "RECORD NOT FOUND" *or* the natural "No member records matched your search." |
| `validation` | expected result, not an error (`validation_error`) | "TRANSACTION REJECTED" *or* the natural "The transaction could not be validated:" |
| `permission` | expected result, not an error (`permission_denied`) | a teller attempting a supervisor-only action |
| `timeout` | recoverable — the system logs back in and continues on its own | session destroyed, redirected to sign-on |
| `maintenance` | recoverable — the system retries the same click | the interstitial's own "Continue" link goes to `/menu`, not back to the original page, so a same-page resume would land somewhere wrong |
| `server` | hard failure, no retry attempted | matches the real site itself: its error page offers no continue/retry link at all, so pretending one exists would be a fake affordance |

This taxonomy is backed by real automated tests (`tests/test_meridian_guarantees.py`), which
drive one business-outcome run and one recoverable-condition run through the live API against
the real site — not just asserted in this write-up. Two things worth knowing, found only by
actually testing this, not by reading the brief:

- **The fake and the real version of the same error don't say the same thing.** `notfound` and
  `validation` render different text depending on whether you triggered them via `?inject=` or
  a real search miss / real bad input. Both texts had to be recognized as the same outcome, or
  testing only the fake version would've missed real users hitting the same case.
- **The `maintenance` test can't actually prove a recovery happened.** MERIDIAN's
  `?inject=maintenance` switch is permanent once set on a URL — it never turns itself off, even
  after reloading. So the test can only prove the retry logic fires and gives up cleanly after
  its retry limit, not that it ever gets past the fault. Said plainly here rather than leaving the
  test's real limit implicit.

## How safety, evidence, and escalation survive the new path

**Safety.** A per-target allowlist (`safety/allowlist_meridian.json`) plus the risk-classifier
fix described above.

**Evidence.** Unchanged in shape — JSONL log, screenshots — plus one addition: a `run_start`
event, logged the moment a run's evidence directory exists, so a run that crashes or hangs
before any terminal event still shows up identified on the dashboard instead of blank.

**Escalation** is the guarantee I checked most carefully, since it's the one a new wrapper could
most easily quietly break. Verified live through *every* surface, not just asserted: a real
MERIDIAN transfer paused and was approved through the operator console via the CLI, the raw API,
and the chatbot — whose chat bubble now shows live "paused, needs a human" state instead of a
frozen page while it waits — plus an automated test exercising the same path with no human at
the keyboard. All four report a real confirmation number on completion, and the `escalated` flag
distinguishes "a human stepped in" from plain success at every layer.

**Redaction** got a deliberate, written decision rather than silent scope-widening: kept to
secrets only (passwords/tokens/credentials), documented directly in
`evidence_lib/redaction.py`'s own docstring. Everything MERIDIAN's evidence captures — balances,
confirmation numbers, member names — is synthetic seed data on an eval sandbox, and it's also
exactly what the evidence/dashboard system exists to show. Redacting it would satisfy the letter
of "handle regulated financial data" while gutting the system's actual debugging purpose. A real
deployment handling genuinely regulated data would encrypt/access-restrict the evidence store,
not blank out its own operational output.

## What was cut, and what's next

- **No automatic window management for headed demo runs.** A headed browser now stays open
  after its run finishes (previously closed instantly, defeating the point of `--slow-mo`), but
  auto-closing the *previous* window on a new one was attempted and reverted — it hit a real
  Playwright constraint (a browser connection can only be closed from the exact thread that
  created it, still alive). Not worth the added complexity this close to demo day; closing
  windows manually between runs is a small, acceptable step.
- **The chatbot maps one message to one capability call, on purpose.** A compound request
  ("check the balance, then transfer $5") silently only does the first part today. Sequential
  chaining was considered and deferred — the real complexity is what a chained request should do
  when the *first* step pauses for escalation, not the happy path.
- **Caching would help the chatbot for a real reason: Gemini quota, not latency.** Every chat
  message costs one Gemini call just to decide which capability to run, and this project hit a
  real free-tier quota limit mid-build -- one Gemini model alias capped at 20 requests/day, which
  is what actually forced the switch to the current `DEFAULT_MODEL`. The safe thing to cache is
  the *decision* ("which capability, which args"), keyed
  on the literal message text -- never the capability's *result*. Caching a balance or a transfer
  outcome would mean showing stale money data, which is the one thing a banking flow can't afford
  to get wrong; caching only the routing decision means every capability call still hits MERIDIAN
  for real, every time.
- **Automated MERIDIAN coverage is representative, not exhaustive** — one business-outcome run,
  one recoverable-condition run, one escalation, through the API, not every capability × every
  condition. Next: extend the same pattern to the remaining capabilities.
- **`pytest` runs against the live fixtures leave real `evidence/` directories behind**, cleaned
  up manually throughout this sprint. Next: a fixture that redirects `EVIDENCE_ROOT` to
  `tmp_path` for test-triggered runs.
- **The dashboard is read-only with no access control** — fine for a demo, not for production
  data even under the "keep it visible" redaction decision above; would need real auth first.
