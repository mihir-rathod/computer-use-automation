"""The endpoints the console depends on: timeline, screenshots, inbox, policy, key admin, run filters, and the chat."""
from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from google.genai import types

import runtime
from api import chat_v1
from api.app import app
from tests.clinic_support import chaos, copy_artifacts, reset
from tests.test_api_v1 import submit, wait  # noqa: F401  (shared helpers)


@pytest.fixture
def api(tmp_path, monkeypatch, clinic_base_url):
    monkeypatch.setenv("ARTIFACTS_DIR", str(copy_artifacts(tmp_path)))
    monkeypatch.setitem(runtime.TARGET_PROFILES["clinic"], "base_url", clinic_base_url)
    monkeypatch.setitem(runtime.TARGET_PROFILES["clinic_supervisor"], "base_url", clinic_base_url)
    reset(clinic_base_url)
    store = runtime.default_store()
    keys = {n: store.create_key(n, r) for n, r in [("alex", "operator"), ("dana", "supervisor"), ("vic", "viewer"), ("root", "admin")]}
    with TestClient(app) as client:
        client.h = lambda who: {"Authorization": f"Bearer {keys[who]}"}
        yield client
    httpx.delete(f"{clinic_base_url}/_test/chaos", timeout=5)
    httpx.delete(f"{clinic_base_url}/_test/drift", timeout=5)


# ---- timeline and screenshots ---------------------------------------------------------------------------------------------

def test_timeline_has_every_step_with_outcome_and_a_screenshot_each(api):
    run_id = submit(api, "alex", "clinic.update_patient_contact", {"mrn": "LK-100002", "phone": "206-555-0166"}).json()["id"]
    wait(api, run_id)
    tl = api.get(f"/v1/runs/{run_id}/timeline", headers=api.h("vic")).json()

    statuses = {s["step_id"]: s["status"] for s in tl["steps"]}
    assert [s["n"] for s in tl["steps"]] == list(range(1, len(tl["steps"]) + 1))
    assert list(statuses.values()).count("skipped") == 2 and "failed" not in statuses.values()  # email and address were not supplied
    shots = [s["screenshot"] for s in tl["steps"] if s["status"] == "ok"]
    assert all(shots) and len(set(shots)) == len(shots)
    assert tl["success_expectation"] and "CONTACT INFORMATION UPDATED" in tl["success_expectation"]
    first = next(s for s in tl["steps"] if s["screenshot"])
    assert api.get(f"/v1/runs/{run_id}/screenshots/{first['screenshot']}", headers=api.h("alex")).headers["content-type"] == "image/png"


def test_a_failed_step_shows_what_was_expected_against_what_happened(api, clinic_base_url):
    httpx.post(f"{clinic_base_url}/_test/drift", json={"level": 2, "seed": "tl"}, timeout=5)
    run_id = submit(api, "alex", "clinic.patient_lookup", {"mrn": "LK-100001"}).json()["id"]
    wait(api, run_id)
    # sign-on itself breaks first under label drift, so the failing step belongs to the sign-on capability; the run reports that
    run = api.get(f"/v1/runs/{run_id}", headers=api.h("vic")).json()
    assert run["status"] == "hard_failure" and run["error_code"] == "login_failed"

    httpx.delete(f"{clinic_base_url}/_test/drift", timeout=5)
    chaos(clinic_base_url, "error500", method="GET", path_glob="/legacy/patients", remaining=None)
    bad = submit(api, "alex", "clinic.patient_lookup", {"mrn": "LK-100001"}).json()["id"]
    wait(api, bad)
    failed = [s for s in api.get(f"/v1/runs/{bad}/timeline", headers=api.h("vic")).json()["steps"] if s["status"] == "failed"]
    assert failed and failed[0]["actual"] and failed[0]["error_code"]


