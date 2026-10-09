"""The console, driven in a real browser against the real API, browser pool and clinic. Each test is something a person does."""
from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect

from api import chat_v1
from tests.clinic_support import effects
from tests.test_ui_backend import FakeModel

from .conftest import pytestmark  # noqa: F401  (skips the module when the console is not built)


def test_sign_in_rejects_a_wrong_key_and_accepts_a_real_one(page, console):
    page.goto(f"{console.base}/ui/")
    page.get_by_label("API key").fill("cua_not_a_key")
    page.get_by_role("button", name="Sign in").click()
    expect(page.locator("#key-err")).to_contain_text("unknown or revoked")

    page.get_by_label("API key").fill(console.keys["alex"])
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", name="What do you want to do?")).to_be_visible()
    expect(page.get_by_text("alex", exact=True)).to_be_visible()

    page.get_by_role("button", name="Sign out").click()
    expect(page.get_by_role("heading", name="Sign in with your API key")).to_be_visible()


def test_a_revoked_key_sends_you_back_to_sign_in(page, console):
    console.open(page, "alex")
    expect(page.get_by_role("heading", name="What do you want to do?")).to_be_visible()
    import runtime
    runtime.default_store().revoke_key("alex")
    page.reload()
    expect(page.get_by_role("heading", name="Sign in with your API key")).to_be_visible()


def test_the_home_page_is_a_task_search_grouped_by_what_it_does(page, console):
    console.open(page, "alex")
    expect(page.get_by_role("heading", name="What do you want to do?")).to_be_visible()
    expect(page.get_by_role("heading", name="Look something up")).to_be_visible()
    expect(page.get_by_role("heading", name="Change something")).to_be_visible()
    expect(page.get_by_role("heading", name="Needs an approval first")).to_be_visible()
    expect(page.get_by_text("Issue a refund on an invoice")).to_be_visible()
    assert page.get_by_text("Sign on to the clinic system").count() == 0  # sign-on is plumbing, not a task

    page.get_by_label("Search tasks").fill("patient")
    expect(page.get_by_text("Look up a patient by MRN")).to_be_visible()
    expect(page.get_by_text("Issue a refund on an invoice")).to_have_count(0)
    page.get_by_label("Search tasks").fill("zzzz")
    expect(page.get_by_text("Nothing matches that")).to_be_visible()
    console.open(page, "alex", "catalog/")                                          # the old address still lands on the home page
    expect(page.get_by_role("heading", name="What do you want to do?")).to_be_visible()


def nav_names(page):
    return [a.inner_text().split("\n")[0].strip() for a in page.locator("nav[aria-label='Primary'] a").all()]


def test_each_role_sees_only_its_own_job_in_the_navigation(page, console):
    console.open(page, "alex")
    expect(page.get_by_role("heading", name="What do you want to do?")).to_be_visible()
    assert nav_names(page) == ["Tasks", "My runs", "Inbox", "Chat"]                 # an operator: no metrics, artifacts, policy or keys

    console.open(page, "dana")
    expect(page.get_by_role("heading", name="What do you want to do?")).to_be_visible()
    assert nav_names(page) == ["Tasks", "My runs", "Inbox", "Chat", "Discover"]        # a supervisor decides things and discovers new ones; it does not manage the platform

    console.open(page, "vic")
    expect(page.get_by_role("heading", name="What do you want to do?")).to_be_visible()
    assert nav_names(page) == ["Tasks", "Runs"]

    console.open(page, "root")
    expect(page.get_by_role("heading", name="What do you want to do?")).to_be_visible()
    assert nav_names(page) == ["Tasks", "Runs", "Inbox", "Chat", "Discover", "Overview", "Artifacts", "Policy", "API keys"]
    expect(page.get_by_text("Manage", exact=True)).to_be_visible()


def test_the_metrics_overview_is_for_administrators(page, console):
    console.open(page, "alex", "overview/")
    expect(page.get_by_text("for administrators")).to_be_visible()
    console.open(page, "root", "overview/")
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()


