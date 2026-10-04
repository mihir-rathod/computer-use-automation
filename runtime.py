"""The one execution path for running a saved capability -- CLI `replay`, the capability API
(api/app.py), and the chatbot all call `run_replay()` here, never reimplement it. This is what
makes "no front door can become a way around the guardrails" true structurally rather than by
convention: every front door launches the same browser, builds
the same SafetyPolicy, and runs the same ReplayEngine, so none of them can accidentally skip
safety, evidence, or escalation. Extracted from cli.py once a second caller (the API) needed the
exact same logic -- before that there was nothing to share yet.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
import os
import secrets
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from playwright.sync_api import sync_playwright

from artifacts_lib.storage import DEFAULT_ARTIFACTS_DIR, load_artifact_by_id
from artifacts_lib.schema import Artifact
from escalation.operator_console import app as operator_app
from escalation.registry import register_session, unregister_session
from escalation.session_manager import SessionManager
from evidence_lib.logger import EvidenceLogger
from evidence_lib.redaction import Redactor
from repair.propose import propose_repair
from replay.engine import ReplayEngine
from replay.validation import validate_input
from replay.result import ReplayError, ReplayResult, ReplayStatus
from runs.store import APPROVED, PENDING_APPROVAL, QUEUED, RUNNING, RunStore
from safety.allowlist import DEFAULT_ALLOWLIST_PATH, AllowlistConfig, AllowlistPolicy
from safety.config import DEFAULT_POLICY_PATH, PolicyConfig, PolicyViolation, check_params, irreversible_step_ids, required_approval
from safety.policy import SafetyPolicy
from safety.risk import RiskClassifier
from surface.pool import get_pool
from surface.web import WebSurface

REPO_ROOT = Path(__file__).resolve().parent
EVIDENCE_ROOT = REPO_ROOT / "evidence"
DEFAULT_RUN_DB = REPO_ROOT / "data" / "runs.db"
_stores: dict[str, RunStore] = {}
_operator_console_started = False

# Playwright's own default (30s) is too long for a demo -- a wrong/stale locator value (e.g. a
# share id that no longer exists) hangs the whole run for the full 30s before failing, which
# looks indistinguishable from "stuck forever." Shorter means a bad value fails fast and visibly
# instead of sitting there; still generous for a real page load or a genuinely slow action.
DEFAULT_ACTION_TIMEOUT_MS = 8000
# Whole-run budget; the engine checks it before every action, so it bounds a run without cutting an
# action off mid-flight (that is DEFAULT_ACTION_TIMEOUT_MS's job).
DEFAULT_RUN_TIMEOUT_S = 120.0

# Adapting to a new target is choosing a profile here, not writing code -- each entry pairs a
# base URL with the allowlist config and login capability that go with it. `--target`/`target`
# selects one; any of base_url/username/password/allowlist still overrides its piece explicitly.
ALLOWLIST_DIR = DEFAULT_ALLOWLIST_PATH.parent
TARGET_PROFILES: dict[str, dict[str, Any]] = {
    "mockbank": {
        "base_url": os.environ.get("MOCKBANK_BASE_URL", "http://localhost:8000"),
        "username": "operator",
        "password": "bankdemo123",
        "allowlist": DEFAULT_ALLOWLIST_PATH,
        "login_capability": "mockbank.login",
        "login_path": "/login",
        "sandbox": True,
        "app_id": "mockbank",
    },
    # Larkspur Clinic Ops (the legacy skin). Sandbox: a seeded demo clinic whose data resets on demand.
    "clinic": {
        "base_url": os.environ.get("CLINIC_BASE_URL", "http://localhost:8100"),
        "username": "frontdesk",
        "password": "desk-demo-123",
        "allowlist": ALLOWLIST_DIR / "allowlist_clinic.json",
        "login_capability": "clinic.login",
        "login_path": "/legacy/login",
        "sandbox": True,
        "app_id": "clinic",
    },
    # Same app, signed on as the billing supervisor: write-offs and the approval queue.
    "clinic_supervisor": {
        "base_url": os.environ.get("CLINIC_BASE_URL", "http://localhost:8100"),
        "username": "supervisor",
        "password": "super-demo-123",
        "allowlist": ALLOWLIST_DIR / "allowlist_clinic.json",
        "login_capability": "clinic.login",
        "login_path": "/legacy/login",
        "sandbox": True,
        "app_id": "clinic",
    },
}


def resolve_target(
    target: str,
    base_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    allowlist: str | None = None,
) -> dict[str, Any]:
    """Merges the selected TARGET_PROFILES entry with any explicit overrides -- adapting to a
    new target is picking a profile, not writing code; an override still wins per-field when
    given (e.g. base_url against a locally proxied copy of the same target)."""
    profile = dict(TARGET_PROFILES[target])
    if base_url is not None:
        profile["base_url"] = base_url
    if username is not None:
        profile["username"] = username
    if password is not None:
        profile["password"] = password
    if allowlist is not None:
        profile["allowlist"] = Path(allowlist)
    return profile


def ensure_operator_console(port: int) -> None:
    """Starts the operator console once per process, in a background thread. Only the
    automation thread (the one running discover/replay) ever touches the live Playwright page --
    this thread only serves HTML and enqueues human-submitted intents via
    SessionManager.request_action (see escalation/session_manager.py's module docstring)."""
    global _operator_console_started
    if _operator_console_started:
        return
    config = uvicorn.Config(operator_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    _operator_console_started = True


def build_safety_policy(base_url: str, allowlist_path: Path = DEFAULT_ALLOWLIST_PATH, policy: PolicyConfig | None = None) -> SafetyPolicy:
    config = AllowlistConfig.from_json(allowlist_path)
    # allowed_route_patterns/action_types come from the checked-in policy; allowed_base_urls is
    # overridden to whatever base_url actually is, so the policy always matches where this run is
    # really pointed rather than silently drifting from the JSON file's documented default.
    config = config.model_copy(update={"allowed_base_urls": [base_url]})
    keywords = policy.risk_keywords if policy is not None else None
    classifier = RiskClassifier(keywords.commit, keywords.domain) if keywords else None
    return SafetyPolicy(AllowlistPolicy(config), risk_classifier=classifier)


def try_login(surface: WebSurface, username: str, password: str, login_capability: str, artifacts_dir: Path | None = None):
    """Signs on by replaying the target's login capability. Returns (login_artifact, result) instead of raising, so a
    login that broke (UI drift, wrong password) can be reported and repaired like any other capability."""
    login_artifact = load_artifact_by_id(login_capability, **_dir_kw(artifacts_dir))
    return login_artifact, ReplayEngine(surface).run(login_artifact, {"username": username, "password": password})


def run_login(surface: WebSurface, username: str, password: str, login_capability: str = "mockbank.login", artifacts_dir: Path | None = None) -> None:
    login_artifact = load_artifact_by_id(login_capability, **({"directory": artifacts_dir} if artifacts_dir else {}))
    result = ReplayEngine(surface).run(login_artifact, {"username": username, "password": password})
    if result.status != ReplayStatus.SUCCESS:
        raise RuntimeError(f"login failed: status={result.status.value} error={result.error}")


def run_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def _dir_kw(artifacts_dir: Path | None) -> dict[str, Any]:
    return {"directory": artifacts_dir} if artifacts_dir else {}


def default_store() -> RunStore:
    """One RunStore per database path per process; RUN_DB_PATH overrides the location (tests point it at a temp file)."""
    path = os.environ.get("RUN_DB_PATH", str(DEFAULT_RUN_DB))
    if path not in _stores:
        _stores[path] = RunStore(path)
    return _stores[path]


def default_policy() -> PolicyConfig:
    return PolicyConfig.load(Path(os.environ.get("SAFETY_POLICY_PATH", DEFAULT_POLICY_PATH)))


def _early_result(artifact: Artifact, status: ReplayStatus, run_id: str | None, **kw: Any) -> ReplayResult:
    now = datetime.now(UTC)
    return ReplayResult(status=status, capability_id=artifact.capability_id, run_id=run_id, started_at=now, finished_at=now, **kw)


@dataclass
class Early:
    """The run's outcome was settled without a browser (refused, deduplicated, waiting for an approval)."""
    result: ReplayResult
    evidence_dir: Path


@dataclass
class Prepared:
    """A run that has been validated, authorised and recorded, and is ready to execute."""
    run_id: str
    evidence_dir: Path
    execute: Callable[..., ReplayResult]


def run_replay(capability_id: str, params: dict[str, Any], **kwargs: Any) -> tuple[ReplayResult, Path]:
    """The one function every synchronous front door (CLI `replay`, the legacy capability API, the chatbot) calls: policy caps,
    approval, idempotency and the run record all happen in `prepare_run`, so no front door can skip them. The async API calls
    `prepare_run` itself and executes on a worker; both take exactly this path."""
    prepared = prepare_run(capability_id, params, **kwargs)
    if isinstance(prepared, Early):
        return prepared.result, prepared.evidence_dir
    return prepared.execute(), prepared.evidence_dir


def prepare_run(
    capability_id: str,
    params: dict[str, Any],
    *,
    target: str | None = "mockbank",
    base_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    allowlist: str | None = None,
    headed: bool = False,
    slow_mo: int = 0,
    evidence_dir: Path | None = None,
    operator_port: int = 8010,
    enable_operator_console: bool = True,
    dry_run: bool = False,
    deadline_s: float | None = DEFAULT_RUN_TIMEOUT_S,
    cancel_event: threading.Event | None = None,
    idempotency_key: str | None = None,
    requested_by: str = "anonymous",
    resume_run_id: str | None = None,
    store: RunStore | None = None,
    policy: PolicyConfig | None = None,
    repair_llm: bool = False,
    artifacts_dir: Path | None = None,
    queued: bool = False,
    version: str | None = None,
) -> Early | Prepared:
    """Everything that happens before a browser is needed: input validation, policy caps, the idempotency claim, the approval
    request and the run record. Returns `Early` when the answer is already known (refused, deduplicated, waiting for approval)
    or `Prepared`, whose `execute()` runs it. `queued=True` records the run as queued so an async caller can return its id now.

    Possible early shapes:
      * PENDING_APPROVAL -- policy requires a recorded approval; approve the run, then call again with
        `resume_run_id` to execute it.
      * `deduplicated=True` -- `idempotency_key` matched an earlier run that completed; its stored result
        is returned and no browser is launched.
      * HARD_FAILURE with code policy_cap_exceeded / idempotency_conflict -- refused before any browser.
    """
    store = store or default_store()
    policy = policy or default_policy()

    if resume_run_id:
        run = store.get(resume_run_id)
        if run is None:
            raise FileNotFoundError(f"unknown run '{resume_run_id}'")
        if run["status"] != APPROVED:
            raise ValueError(f"run {resume_run_id} is {run['status']}, not approved")
        if capability_id and capability_id != run["capability_id"]:
            raise ValueError(f"run {resume_run_id} is for {run['capability_id']}, not {capability_id}")
        # An approval is for a specific identity: resuming as a different target account would run it as someone else.
        if run["target"] and target not in (None, run["target"]):
            raise ValueError(f"run {resume_run_id} was requested for target '{run['target']}', not '{target}'")
        target = run["target"] or target or "mockbank"
        capability_id, params = run["capability_id"], json.loads(run["params_json"])
        artifact = load_artifact_by_id(capability_id, **_dir_kw(artifacts_dir), version=run["version"])
        evidence_dir = Path(run["evidence_dir"])
        run_id_, tier = run["id"], (store.approvals_for(run["id"]) or [{}])[-1].get("tier")
        if not store.claim_approved(run_id_, QUEUED if queued else RUNNING):
            raise ValueError(f"run {resume_run_id} is already being executed or was executed by someone else")
    else:
        artifact = load_artifact_by_id(capability_id, version=version, **_dir_kw(artifacts_dir))
        evidence_dir = evidence_dir or (EVIDENCE_ROOT / run_id("replay_run"))
        # A request that cannot possibly run must not reach an approver's queue or burn an idempotency key.
        problems = validate_input(artifact.input_schema, params)
        if problems:
            return Early(_early_result(artifact, ReplayStatus.HARD_FAILURE, None, error=ReplayError(message="; ".join(problems), code="input_invalid")), evidence_dir)
        profile_app = TARGET_PROFILES.get(target or "mockbank", {}).get("app_id")
        if profile_app and artifact.target.app_id != profile_app:
            early = _early_result(artifact, ReplayStatus.HARD_FAILURE, None, error=ReplayError(
                code="target_mismatch", message=f"{capability_id} is a {artifact.target.app_id} capability but target '{target}' is a {profile_app} app"))
            return Early(early, evidence_dir)
        tier = None if dry_run else required_approval(policy, artifact)

        violations = [] if dry_run else check_params(policy, capability_id, params)
        cap = policy.for_capability(capability_id).caps.max_commits_per_day
        if cap is not None and not dry_run and tier is not None and store.committed_today(capability_id) >= cap:
            violations.append(PolicyViolation("policy_cap_exceeded", f"{capability_id} already committed {cap} time(s) today, the policy limit"))
        if violations:
            claim = store.begin(capability_id, artifact.version, params, requested_by, None, evidence_dir=str(evidence_dir), target=target)
            result = _early_result(artifact, ReplayStatus.HARD_FAILURE, claim.run["id"],
                                   error=ReplayError(message="; ".join(v.message for v in violations), code=violations[0].code))
            store.finish(claim.run["id"], result, str(evidence_dir))
            _write_evidence(evidence_dir, artifact, params, result, policy, target, dry_run, note="refused by policy before any browser started")
            return Early(result, evidence_dir)

        needs_approval = tier in ("operator", "supervisor")
        claim = store.begin(capability_id, artifact.version, params, requested_by, None if dry_run else idempotency_key,
                            status=PENDING_APPROVAL if needs_approval else (QUEUED if queued else RUNNING), evidence_dir=str(evidence_dir), target=target)
        if claim.kind == "replay":
            stored = store.stored_result(claim.run)
            if stored is not None:
                stored.deduplicated, stored.run_id = True, claim.run["id"]
                if claim.run.get("resolution") == "committed":
                    stored.committed = True  # a person confirmed against the target's records that it did take effect
                return Early(stored, Path(claim.run["evidence_dir"] or evidence_dir))
        if claim.kind != "new":
            existing = claim.run or {}
            result = _early_result(artifact, ReplayStatus.HARD_FAILURE, existing.get("id"),
                                   error=ReplayError(message=claim.reason, code="idempotency_conflict"))
            return Early(result, Path(existing.get("evidence_dir") or evidence_dir))
        run_id_ = claim.run["id"]
        if needs_approval:
            store.request_approval(run_id_, tier, requested_by)
            result = _early_result(artifact, ReplayStatus.PENDING_APPROVAL, run_id_, approval_tier=tier)
            _write_evidence(evidence_dir, artifact, params, result, policy, target, dry_run, note=f"waiting for a {tier} approval")
            return Early(result, evidence_dir)

    def execute(cancel_event_: threading.Event | None = None) -> ReplayResult:
        store.mark_running(run_id_)
        try:
            result = _replay_in_browser(
                artifact, params, target=target, base_url=base_url, username=username, password=password, allowlist=allowlist,
                headed=headed, slow_mo=slow_mo, evidence_dir=evidence_dir, operator_port=operator_port,
                enable_operator_console=enable_operator_console, dry_run=dry_run, deadline_s=deadline_s, cancel_event=cancel_event_ or cancel_event,
                confirmed_steps=irreversible_step_ids(artifact) if tier in ("operator", "supervisor") else frozenset(),
                policy=policy, repair_hook=_repair_hook(store, run_id_, repair_llm), artifacts_dir=artifacts_dir,
            )
        except Exception as exc:  # noqa: BLE001
            # The browser or target could not be reached, or something broke around the engine. Left alone, the run record
            # stays "running" for ever and its idempotency key is blocked. Close it as a failure and report it as one.
            result = _early_result(artifact, ReplayStatus.HARD_FAILURE, run_id_, error=ReplayError(
                code="runner_error", message=f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else exc}"))
        result.run_id, result.approval_tier = run_id_, tier
        store.finish(run_id_, result, str(evidence_dir))
        evidence_dir.mkdir(parents=True, exist_ok=True)
        redactor = Redactor.from_config(policy.redaction)
        (evidence_dir / "result.json").write_text(json.dumps(redactor.scrub(json.loads(result.model_dump_json())), indent=2))
        return result



    return Prepared(run_id_, evidence_dir, execute)


def _repair_hook(store: RunStore, run_id_: str, use_llm: bool):
    def hook(artifact: Artifact, step_id: str | None, surface: WebSurface) -> str | None:
        if step_id is None:
            return None
        picker = None
        if use_llm:
            from agent.gemini_client import GeminiClient
            from repair.llm import make_llm_picker

            picker = make_llm_picker(GeminiClient())
        proposal = propose_repair(artifact, step_id, surface, new_repair_id(), run_id_, llm_pick=picker)
        if proposal is None:
            return None
        store.save_repair(proposal)
        return proposal.id

    return hook


def new_repair_id() -> str:
    return f"rep_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(3)}"


def _write_evidence(evidence_dir: Path, artifact: Artifact, params: dict[str, Any], result: ReplayResult, policy: PolicyConfig,
                    target: str, dry_run: bool, note: str) -> None:
    redactor = Redactor.from_config(policy.redaction)
    with EvidenceLogger(evidence_dir, redactor=redactor) as logger:
        logger.log("system", "run_start", kind="replay", capability_id=artifact.capability_id, target=target, params=params, dry_run=dry_run)
        logger.log("system", "run_not_started", status=result.status.value, note=note,
                   error=result.error.model_dump() if result.error else None, run_id=result.run_id)
    (evidence_dir / "result.json").write_text(json.dumps(redactor.scrub(json.loads(result.model_dump_json())), indent=2))


def _replay_in_browser(
    artifact: Artifact, params: dict[str, Any], *, target: str, base_url: str | None, username: str | None,
    password: str | None, allowlist: str | None, headed: bool, slow_mo: int, evidence_dir: Path, operator_port: int,
    enable_operator_console: bool, dry_run: bool, deadline_s: float | None, cancel_event: threading.Event | None,
    confirmed_steps: frozenset[str], policy: PolicyConfig,
    repair_hook: Any = None, artifacts_dir: Path | None = None,
) -> ReplayResult:
    """Launch a browser, log in, deterministically replay one capability, write evidence.

    `slow_mo` (milliseconds of pause Playwright inserts before each action) is separate from
    `headed` on purpose, matching Playwright's own convention: a fast, real replay completes in
    a couple of seconds even headed, which is correct for production but too fast for a human to
    actually watch happen. To watch one, pass both --headed and a --slow-mo (e.g. 500-800ms).
    """
    capability_id = artifact.capability_id
    profile = resolve_target(target, base_url, username, password, allowlist)
    logger = EvidenceLogger(evidence_dir, redactor=Redactor.from_config(policy.redaction).with_secrets_from({**params, "password": profile["password"]}))
    # Same reasoning as cmd_discover's own run_start log (cli.py): the dashboard needs to know
    # what a run is even if it crashes or hangs before ever reaching the "replay"/"result" event.
    logger.log("system", "run_start", kind="replay", capability_id=capability_id, target=target, params=params, dry_run=dry_run)
    safety_policy = build_safety_policy(profile["base_url"], profile["allowlist"], policy)

    if enable_operator_console:
        ensure_operator_console(operator_port)

    def _job(page: Any) -> ReplayResult:
        session = None
        try:
            page.set_default_timeout(DEFAULT_ACTION_TIMEOUT_MS)
            page.goto(f"{profile['base_url']}{profile['login_path']}")

            surface = WebSurface(page, base_url=profile["base_url"], screenshot_dir=evidence_dir / "screenshots", evidence_logger=logger, safety_policy=safety_policy)
            login_artifact, login = try_login(surface, profile["username"], profile["password"], profile["login_capability"], artifacts_dir)
            if login.status != ReplayStatus.SUCCESS:
                err = login.error or ReplayError(message=login.business_outcome or login.status.value, code=login.business_outcome)
                failed = _early_result(artifact, ReplayStatus.HARD_FAILURE, None, error=ReplayError(
                    step_id=err.step_id, code="login_failed",
                    message=f"sign-on with {login_artifact.capability_id} failed at {err.step_id or 'its checkpoint'} ({err.code or login.status.value}): {err.message}"))
                if repair_hook is not None and err.code == "locator_unresolved":
                    failed.repair_proposal_id = repair_hook(login_artifact, err.step_id, surface)
                return failed

            if enable_operator_console:
                session = SessionManager(evidence_dir.name, surface, evidence_dir, evidence_logger=logger, capability_id=capability_id, goal=None)
                register_session(session)

            engine = ReplayEngine(
                surface, evidence_logger=logger,
                reauth_credentials={"username": profile["username"], "password": profile["password"]}, artifacts_dir=artifacts_dir or DEFAULT_ARTIFACTS_DIR,
                session_manager=session,
                deadline_s=deadline_s, cancel_event=cancel_event, dry_run=dry_run, confirmed_steps=confirmed_steps,
            )
            result = engine.run(artifact, params)
            if repair_hook is not None and result.status == ReplayStatus.HARD_FAILURE and result.error and result.error.code == "locator_unresolved":
                # Looks at the page the step failed on, while it is still open. A proposal only; nothing is applied.
                result.repair_proposal_id = repair_hook(artifact, result.error.step_id, surface)
            return result
        finally:
            if session is not None:
                unregister_session(session.session_id)

    # Headless, normal-speed runs go through the browser pool: a fixed set of worker threads that each own
    # their Chromium for life and give every run a fresh, isolated context (surface/pool.py). Headed and
    # slow-mo runs are launch options and are watched by a person, so they keep a dedicated thread.
    if not headed and not slow_mo:
        return get_pool().run(_job)
    return _run_on_dedicated_thread(_job, headed=headed, slow_mo=slow_mo)


# How long a finished headed run's browser may stay open waiting for a person to close it, as a backstop.
HEADED_LINGER_MAX_S = 30 * 60


def _wait_until_closed_by_user(browser: Any, max_s: float) -> None:
    """Keeps the Playwright connection (and its event loop) alive until the person closes every tab or window of the
    browser, or the backstop passes. On macOS, Chrome launched by Playwright ignores Cmd+Q and closing the last window
    for as long as the driver is attached, so the driver has to be stopped *by us* once the windows are gone."""
    deadline = time.monotonic() + max_s
    while time.monotonic() < deadline:
        try:
            if not browser.is_connected():
                return
            pages = [pg for ctx in browser.contexts for pg in ctx.pages if not pg.is_closed()]
            if not pages:
                return
            pages[0].wait_for_timeout(500)  # pumps Playwright's event loop, so close events are seen
        except Exception:
            return  # the page or browser went away mid-check: that is the person closing it


def _run_on_dedicated_thread(job: Any, *, headed: bool, slow_mo: int) -> ReplayResult:
    # Not `with sync_playwright() as p:` -- its __exit__ stops the driver unconditionally -- and not on the caller's
    # thread either: Playwright's sync API is bound to the thread that started it, and leaving one running on a shared
    # API thread broke the *next unrelated request* ("Playwright Sync API inside the asyncio loop").
    #
    # A headed run's result is handed back as soon as it is ready, but its thread keeps the browser open so a person can
    # review the final state. When they close the window (or Cmd+Q), the thread notices and stops the driver, which is
    # what actually ends the Chrome process. Without that, the browser lingered until the whole server stopped.
    result_box: dict[str, ReplayResult | BaseException] = {}
    ready = threading.Event()

    def _worker() -> None:
        p = sync_playwright().start()
        browser = None
        try:
            browser = p.chromium.launch(headless=not headed, slow_mo=slow_mo)
            result_box["result"] = job(browser.new_page())
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the calling thread below
            result_box["result"] = exc
        finally:
            ready.set()
            try:
                if headed and browser is not None:
                    _wait_until_closed_by_user(browser, HEADED_LINGER_MAX_S)
            finally:
                try:
                    if browser is not None:
                        browser.close()
                except Exception:
                    pass
                try:
                    p.stop()
                except Exception:
                    pass

    threading.Thread(target=_worker, daemon=True, name="headed-run" if headed else "dedicated-run").start()
    ready.wait()
    outcome = result_box["result"]
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome
