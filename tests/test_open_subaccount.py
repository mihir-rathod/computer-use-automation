"""Integration tests for the hand-written mockbank.open_subaccount artifact -- the capability
that exercises validation_error (not covered by mockbank.member_balance_lookup) and a genuine
irreversible, confirmation-gated step at full artifact/replay granularity, closing both gaps
deliberately: member_balance_
lookup is read-only and never needed either.
"""
from __future__ import annotations

import threading
import time

from artifacts_lib.schema import ActionType
from artifacts_lib.storage import load_artifact_by_id
from escalation.session_manager import SessionManager, SessionMode
from evidence_lib.logger import EvidenceLogger
from replay.engine import ReplayEngine
from replay.result import ReplayStatus
from safety.allowlist import AllowlistConfig, AllowlistPolicy
from safety.policy import SafetyPolicy
from surface.base import Action
from surface.web import WebSurface
from tests.conftest import login

ARTIFACT = load_artifact_by_id("mockbank.open_subaccount")


def _safety_policy(base_url: str) -> SafetyPolicy:
    config = AllowlistConfig(
        allowed_base_urls=[base_url],
        allowed_route_patterns=["/login", "/search", "/member/*"],
        allowed_action_types=["navigate", "click", "type", "select", "extract", "wait_for", "dismiss_dialog"],
    )
    return SafetyPolicy(AllowlistPolicy(config))


def test_validation_error_business_outcome_never_reaches_irreversible_step(page, mockbank_base_url, tmp_path):
    login(page, mockbank_base_url)
    surface = WebSurface(page, base_url=mockbank_base_url, safety_policy=_safety_policy(mockbank_base_url))
    engine = ReplayEngine(surface)

    result = engine.run(ARTIFACT, {"member_id": "10002", "account_type": "savings", "initial_deposit": 0})

    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.business_outcome == "validation_error"
    assert result.outputs == {"status": "validation_error", "confirmation_number": None}
    assert result.steps_completed == ["s1", "s2", "s3"]  # never reached s4 (Continue) or s5 (Confirm)


def test_not_found_and_permission_denied_still_work(page, mockbank_base_url):
    login(page, mockbank_base_url)
    surface = WebSurface(page, base_url=mockbank_base_url, safety_policy=_safety_policy(mockbank_base_url))
    engine = ReplayEngine(surface)

    result = engine.run(ARTIFACT, {"member_id": "99999", "account_type": "savings", "initial_deposit": 100})
    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.business_outcome == "not_found"

    result2 = engine.run(ARTIFACT, {"member_id": "40004", "account_type": "savings", "initial_deposit": 100})
    assert result2.status == ReplayStatus.BUSINESS_OUTCOME
    assert result2.business_outcome == "permission_denied"


def test_irreversible_step_blocks_then_a_human_approves_via_escalation(page, mockbank_base_url, tmp_path):
    """The centerpiece: replay reaches the irreversible "Confirm & Open Account" step, the
    safety policy blocks it (unconfirmed), that's classified as a hard-failure-with-no-known-
    signal, which escalates -- a human reviews the SAME live session, explicitly approves by
    submitting the exact action with confirmed=True, resumes, and the run completes for real,
    including extracting a genuine confirmation number.
    """
    login(page, mockbank_base_url)
    surface = WebSurface(page, base_url=mockbank_base_url, safety_policy=_safety_policy(mockbank_base_url))
    logger = EvidenceLogger(tmp_path)
    session = SessionManager("open-subaccount-test", surface, tmp_path, evidence_logger=logger, capability_id=ARTIFACT.capability_id, goal="test")
    engine = ReplayEngine(surface, evidence_logger=logger, session_manager=session)

    operator_done = threading.Event()

    def operator_thread():
        deadline = time.monotonic() + 10
        while session.mode != SessionMode.PAUSED and time.monotonic() < deadline:
            time.sleep(0.05)
        assert session.mode == SessionMode.PAUSED
        assert session.current_step_id == "s5"
        assert "confirmation" in session.pause_reason

        confirm_ref = next(
            el.ref for el in session.latest_observed.elements
            if el.role == "button" and el.name == "Confirm & Open Account"
        )
        session.request_action(Action(kind=ActionType.CLICK, ref=confirm_ref, actor="human", confirmed=True))
        time.sleep(0.5)
        session.resume()
        operator_done.set()

    threading.Thread(target=operator_thread, daemon=True).start()

    result = engine.run(ARTIFACT, {"member_id": "10001", "account_type": "checking", "initial_deposit": 250})

    assert operator_done.wait(timeout=15)
    assert result.status == ReplayStatus.SUCCESS, result.error
    assert result.outputs["status"] == "opened"
    assert result.outputs["confirmation_number"].startswith("CNF-")

    events = [__import__("json").loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert any(e["event_type"] == "pause" and "confirmation" in e["data"]["reason"] for e in events)
    assert any(e["actor"] == "human" and e["event_type"] == "action" and e["data"]["confirmed"] for e in events)
