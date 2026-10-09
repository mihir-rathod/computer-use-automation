"""Discovery a new task from the console, in a real browser: fill in the contract, watch the model work, review, promote.
A scripted model stands in for Gemini; everything else (service, recorder, verification replay, clinic) is real."""
from __future__ import annotations

import httpx
from playwright.sync_api import expect

import runtime
from discover import service as discover_service
from tests.clinic_support import effects
from tests.test_commit_recording import ScriptedModel
from tests.test_console_discovery import DETAILS, PROPOSAL, REFUND, REQUEST, Proposal, menu_script, read_script, refund_steps

from .conftest import pytestmark  # noqa: F401


def use_model(monkeypatch, script):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(discover_service, "get_model", lambda: ScriptedModel(script))


def start_via_api(console, who, contract):
    reply = httpx.post(f"{console.base}/v1/discover", json={"contract": contract}, headers={"Authorization": f"Bearer {console.keys[who]}"}, timeout=10)
    assert reply.status_code == 200, reply.text
    return reply.json()["id"]


def test_only_supervisors_see_discovery(page, console):
    for who, sees in [("alex", False), ("dana", True), ("root", True)]:
        console.open(page, who)
        expect(page.get_by_role("heading", name="What do you want to do?")).to_be_visible()
        assert (page.locator("nav[aria-label='Primary'] a", has_text="Discover").count() == 1) is sees, who
    console.open(page, "alex", "discover/")
    expect(page.get_by_text("Discovery needs a supervisor")).to_be_visible()


def test_a_supervisor_describes_a_task_in_words_and_it_is_discovered_and_made_available(page, console, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(discover_service, "get_model", lambda: Proposal(**PROPOSAL))
    console.open(page, "dana", "discover/new/")
    expect(page.get_by_role("heading", name="Discover a new task")).to_be_visible()
    page.locator("#t-target").select_option("clinic")
    page.get_by_label("What should it do?").fill(REQUEST)
    page.get_by_role("button", name="Draft the task").click()

    expect(page.get_by_text("Check this is what you meant")).to_be_visible()
    expect(page.locator("#t-name")).to_have_value("Look up an appointment")
    expect(page.locator("#i-e0")).to_have_value("A-20002")
    expect(page.get_by_role("radio", name="It only reads")).to_be_checked()
    use_model(monkeypatch, menu_script())
    page.get_by_role("button", name="Start discovery").click()

    expect(page.get_by_text("Recorded and checked. Nobody can run it until you promote it.")).to_be_visible()
    expect(page.get_by_text("Replayed with appointment A-20002")).to_be_visible()
    expect(page.get_by_text("Read “patient”")).to_be_visible()
    assert page.locator("img.thumb").count() >= 2

    page.get_by_role("link", name="Review the recorded steps").click()
    expect(page.get_by_label("Version", exact=True)).to_contain_text("(draft)")
    expect(page.get_by_role("heading", name="Look up an appointment")).to_be_visible()
    page.go_back()

    page.get_by_role("button", name="Make it available").click()
    page.get_by_label("Reason").fill("checked the steps")
    page.get_by_role("button", name="Make it available").last.click()
    expect(page.get_by_text("This task is in use")).to_be_visible()

    console.open(page, "alex")
    expect(page.get_by_text("Look up an appointment", exact=True)).to_be_visible()


def test_the_draft_asks_a_question_when_an_example_is_missing(page, console, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(discover_service, "get_model", lambda: Proposal(name="Look up an appointment", question="Which appointment number should I use as the example?"))
    console.open(page, "dana", "discover/new/")
    page.get_by_label("What should it do?").fill("Look up an appointment for me please")
    page.get_by_role("button", name="Draft the task").click()
    expect(page.get_by_text("Which appointment number should I use as the example?")).to_be_visible()
    expect(page.get_by_text("Check this is what you meant")).to_have_count(0)


def test_the_commit_question_is_answered_in_the_console(page, console, monkeypatch):
    use_model(monkeypatch, refund_steps())
    sid = start_via_api(console, "dana", REFUND)
    console.open(page, "dana", f"discover/session/?id={sid}")
    expect(page.get_by_text("The model is about to take the final step")).to_be_visible()
    assert effects(console.clinic) == []
    page.get_by_role("button", name="Let it take the step").click()
    page.get_by_label("Reason").fill("practice system")
    page.get_by_role("button", name="Take the step", exact=True).click()
    expect(page.get_by_text("Recorded and checked.")).to_be_visible()
    expect(page.get_by_text("Final step approved by dana.")).to_be_visible()
    assert len(effects(console.clinic)) == 1


def test_a_stuck_session_leads_to_trying_again_with_the_contract_filled_in(page, console, monkeypatch):
    use_model(monkeypatch, [("give_up", lambda p: {"reasoning": "could not find the field"})])
    sid = start_via_api(console, "dana", DETAILS)
    console.open(page, "dana", f"discover/session/?id={sid}")
    expect(page.get_by_text("The model got stuck")).to_be_visible()
    expect(page.get_by_text("The model said: “could not find the field”")).to_be_visible()
    page.get_by_role("link", name="Try again with a hint").click()
    expect(page.get_by_role("heading", name="Discover it again")).to_be_visible()
    expect(page.locator("#t-name")).to_have_value("Look up an appointment")
    expect(page.locator("#i-e0")).to_have_value("A-20002")
    page.get_by_text("More details (optional)").click()
    page.get_by_label("Hint for the model").fill("the number box is the first field")
    use_model(monkeypatch, read_script())
    page.get_by_role("button", name="Start discovery").click()
    expect(page.get_by_text("Recorded and checked.")).to_be_visible()


def test_a_bad_contract_explains_what_to_fix(page, console, monkeypatch):
    use_model(monkeypatch, read_script())
    console.open(page, "dana", "discover/new/")
    page.get_by_role("button", name="I'll fill in the details myself").click()
    page.locator("#t-name").fill("Bad one")
    page.locator("#t-goal").fill("Type the password and press Continue, then read the value shown.")
    page.locator("#i-n0").fill("password")
    page.locator("#i-e0").fill("x")
    page.locator("#o-n0").fill("thing")
    page.get_by_role("button", name="Start discovery").click()
    expect(page.get_by_role("alert").filter(has_text="That did not work")).to_contain_text("password")
    assert runtime.default_store().discovery_list() == []