def test_screenshots_are_for_operators_and_cannot_be_used_to_read_other_files(api):
    run_id = submit(api, "alex", "clinic.patient_lookup", {"mrn": "LK-100001"}).json()["id"]
    wait(api, run_id)
    name = api.get(f"/v1/runs/{run_id}/timeline", headers=api.h("vic")).json()["screenshots"][0]
    assert api.get(f"/v1/runs/{run_id}/screenshots/{name}", headers=api.h("vic")).status_code == 403
    assert api.get(f"/v1/runs/{run_id}/screenshots/{name}").status_code == 401
    for bad in ("..%2f..%2fREADME.md", "001.png%00.txt", "log.jsonl", "001.PNG", "../log.jsonl"):
        assert api.get(f"/v1/runs/{run_id}/screenshots/{bad}", headers=api.h("alex")).status_code == 404
    assert api.get("/v1/runs/run_nope/screenshots/001.png", headers=api.h("alex")).status_code == 404


# ---- inbox, filters, policy, keys ----------------------------------------------------------------------------------------

def test_inbox_collects_everything_waiting_for_a_person(api, clinic_base_url):
    assert api.get("/v1/inbox", headers=api.h("vic")).json()["counts"]["total"] == 0
    refund = {"invoice": "INV-30001", "amount": "10.00", "reason": "duplicate_payment"}
    pending = submit(api, "alex", "clinic.issue_refund", refund).json()["id"]
    chaos(clinic_base_url, "error500_after", method="POST", path_glob="/legacy/transactions/confirm")
    lost = submit(api, "alex", "clinic.issue_refund", {**refund, "amount": "11.00"}).json()["id"]
    api.post(f"/v1/runs/{lost}/approve", json={"reason": "ok"}, headers=api.h("dana"))
    wait(api, lost)
    httpx.post(f"{clinic_base_url}/_test/drift", json={"level": 2, "seed": "ib"}, timeout=5)
    wait(api, submit(api, "alex", "clinic.patient_lookup", {"mrn": "LK-100001"}).json()["id"])

    inbox = api.get("/v1/inbox", headers=api.h("vic")).json()
    assert [a["run_id"] for a in inbox["approvals"]] == [pending] and inbox["approvals"][0]["tier"] == "supervisor"
    assert [r["id"] for r in inbox["needs_review"]] == [lost]
    [repair] = inbox["repairs"]
    assert repair["confident"] and repair["new_locators"] and repair["has_screenshot"] and repair["capability_id"] == "clinic.login"
    assert inbox["counts"] == {"approvals": 1, "needs_review": 1, "repairs": 1, "discovery_commits": 0, "paused": 0, "total": 3}
    assert api.get(f"/v1/repairs/{repair['id']}/screenshot", headers=api.h("alex")).headers["content-type"] == "image/png"
    assert api.get(f"/v1/repairs/{repair['id']}/screenshot", headers=api.h("vic")).status_code == 403


def test_run_list_filters(api):
    a = submit(api, "alex", "clinic.patient_lookup", {"mrn": "LK-100001"}).json()["id"]
    b = submit(api, "dana", "clinic.patient_lookup", {"mrn": "LK-999999"}).json()["id"]
    wait(api, a), wait(api, b)
    ids = lambda **q: {r["id"] for r in api.get("/v1/runs", params=q, headers=api.h("vic")).json()["runs"]}  # noqa: E731
    assert ids() == {a, b} and ids(requested_by="alex") == {a} and ids(status="business_outcome") == {b}
    assert ids(q="dana") == {b} and ids(q=a[-8:]) == {a} and ids(capability_id="clinic.issue_refund") == set()
    assert len(ids(limit=1)) == 1 and ids(limit=1, offset=1) != ids(limit=1)


def test_policy_view_is_for_operators_and_has_no_secrets(api):
    assert api.get("/v1/policy", headers=api.h("vic")).status_code == 403
    body = api.get("/v1/policy", headers=api.h("alex")).json()
    assert body["capabilities"]["clinic.issue_refund"]["approval"] == "supervisor" and "supervisor" in body["approval_tiers"]
    assert body["evidence"]["screenshots"] in ("errors", "every_action") and "email" in body["redaction"]["patterns"]


