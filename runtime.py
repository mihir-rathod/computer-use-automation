"""The one execution path for running a saved capability -- CLI `replay`, the capability API
(api/app.py), and the chatbot all call `run_replay()` here, never reimplement it. This is what
makes "don't let the wrapper become a way around the guardrails" (ASSIGNMENT_ORIGINAL.md 3.5)
true structurally rather than by convention: every front door launches the same browser, builds
the same SafetyPolicy, and runs the same ReplayEngine, so none of them can accidentally skip
safety, evidence, or escalation. Extracted from cli.py once a second caller (the API) needed the
exact same logic -- before that there was nothing to share yet.
"""
from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from playwright.sync_api import sync_playwright

from artifacts_lib.storage import load_artifact_by_id
from escalation.operator_console import app as operator_app
from escalation.registry import register_session, unregister_session
from escalation.session_manager import SessionManager
from evidence_lib.logger import EvidenceLogger
from replay.engine import ReplayEngine
from replay.result import ReplayResult, ReplayStatus
from safety.allowlist import DEFAULT_ALLOWLIST_PATH, AllowlistConfig, AllowlistPolicy
from safety.policy import SafetyPolicy
from surface.web import WebSurface

REPO_ROOT = Path(__file__).resolve().parent
EVIDENCE_ROOT = REPO_ROOT / "evidence"
_operator_console_started = False

# Playwright's own default (30s) is too long for a demo -- a wrong/stale locator value (e.g. a
# share id that no longer exists) hangs the whole run for the full 30s before failing, which
# looks indistinguishable from "stuck forever." Found live, the night before the demo. Shorter
# means a bad value fails fast and visibly instead of sitting there; still generous for a real
# page load or a genuinely slow action against the live site.
DEFAULT_ACTION_TIMEOUT_MS = 8000

