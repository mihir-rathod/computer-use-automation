"""A refund, seen from the console: it stops just before it commits, shows up in the Inbox, and a supervisor reads the page before approving or rejecting."""
from __future__ import annotations

import time

import httpx
import pytest
from playwright.sync_api import expect

from tests.clinic_support import effects

from .conftest import pytestmark  # noqa: F401

REFUND = {"invoice": "INV-30001", "amount": "15.00", "reason": "duplicate_payment"}


def headers(console, who):
    return {"Authorization": f"Bearer {console.keys[who]}"}


def start_refund(console, who):
    """A refund asked for by `who`, stopped at its irreversible step."""
    run = httpx.post(f"{console.base}/v1/runs", json={"capability_id": "clinic.issue_refund", "params": REFUND, "target": "clinic"}, headers=headers(console, who), timeout=10).json()["id"]
    end = time.time() + 60
    while time.time() < end:
        body = httpx.get(f"{console.base}/v1/runs/{run}", headers=headers(console, "vic"), timeout=10).json()
        if body["awaiting_approval"]:
            return run
        if body["status"] not in ("queued", "running"):
            raise AssertionError(f"the refund settled without waiting for an approval: {body['status']} {body.get('error_code')} {(body.get('result') or {}).get('error')}")
        time.sleep(0.3)
    raise AssertionError(f"the refund never reached its approval: {body['status']}")


@pytest.fixture
def waiting_refund(console):
    run = start_refund(console, "alex")
    yield run
    # a run left waiting would hold a browser for the next test: reject it, whatever the test did, and let it end before the next test starts
    httpx.post(f"{console.base}/v1/runs/{run}/approval/reject", json={"reason": "test cleanup"}, headers=headers(console, "dana"), timeout=10)
    end = time.time() + 30
    while time.time() < end and httpx.get(f"{console.base}/v1/runs/{run}", headers=headers(console, "vic"), timeout=10).json()["status"] in ("queued", "running"):
        time.sleep(0.2)


def test_the_inbox_shows_a_run_stopped_at_its_step_and_the_run_page_shows_the_page(page, console, waiting_refund):
    console.open(page, "dana", "inbox/")
    expect(page.get_by_text("stopped at the step")).to_be_visible()
    expect(page.get_by_text("Needs a supervisor")).to_be_visible()
    page.get_by_role("link", name="Review and decide").click()

    expect(page.get_by_role("heading", name="Waiting for your approval")).to_be_visible()
    expect(page.get_by_text("Needs approval", exact=True)).to_be_visible()
    expect(page.get_by_text("INV-30001").first).to_be_visible()  # what the page says
    expect(page.get_by_role("img", name="The page the run is stopped on")).to_be_visible()
    assert effects(console.clinic) == []