def test_the_inbox_badge_counts_only_what_this_person_can_decide(page, console):
    console.open(page, "alex", "task/?id=clinic.issue_refund")
    page.get_by_label("Invoice").fill("INV-30001")
    page.get_by_label("Amount").fill("15.00")
    page.get_by_label("Reason").select_option("duplicate_payment")
    page.get_by_role("button", name="Run", exact=True).click()
    expect(page.get_by_role("heading", name="Waiting for your approval")).to_be_visible()  # it ran up to the refund button and stopped

    console.open(page, "sam" if "sam" in console.keys else "alex", "")
    expect(page.locator(".nav .badge")).to_have_count(0)                              # the requester cannot approve it, so nothing is waiting for them
    console.open(page, "dana", "")
    expect(page.locator(".nav .badge")).to_have_text("1")                             # a supervisor can


def test_the_form_checks_input_before_anything_is_sent_and_says_what_is_wrong(page, console):
    console.open(page, "alex", "task/?id=clinic.issue_refund")
    page.get_by_role("button", name="Run", exact=True).click()
    expect(page.get_by_text("Required").first).to_be_visible()

    page.get_by_label("Invoice").fill("nope")
    page.get_by_label("Amount").fill("12")
    page.get_by_label("Reason").select_option("billing_error")
    page.get_by_role("button", name="Run", exact=True).click()
    expect(page.get_by_text("Doesn't match the expected format, e.g. INV-00000")).to_be_visible()
    assert effects(console.clinic, "refund.request_approval") == [] and httpx.get(f"{console.base}/v1/health").status_code == 200


def test_a_read_runs_live_and_shows_steps_with_screenshots(page, console):
    console.open(page, "alex", "task/?id=clinic.patient_lookup")
    page.get_by_label("MRN").fill("LK-100002")
    page.get_by_role("button", name="Run", exact=True).click()

    expect(page.get_by_text("Succeeded").first).to_be_visible()
    expect(page.get_by_text("Pell, Jordan")).to_be_visible()
    expect(page.get_by_role("heading", name="Steps")).to_be_visible()
    expect(page.locator("img.thumb").first).to_be_visible()
    assert page.locator(".tl-item[data-status='ok']").count() >= 5


def test_a_partial_update_changes_only_what_was_filled_in(page, console):
    c = httpx.Client(base_url=console.clinic)
    c.post("/api/auth/login", json={"username": "frontdesk", "password": "desk-demo-123"})
    before = c.get("/api/patients/2").json()["patient"]

    console.open(page, "alex", "task/?id=clinic.update_patient_contact")
    page.get_by_label("MRN").fill("LK-100002")
    page.get_by_role("button", name="Run", exact=True).click()
    expect(page.get_by_text("provide at least one of").or_(page.get_by_text("Fill in at least one"))).to_be_visible()
    page.get_by_label("Phone").fill("206-555-0123")
    page.get_by_role("button", name="Run", exact=True).click()

    expect(page.get_by_text("Succeeded").first).to_be_visible()
    expect(page.get_by_text("only if “email” given")).to_be_visible()
    expect(page.locator(".tl-item[data-status='skipped']")).to_have_count(2)
    after = c.get("/api/patients/2").json()["patient"]
    assert after["phone"] == "(206) 555-0123" and (after["email"], after["address"]) == (before["email"], before["address"])


def test_a_refund_stops_at_its_step_then_a_supervisor_approves_it_with_a_reason(page, console):
    console.open(page, "alex", "task/?id=clinic.issue_refund")
    page.get_by_label("Invoice").fill("INV-30001")
    page.get_by_label("Amount").fill("15.00")
    page.get_by_label("Reason").select_option("duplicate_payment")
    expect(page.get_by_text("It runs up to the final step, then waits for a supervisor to approve it.")).to_be_visible()
    page.get_by_role("button", name="Run", exact=True).click()

    expect(page.get_by_role("heading", name="Waiting for your approval")).to_be_visible()
    assert effects(console.clinic) == []
    expect(page.get_by_role("button", name="Approve", exact=True)).to_be_disabled()      # the requester cannot approve their own run
    expect(page.get_by_text("You requested this").first).to_be_visible()

    console.open(page, "vic", "inbox/")
    expect(page.locator(".pill.wait", has_text="Needs a supervisor")).to_be_visible()
    assert page.get_by_role("button", name="Approve and run").count() == 0               # a viewer is not offered a decision
    page.get_by_text("alex").first.wait_for()

    console.open(page, "dana", "inbox/")
    expect(page.locator(".nav .badge")).to_have_text("1")
    page.get_by_role("link", name="Review and decide").click()
    page.get_by_role("button", name="Approve", exact=True).click()
    page.get_by_role("button", name="Approve it").click()                                # the dialog's confirm button, with no reason yet
    expect(page.get_by_text("A reason is required")).to_be_visible()
    page.locator("dialog[open]").get_by_label("Reason").fill("Invoice checked against the ledger")
    page.get_by_role("button", name="Approve it").click()

    expect(page.get_by_text("Succeeded").first).to_be_visible()
    expect(page.get_by_text("Yes, at step")).to_be_visible()
    expect(page.get_by_text("Invoice checked against the ledger")).to_be_visible()
    expect(page.get_by_text("approved by dana")).to_be_visible()
    assert len(effects(console.clinic)) == 1


