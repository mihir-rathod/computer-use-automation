from __future__ import annotations

import pytest

from clinic.errors import AlreadyProcessed, NotFound, PermissionDenied, ValidationFailed
from clinic.seed import seed

from .conftest import World


def test_seed_is_deterministic_and_reset_is_idempotent(world: World):
    first = world.fixtures
    again = seed(world.db, world.clock)
    assert first == again
    assert world.db.scalar("SELECT COUNT(*) FROM patients") == 42
    assert world.db.scalar("SELECT COUNT(*) FROM audit") == 0


def test_search_by_mrn_last_name_dob_and_requires_a_criterion(world: World):
    desk = world.actor()
    assert world.clinic.search_patients(desk, mrn="lk-100001")["total"] == 1
    assert world.clinic.search_patients(desk, last_name="brenn")["items"][0]["mrn"] == "LK-100001"
    dob = world.clinic.patient_by_mrn("LK-100001")["dob"]
    assert world.clinic.search_patients(desk, dob=dob)["total"] >= 1
    assert world.clinic.search_patients(desk, mrn="LK-999999")["total"] == 0
    with pytest.raises(ValidationFailed):
        world.clinic.search_patients(desk)
    with pytest.raises(ValidationFailed):
        world.clinic.search_patients(desk, dob="03/02/1980")
    assert world.audit.list(action="patient.search", outcome="rejected")


def test_invoice_pagination_for_patient_with_many_invoices(world: World):
    pid = world.clinic.patient_by_mrn("LK-100003")["id"]
    page1 = world.clinic.patient_detail(world.actor(), pid)
    page3 = world.clinic.patient_detail(world.actor(), pid, invoice_page=3)
    assert page1["invoices"]["total"] == 28 and page1["invoices"]["pages"] == 3
    assert len(page1["invoices"]["items"]) == 10 and len(page3["invoices"]["items"]) == 8


def test_update_contact_validates_and_noop_is_not_an_effect(world: World):
    desk, pid = world.actor(), world.fixtures["standard"]["patient_id"]
    with pytest.raises(ValidationFailed) as bad:
        world.clinic.update_contact(desk, pid, "123", "nope", "")
    assert len(bad.value.problems) == 3
    result = world.clinic.update_contact(desk, pid, "425-555-0199", "avery@example.test", "1 Test Way")
    assert result["changed"] == ["address", "email", "phone"] or result["changed"]
    assert len(world.effects(action="patient.update_contact")) == 1
    again = world.clinic.update_contact(desk, pid, "425-555-0199", "avery@example.test", "1 Test Way")
    assert again["changed"] == []
    assert len(world.effects(action="patient.update_contact")) == 1


def test_reschedule_checks_slot_availability(world: World):
    desk, appt = world.actor(), world.fixtures["standard"]["upcoming_appointment"]
    day = "2026-03-10"
    slots = world.clinic.available_slots(appt, day)
    provider = world.clinic._appointment(appt)["provider"]
    booked = {r["starts_at"][11:16] for r in world.db.query(
        "SELECT starts_at FROM appointments WHERE provider=? AND status='scheduled' AND substr(starts_at,1,10)=?",
        (provider, day))}
    assert slots and booked and not booked & set(slots)
    world.clinic.reschedule(desk, appt, day, slots[0])
    assert world.clinic._appointment(appt)["starts_at"] == f"{day}T{slots[0]}"
    with pytest.raises(ValidationFailed):
        world.clinic.reschedule(desk, appt, "2026-03-07", "09:00")  # a Saturday
    with pytest.raises(ValidationFailed):
        world.clinic.reschedule(desk, appt, "2026-02-27", "09:00")  # in the past


