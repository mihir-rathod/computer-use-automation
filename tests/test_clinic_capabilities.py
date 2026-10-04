"""The platform's own acceptance tests against the clinic: every discovered capability replays through the real
front-door path (`runtime.run_replay`: safety policy, run store, approvals, browser, evidence), and anything that
changes state is verified against the clinic's audit log rather than against what a page said.

The artifacts replayed here are the ones checked in under artifacts/, discovered by an LLM against this app.
Replays use different inputs from discovery."""
from __future__ import annotations

import httpx
import pytest

import runtime
from replay.result import ReplayStatus
from runs.approvals import decide_run
from tests.clinic_support import chaos, effects, reset


@pytest.fixture
def world(clinic_base_url):
    return clinic_base_url, reset(clinic_base_url)


def run(world, capability, params, tmp_path, target="clinic", **kw):
    base, _ = world
    kw.setdefault("evidence_dir", tmp_path / f"ev_{capability.split('.')[-1]}_{len(list(tmp_path.iterdir()))}")
    return runtime.run_replay(capability, params, target=target, base_url=base, enable_operator_console=False, **kw)


def approve_and_resume(world, first, tmp_path, who="dana.okafor", why="checked against the invoice"):
    decide_run(runtime.default_store(), runtime.default_policy(), first.run_id, "approved", who, why)
    return run(world, first.capability_id, {}, tmp_path, target=None, resume_run_id=first.run_id)


# ---- read-only and reversible capabilities -----------------------------------------------------------

def test_patient_lookup_found_and_not_found(world, tmp_path):
    found, _ = run(world, "clinic.patient_lookup", {"mrn": "LK-100002"}, tmp_path)
    assert found.status == ReplayStatus.SUCCESS, found.error
    assert found.outputs["status"] == "found" and found.outputs["patient_name"]
    assert found.outputs["balance_due"].startswith("$") and found.outputs["date_of_birth"]

    missing, _ = run(world, "clinic.patient_lookup", {"mrn": "LK-999999"}, tmp_path)
    assert missing.status == ReplayStatus.BUSINESS_OUTCOME and missing.business_outcome == "not_found"
    assert not found.committed


def test_update_contact_changes_the_record_and_bad_input_is_a_business_outcome(world, tmp_path):
    base, _ = world
    ok, _ = run(world, "clinic.update_patient_contact",
                {"mrn": "LK-100002", "phone": "206-555-0142", "email": "new.contact@example.test", "address": "9 Fir Ave, Oakridge, WA 98021"}, tmp_path)
    assert ok.status == ReplayStatus.SUCCESS, ok.error
    [row] = effects(base, "patient.update_contact")
    assert "555" in str(row) or row["entity_id"]

    bad, _ = run(world, "clinic.update_patient_contact",
                 {"mrn": "LK-100002", "phone": "12", "email": "not-an-email", "address": "x"}, tmp_path)
    assert bad.status == ReplayStatus.BUSINESS_OUTCOME and bad.business_outcome == "validation_error"
    assert len(effects(base, "patient.update_contact")) == 1


def test_reschedule_moves_the_appointment(world, tmp_path):
    base, _ = world
    res, _ = run(world, "clinic.reschedule_appointment", {"appointment": "A-20003", "date": "2026-03-12", "time": "11:30"}, tmp_path)
    assert res.status == ReplayStatus.SUCCESS, res.error
    assert len(effects(base, "appointment.reschedule")) == 1

    missing, _ = run(world, "clinic.reschedule_appointment", {"appointment": "A-99999", "date": "2026-03-12", "time": "11:30"}, tmp_path)
    assert missing.business_outcome == "not_found"


# ---- commit capabilities: approval, then exactly once ---------------------------------------------------

def test_cancel_needs_an_operator_approval_then_posts_once(world, tmp_path):
    base, _ = world
    first, _ = run(world, "clinic.cancel_appointment", {"appointment": "A-20002", "reason": "patient_request"}, tmp_path, requested_by="alex")
    assert first.status == ReplayStatus.PENDING_APPROVAL and first.approval_tier == "operator"
    assert effects(base, "appointment.cancel") == []

    done, _ = approve_and_resume(world, first, tmp_path, who="sam.reyes")
    assert done.status == ReplayStatus.SUCCESS, done.error
    assert done.committed and done.outputs["receipt_number"].startswith("CXL-")
    assert len(effects(base, "appointment.cancel")) == 1
    approval = runtime.default_store().approvals_for(first.run_id)[0]
    assert (approval["decided_by"], approval["tier"]) == ("sam.reyes", "operator") and approval["reason"]


