"""Real-browser tests of the legacy skin, including the properties that make it hard to automate."""
from __future__ import annotations

import httpx
from playwright.sync_api import expect

from .conftest import audit


def sign_in(page, base, user="frontdesk", password="desk-demo-123"):
    page.goto(f"{base}/legacy/login")
    page.locator('input[name="username"]').fill(user)
    page.locator('input[name="password"]').fill(password)
    page.get_by_role("button", name="Sign In").click()
    expect(page.get_by_text("Main menu").first).to_be_visible()


def test_inputs_have_no_accessible_names_so_role_locators_find_nothing(page, clinic):
    sign_in(page, clinic)
    page.get_by_role("link", name="Patient lookup").click()
    expect(page.get_by_role("textbox", name="MRN")).to_have_count(0)
    expect(page.locator('input[name="mrn"]')).to_have_count(1)


def test_find_a_patient_and_see_ambiguous_row_links(page, clinic):
    sign_in(page, clinic)
    page.get_by_role("link", name="Patient lookup").click()
    page.locator('input[name="mrn"]').fill("LK-100003")
    page.get_by_role("button", name="Search").click()
    page.get_by_role("link", name="Select").click()
    expect(page.get_by_text("Patient record")).to_be_visible()
    assert page.get_by_role("link", name="Refund").count() > 1  # same text on every row


def test_cancel_through_function_entry_and_refresh_does_not_double_post(page, clinic):
    sign_in(page, clinic)
    page.get_by_role("link", name="Cancel appointment").click()
    page.locator('input[name="number"]').fill("A-20002")
    page.get_by_role("button", name="Continue").click()
    page.locator('select[name="reason"]').select_option("weather")
    page.get_by_role("button", name="Continue").click()
    expect(page.get_by_text("cannot be reversed")).to_be_visible()
    page.get_by_role("button", name="Confirm").click()
    expect(page.get_by_text("APPOINTMENT CANCELLED")).to_be_visible()
    page.reload()
    expect(page.get_by_text("ALREADY PROCESSED")).to_be_visible()
    assert len(audit(clinic, action="appointment.cancel", effects_only=True)) == 1
    assert audit(clinic, outcome="duplicate_blocked")


def test_unknown_number_is_record_not_found(page, clinic):
    sign_in(page, clinic)
    page.get_by_role("link", name="Cancel appointment").click()
    page.locator('input[name="number"]').fill("A-99999")
    page.get_by_role("button", name="Continue").click()
    expect(page.get_by_text("RECORD NOT FOUND")).to_be_visible()


def test_expired_session_redirects_to_login_with_a_message(page, clinic):
    sign_in(page, clinic)
    httpx.post(f"{clinic}/_test/chaos", json={"kind": "expire_session", "path_glob": "/legacy/patients*"})
    page.get_by_role("link", name="Patient lookup").click()
    expect(page.get_by_text("YOUR SESSION HAS TIMED OUT")).to_be_visible()


def test_maintenance_page_continue_goes_to_the_menu_not_back(page, clinic):
    sign_in(page, clinic)
    httpx.post(f"{clinic}/_test/chaos", json={"kind": "maintenance", "path_glob": "/legacy/patients*"})
    page.get_by_role("link", name="Patient lookup").click()
    expect(page.get_by_text("SCHEDULED MAINTENANCE IN PROGRESS")).to_be_visible()
    page.get_by_role("link", name="Continue").click()
    expect(page).to_have_url(f"{clinic}/legacy/menu")


def test_drift_level_three_renames_form_fields_but_the_app_still_works(page, clinic):
    httpx.post(f"{clinic}/_test/drift", json={"level": 3, "seed": "e2e"})
    page.goto(f"{clinic}/legacy/login")
    expect(page.locator('input[name="username"]')).to_have_count(0)
    inputs = page.locator("input[type=text], input[type=password]")
    inputs.nth(0).fill("frontdesk")
    inputs.nth(1).fill("desk-demo-123")
    page.get_by_role("button", name="Continue to portal").click()
    expect(page.get_by_text("Main menu").first).to_be_visible()
