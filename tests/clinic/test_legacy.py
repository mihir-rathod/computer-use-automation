from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from clinic.app import create_app
from clinic.settings import Settings


@pytest.fixture
def app():
    return create_app(Settings())


def login(app, user="frontdesk", password="desk-demo-123") -> TestClient:
    c = TestClient(app, follow_redirects=False)
    r = c.post("/legacy/login", data={"username": user, "password": password})
    assert r.status_code == 303 and r.headers["location"] == "/legacy/menu"
    return c


def token_of(html: str) -> str:
    return re.search(r'name="txn" value="([0-9a-f]+)"', html).group(1)


def test_redirects_to_login_and_shows_timeout_message(app):
    c = TestClient(app, follow_redirects=False)
    assert c.get("/legacy/menu").headers["location"] == "/legacy/login"
    assert "YOUR SESSION HAS TIMED OUT" in c.get("/legacy/login?reason=timeout").text
    bad = c.post("/legacy/login", data={"username": "frontdesk", "password": "nope"})
    assert "Invalid user ID or password." in bad.text


def test_menu_hides_approvals_from_front_desk_but_not_supervisor(app):
    assert "Approvals" not in login(app).get("/legacy/menu").text
    assert "Approvals" in login(app, "supervisor", "super-demo-123").get("/legacy/menu").text


def test_patient_search_results_pagination_and_empty(app):
    c = login(app)
    assert "No patients matched your search." in c.get("/legacy/patients?mrn=LK-999999").text
    assert "Please correct the following:" in c.get("/legacy/patients?dob=bad").text
    small = login(create_app(Settings(page_size=2)))
    first = small.get("/legacy/patients?last=o").text
    assert "Page 1 of 3" in first and "Next" in first and "Prev" not in first
    second = small.get("/legacy/patients?last=o&page=2").text
    assert "Page 2 of 3" in second and "Next" in second and "Prev" in second
    assert 'href="/legacy/patients/' in c.get("/legacy/patients?mrn=LK-100001").text


def test_inputs_are_unlabeled_and_tables_have_no_header_cells(app):
    html = login(app).get("/legacy/patients?mrn=LK-100001").text
    assert "<label" not in html and "<th" not in html


def test_cancel_flow_review_confirm_receipt_and_duplicate_submit(app):
    c = login(app)
    appt = app.state.world.fixtures["standard"]["upcoming_appointment"]
    assert c.post("/legacy/fn/cancel", data={"number": appt}).headers["location"] == f"/legacy/appointments/{appt}/cancel"
    assert c.get(f"/legacy/appointments/{appt}/cancel").status_code == 200
    review = c.post("/legacy/transactions/cancel/review", data={"number": appt, "reason": "weather", "notes": ""}).text
    assert "Confirm appointment cancellation" in review and "cannot be reversed" in review
    token = token_of(review)
    receipt = c.post("/legacy/transactions/confirm", data={"txn": token}).text
    assert "APPOINTMENT CANCELLED" in receipt and "CXL-" in receipt
    again = c.post("/legacy/transactions/confirm", data={"txn": token}).text
    assert "ALREADY PROCESSED" in again
    assert len(app.state.world.audit.list(action="appointment.cancel", effects_only=True)) == 1


def test_validation_errors_come_back_on_the_same_form(app):
    c = login(app)
    inv = app.state.world.fixtures["self_pay"]["invoice"]
    html = c.post("/legacy/transactions/claim/review", data={"number": inv, "amount": "95.00"}).text
    assert "Please correct the following:" in html and "no insurance" in html
    html = c.post("/legacy/transactions/refund/review", data={"number": inv, "amount": "abc", "reason": ""}).text
    assert "Amount must be a number." in html
    assert "RECORD NOT FOUND" in c.post("/legacy/fn/claim", data={"number": "INV-00000"}).text


def test_refund_above_cap_then_supervisor_approves(app):
    desk, boss = login(app), login(app, "supervisor", "super-demo-123")
    inv = app.state.world.fixtures["big_refund"]["invoice"]
    review = desk.post("/legacy/transactions/refund/review",
                       data={"number": inv, "amount": "250.00", "reason": "billing_error", "notes": ""}).text
    assert "need supervisor approval" in review
    receipt = desk.post("/legacy/transactions/confirm", data={"txn": token_of(review)}).text
    assert "REFUND SENT FOR SUPERVISOR APPROVAL" in receipt
    number = re.search(r"APR-\d+", receipt).group(0)
    assert "SUPERVISOR AUTHORIZATION REQUIRED" in desk.get("/legacy/approvals").text
    assert number in boss.get("/legacy/approvals").text
    done = boss.post("/legacy/approvals/decide", data={"number": number, "decision": "Approve", "note": "ok"}).text
    assert f"APPROVAL {number} APPROVED" in done
    assert app.state.world.clinic.stats()["refunds_issued"] == 1


def test_writeoff_denied_for_front_desk_at_confirm(app):
    c = login(app)
    inv = app.state.world.fixtures["standard"]["claimable_invoice"]
    review = c.post("/legacy/transactions/writeoff/review", data={"number": inv, "amount": "10.00", "reason": "hardship"}).text
    assert "Write-offs require supervisor authorization." in review
    denied = c.post("/legacy/transactions/confirm", data={"txn": token_of(review)}).text
    assert "SUPERVISOR AUTHORIZATION REQUIRED" in denied
    assert not app.state.world.audit.list(action="writeoff.apply", effects_only=True)


def test_reschedule_and_contact_update(app):
    c = login(app)
    appt = app.state.world.fixtures["standard"]["upcoming_appointment"]
    slots_page = c.get(f"/legacy/appointments/{appt}/reschedule?date=2026-03-12").text
    slot = re.search(r'<option value="(\d\d:\d\d)"', slots_page).group(1)
    done = c.post(f"/legacy/appointments/{appt}/reschedule", data={"date": "2026-03-12", "time": slot}).text
    assert "APPOINTMENT RESCHEDULED" in done
    pid = app.state.world.fixtures["standard"]["patient_id"]
    bad = c.post(f"/legacy/patients/{pid}/contact", data={"phone": "1", "email": "x", "address": ""}).text
    assert "Phone number is not valid." in bad
    ok = c.post(f"/legacy/patients/{pid}/contact", data={"phone": "425-555-0100", "email": "a@example.test", "address": "9 Test Rd"}).text
    assert "CONTACT INFORMATION UPDATED" in ok


def test_drift_levels_change_what_an_automation_sees_but_the_app_keeps_working(app):
    from clinic.ui import UI

    world = app.state.world
    world.ui = UI(3, "run-a")
    c = TestClient(app, follow_redirects=False)
    page = c.get("/legacy/login").text
    assert 'name="username"' not in page and "Log in" not in page or "Continue to portal" in page
    name_user, name_pw = world.ui.n("username"), world.ui.n("password")
    r = c.post("/legacy/login", data={name_user: "frontdesk", name_pw: "desk-demo-123"})
    assert r.status_code == 303
    menu = c.get("/legacy/menu").text
    assert "Patient lookup" not in menu and "Patients" in menu