def test_key_admin_is_admin_only_shows_a_key_once_and_cannot_lock_you_out(api):
    assert api.get("/v1/keys", headers=api.h("dana")).status_code == 403
    made = api.post("/v1/keys", json={"name": "new.person", "role": "operator"}, headers=api.h("root"))
    assert made.status_code == 201 and made.json()["key"].startswith("cua_")
    listing = api.get("/v1/keys", headers=api.h("root")).json()["keys"]
    assert "new.person" in [k["name"] for k in listing] and made.json()["key"] not in str(listing) and "key_hash" not in str(listing)
    assert api.get("/v1/me", headers={"Authorization": f"Bearer {made.json()['key']}"}).json()["role"] == "operator"
    assert api.post("/v1/keys/root/revoke", headers=api.h("root")).status_code == 409
    assert api.post("/v1/keys/new.person/revoke", headers=api.h("root")).json()["revoked"] == 1
    assert api.get("/v1/me", headers={"Authorization": f"Bearer {made.json()['key']}"}).status_code == 401
    assert api.post("/v1/keys", json={"name": "x", "role": "god"}, headers=api.h("root")).status_code == 422


def test_the_console_is_served_from_the_api_when_built(api):
    from api.app import UI_DIR

    if not UI_DIR.is_dir():
        pytest.skip("ui/ has not been built (npm run build in ui/)")
    assert api.get("/ui/").status_code == 200 and "Capability Console" in api.get("/ui/").text
    assert api.get("/ui/run/").status_code == 200 and api.get("/", follow_redirects=False).headers["location"] == "/ui/"


# ---- chat --------------------------------------------------------------------------------------------------------------------

class FakeModel:
    """Stands in for Gemini: answers with a scripted function call or text."""

    def __init__(self):
        self.script: list = []
        self.seen: list = []
        self.instructions: list = []

    def generate(self, contents, tools=None, system_instruction=None, tool_choice="AUTO"):
        self.seen.append(contents)
        self.instructions.append(system_instruction)
        kind, payload = self.script.pop(0)
        part = types.Part.from_function_call(name=payload[0], args=payload[1]) if kind == "call" else types.Part.from_text(text=payload)
        return SimpleNamespace(candidates=[SimpleNamespace(content=types.Content(role="model", parts=[part]))])