# Adapting to a new target is choosing a profile here, not writing code -- each entry pairs a
# base URL with the allowlist config and login capability that go with it. `--target`/`target`
# selects one; any of base_url/username/password/allowlist still overrides its piece explicitly.
TARGET_PROFILES: dict[str, dict[str, Any]] = {
    "mockbank": {
        "base_url": os.environ.get("MOCKBANK_BASE_URL", "http://localhost:8000"),
        "username": "operator",
        "password": "bankdemo123",
        "allowlist": DEFAULT_ALLOWLIST_PATH,
        "login_capability": "mockbank.login",
        "login_path": "/login",
    },
    "meridian": {
        "base_url": "https://web-sample.interface-hiring.com",
        "username": "teller1",
        "password": "password",
        "allowlist": REPO_ROOT / "safety" / "allowlist_meridian.json",
        "login_capability": "meridian.signon",
        "login_path": "/signon",
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


def build_safety_policy(base_url: str, allowlist_path: Path = DEFAULT_ALLOWLIST_PATH) -> SafetyPolicy:
    config = AllowlistConfig.from_json(allowlist_path)
    # allowed_route_patterns/action_types come from the checked-in policy; allowed_base_urls is
    # overridden to whatever base_url actually is, so the policy always matches where this run is
    # really pointed rather than silently drifting from the JSON file's documented default.
    config = config.model_copy(update={"allowed_base_urls": [base_url]})
    return SafetyPolicy(AllowlistPolicy(config))


def run_login(surface: WebSurface, username: str, password: str, login_capability: str = "mockbank.login") -> None:
    login_artifact = load_artifact_by_id(login_capability)
    result = ReplayEngine(surface).run(login_artifact, {"username": username, "password": password})
    if result.status != ReplayStatus.SUCCESS:
        raise RuntimeError(f"login failed: status={result.status.value} error={result.error}")


def run_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def run_replay(
    capability_id: str,
    params: dict[str, Any],
    *,
    target: str = "mockbank",
    base_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    allowlist: str | None = None,
    headed: bool = False,
    slow_mo: int = 0,
    evidence_dir: Path | None = None,
    operator_port: int = 8010,
    enable_operator_console: bool = True,
) -> tuple[ReplayResult, Path]:
    """Launch a browser, log in, deterministically replay one capability, write evidence.
    The single function every front door (CLI `replay`, the capability API, the chatbot) calls
    -- see module docstring for why that matters.

    `slow_mo` (milliseconds of pause Playwright inserts before each action) is separate from
    `headed` on purpose, matching Playwright's own convention: a fast, real replay against
    MERIDIAN completes in a couple of seconds even headed, which is correct for production but
    too fast for a human to actually watch happen. For a live demo, pass both --headed and a
    --slow-mo (e.g. 500-800ms) together.
    """
    profile = resolve_target(target, base_url, username, password, allowlist)
    artifact = load_artifact_by_id(capability_id)
    evidence_dir = evidence_dir or (EVIDENCE_ROOT / run_id("replay_run"))
    logger = EvidenceLogger(evidence_dir)
    # Same reasoning as cmd_discover's own run_start log (cli.py): the dashboard needs to know
    # what a run is even if it crashes or hangs before ever reaching the "replay"/"result" event.
    logger.log("system", "run_start", kind="replay", capability_id=capability_id, target=target, params=params)
    safety_policy = build_safety_policy(profile["base_url"], profile["allowlist"])

    if enable_operator_console:
        ensure_operator_console(operator_port)

    # Run on a dedicated, never-reused thread -- not `with sync_playwright() as p:` (its own
    # __exit__ stops the driver unconditionally, killing the browser even if .close() is
    # skipped -- verified empirically) and not directly on the caller's thread either. The
    # capability API serves requests from a shared thread pool (FastAPI's run_in_threadpool);
    # skipping p.stop() there for a headed run leaves that *pooled* thread's Playwright-internal
    # event-loop state dangling, which broke the *next unrelated request* that happened to reuse
    # the same thread ("Playwright Sync API inside the asyncio loop" -- reproduced live, not
    # theoretical). Isolating each run to its own throwaway thread means a leaked headed session
    # only ever poisons a thread nobody else will touch again.
    #
    # When headed, a human is watching specifically to review the final state (the whole reason
    # --slow-mo exists: a normal replay is too fast to watch at all) -- closing the instant the
    # run finishes would undercut that, so this thread deliberately never calls browser.close()/
    # p.stop() and simply keeps running (Playwright's own background dispatcher thread, which it
    # spawns internally, keeps the driver connection alive independently of this function
    # returning). Only the non-headed path -- the common case, and the only one with no one
    # watching -- tears down automatically.
    result_box: dict[str, ReplayResult | BaseException] = {}

    def _worker() -> None:
        session = None
        p = sync_playwright().start()
        browser = None
        try:
            browser = p.chromium.launch(headless=not headed, slow_mo=slow_mo)
            page = browser.new_page()
            page.set_default_timeout(DEFAULT_ACTION_TIMEOUT_MS)
            page.goto(f"{profile['base_url']}{profile['login_path']}")

            surface = WebSurface(page, base_url=profile["base_url"], screenshot_dir=evidence_dir / "screenshots", evidence_logger=logger, safety_policy=safety_policy)
            run_login(surface, profile["username"], profile["password"], login_capability=profile["login_capability"])

            if enable_operator_console:
                session = SessionManager(evidence_dir.name, surface, evidence_dir, evidence_logger=logger, capability_id=capability_id, goal=None)
                register_session(session)

            engine = ReplayEngine(
                surface, evidence_logger=logger,
                reauth_credentials={"username": profile["username"], "password": profile["password"]},
                session_manager=session,
            )
            result_box["result"] = engine.run(artifact, params)
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the calling thread below
            result_box["result"] = exc
        finally:
            if session is not None:
                unregister_session(session.session_id)
            if not headed:
                if browser is not None:
                    browser.close()
                p.stop()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join()
    outcome = result_box["result"]
    if isinstance(outcome, BaseException):
        raise outcome
    result = outcome

    (evidence_dir / "result.json").write_text(result.model_dump_json(indent=2))
    return result, evidence_dir
