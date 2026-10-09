"""A run started from chat that needs a person says so in the chat, and says where to go."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from api import chat_v1
from artifacts_lib import storage
from tests.test_ui_backend import FakeModel

from .conftest import pytestmark  # noqa: F401

REFUND_CALL = ("call", ("clinic__issue_refund", {"invoice": "INV-30001", "amount": "15.00", "reason": "duplicate_payment"}))


def say(page, text):
    page.get_by_label("Message").fill(text)
    page.get_by_role("button", name="Send").click()


def test_a_refund_from_chat_tells_an_admin_to_review_and_decide(page, console, monkeypatch):
    fake = FakeModel()
    fake.script = [REFUND_CALL]
    monkeypatch.setattr(chat_v1, "get_client", lambda: fake)
    console.open(page, "root", "chat/")
    say(page, "refund 15 dollars on INV-30001, duplicate payment")

    card = page.get_by_test_id("run-card")
    expect(card.get_by_text("it needs a supervisor")).to_be_visible()  # it ran up to the refund button and stopped
    expect(card.get_by_text("Needs approval", exact=True)).to_be_visible()
    card.get_by_role("link", name="Review and decide").click()
    expect(page.get_by_role("heading", name="Waiting for your approval")).to_be_visible()


def test_a_refund_from_chat_tells_an_operator_it_is_not_theirs_to_decide(page, console, monkeypatch):
    fake = FakeModel()
    fake.script = [REFUND_CALL]
    monkeypatch.setattr(chat_v1, "get_client", lambda: fake)
    console.open(page, "alex", "chat/")
    say(page, "refund 15 dollars on INV-30001, duplicate payment")

    card = page.get_by_test_id("run-card")
    expect(card.get_by_text("it needs a supervisor")).to_be_visible()
    expect(card.get_by_text("You can't decide this one")).to_be_visible()
    expect(card.get_by_role("link", name="See the run")).to_be_visible()
    assert card.get_by_role("link", name="Review and decide").count() == 0


def test_a_run_from_chat_that_gets_stuck_says_so_and_links_to_the_take_over(page, console, monkeypatch):
    adir = Path(os.environ["ARTIFACTS_DIR"])
    path = adir / "clinic.patient_lookup" / f"{storage.current_version('clinic.patient_lookup', adir)}.json"
    raw = json.loads(path.read_text())
    raw["steps"][1]["target"]["locators"] = [{"strategy": "css", "value": "#this-field-was-renamed"}]  # the screen "changed"
    path.write_text(json.dumps(raw))
    fake = FakeModel()
    fake.script = [("call", ("clinic__patient_lookup", {"mrn": "LK-100002"}))]
    monkeypatch.setattr(chat_v1, "get_client", lambda: fake)
    console.open(page, "alex", "chat/")
    say(page, "look up patient LK-100002")

    card = page.get_by_test_id("run-card")
    expect(card.get_by_text("Stuck at step s2: it needs a person to take over")).to_be_visible()
    expect(card.get_by_text("Needs a person", exact=True)).to_be_visible()
    card.get_by_role("link", name="Take over").click()
    expect(page.get_by_role("heading", name="This run needs a person")).to_be_visible()

    run = page.url.split("id=")[1]  # leave nothing waiting for the next test
    headers = {"Authorization": f"Bearer {console.keys['alex']}"}
    httpx.post(f"{console.base}/v1/runs/{run}/escalation/stop", headers=headers, timeout=10)
    end = time.time() + 30
    while time.time() < end and httpx.get(f"{console.base}/v1/runs/{run}", headers=headers, timeout=10).json()["status"] in ("queued", "running"):
        time.sleep(0.2)
