"""A stuck run, seen from the console: it shows up in the Inbox, a person takes over on the run page, types what the page needed, and hands it back."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from artifacts_lib import storage

from .conftest import pytestmark  # noqa: F401


@pytest.fixture
def stuck_run(console):
    """patient_lookup with its typing step pointed at a locator that no longer exists, started by alex and left waiting."""
    adir = Path(os.environ["ARTIFACTS_DIR"])
    path = adir / "clinic.patient_lookup" / f"{storage.current_version('clinic.patient_lookup', adir)}.json"
    raw = json.loads(path.read_text())
    raw["steps"][1]["target"]["locators"] = [{"strategy": "css", "value": "#this-field-was-renamed"}]
    path.write_text(json.dumps(raw))
    headers = {"Authorization": f"Bearer {console.keys['alex']}"}
    run = httpx.post(f"{console.base}/v1/runs", json={"capability_id": "clinic.patient_lookup", "params": {"mrn": "LK-100002"}, "target": "clinic"}, headers=headers, timeout=10).json()["id"]
    yield run
    # a run left waiting would hold the only slot for the next test: stop it, whatever the test did
    httpx.post(f"{console.base}/v1/runs/{run}/escalation/stop", headers=headers, timeout=10)


def test_a_stuck_run_is_found_in_the_inbox_and_fixed_by_taking_over(page, console, stuck_run):
    console.open(page, "alex", "inbox/")
    expect(page.get_by_role("tab", name="Waiting for a person")).to_have_attribute("aria-selected", "true")
    expect(page.get_by_text("It could not do step s2")).to_be_visible()
    page.get_by_role("link", name="Take over").click()

    expect(page.get_by_role("heading", name="This run needs a person")).to_be_visible()
    expect(page.get_by_text("Stuck at step s2")).to_be_visible()
    expect(page.get_by_text("Needs a person", exact=True)).to_be_visible()
    page.get_by_label("Text for mrn").fill("LK-100002")
    page.get_by_role("button", name="Type into mrn").click()
    expect(page.get_by_text("That worked.")).to_be_visible()
    page.get_by_role("button", name="Hand it back").click()

    expect(page.get_by_text("Succeeded")).to_be_visible()
    expect(page.get_by_text("Pell, Jordan")).to_be_visible()


def test_a_person_can_stop_a_stuck_run(page, console, stuck_run):
    console.open(page, "alex", f"run/?id={stuck_run}")
    expect(page.get_by_role("heading", name="This run needs a person")).to_be_visible()
    page.get_by_role("button", name="Stop the run").click()
    page.get_by_role("dialog").get_by_role("button", name="Stop the run").click()
    expect(page.get_by_text("Failed")).to_be_visible()


def test_a_viewer_sees_that_a_run_needs_a_person_but_cannot_take_over(page, console, stuck_run):
    console.open(page, "vic", f"run/?id={stuck_run}")
    expect(page.get_by_text("An operator can take over from this page.")).to_be_visible()
    assert page.get_by_role("button", name="Hand it back").count() == 0
    httpx.post(f"{console.base}/v1/runs/{stuck_run}/escalation/stop", headers={"Authorization": f"Bearer {console.keys['alex']}"}, timeout=10)