def test_a_dry_run_is_offered_for_commits_and_changes_nothing(page, console):
    console.open(page, "alex", "task/?id=clinic.issue_refund")
    page.get_by_label("Invoice").fill("INV-30001")
    page.get_by_label("Amount").fill("15.00")
    page.get_by_label("Reason").select_option("duplicate_payment")
    page.get_by_label("Rehearse only").check()
    page.get_by_role("button", name="Rehearse").click()
    expect(page.get_by_text("Rehearsal only")).to_be_visible()
    assert effects(console.clinic) == []


def test_an_unknown_commit_outcome_is_settled_from_the_inbox(page, console):
    from tests.clinic_support import chaos
    chaos(console.clinic, "error500_after", method="POST", path_glob="/legacy/transactions/confirm")
    from tests.test_api_v1 import wait as _wait  # noqa: F401
    import runtime
    from fastapi.testclient import TestClient
    from api.app import app

    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {console.keys['alex']}"}
        run_id = c.post("/v1/runs", json={"capability_id": "clinic.issue_refund", "target": "clinic", "params": {"invoice": "INV-30001", "amount": "12.00", "reason": "duplicate_payment"}, "pause_for_human": False}, headers=h).json()["id"]
        c.post(f"/v1/runs/{run_id}/approve", json={"reason": "ok"}, headers={"Authorization": f"Bearer {console.keys['dana']}"})
        import time
        for _ in range(60):
            if c.get(f"/v1/runs/{run_id}", headers=h).json()["status"] == "needs_review":
                break
            time.sleep(1)

    console.open(page, "dana", "inbox/")
    page.get_by_role("tab", name="Unknown outcomes").click()
    expect(page.get_by_text("not retried")).to_be_visible()
    page.get_by_role("button", name="Settle this run").click()
    page.get_by_role("radio", name="It did take effect").click()
    page.locator("dialog[open]").get_by_label("Reason").fill("Refund is in the ledger")
    page.get_by_role("button", name="Record outcome").click()
    expect(page.get_by_text("You're all caught up")).to_be_visible()
    assert runtime.default_store().get(run_id)["resolution"] == "committed"


def test_a_screen_change_becomes_a_repair_that_a_person_approves_and_can_roll_back(page, console):
    from tests.clinic_support import effects as _e  # noqa: F401
    httpx.post(f"{console.clinic}/_test/drift", json={"level": 2, "seed": "ui"}, timeout=5)
    console.open(page, "alex", "task/?id=clinic.patient_lookup")
    page.get_by_label("MRN").fill("LK-100001")
    page.get_by_role("button", name="Run", exact=True).click()
    expect(page.get_by_text("This run failed")).to_be_visible()
    expect(page.get_by_text("Sign in to the system")).to_be_visible()      # sign-on is its own row, not a step of the task
    expect(page.get_by_text("Failed before the task could start")).to_be_visible()
    expect(page.get_by_text("A repair was proposed")).to_be_visible()

    page.get_by_role("link", name="Inbox", exact=True).click()
    page.get_by_role("tab", name="Repairs").click()
    expect(page.get_by_text("Clear match")).to_be_visible()
    expect(page.get_by_text("+ role: button[name='Log in']")).to_be_visible()
    page.get_by_role("button", name="Approve repair").click()
    page.locator("dialog[open]").get_by_label("Reason").fill("Sign-on button was relabelled")
    page.get_by_role("button", name="Approve and promote").click()
    expect(page.get_by_text("You're all caught up")).to_be_visible()

    console.open(page, "alex", "artifact/?id=clinic.login")
    expect(page.get_by_text("Sign-on button was relabelled").first).to_be_visible()
    expect(page.locator(".pill", has_text="current")).to_be_visible()
    page.get_by_label("From version").select_option("1.0.0")
    page.get_by_label("To version").select_option("1.0.1")
    expect(page.get_by_text("button[name='Log in']").first).to_be_visible()
    page.get_by_role("button", name="Roll back").click()
    page.locator("dialog[open]").get_by_label("Reason").fill("Back out for the test")
    page.get_by_role("button", name="Roll back").last.click()
    expect(page.get_by_text("Rolled back.")).to_be_visible()