def test_cancel_has_no_fee_outside_window_and_a_fee_inside(world: World):
    desk = world.actor()
    early = world.fixtures["standard"]["upcoming_appointment"]
    draft = world.clinic.review_cancel(desk, early, "patient_request")
    assert draft["review"]["warnings"] == []
    receipt = world.clinic.confirm(desk, draft["token"])
    assert receipt["receipt_number"].startswith("CXL-") and not any("fee" in line["label"].lower() and "INV" in line["value"] for line in receipt["lines"])

    late = world.fixtures["late_cancel"]["appointment"]
    draft = world.clinic.review_cancel(desk, late, "other", "patient called")
    assert draft["review"]["warnings"]
    receipt = world.clinic.confirm(desk, draft["token"])
    assert any(line["label"] == "Fee invoice" for line in receipt["lines"])
    assert world.clinic._appointment(late)["status"] == "cancelled"


def test_cancel_validation(world: World):
    desk = world.actor()
    appt = world.fixtures["standard"]["upcoming_appointment"]
    with pytest.raises(ValidationFailed):
        world.clinic.review_cancel(desk, appt, "other", "")  # notes required for Other
    with pytest.raises(ValidationFailed):
        world.clinic.review_cancel(desk, appt, "nonsense")
    with pytest.raises(NotFound):
        world.clinic.review_cancel(desk, "A-99999", "weather")


def test_confirm_twice_posts_once_while_guard_is_on(world: World):
    desk = world.actor()
    draft = world.clinic.review_claim(desk, world.fixtures["standard"]["claimable_invoice"], 12400)
    first = world.clinic.confirm(desk, draft["token"])
    with pytest.raises(AlreadyProcessed) as dup:
        world.clinic.confirm(desk, draft["token"])
    assert dup.value.result["receipt_number"] == first["receipt_number"]
    assert len(world.effects(action="claim.submit")) == 1
    assert world.audit.list(outcome="duplicate_blocked")


def test_claim_rules(world: World):
    desk = world.actor()
    with pytest.raises(ValidationFailed) as no_insurer:
        world.clinic.review_claim(desk, world.fixtures["self_pay"]["invoice"], 9500)
    assert "no insurance" in no_insurer.value.message
    claimable = world.fixtures["standard"]["claimable_invoice"]
    with pytest.raises(ValidationFailed):
        world.clinic.review_claim(desk, claimable, 999_999)
    token = world.clinic.review_claim(desk, claimable, 12400)["token"]
    world.clinic.confirm(desk, token)
    with pytest.raises(ValidationFailed):
        world.clinic.review_claim(desk, claimable, 12400)  # already claimed


def test_small_refund_issues_immediately(world: World):
    desk, inv = world.actor(), world.fixtures["standard"]["refundable_invoice"]
    draft = world.clinic.review_refund(desk, inv, 5000, "duplicate_payment")
    assert not draft["review"]["requires_approval"]
    result = world.clinic.confirm(desk, draft["token"])
    assert result["status"] == "completed"
    assert world.clinic.stats()["refunds_issued"] == 1


def test_refund_above_cap_needs_supervisor_and_moves_no_money_until_approved(world: World):
    desk, boss = world.actor(), world.actor("supervisor")
    inv = world.fixtures["big_refund"]["invoice"]
    draft = world.clinic.review_refund(desk, inv, 25_000, "billing_error")
    assert draft["review"]["requires_approval"]
    result = world.clinic.confirm(desk, draft["token"])
    assert result["status"] == "pending_approval"
    stats = world.clinic.stats()
    assert stats["refunds_issued"] == 0 and stats["refunds_pending"] == 1

    with pytest.raises(PermissionDenied):
        world.clinic.decide_approval(desk, result["receipt_number"], "approve")
    assert world.audit.list(action="approval.approve", outcome="denied")

    outcome = world.clinic.decide_approval(boss, result["receipt_number"], "approve", "ok")
    assert outcome["status"] == "approved"
    stats = world.clinic.stats()
    assert stats["refunds_issued"] == 1 and stats["refunds_pending"] == 0
    with pytest.raises(ValidationFailed):
        world.clinic.decide_approval(boss, result["receipt_number"], "approve")