def test_a_supervisor_approves_and_the_refund_posts_once(page, console, waiting_refund):
    console.open(page, "dana", f"run/?id={waiting_refund}")
    page.get_by_role("button", name="Approve", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Reason").fill("invoice checked")
    dialog.get_by_role("button", name="Approve it").click()

    expect(page.get_by_text("Succeeded")).to_be_visible()
    expect(page.get_by_text("invoice checked")).to_be_visible()  # recorded with the approval
    assert len(effects(console.clinic)) == 1


def test_a_rejection_commits_nothing(page, console, waiting_refund):
    console.open(page, "dana", f"run/?id={waiting_refund}")
    page.get_by_role("button", name="Reject", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Reason").fill("wrong invoice")
    dialog.get_by_role("button", name="Reject").click()

    expect(page.get_by_text("This run failed")).to_be_visible()
    expect(page.get_by_text("dana rejected it").first).to_be_visible()
    assert effects(console.clinic) == []


def test_the_person_who_asked_and_an_operator_cannot_decide_a_refund(page, console, waiting_refund):
    console.open(page, "alex", f"run/?id={waiting_refund}")
    expect(page.get_by_role("heading", name="Waiting for your approval")).to_be_visible()
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_disabled()
    expect(page.get_by_text("You requested this, so someone else has to approve it.")).to_be_visible()

    console.open(page, "sam", f"run/?id={waiting_refund}")
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_disabled()
    expect(page.get_by_text("This needs a supervisor")).to_be_visible()


def test_a_viewer_sees_that_it_is_waiting_but_cannot_decide(page, console, waiting_refund):
    console.open(page, "vic", f"run/?id={waiting_refund}")
    expect(page.get_by_text("Waiting for a supervisor to approve")).to_be_visible()
    assert page.get_by_role("button", name="Approve", exact=True).count() == 0


def test_a_supervisor_does_the_step_on_the_page_and_hands_it_back(page, console, waiting_refund):
    """The approver works on the run's own browser: the controls list the page's elements, and a click is done for real."""
    console.open(page, "dana", f"run/?id={waiting_refund}")
    expect(page.get_by_role("heading", name="What you can do on the page")).to_be_hidden()  # a label, not a heading: the list itself is what matters
    page.get_by_role("button", name="Click Confirm").click()
    expect(page.get_by_text("That worked.")).to_be_visible()
    assert len(effects(console.clinic)) == 1  # the person's click posted it

    page.get_by_role("button", name="I did it myself").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Reason").fill("confirmed the refund myself")
    dialog.get_by_role("button", name="I did it").click()

    expect(page.get_by_text("Succeeded")).to_be_visible()
    expect(page.get_by_text("confirmed the refund myself")).to_be_visible()
    assert len(effects(console.clinic)) == 1  # and the run did not click it again


def test_an_admin_can_approve_a_refund_they_asked_for_themselves(page, console):
    run = start_refund(console, "root")
    console.open(page, "root", f"run/?id={run}")
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_enabled()
    expect(page.get_by_text("As an admin you may decide your own request.")).to_be_visible()
    page.get_by_role("button", name="Approve", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Reason").fill("my own refund, checked")
    dialog.get_by_role("button", name="Approve it").click()
    expect(page.get_by_text("Succeeded")).to_be_visible()
    assert len(effects(console.clinic)) == 1


def test_an_operator_is_not_offered_the_controls_for_a_supervisors_step(page, console, waiting_refund):
    console.open(page, "sam", f"run/?id={waiting_refund}")
    expect(page.get_by_role("button", name="Click Confirm")).to_be_disabled()
    expect(page.get_by_role("button", name="I did it myself")).to_be_disabled()
    expect(page.get_by_text("This needs a supervisor")).to_be_visible()


def test_leaving_the_page_and_coming_back_finds_the_approval_still_waiting(page, console, waiting_refund):
    """The waiting run is on the server, not in the tab: go elsewhere, come back by the Inbox, the browser's Back button or the link, and the same page is there to decide on."""
    console.open(page, "dana", f"run/?id={waiting_refund}")
    expect(page.get_by_role("heading", name="Waiting for your approval")).to_be_visible()
    page.get_by_role("button", name="Click Confirm").wait_for()

    page.get_by_role("link", name="My runs").click()
    expect(page.get_by_role("heading", name="My runs")).to_be_visible()
    page.go_back()  # the browser's Back button
    expect(page.get_by_role("heading", name="Waiting for your approval")).to_be_visible()
    expect(page.get_by_role("img", name="The page the run is stopped on")).to_be_visible()

    page.get_by_role("link", name="Inbox").click()  # or by the Inbox
    page.get_by_role("link", name="Review and decide").click()
    expect(page.get_by_role("heading", name="Waiting for your approval")).to_be_visible()
    assert effects(console.clinic) == []  # nothing was committed by leaving


def test_clicking_a_link_asks_first_and_leaving_the_page_is_flagged(page, console, waiting_refund):
    """Main menu takes the run's browser away from the page with the Confirm button. The console asks before it does that, and says so if it happened."""
    console.open(page, "dana", f"run/?id={waiting_refund}")
    page.get_by_role("button", name="Click Main menu").click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("The run cannot carry on from another page")).to_be_visible()
    dialog.get_by_role("button", name="Stay here").click()
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_enabled()  # nothing happened

    page.get_by_role("button", name="Click Main menu").click()
    page.get_by_role("dialog").get_by_role("button", name="Leave the page").click()
    expect(page.get_by_text("You are no longer on the page the run stopped on")).to_be_visible()
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_disabled()  # approving could not work from here
    assert effects(console.clinic) == []

    page.get_by_role("button", name="Resume automation").click()  # the run goes back through its earlier steps to the page it stopped on
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_enabled()  # only once it asks again: until then the buttons are off
    expect(page.get_by_text("You are no longer on the page the run stopped on")).to_be_hidden()
    assert effects(console.clinic) == []

    page.get_by_role("button", name="Approve", exact=True).click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Reason").fill("back on the right page")
    dialog.get_by_role("button", name="Approve it").click()
    expect(page.get_by_text("Succeeded")).to_be_visible()
    assert len(effects(console.clinic)) == 1