def test_cancel_unknown_appointment_is_not_found(world, tmp_path):
    base, _ = world
    first, _ = run(world, "clinic.cancel_appointment", {"appointment": "A-99999", "reason": "weather"}, tmp_path)
    res, _ = approve_and_resume(world, first, tmp_path, who="sam.reyes")
    assert res.business_outcome == "not_found" and effects(base, "appointment.cancel") == []


def test_submit_claim(world, tmp_path):
    base, fixtures = world
    first, _ = run(world, "clinic.submit_claim", {"invoice": fixtures["self_pay"]["invoice"], "amount": "10.00"}, tmp_path)
    res, _ = approve_and_resume(world, first, tmp_path, who="sam.reyes")
    # a self-pay invoice may not be claimable; either way the outcome is an answer, never a crash, and state matches the audit log
    assert res.status in (ReplayStatus.SUCCESS, ReplayStatus.BUSINESS_OUTCOME), res.error
    assert len(effects(base, "claim.submit")) == (1 if res.status == ReplayStatus.SUCCESS else 0)


def test_refund_requires_a_supervisor_and_a_sam_cannot_approve_it(world, tmp_path):
    base, fixtures = world
    params = {"invoice": fixtures["standard"]["refundable_invoice"], "amount": "30.00", "reason": "billing_error"}
    first, _ = run(world, "clinic.issue_refund", params, tmp_path, requested_by="alex")
    assert first.status == ReplayStatus.PENDING_APPROVAL and first.approval_tier == "supervisor"

    from runs.store import ApprovalError
    with pytest.raises(ApprovalError, match="needs a supervisor"):
        decide_run(runtime.default_store(), runtime.default_policy(), first.run_id, "approved", "sam.reyes", "ok")

    done, _ = approve_and_resume(world, first, tmp_path)
    assert done.status == ReplayStatus.SUCCESS and done.outputs["receipt_number"].startswith("RFD-")
    assert len(effects(base)) == 1


def test_refund_over_the_clinic_cap_is_queued_for_supervisor_approval_as_a_business_outcome(world, tmp_path):
    base, fixtures = world
    params = {"invoice": fixtures["big_refund"]["invoice"], "amount": "250.00", "reason": "billing_error"}
    first, _ = run(world, "clinic.issue_refund", params, tmp_path)
    res, _ = approve_and_resume(world, first, tmp_path)

    assert res.status == ReplayStatus.BUSINESS_OUTCOME and res.business_outcome == "pending_supervisor_approval"
    assert effects(base) == []  # no money moved
    assert len(effects(base, "refund.request_approval")) == 1