def test_chat_keeps_your_draft_survives_navigation_and_can_be_cleared(page, console, monkeypatch):
    fake = FakeModel()
    fake.script = [("call", ("clinic__patient_lookup", {"mrn": "LK-100001"}))]
    monkeypatch.setattr(chat_v1, "get_client", lambda: fake)

    console.open(page, "alex", "chat/")
    expect(page.get_by_text("Ask for something")).to_be_visible()
    page.get_by_label("Message").fill("half a thought that I have not sent")
    page.get_by_role("link", name="My runs").click()
    expect(page.get_by_role("heading", name="My runs")).to_be_visible()
    page.get_by_role("link", name="Chat").click()
    expect(page.get_by_label("Message")).to_have_value("half a thought that I have not sent")

    page.get_by_label("Message").fill("look up patient LK-100001")
    page.get_by_role("button", name="Send").click()
    expect(page.get_by_test_id("run-card")).to_be_visible()
    expect(page.get_by_text("Brennan, Avery")).to_be_visible()
    page.get_by_role("link", name="Tasks").click()
    page.get_by_role("link", name="Chat").click()
    expect(page.get_by_text("look up patient LK-100001")).to_be_visible()      # history survived leaving the page

    page.get_by_role("button", name="Clear chat").first.click()
    page.get_by_role("button", name="Clear chat").last.click()
    expect(page.get_by_text("Ask for something")).to_be_visible()


def test_a_viewer_can_look_but_not_manage(page, console):
    console.open(page, "vic", "")
    expect(page.get_by_text("your key can't start them")).to_be_visible()
    assert page.get_by_role("link", name="API keys").count() == 0 and page.get_by_role("link", name="Policy").count() == 0
    console.open(page, "vic", "keys/")
    expect(page.get_by_text("needs an admin key")).to_be_visible()
    console.open(page, "vic", "task/?id=clinic.patient_lookup")
    page.get_by_label("MRN").fill("LK-100001")
    page.get_by_role("button", name="Run", exact=True).click()
    expect(page.get_by_text("needs the operator role").first).to_be_visible()


