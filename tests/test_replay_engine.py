"""Integration tests for the deterministic replay engine against real MockBank + real Chromium.

Covers the three-way result contract end to end: success with typed
outputs, business outcomes (not_found, permission_denied), recoverable conditions (transient
unavailable, terms modal, slow load, session expiry), and hard failures -- plus the
idempotency safety gate on auto-resume after reauthentication.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from artifacts_lib.storage import load_artifact
from evidence_lib.logger import EvidenceLogger
from replay.engine import ReplayEngine
from replay.result import ReplayStatus
from surface.web import WebSurface
from tests.conftest import login

FIXTURES_DIR = Path(__file__).parent / "fixtures"
LOOKUP_ARTIFACT_PATH = FIXTURES_DIR / "mockbank.member_balance_lookup.json"

CREDENTIALS = {"username": "operator", "password": "bankdemo123"}


def _engine(page, base_url, tmp_path, **kwargs) -> ReplayEngine:
    surface = WebSurface(page, base_url=base_url, screenshot_dir=tmp_path / "screenshots")
    logger = EvidenceLogger(tmp_path)
    return ReplayEngine(surface, evidence_logger=logger, artifacts_dir=FIXTURES_DIR, **kwargs)


def test_success_path_extracts_typed_outputs(page, mockbank_base_url, tmp_path):
    login(page, mockbank_base_url)
    artifact = load_artifact(LOOKUP_ARTIFACT_PATH)
    engine = _engine(page, mockbank_base_url, tmp_path)

    result = engine.run(artifact, {"member_id": "10001"})

    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs["status"] == "found"
    assert result.outputs["savings_balance"] == pytest.approx(4231.55)
    assert result.outputs["checking_balance"] == pytest.approx(812.10)
    assert result.outputs["account_status"] == "Active"
    assert result.steps_completed == ["s1", "s2", "s3", "s4", "s5", "s6"]


def test_not_found_business_outcome(page, mockbank_base_url, tmp_path):
    login(page, mockbank_base_url)
    artifact = load_artifact(LOOKUP_ARTIFACT_PATH)
    engine = _engine(page, mockbank_base_url, tmp_path)

    result = engine.run(artifact, {"member_id": "99999"})

    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.business_outcome == "not_found"
    assert result.outputs == {"status": "not_found", "savings_balance": None, "checking_balance": None, "account_status": None}


def test_permission_denied_business_outcome(page, mockbank_base_url, tmp_path):
    login(page, mockbank_base_url)
    artifact = load_artifact(LOOKUP_ARTIFACT_PATH)
    engine = _engine(page, mockbank_base_url, tmp_path)

    result = engine.run(artifact, {"member_id": "40004"})

    assert result.status == ReplayStatus.BUSINESS_OUTCOME
    assert result.business_outcome == "permission_denied"
    assert result.outputs["status"] == "permission_denied"


def test_recoverable_transient_unavailable_retries_and_succeeds(page, mockbank_base_url, tmp_path):
    login(page, mockbank_base_url)
    page.goto(f"{mockbank_base_url}/_debug/simulate?condition=unavailable")
    artifact = load_artifact(LOOKUP_ARTIFACT_PATH)
    engine = _engine(page, mockbank_base_url, tmp_path)

    result = engine.run(artifact, {"member_id": "10002"})

    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs["status"] == "found"


def test_recoverable_terms_modal_is_dismissed(page, mockbank_base_url, tmp_path):
    login(page, mockbank_base_url)
    page.goto(f"{mockbank_base_url}/_debug/simulate?condition=terms_modal")
    artifact = load_artifact(LOOKUP_ARTIFACT_PATH)
    engine = _engine(page, mockbank_base_url, tmp_path)

    result = engine.run(artifact, {"member_id": "10003"})

    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs["account_status"] == "Active"


def test_recoverable_slow_load_is_tolerated(page, mockbank_base_url, tmp_path):
    login(page, mockbank_base_url)
    page.goto(f"{mockbank_base_url}/_debug/simulate?condition=slow")
    artifact = load_artifact(LOOKUP_ARTIFACT_PATH)
    engine = _engine(page, mockbank_base_url, tmp_path)

    result = engine.run(artifact, {"member_id": "10001"})

    assert result.status == ReplayStatus.SUCCESS


def test_hard_failure_on_missing_required_input(page, mockbank_base_url, tmp_path):
    login(page, mockbank_base_url)
    artifact = load_artifact(LOOKUP_ARTIFACT_PATH)
    engine = _engine(page, mockbank_base_url, tmp_path)

    result = engine.run(artifact, {})

    assert result.status == ReplayStatus.HARD_FAILURE
    assert "member_id" in result.error.message


def test_hard_failure_when_locator_never_resolves(page, mockbank_base_url, tmp_path):
    from artifacts_lib.schema import Artifact

    login(page, mockbank_base_url)
    raw = json.loads(LOOKUP_ARTIFACT_PATH.read_text())
    # break s2's target so nothing can ever resolve, with no business/recoverable signal to explain it
    raw["steps"][1]["target"]["locators"] = [{"strategy": "css", "value": "#totally-does-not-exist"}]
    artifact = Artifact.model_validate(raw)
    engine = _engine(page, mockbank_base_url, tmp_path)

    result = engine.run(artifact, {"member_id": "10001"})

    assert result.status == ReplayStatus.HARD_FAILURE
    assert result.error.step_id == "s2"


def test_reauthenticate_and_resume_after_session_expiry(page, mockbank_base_url, tmp_path):
    login(page, mockbank_base_url)
    page.goto(f"{mockbank_base_url}/_debug/simulate?condition=expire_session")
    artifact = load_artifact(LOOKUP_ARTIFACT_PATH)
    engine = _engine(page, mockbank_base_url, tmp_path, reauth_credentials=CREDENTIALS)

    result = engine.run(artifact, {"member_id": "10001"})

    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs["status"] == "found"

    raw = tmp_path.joinpath("log.jsonl").read_text()
    events = [json.loads(line) for line in raw.splitlines()]
    # the nested login replay should show up as its own set of "replay" actor events
    assert any(e["data"].get("capability_id") == "mockbank.login" for e in events if e["event_type"] == "result")


def test_non_idempotent_step_blocks_auto_resume(page, mockbank_base_url, tmp_path):
    """A non-idempotent step that already completed must block auto-restart-on-reauth, even
    though reauthentication itself would otherwise succeed -- this is the concrete mechanism
    behind "never auto-retry a non-idempotent step after an ambiguous failure" (see the schema's
    `idempotent` field).

    Uses a minimal 2-step artifact built in-test rather than the real lookup artifact: s1 is a
    pure client-side `type` (no server round-trip, so it can't consume or be affected by
    MockBank's one-shot debug flag) marked non-idempotent purely for this test; s2 is the first
    step that actually hits the server, so it's the one that observes the pre-armed session
    expiry. `page.request.get` (not `page.goto`) arms the flag without navigating the page away
    from the /search form s1 needs to already be visible.
    """
    from artifacts_lib.schema import Artifact

    login(page, mockbank_base_url)  # lands on /search
    raw = {
        "artifact_schema_version": "1.0",
        "capability_id": "mockbank.test_two_step",
        "version": "1.0.0",
        "name": "Two-step test artifact",
        "description": "Minimal artifact for exercising the non-idempotent auto-resume safety gate.",
        "target": {"app_id": "mockbank", "surface_type": "web", "base_url": mockbank_base_url, "vendor_product": "mockbank-core", "tenant_id": None},
        "preconditions": {"requires_capability": "mockbank.login", "note": "test fixture"},
        "input_schema": {"type": "object", "properties": {}, "required": []},
        "output_schema": {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]},
        "success_checkpoint": {"type": "url_matches", "value": "**/member/*"},
        "steps": [
            {
                "step_id": "s1", "action": "type",
                "target": {"semantic_description": "member id field", "locators": [{"strategy": "role", "value": "textbox[name='Member ID']"}]},
                "params": {"text": "10001"},
                "risk_level": "safe", "idempotent": False,
            },
            {
                "step_id": "s2", "action": "click",
                "target": {"semantic_description": "search button", "locators": [{"strategy": "role", "value": "button[name='Search']"}]},
                "params": {},
                "checkpoint": {"type": "url_matches", "value": "**/member/*"},
                "risk_level": "safe", "idempotent": True,
            },
        ],
        "error_handling": {
            "business_outcomes": [],
            "recoverable": [{"signal": {"type": "redirected_to", "value": "**/login*"}, "action": "reauthenticate_and_resume"}],
            "hard_failure_default": "stop_and_escalate",
        },
        "safety": {"risk_level": "read_only", "requires_confirmation": False},
        "success_output_defaults": {"status": "ok"},
        "provenance": {"discovered_by": "hand_written", "discovery_run_id": "test", "created_at": "2026-08-14T00:00:00Z", "reviewed": True},
    }
    artifact = Artifact.model_validate(raw)
    engine = _engine(page, mockbank_base_url, tmp_path, reauth_credentials=CREDENTIALS)

    page.request.get(f"{mockbank_base_url}/_debug/simulate?condition=expire_session")

    result = engine.run(artifact, {})

    assert result.status == ReplayStatus.HARD_FAILURE
    assert "non-idempotent" in result.error.message
