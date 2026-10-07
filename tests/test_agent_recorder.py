"""Pure-unit tests for the discovery recorder -- no browser, no LLM. Constructs a fake but
realistic transcript by hand to verify parameterization and checkpoint synthesis independent
of any particular live run.
"""
from __future__ import annotations

import pytest

from agent.loop import DiscoveryResult, RecordedAction
from agent.recorder import _parameterize, _synthesize_checkpoint, build_artifact
from artifacts_lib.schema import (
    ActionType,
    CapabilityTarget,
    ErrorHandling,
    JSONSchemaObject,
    Locator,
    LocatorStrategy,
    SafetyMeta,
    Signal,
    SignalType,
    Target,
)
from surface.base import Action, ActionResult, ObservedState


def _state(url: str) -> ObservedState:
    return ObservedState(url=url, title="", elements=[])


def test_parameterize_replaces_concrete_value():
    assert _parameterize("value is 10001 today", {"member_id": "10001"}) == "value is {{member_id}} today"


def test_parameterize_leaves_unrelated_text_alone():
    assert _parameterize("hello world", {"member_id": "10001"}) == "hello world"


def test_synthesize_checkpoint_url_change_becomes_url_matches():
    target = Target(semantic_description="x", locators=[Locator(strategy=LocatorStrategy.CSS, value="#x")])
    recorded = RecordedAction(
        action=Action(kind=ActionType.CLICK, target=target),
        result=ActionResult(success=True, resolved_target=target),
        observed_before=_state("http://x/search"),
        observed_after=_state("http://x/member/10001"),
    )
    checkpoint = _synthesize_checkpoint(recorded, {"member_id": "10001"})
    assert checkpoint.type == SignalType.URL_MATCHES
    assert checkpoint.value == "**/member/{{member_id}}"


def test_synthesize_checkpoint_type_becomes_element_value_equals():
    target = Target(semantic_description="member id field", locators=[Locator(strategy=LocatorStrategy.ROLE, value="textbox[name='Member ID']")])
    recorded = RecordedAction(
        action=Action(kind=ActionType.TYPE, target=target, params={"text": "10001"}),
        result=ActionResult(success=True, resolved_target=target),
        observed_before=_state("http://x/search"),
        observed_after=_state("http://x/search"),
    )
    checkpoint = _synthesize_checkpoint(recorded, {"member_id": "10001"})
    assert checkpoint.type == SignalType.ELEMENT_VALUE_EQUALS
    assert checkpoint.value == "{{member_id}}"


def test_synthesize_checkpoint_extract_gets_none():
    target = Target(semantic_description="balance cell", locators=[Locator(strategy=LocatorStrategy.CSS, value="#bal")])
    recorded = RecordedAction(
        action=Action(kind=ActionType.EXTRACT, target=target),
        result=ActionResult(success=True, resolved_target=target, extracted_value="$100"),
        observed_before=_state("http://x/member/10001"),
        observed_after=_state("http://x/member/10001"),
    )
    assert _synthesize_checkpoint(recorded, {}) is None


def _artifact_kwargs(**overrides):
    base = {
        "capability_id": "mockbank.test_lookup", "version": "1.0.0", "name": "Test lookup", "description": "test",
        "target": CapabilityTarget(app_id="mockbank", surface_type="web", base_url="http://x", vendor_product="mockbank-core"),
        "input_schema": JSONSchemaObject(properties={"member_id": {"type": "string"}}, required=["member_id"]),
        "output_schema": JSONSchemaObject(properties={"savings_balance": {"type": "number"}}, required=[]),
        "success_checkpoint": Signal(type=SignalType.TEXT_PRESENT, value="Account Summary"),
        "error_handling": ErrorHandling(),
        "safety": SafetyMeta(risk_level="read_only"),
        "discovered_by": "test-model", "discovery_run_id": "run-1",
    }
    base.update(overrides)
    return base


def test_build_artifact_from_transcript():
    member_id_target = Target(semantic_description="member id field", locators=[Locator(strategy=LocatorStrategy.ROLE, value="textbox[name='Member ID']")])
    search_target = Target(semantic_description="search button", locators=[Locator(strategy=LocatorStrategy.ROLE, value="button[name='Search']")])
    balance_target = Target(semantic_description="savings balance cell", locators=[Locator(strategy=LocatorStrategy.ROLE, value="cell[name='Savings Balance value']")])

    transcript = [
        RecordedAction(
            action=Action(kind=ActionType.TYPE, target=member_id_target, params={"text": "10001"}),
            result=ActionResult(success=True, resolved_target=member_id_target),
            observed_before=_state("http://x/search"), observed_after=_state("http://x/search"),
        ),
        RecordedAction(
            action=Action(kind=ActionType.CLICK, target=search_target),
            result=ActionResult(success=True, resolved_target=search_target),
            observed_before=_state("http://x/search"), observed_after=_state("http://x/member/10001"),
        ),
        RecordedAction(
            action=Action(kind=ActionType.EXTRACT, target=balance_target),
            result=ActionResult(success=True, resolved_target=balance_target, extracted_value="$4,231.55"),
            observed_before=_state("http://x/member/10001"), observed_after=_state("http://x/member/10001"),
            output_name="savings_balance",
        ),
    ]
    result = DiscoveryResult(stop_reason="finished", reasoning="done", transcript=transcript)

    artifact = build_artifact(result, {"member_id": "10001"}, **_artifact_kwargs())

    assert len(artifact.steps) == 3
    assert artifact.steps[0].params["text"] == "{{member_id}}"
    assert artifact.steps[1].checkpoint.value == "**/member/{{member_id}}"
    assert artifact.steps[2].output_binding == "savings_balance"
    assert artifact.provenance.discovered_by == "test-model"
    assert artifact.provenance.reviewed is False


def test_build_artifact_rejects_unfinished_run():
    result = DiscoveryResult(stop_reason="give_up", reasoning="stuck", transcript=[])
    with pytest.raises(ValueError, match="did not finish"):
        build_artifact(result, {}, **_artifact_kwargs())


def test_url_checkpoint_does_not_pin_the_record_id_seen_during_discovery():
    from agent.recorder import _generalize_path, _parameterize

    assert _generalize_path("/legacy/patients/1") == "/legacy/patients/*"
    assert _generalize_path("/legacy/patients/42/contact") == "/legacy/patients/*/contact"
    assert _generalize_path("/legacy/fn/refund") == "/legacy/fn/refund"
    # an id that is an input stays a precise placeholder: parameterize first, then generalize what is left
    assert _generalize_path(_parameterize("/legacy/appointments/A-20003/cancel", {"appointment": "A-20003"})) == "/legacy/appointments/{{appointment}}/cancel"