def test_denying_an_approval_issues_nothing(world: World):
    desk, boss = world.actor(), world.actor("supervisor")
    draft = world.clinic.review_refund(desk, world.fixtures["big_refund"]["invoice"], 30_000, "billing_error")
    receipt = world.clinic.confirm(desk, draft["token"])["receipt_number"]
    world.clinic.decide_approval(boss, receipt, "deny", "not justified")
    assert world.clinic.stats()["refunds_issued"] == 0


def test_supervisor_refunds_above_cap_directly(world: World):
    boss = world.actor("supervisor")
    draft = world.clinic.review_refund(boss, world.fixtures["big_refund"]["invoice"], 25_000, "billing_error")
    assert not draft["review"]["requires_approval"]
    assert world.clinic.confirm(boss, draft["token"])["status"] == "completed"


def test_refund_cannot_exceed_what_was_paid(world: World):
    desk = world.actor()
    with pytest.raises(ValidationFailed):
        world.clinic.review_refund(desk, world.fixtures["standard"]["refundable_invoice"], 10**7, "billing_error")


def test_writeoff_is_supervisor_gated_at_confirm_time_not_view_time(world: World):
    desk, boss = world.actor(), world.actor("supervisor")
    inv = world.fixtures["standard"]["claimable_invoice"]
    draft = world.clinic.review_writeoff(desk, inv, 1000, "hardship")  # front desk may open the form
    with pytest.raises(PermissionDenied):
        world.clinic.confirm(desk, draft["token"])
    assert not world.effects(action="writeoff.apply")
    assert world.audit.list(action="writeoff.confirm", outcome="denied")

    draft = world.clinic.review_writeoff(boss, inv, 1000, "hardship")
    assert world.clinic.confirm(boss, draft["token"])["receipt_number"].startswith("WOF-")
    assert len(world.effects(action="writeoff.apply")) == 1


def test_a_draft_belongs_to_the_operator_who_started_it(world: World):
    token = world.clinic.review_claim(world.actor(), world.fixtures["standard"]["claimable_invoice"], 100)["token"]
    with pytest.raises(PermissionDenied):
        world.clinic.confirm(world.actor(name="frontdesk2"), token)


def test_expired_and_discarded_drafts_cannot_be_confirmed():
    from clinic.settings import Settings

    world = World(Settings(draft_ttl_seconds=0))
    desk = world.actor()
    token = world.clinic.review_claim(desk, world.fixtures["standard"]["claimable_invoice"], 100)["token"]
    with pytest.raises(ValidationFailed, match="expired"):
        world.clinic.confirm(desk, token)

    world2 = World()
    token = world2.clinic.review_claim(world2.actor(), world2.fixtures["standard"]["claimable_invoice"], 100)["token"]
    world2.clinic.discard(world2.actor(), token)
    with pytest.raises(ValidationFailed, match="cancelled"):
        world2.clinic.confirm(world2.actor(), token)


def test_with_duplicate_guard_off_a_retry_double_posts_a_refund(world: World):
    """The point of the switch: proves a platform can't rely on the target to stop a double-post."""
    desk, inv = world.actor(), world.fixtures["standard"]["refundable_invoice"]
    token = world.clinic.review_refund(desk, inv, 1000, "duplicate_payment")["token"]
    world.guard = False
    world.clinic.confirm(desk, token)
    world.clinic.confirm(desk, token)
    assert len(world.effects(action="refund.issue")) == 2
    assert world.clinic.stats()["refunds_issued"] == 2


def test_schedule_and_csv(world: World):
    rows = world.clinic.schedule(world.actor(), "2026-03-02")
    assert rows and all(r["starts_at"].startswith("2026-03-02") for r in rows)
    csv_text = world.clinic.schedule_csv(world.actor(), "2026-03-02")
    assert csv_text.splitlines()[0].startswith("appointment,time,provider")
    with pytest.raises(ValidationFailed):
        world.clinic.schedule(world.actor(), "soon")
