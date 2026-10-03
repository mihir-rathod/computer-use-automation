"""The one test that makes a REAL Gemini API call against a REAL MockBank instance --
the discovery run has to be real -- a mocked model proves nothing about whether discovery works.
Skipped automatically without GEMINI_API_KEY, so `pytest` stays free of any external dependency
or cost for anyone verifying the rest of the system.
"""
from __future__ import annotations

import os

import pytest

from agent.gemini_client import GeminiClient
from agent.loop import DiscoveryLoop
from agent.recorder import build_artifact
from artifacts_lib.schema import (
    CapabilityTarget,
    ErrorHandling,
    JSONSchemaObject,
    SafetyMeta,
    Signal,
    SignalType,
)
from replay.engine import ReplayEngine
from replay.result import ReplayStatus
from surface.web import WebSurface
from tests.conftest import login

pytestmark = pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="requires a real GEMINI_API_KEY")

GOAL = (
    "Search for the member with the given member_id and read their account details. Extract "
    "three values using extract(): the savings balance with output_name 'savings_balance', "
    "the checking balance with output_name 'checking_balance', and the account status with "
    "output_name 'account_status'. The goal is complete once all three have been extracted "
    "and are visible on screen."
)


def test_discovery_loop_finds_member_balance_and_replays(page, mockbank_base_url):
    login(page, mockbank_base_url)
    surface = WebSurface(page, base_url=mockbank_base_url)
    loop = DiscoveryLoop(surface, GeminiClient(), max_steps=15, timeout_seconds=180)

    result = loop.run(goal=GOAL, parameters={"member_id": "10001"}, start_path="/search")

    assert result.stop_reason == "finished", f"stop_reason={result.stop_reason} reasoning={result.reasoning}"
    assert len(result.transcript) >= 4  # at minimum: navigate to start, type member id, click search, extract something

    artifact = build_artifact(
        result, {"member_id": "10001"},
        capability_id="mockbank.llm_member_balance_lookup", version="1.0.0",
        name="LLM-discovered member balance lookup",
        description="Discovered live by Gemini against a real running MockBank instance.",
        target=CapabilityTarget(app_id="mockbank", surface_type="web", base_url=mockbank_base_url, vendor_product="mockbank-core"),
        input_schema=JSONSchemaObject(properties={"member_id": {"type": "string", "pattern": "^[0-9]{4,10}$"}}, required=["member_id"]),
        output_schema=JSONSchemaObject(properties={
            "savings_balance": {"type": ["number", "null"]},
            "checking_balance": {"type": ["number", "null"]},
            "account_status": {"type": ["string", "null"]},
        }, required=[]),
        success_checkpoint=Signal(type=SignalType.TEXT_PRESENT, value="Account Summary"),
        error_handling=ErrorHandling(),
        safety=SafetyMeta(risk_level="read_only"),
        discovered_by="gemini-flash-latest",
        discovery_run_id="test-live-run",
    )
    assert len(artifact.steps) == len(result.transcript)

    # the discovered artifact must also be replayable -- proves discovery -> artifact -> replay
    # end to end, with a DIFFERENT member id than the one used during discovery (genuine
    # parameterization, not a hardcoded value that happens to work).
    replay_surface = WebSurface(page, base_url=mockbank_base_url)
    engine = ReplayEngine(replay_surface)
    replay_result = engine.run(artifact, {"member_id": "10002"})

    assert replay_result.status == ReplayStatus.SUCCESS, replay_result.error
    assert replay_result.outputs["savings_balance"] == pytest.approx(150.00)
