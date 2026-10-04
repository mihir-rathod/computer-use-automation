"""Edge cases found by the second QA pass (every capability, with faults), each pinned."""
from __future__ import annotations

import runtime
from api import chatbot
from artifacts_lib.storage import load_artifact_by_id
from replay.result import ReplayStatus
from replay.validation import validate_input
from tests.clinic_support import effects, reset


def test_input_validation_checks_enum_length_and_range():
    from artifacts_lib.schema import JSONSchemaObject

    schema = JSONSchemaObject(properties={
        "reason": {"type": "string", "enum": ["a", "b"]}, "note": {"type": "string", "maxLength": 3, "minLength": 1},
        "n": {"type": "number", "minimum": 1, "maximum": 5}}, required=[])
    assert validate_input(schema, {"reason": "a", "note": "ab", "n": 3}) == []
    errors = " | ".join(validate_input(schema, {"reason": "c", "note": "abcd", "n": 9}))
    assert "must be one of" in errors and "longer than 3" in errors and "at most 5" in errors
    assert "shorter than 1" in " | ".join(validate_input(schema, {"note": ""})) and "at least 1" in " | ".join(validate_input(schema, {"n": 0}))


def test_a_request_that_cannot_run_never_reaches_an_approvers_queue(monkeypatch):
    """Found by the API probe: amount sent as a JSON number was queued for supervisor approval, then failed validation."""
    monkeypatch.setattr(runtime, "_replay_in_browser", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no browser")))
    res, _ = runtime.run_replay("clinic.issue_refund", {"invoice": "INV-30001", "amount": 20, "reason": "billing_error"},
                                target="clinic", requested_by="q", enable_operator_console=False)
    assert res.status == ReplayStatus.HARD_FAILURE and res.error.code == "input_invalid"
    assert runtime.default_store().pending_approvals() == []

    res, _ = runtime.run_replay("clinic.issue_refund", {"invoice": "INV-30001", "amount": "20.00", "reason": "vibes"},
                                target="clinic", requested_by="q", enable_operator_console=False)
    assert res.error.code == "input_invalid" and "must be one of" in res.error.message


def test_selecting_an_option_that_does_not_exist_says_what_is_offered_and_is_quick(clinic, signed_in):
    import time

    from artifacts_lib.schema import ActionType
    from surface.base import Action
    from surface.web import WebSurface

    signed_in.goto(f"{clinic}/legacy/fn/cancel")
    signed_in.locator('input[name="number"]').fill("A-20002")
    signed_in.get_by_role("button", name="Continue").click()
    surface = WebSurface(signed_in, base_url=clinic)
    ref = next(e.ref for e in surface.perceive().elements if e.role == "combobox")
    started = time.monotonic()
    result = surface.act(Action(kind=ActionType.SELECT, ref=ref, params={"value": "because"}, actor="agent"))
    assert not result.success and "not offered" in result.error and "weather" in result.error and not result.dispatched
    assert time.monotonic() - started < 6


# ---- chatbot argument guard ---------------------------------------------------------------------

def test_chatbot_refuses_placeholder_values_the_model_invented():
    """Found live: asked to change only a phone number, the model filled email and address with 'unknown'."""
    artifact = load_artifact_by_id("clinic.update_patient_contact")
    question = chatbot._check_arguments(artifact, {"mrn": "LK-100002", "phone": "206-555-0188", "email": "unknown", "address": "Unknown"}, "update phone")
    assert question and "email" in question and "address" in question and "won't guess" in question


def test_chatbot_asks_for_missing_required_values_and_rejects_invalid_ones():
    artifact = load_artifact_by_id("clinic.issue_refund")
    assert "amount" in chatbot._check_arguments(artifact, {"invoice": "INV-30001", "reason": "billing_error"}, "refund")
    assert "must be one of" in chatbot._check_arguments(artifact, {"invoice": "INV-30001", "amount": "5.00", "reason": "vibes"}, "refund")
    assert chatbot._check_arguments(artifact, {"invoice": "INV-30001", "amount": "5.00", "reason": "billing_error"}, "refund") is None


def test_chat_page_lists_the_real_capabilities_not_a_stale_table(monkeypatch):
    rows = {r["name"]: r for r in chatbot._capability_summary()}
    assert "Issue a refund on an invoice" in rows and "invoice" in rows["Issue a refund on an invoice"]["required"]
    assert not any("transfer" in n.lower() or "hold" in n.lower() for n in rows)  # leftovers from the previous target
    template = (chatbot.TEMPLATES_DIR / "chat.html").read_text() if hasattr(chatbot, "TEMPLATES_DIR") else open("api/templates/chat.html").read()
    assert "Funds transfer" not in template and "Place hold" not in template