def test_an_admin_creates_a_key_that_is_shown_once_and_can_revoke_it(page, console):
    console.open(page, "root", "keys/")
    page.get_by_label("Name").fill("assistant.bot")
    page.get_by_label("Role").select_option("operator")
    page.get_by_role("button", name="Create key").click()
    import re
    expect(page.get_by_label("The new key")).to_have_value(re.compile(r"^cua_"))
    key = page.get_by_label("The new key").input_value()
    page.get_by_role("button", name="I've saved it").click()
    expect(page.get_by_text("assistant.bot")).to_be_visible()
    assert key not in page.content()  # the list never shows a key again

    page.get_by_role("row", name="assistant.bot").get_by_role("button", name="Revoke").click()
    expect(page.get_by_text("revoked", exact=False).first).to_be_visible()
    assert httpx.get(f"{console.base}/v1/me", headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_a_re_render_between_g_and_the_letter_does_not_lose_the_shortcut(page, console):
    """The sidebar re-renders on a timer (the inbox badge). A re-render between the two key presses used to reset the "g", so the shortcut did nothing on a slower machine."""
    console.open(page, "root", "")
    expect(page.get_by_role("heading", name="What do you want to do?")).to_be_visible()
    page.get_by_role("heading", name="What do you want to do?").click()
    page.keyboard.press("g")
    page.get_by_role("button", name="Theme", exact=False).click()      # changes the sidebar's state: a re-render between the two keys
    page.keyboard.press("r")
    expect(page.get_by_role("heading", name="Runs")).to_be_visible()


def test_keyboard_shortcuts_theme_and_the_policy_page(page, console):
    console.open(page, "root", "")
    expect(page.get_by_role("heading", name="What do you want to do?")).to_be_visible()
    page.get_by_role("heading", name="What do you want to do?").click()      # the search box is focused on arrival, and typing there is typing
    page.keyboard.press("g")
    page.keyboard.press("r")
    expect(page.get_by_role("heading", name="Runs")).to_be_visible()
    page.keyboard.press("?")
    expect(page.get_by_role("heading", name="Keyboard shortcuts")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("heading", name="Keyboard shortcuts")).to_be_hidden()

    page.get_by_role("button", name="Theme", exact=False).click()   # system -> light
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "light"
    page.reload()
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == "light"   # remembered, with no flash

    page.get_by_role("link", name="Policy").click()
    expect(page.get_by_text("Safety policy")).to_be_visible()
    expect(page.get_by_text("clinic.issue_refund")).to_be_visible()


def test_pages_show_useful_empty_and_error_states(page, console):
    console.open(page, "alex", "run/?id=run_does_not_exist")
    expect(page.get_by_text("Not found")).to_be_visible()
    console.open(page, "alex", "task/?id=clinic.nothing")
    expect(page.get_by_text("Not found")).to_be_visible()
    console.open(page, "alex", "runs/")
    expect(page.get_by_text("No runs yet")).to_be_visible()
    console.open(page, "alex", "inbox/")
    expect(page.get_by_text("You're all caught up")).to_be_visible()


def test_watching_a_run_shows_a_live_view_that_follows_the_steps(page, console):
    console.open(page, "alex", "task/?id=clinic.patient_lookup")
    page.get_by_role("radio", name="Slow").click()
    page.get_by_label("MRN").fill("LK-100002")
    page.get_by_role("button", name="Run", exact=True).click()

    live = page.locator("section[aria-label='Live view']")
    expect(live).to_be_visible()
    expect(live.get_by_text("Now:")).to_be_visible()
    expect(live.locator("img.thumb")).to_be_visible()
    expect(page.get_by_text("Watch mode is on (700 ms around each action)")).to_be_visible()
    first = live.locator(".small").first.inner_text()
    page.wait_for_function("(a) => document.querySelector(\"section[aria-label='Live view'] .small\")?.innerText !== a", arg=first)   # it moves on to the next step
    expect(page.get_by_text("Succeeded").first).to_be_visible()
    expect(live).to_have_count(0)                                                       # the live view is for while it runs; the steps keep their pictures

    page.reload()
    console.open(page, "alex", "task/?id=clinic.patient_lookup")
    expect(page.get_by_role("radio", name="Slow")).to_have_attribute("aria-checked", "true")   # the choice is remembered


def test_the_browser_window_option_only_appears_when_the_server_may_open_one(page, console, monkeypatch):
    monkeypatch.delenv("CUA_ALLOW_WINDOW", raising=False)
    console.open(page, "alex", "task/?id=clinic.patient_lookup")
    page.get_by_role("radio", name="Slow").click()
    expect(page.get_by_text("Slows the run down")).to_be_visible()
    assert page.get_by_label("Also open the browser window on the machine running the server").count() == 0

    monkeypatch.setenv("CUA_ALLOW_WINDOW", "1")
    console.open(page, "alex", "task/?id=clinic.patient_lookup")
    expect(page.get_by_label("Also open the browser window on the machine running the server")).to_be_visible()


def test_chat_has_the_same_watch_control_and_passes_it_on(page, console, monkeypatch):
    fake = FakeModel()
    fake.script = [("call", ("clinic__patient_lookup", {"mrn": "LK-100001"}))]
    monkeypatch.setattr(chat_v1, "get_client", lambda: fake)
    console.open(page, "alex", "chat/")
    page.get_by_role("radio", name="Slow").click()
    page.get_by_label("Message").fill("look up patient LK-100001")
    page.get_by_role("button", name="Send").click()
    expect(page.get_by_test_id("run-card")).to_be_visible()
    import runtime
    run = runtime.default_store().list_runs(limit=1)[0]
    assert run["pace_ms"] == 700 and run["requested_by"] == "alex"
    expect(page.get_by_text("Brennan, Avery")).to_be_visible()