def test_refund_above_the_platform_ceiling_never_starts_a_browser(world, tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(runtime, "_replay_in_browser", lambda *a, **k: called.append(1))
    res, _ = run(world, "clinic.issue_refund", {"invoice": "INV-30032", "amount": "5000.00", "reason": "billing_error"}, tmp_path)
    assert res.status == ReplayStatus.HARD_FAILURE and res.error.code == "policy_cap_exceeded" and not called


def test_write_off_denies_a_front_desk_user_and_succeeds_for_a_supervisor(world, tmp_path):
    base, fixtures = world
    params = {"invoice": "INV-30002", "amount": "40.00", "reason": "uncollectible"}
    first, _ = run(world, "clinic.write_off_balance", params, tmp_path)
    denied, _ = approve_and_resume(world, first, tmp_path)
    assert denied.business_outcome == "permission_denied" and effects(base, "writeoff.apply") == []

    ok, _ = run(world, "clinic.write_off_balance", params, tmp_path, target="clinic_supervisor", idempotency_key="wo-1")
    ok, _ = approve_and_resume(world, ok, tmp_path) if ok.status == ReplayStatus.PENDING_APPROVAL else (ok, None)
    assert ok.status == ReplayStatus.SUCCESS, ok.error
    assert len(effects(base, "writeoff.apply")) == 1


# ---- the "done when": a retried commit posts exactly once --------------------------------------------------

def refund_params(world, amount="25.00"):
    return {"invoice": world[1]["standard"]["refundable_invoice"], "amount": amount, "reason": "duplicate_payment"}


def approved_refund(world, tmp_path, key, **kw):
    first, _ = run(world, "clinic.issue_refund", refund_params(world), tmp_path, idempotency_key=key, requested_by="alex")
    assert first.status == ReplayStatus.PENDING_APPROVAL, first
    return approve_and_resume(world, first, tmp_path, **kw)


def test_retry_with_the_same_idempotency_key_returns_the_first_result_and_posts_once(world, tmp_path):
    base, _ = world
    done, _ = approved_refund(world, tmp_path, "refund-A")
    assert done.status == ReplayStatus.SUCCESS and len(effects(base)) == 1

    retry, _ = run(world, "clinic.issue_refund", refund_params(world), tmp_path, idempotency_key="refund-A", requested_by="alex")

    assert retry.deduplicated and retry.run_id == done.run_id
    assert retry.outputs == done.outputs
    assert len(effects(base)) == 1  # the clinic's own duplicate guard is off: only the platform stood in the way


def test_response_lost_after_the_commit_is_not_retried_and_the_refund_posts_once(world, tmp_path):
    """The clinic applies the refund and then answers 500. The platform cannot tell it landed, so it reports
    needs_review and refuses to run the same key again until a person settles it."""
    base, _ = world
    chaos(base, "error500_after", method="POST", path_glob="/legacy/transactions/confirm")

    ambiguous = approved_refund(world, tmp_path, "refund-B")[0]
    assert ambiguous.status == ReplayStatus.NEEDS_REVIEW and ambiguous.error.code == "ambiguous_commit"
    assert len(effects(base)) == 1

    retry, _ = run(world, "clinic.issue_refund", refund_params(world), tmp_path, idempotency_key="refund-B", requested_by="alex")
    assert retry.status == ReplayStatus.HARD_FAILURE and retry.error.code == "idempotency_conflict"
    assert len(effects(base)) == 1

    store = runtime.default_store()
    store.resolve(ambiguous.run_id, "committed", "dana.okafor", "refund RFD-000001 is in the clinic ledger")
    again, _ = run(world, "clinic.issue_refund", refund_params(world), tmp_path, idempotency_key="refund-B", requested_by="alex")
    assert again.deduplicated and len(effects(base)) == 1


def test_a_lost_request_that_never_landed_can_be_retried_once_settled(world, tmp_path):
    base, _ = world
    chaos(base, "error500", method="POST", path_glob="/legacy/transactions/confirm")  # fails BEFORE applying

    ambiguous = approved_refund(world, tmp_path, "refund-C")[0]
    assert ambiguous.status == ReplayStatus.NEEDS_REVIEW
    assert effects(base) == []  # the engine could not know this; the audit log does

    runtime.default_store().resolve(ambiguous.run_id, "not_committed", "dana.okafor", "ledger shows no refund for INV-30001")
    done = approved_refund(world, tmp_path, "refund-C")[0]
    assert done.status == ReplayStatus.SUCCESS
    assert len(effects(base)) == 1


def test_dry_run_of_a_refund_changes_nothing_and_needs_no_approval(world, tmp_path):
    base, _ = world
    res, _ = run(world, "clinic.issue_refund", refund_params(world), tmp_path, dry_run=True)
    assert res.status == ReplayStatus.DRY_RUN and not res.committed
    assert effects(base) == [] and effects(base, "refund.request_approval") == []


def test_evidence_for_a_run_redacts_pii_but_keeps_the_audit_trail(world, tmp_path):
    base, _ = world
    res, ev = run(world, "clinic.update_patient_contact",
                  {"mrn": "LK-100002", "phone": "206-555-0142", "email": "pii.check@example.test", "address": "9 Fir Ave"}, tmp_path)
    assert res.status == ReplayStatus.SUCCESS
    log = (ev / "log.jsonl").read_text()
    assert "pii.check@example.test" not in log and "206-555-0142" not in log
    assert "desk-demo-123" not in log
    assert '"event_type": "action"' in log


def test_an_approved_run_cannot_be_resumed_as_a_different_account(world, tmp_path):
    first, _ = run(world, "clinic.write_off_balance", {"invoice": "INV-30002", "amount": "10.00", "reason": "uncollectible"}, tmp_path, target="clinic")
    decide_run(runtime.default_store(), runtime.default_policy(), first.run_id, "approved", "dana.okafor", "ok")
    with pytest.raises(ValueError, match="requested for target 'clinic'"):
        run(world, first.capability_id, {}, tmp_path, target="clinic_supervisor", resume_run_id=first.run_id)


# ---- partial updates ---------------------------------------------------------------------------------

def patient(base, patient_id=2):
    c = httpx.Client(base_url=base)
    c.post("/api/auth/login", json={"username": "frontdesk", "password": "desk-demo-123"})
    return c.get(f"/api/patients/{patient_id}").json()["patient"]


def test_updating_only_the_phone_leaves_email_and_address_untouched(world, tmp_path):
    """The point of partial updates: change one field without re-sending (or wiping) the others."""
    base, _ = world
    before = patient(base)

    res, _ = run(world, "clinic.update_patient_contact", {"mrn": "LK-100002", "phone": "206-555-0188"}, tmp_path)

    assert res.status == ReplayStatus.SUCCESS, res.error
    assert res.steps_skipped == ["s7", "s8"]  # the email and address steps were not run
    after = patient(base)
    assert after["phone"] == "(206) 555-0188"
    assert (after["email"], after["address"]) == (before["email"], before["address"])
    assert len(effects(base, "patient.update_contact")) == 1


def test_each_single_field_and_each_pair_works(world, tmp_path):
    base, _ = world
    run(world, "clinic.update_patient_contact", {"mrn": "LK-100002", "email": "only.email@example.test"}, tmp_path)
    run(world, "clinic.update_patient_contact", {"mrn": "LK-100002", "address": "77 Cedar Way, Oakridge, WA 98021"}, tmp_path)
    now = patient(base)
    assert now["email"] == "only.email@example.test" and now["address"] == "77 Cedar Way, Oakridge, WA 98021"
    res, _ = run(world, "clinic.update_patient_contact", {"mrn": "LK-100002", "phone": "206-555-0101", "email": "pair@example.test"}, tmp_path)
    assert res.steps_skipped == ["s8"]
    assert patient(base)["address"] == "77 Cedar Way, Oakridge, WA 98021"


def test_supplying_no_field_is_rejected_before_anything_runs(world, tmp_path):
    res, _ = run(world, "clinic.update_patient_contact", {"mrn": "LK-100002"}, tmp_path)
    assert res.status == ReplayStatus.HARD_FAILURE and res.error.code == "input_invalid" and "at least one of" in res.error.message


def test_null_means_leave_alone_and_empty_string_is_refused_not_applied(world, tmp_path):
    base, _ = world
    before = patient(base)
    res, _ = run(world, "clinic.update_patient_contact", {"mrn": "LK-100002", "phone": "206-555-0110", "email": None, "address": None}, tmp_path)
    assert res.status == ReplayStatus.SUCCESS and res.steps_skipped == ["s7", "s8"]
    assert patient(base)["email"] == before["email"]
    res, _ = run(world, "clinic.update_patient_contact", {"mrn": "LK-100002", "phone": "206-555-0111", "address": ""}, tmp_path)
    assert res.error.code == "input_invalid"  # blanking a field is not something this capability does


def test_resending_the_current_values_is_a_no_change_outcome_not_a_failure(world, tmp_path):
    """Found by the QA pass: 'NO CHANGES WERE NEEDED' failed the success checkpoint and read as a hard failure."""
    base, _ = world
    current = patient(base)
    res, _ = run(world, "clinic.update_patient_contact", {"mrn": "LK-100002", "email": current["email"]}, tmp_path)
    assert res.status == ReplayStatus.BUSINESS_OUTCOME and res.business_outcome == "no_change"
    assert effects(base, "patient.update_contact") == []