@pytest.fixture
def model(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(chat_v1, "get_client", lambda: fake)
    return fake


def say(api, who, text):
    return api.post("/v1/chat", json={"message": text}, headers=api.h(who))


def test_chat_starts_a_real_run_under_the_callers_key_and_remembers_the_conversation(api, model):
    model.script = [("call", ("clinic__patient_lookup", {"mrn": "LK-100002"}))]
    out = say(api, "alex", "look up patient LK-100002").json()["messages"]

    assert [m["role"] for m in out] == ["user", "assistant"] and out[1]["run_id"]
    done = wait(api, out[1]["run_id"])
    assert done["status"] == "success" and done["requested_by"] == "alex" and done["result"]["outputs"]["patient_name"] == "Pell, Jordan"
    saved = api.get("/v1/chat", headers=api.h("alex")).json()["messages"]
    assert [m["role"] for m in saved] == ["user", "assistant"] and saved[1]["run_id"] == out[1]["run_id"]
    assert api.get("/v1/chat", headers=api.h("dana")).json()["messages"] == []  # each person's chat is their own


def test_chat_asks_instead_of_guessing_and_refuses_invented_values(api, model):
    model.script = [("text", "Which patient? I need their MRN."), ("call", ("clinic__update_patient_contact", {"mrn": "LK-100002", "phone": "206-555-0188", "email": "unknown"}))]
    assert "MRN" in say(api, "alex", "look someone up").json()["messages"][1]["text"]
    refused = say(api, "alex", "change the phone for LK-100002 to 206-555-0188").json()["messages"][1]
    assert "won't guess" in refused["text"] and "email" in refused["text"] and refused["run_id"] is None
    assert runtime.default_store().list_runs() == []
    # the earlier turns are given to the model, so an answer to its question makes sense
    assert len(model.seen[1]) > 1


def test_chat_tells_the_model_the_format_each_input_must_have(api):
    # found live: shown only a type, the model wrote "4PM" and "03/04/2026" for inputs that must be 16:00 and 2026-03-04, and the run was refused
    tools, _ = chat_v1.build_tools()
    reschedule = next(d for d in tools[0].function_declarations if d.name == "clinic__reschedule_appointment")
    props = reschedule.parameters.properties
    assert props["time"].pattern == "^[0-9]{2}:[0-9]{2}$" and "^[0-9]{2}:[0-9]{2}$" in props["time"].description
    assert "^[0-9]{4}-[0-9]{2}-[0-9]{2}$" in props["date"].description


def test_chat_tells_the_model_todays_date(api, model):
    # found live: asked for "tomorrow" with no date to go on, the model wrote a date a year in the past, which fits the pattern, so nothing refused it
    model.script = [("text", "ok")]
    say(api, "alex", "move A-20007 to tomorrow at 4pm")
    assert f"Today is {datetime.now():%A}, {datetime.now():%Y-%m-%d}" in model.instructions[0]


def test_chat_never_bypasses_approval_and_cannot_approve(api, model):
    model.script = [("call", ("clinic__issue_refund", {"invoice": "INV-30001", "amount": "10.00", "reason": "duplicate_payment"}))]
    reply = say(api, "alex", "refund 10 dollars on INV-30001, duplicate payment").json()["messages"][1]
    run = api.get(f"/v1/runs/{reply['run_id']}", headers=api.h("vic")).json()
    assert run["status"] == "pending_approval"
    assert api.post(f"/v1/runs/{reply['run_id']}/approve", json={"reason": "x"}, headers=api.h("alex")).status_code == 403


def test_chat_can_be_cleared_and_a_viewer_cannot_start_runs(api, model):
    model.script = [("text", "hello")]
    say(api, "alex", "hi")
    assert api.delete("/v1/chat", headers=api.h("alex")).json()["cleared"] == 2
    assert api.get("/v1/chat", headers=api.h("alex")).json()["messages"] == []
    assert say(api, "vic", "hi").status_code == 403 and api.get("/v1/chat", headers=api.h("vic")).status_code == 200


def test_a_model_outage_is_reported_politely(api, monkeypatch):
    class Down:
        def generate(self, *a, **k):
            raise RuntimeError("503")

    monkeypatch.setattr(chat_v1, "get_client", lambda: Down())
    out = say(api, "alex", "hi").json()["messages"]
    assert "couldn't reach" in out[1]["text"] and "catalog" in out[1]["text"]


def test_a_sign_on_failure_is_not_blamed_on_the_tasks_own_steps(api, clinic_base_url):
    """Found by looking at the console: the failing sign-on step id (s4) was matched to the requested task's step s4, so 'Click Select' was
    shown as failed, and not-reached steps showed the sign-on's screenshots."""
    httpx.post(f"{clinic_base_url}/_test/drift", json={"level": 2, "seed": "so"}, timeout=5)
    run_id = submit(api, "alex", "clinic.patient_lookup", {"mrn": "LK-100001"}).json()["id"]
    wait(api, run_id)
    tl = api.get(f"/v1/runs/{run_id}/timeline", headers=api.h("vic")).json()

    assert tl["sign_on"]["status"] == "failed" and tl["sign_on"]["capability_id"] == "clinic.login" and tl["sign_on"]["screenshot"]
    assert {s["status"] for s in tl["steps"]} == {"not_run"}  # none of the task's own steps ran, and none is blamed
    assert all(s["screenshot"] is None for s in tl["steps"])
