"""Automated proof that the core guarantees hold on MERIDIAN specifically, not just MockBank --
ASSIGNMENT_ORIGINAL.md 3.5's "don't let the wrapper become a way around the guardrails" claim,
tested through the actual capability API (not just the CLI, which has been the only thing
exercising this so far), against the real live external target.

Skipped by default: these hit the real, shared, external site with genuine side effects (a real
transfer posts). Opt in with RUN_MERIDIAN_LIVE_TESTS=1 -- the default `pytest -q` run stays
fast, offline, and side-effect-free, matching ASSIGNMENT_ORIGINAL.md 3.4's own promise that the
suite needs no live services.
"""
from __future__ import annotations

import os
import threading
import time

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

import runtime
from api.app import app
from artifacts_lib.schema import ActionType
from artifacts_lib.storage import load_artifact_by_id
from escalation.registry import get_session
from escalation.session_manager import SessionMode
from replay.engine import ReplayEngine
from replay.result import ReplayStatus
from surface.base import Action
from surface.web import WebSurface

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_MERIDIAN_LIVE_TESTS"),
    reason="hits the real, live, external MERIDIAN site with genuine side effects -- opt in with RUN_MERIDIAN_LIVE_TESTS=1",
)

client = TestClient(app)
_PROFILE = runtime.TARGET_PROFILES["meridian"]


def test_business_outcome_via_the_real_capability_api():
    resp = client.post(
        "/capabilities/meridian.balance_inquiry/invoke",
        json={"params": {"member_id": "999999"}, "target": "meridian"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "business_outcome"
    assert body["business_outcome"] == "not_found"


def test_recoverable_condition_exercises_bounded_retry_against_a_real_persistent_fault():
    """MERIDIAN's own `?inject=maintenance` recon hook is a real, PERSISTENT server-side effect
    on that specific URL -- verified live before writing this test, not assumed: three separate
    navigations to the same injected URL all showed "SCHEDULED MAINTENANCE IN PROGRESS", it
    never clears on its own. So this isn't a scenario the artifact's own recoverable rule could
    ever resolve into a genuine success -- the hook exists for a human to *see* the signal text
    during recon, not to be embedded in an automated retry loop. What this proves instead, for
    real, against the live site: the recoverable path actually fires, actually retries (not just
    fails immediately), and correctly gives up with a clear hard_failure after max_attempts --
    not an infinite loop, not a silent false-success, not misclassified as some other outcome.

    Drives the engine directly against a patched copy of the artifact's own first step (the
    injection hook isn't exposed through the artifact's public input_schema -- a real caller
    should never be able to force a fault), the same level test_open_subaccount.py already tests
    validation_error at.

    Runs on its own dedicated thread, not the main pytest thread -- same reasoning as
    runtime.run_replay()'s own worker-thread isolation (see that module's docstring): with the
    full suite running together, an earlier test's use of FastAPI's TestClient leaves the main
    thread with a running asyncio loop reference, which Playwright's sync API refuses to share
    ("Sync API inside the asyncio loop" -- reproduced live running the full suite, not
    theoretical; passed in isolation, which is exactly the kind of thing this pattern exists to
    make immune to regardless of what else the suite happens to run first).
    """
    artifact = load_artifact_by_id("meridian.balance_inquiry")
    injected = artifact.model_copy(deep=True)
    injected.steps[0].params = {**injected.steps[0].params, "url": "/members?inject=maintenance"}

    safety_policy = runtime.build_safety_policy(_PROFILE["base_url"], _PROFILE["allowlist"])
    result_box: dict[str, object] = {}

    def _worker() -> None:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"{_PROFILE['base_url']}{_PROFILE['login_path']}")
            surface = WebSurface(page, base_url=_PROFILE["base_url"], safety_policy=safety_policy)
            runtime.run_login(surface, _PROFILE["username"], _PROFILE["password"], login_capability=_PROFILE["login_capability"])

            engine = ReplayEngine(surface, reauth_credentials={"username": _PROFILE["username"], "password": _PROFILE["password"]})
            result_box["result"] = engine.run(injected, {"member_id": "100987"})
            browser.close()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=60)

    result = result_box["result"]
    assert result.status == ReplayStatus.HARD_FAILURE
    assert "recovery action 'retry' did not clear" in result.error.message


def test_escalation_via_the_real_capability_api():
    """A real irreversible MERIDIAN action, triggered through /invoke (not the CLI, which is
    all that had been proven live before this), genuinely pauses, gets approved through the
    same operator-console mechanism (escalation/session_manager.py) a human would use, and
    completes for real -- proving the API surface preserves escalation, not just asserting it.
    """
    run_id_name = runtime.run_id("replay_run")
    evidence_dir = str(runtime.EVIDENCE_ROOT / run_id_name)
    result_box: dict[str, object] = {}

    def _invoke() -> None:
        result_box["response"] = client.post(
            "/capabilities/meridian.funds_transfer/invoke",
            json={
                "params": {
                    "member_id": "100987", "from_share": "100987-S0001-4",
                    "to_share": "100987-MMKT-5", "amount": 1.00, "memo": "phase 6 guarantees test",
                },
                "target": "meridian", "evidence_dir": evidence_dir,
            },
        )

    thread = threading.Thread(target=_invoke, daemon=True)
    thread.start()

    deadline = time.monotonic() + 30
    session = None
    while time.monotonic() < deadline:
        session = get_session(run_id_name)
        if session is not None and session.mode == SessionMode.PAUSED:
            break
        time.sleep(0.3)
    assert session is not None and session.mode == SessionMode.PAUSED, "run never reached a paused state to escalate"

    confirm_ref = next(
        el.ref for el in session.latest_observed.elements
        if el.role == "button" and el.name and "post transfer" in el.name.lower()
    )
    session.request_action(Action(kind=ActionType.CLICK, ref=confirm_ref, actor="human", confirmed=True))
    time.sleep(2)
    session.resume()

    thread.join(timeout=60)
    resp = result_box["response"]
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success", body
    assert body["escalated"] is True
    assert body["outputs"]["confirmation_number"]
