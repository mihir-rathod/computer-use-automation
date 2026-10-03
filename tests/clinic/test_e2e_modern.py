"""Real-browser tests of the modern React skin. Needs `npm run build` in clinic/modern."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from .conftest import audit

DIST = Path(__file__).resolve().parents[2] / "clinic" / "modern" / "dist" / "index.html"
pytestmark = pytest.mark.skipif(not DIST.exists(), reason="modern skin not built (npm run build in clinic/modern)")

DESK = ("frontdesk", "desk-demo-123")


def sign_in(page, base, creds=DESK):
    page.goto(f"{base}/app/login")
    page.get_by_label("Username").fill(creds[0])
    page.get_by_label("Password").fill(creds[1])
    page.get_by_role("button", name="Sign In").click()
    expect(page.get_by_role("heading", name="Patient lookup")).to_be_visible()


def open_patient(page, mrn="LK-100001", name="Avery Brennan"):
    page.get_by_label("MRN").fill(mrn)
    page.get_by_role("link", name=f"Open {name}, {mrn}").click()
    expect(page.get_by_role("heading", name=name.split()[1] + ", " + name.split()[0])).to_be_visible()


def test_search_as_you_type_then_empty_state(page, clinic):
    sign_in(page, clinic)
    page.get_by_label("MRN").fill("LK-100001")
    expect(page.get_by_role("cell", name="Brennan, Avery")).to_be_visible()
    page.get_by_label("MRN").fill("LK-999999")
    expect(page.get_by_text("No patients matched your search.")).to_be_visible()


def test_update_contact_through_a_modal_and_toast(page, clinic):
    sign_in(page, clinic)
    open_patient(page)
    page.get_by_role("button", name="Update contact").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Phone").fill("425-555-0123")
    dialog.get_by_role("button", name="Save changes").click()
    expect(page.get_by_role("status").get_by_text("Contact information updated")).to_be_visible()
    expect(page.get_by_text("(425) 555-0123")).to_be_visible()
    rows = audit(clinic, action="patient.update_contact", effects_only=True)
    assert len(rows) == 1 and rows[0]["skin"] == "modern"


def test_cancel_appointment_review_confirm_receipt(page, clinic):
    sign_in(page, clinic)
    open_patient(page)
    page.get_by_role("button", name="Cancel appointment A-20002").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Reason").select_option(label="Patient request")
    dialog.get_by_role("button", name="Continue").click()
    expect(dialog.get_by_text("Confirm appointment cancellation")).to_be_visible()
    dialog.get_by_role("button", name="Confirm").click()
    expect(dialog.get_by_text("APPOINTMENT CANCELLED")).to_be_visible()
    expect(dialog.get_by_text("CXL-000001")).to_be_visible()
    dialog.get_by_role("button", name="Done").click()
    expect(page.get_by_role("row", name="A-20002").get_by_text("cancelled")).to_be_visible()
    rows = audit(clinic, action="appointment.cancel", effects_only=True)
    assert len(rows) == 1 and rows[0]["skin"] == "modern"


def test_cancel_review_back_does_not_post(page, clinic):
    sign_in(page, clinic)
    open_patient(page)
    page.get_by_role("button", name="Cancel appointment A-20002").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Reason").select_option(label="Weather")
    dialog.get_by_role("button", name="Continue").click()
    dialog.get_by_role("button", name="Back").click()
    expect(dialog.get_by_label("Reason")).to_be_visible()
    assert not audit(clinic, action="appointment.cancel", effects_only=True)


def test_reschedule_loads_slots_asynchronously(page, clinic):
    sign_in(page, clinic)
    open_patient(page)
    page.get_by_role("button", name="Reschedule appointment A-20002").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("New date").fill("2026-03-12")
    expect(dialog.get_by_label("New time")).to_be_visible()
    dialog.get_by_role("button", name="Save changes").click()
    expect(page.get_by_role("status").get_by_text("moved to 2026-03-12")).to_be_visible()
    assert len(audit(clinic, action="appointment.reschedule", effects_only=True)) == 1


def test_schedule_csv_is_a_real_download(page, clinic):
    sign_in(page, clinic)
    page.get_by_role("link", name="Today's schedule").click()
    expect(page.get_by_role("cell", name="A-20007")).to_be_visible()
    with page.expect_download() as info:
        page.get_by_role("button", name="Download CSV").click()
    download = info.value
    assert download.suggested_filename == "schedule-2026-03-02.csv"
    assert Path(download.path()).read_text().splitlines()[0].startswith("appointment,time,provider")


def test_session_expiry_shows_a_modal_and_signing_in_resumes(page, clinic):
    sign_in(page, clinic)
    httpx.post(f"{clinic}/_test/chaos", json={"kind": "expire_session", "path_glob": "/api/patients*"})
    page.get_by_label("MRN").fill("LK-100001")
    dialog = page.get_by_role("dialog", name="Your session has expired")
    expect(dialog).to_be_visible()
    dialog.get_by_label("Password").fill(DESK[1])
    dialog.get_by_role("button", name="Sign In").click()
    expect(dialog).to_have_count(0)
    expect(page.get_by_role("cell", name="Brennan, Avery")).to_be_visible()


def test_label_drift_changes_what_the_page_says(page, clinic):
    httpx.post(f"{clinic}/_test/drift", json={"level": 2, "seed": "e2e"})
    page.goto(f"{clinic}/app/login")
    page.get_by_label("Username").fill(DESK[0])
    page.get_by_label("Password").fill(DESK[1])
    page.get_by_role("button", name="Log in").click()
    expect(page.get_by_role("link", name="Find a patient")).to_be_visible()
    expect(page.get_by_role("link", name="Patient lookup")).to_have_count(0)


def test_front_desk_sees_no_approvals_link_but_supervisor_does(page, clinic):
    sign_in(page, clinic)
    expect(page.get_by_role("link", name="Approvals")).to_have_count(0)
    page.get_by_role("button", name="Sign out").click()
    sign_in(page, clinic, ("supervisor", "super-demo-123"))
    expect(page.get_by_role("link", name="Approvals")).to_be_visible()
