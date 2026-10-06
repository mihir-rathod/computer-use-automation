"""Discovery a new task from the console, end to end and offline: a scripted stand-in for the model decides *what to do next*; the session service,
the contract, the commit gate, the recorder, the verification replay, the draft storage and the API are all the production code paths.
The clinic's own audit log is the ground truth for what really changed."""
from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

import runtime
from api.app import app
from artifacts_lib import storage
from discover import service as discover_service
from tests.clinic_support import copy_artifacts, effects, reset
from tests.test_commit_recording import ScriptedModel, ref

DETAILS = {
    "task_name": "appointment_details", "name": "Look up an appointment", "description": "Reads who and when an appointment is with",
    "target": "clinic", "start_path": "/legacy/fn/cancel",
    "goal": "On the Cancel appointment entry page type the appointment number and press Continue. On the page that follows, read the patient and the provider. Do not cancel anything.",
    "inputs": [{"name": "appointment", "example": "A-20002", "pattern": "^A-[0-9]{5}$"}],
    "outputs": [{"name": "patient"}, {"name": "provider"}],
    "success_text": "Provider", "outcomes": [{"when_text": "RECORD NOT FOUND", "outcome": "not_found"}],
    "effect": "read_only", "verify": {"inputs": {"appointment": "A-20001"}},
}


def read_script(patient_pattern=r'cell "[A-Za-z]+, ', provider_pattern=r'cell "(Dr|[A-Z])'):
    return [
        ("type_text", lambda p: {"ref": ref(p, r'textbox.*(name="number"|no\.")'), "text": "A-20002"}),
        ("click", lambda p: {"ref": ref(p, r'button "Continue"')}),
        ("extract", lambda p: {"ref": ref(p, patient_pattern), "output_name": "patient"}),
        ("extract", lambda p: {"ref": ref(p, provider_pattern), "output_name": "provider"}),
        ("finish", lambda p: {"reasoning": "read both"}),
    ]


@pytest.fixture
def discover(tmp_path, monkeypatch, clinic_base_url):
    monkeypatch.setenv("ARTIFACTS_DIR", str(copy_artifacts(tmp_path)))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setitem(runtime.TARGET_PROFILES["clinic"], "base_url", clinic_base_url)
    monkeypatch.setattr(runtime, "EVIDENCE_ROOT", tmp_path / "evidence")
    fixtures = reset(clinic_base_url)
    store = runtime.default_store()
    keys = {n: store.create_key(n, r) for n, r in [("alex", "operator"), ("dana", "supervisor"), ("root", "admin")]}
    with TestClient(app) as client:
        client.h = lambda who: {"Authorization": f"Bearer {keys[who]}"}
        client.use = lambda script: monkeypatch.setattr(discover_service, "get_model", lambda: ScriptedModel(script))
        client.adir = tmp_path / "artifacts"
        client.fixtures = fixtures
        yield client


def wait_for(api, sid, until, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        body = api.get(f"/v1/discover/{sid}", headers=api.h("dana")).json()
        if body["status"] in until:
            return body
        time.sleep(0.3)
    raise AssertionError(f"session did not reach {until}: {body['status']} {body.get('error')}")


def start(api, contract=None, who="dana"):
    return api.post("/v1/discover", json={"contract": contract or DETAILS}, headers=api.h(who))


def test_a_read_only_task_is_taught_verified_and_left_as_a_draft(discover):
    discover.use(read_script())
    first = start(discover)
    assert first.status_code == 200, first.text
    done = wait_for(discover, first.json()["id"], ("ready", "needs_attention", "failed", "stuck"))
    assert done["status"] == "ready", (done["error"], done["verify"], done["lint"])
    assert done["verify"]["passed"] is True and done["verify"]["inputs"] == {"appointment": "A-20001"}
    assert [t["tool"] for t in done["turns"]][-1] == "finish" and any(t["screenshot"] for t in done["turns"])
    # a draft: it exists, but nothing can run it yet
    cid = "clinic.appointment_details"
    assert storage.list_versions(cid, discover.adir) == ["1.0.0"] and storage.current_version(cid, discover.adir) is None
    assert cid not in [a.capability_id for a in storage.list_artifacts(discover.adir)]
    assert effects(discover_base(), "appointment.cancel") == []


def discover_base():
    return runtime.TARGET_PROFILES["clinic"]["base_url"]


REFUND = {
    "task_name": "small_refund", "name": "Issue a small refund", "description": "Refunds part of a paid invoice", "target": "clinic", "start_path": "/legacy/fn/refund",
    "goal": "From the Issue refund page enter the invoice number and press Continue, enter the amount, choose the reason, press Continue, then Confirm. Read the receipt number.",
    "inputs": [{"name": "invoice", "example": "INV-30001", "pattern": "^INV-[0-9]{5}$"}, {"name": "amount", "example": "15.00", "pattern": "^[0-9]+(\\.[0-9]{2})?$"},
               {"name": "reason", "example": "duplicate_payment", "choices": ["duplicate_payment", "billing_error"]}],
    "outputs": [{"name": "receipt_number"}], "success_text": "Receipt number", "success_status": "issued",
    "effect": "irreversible", "commit_approval": "supervisor",
}


def refund_steps(upto=None):
    steps = [
        ("type_text", lambda p: {"ref": ref(p, r'textbox.*(name="number"|no\.")'), "text": "INV-30001"}),
        ("click", lambda p: {"ref": ref(p, r'button "Continue"')}),
        ("type_text", lambda p: {"ref": ref(p, r'textbox.*name="amount"'), "text": "15.00"}),
        ("select_option", lambda p: {"ref": ref(p, r'combobox.*name="reason"'), "value": "duplicate_payment"}),
        ("click", lambda p: {"ref": ref(p, r'button "Continue"')}),
        ("click", lambda p: {"ref": ref(p, r'button "Confirm"')}),
        ("extract", lambda p: {"ref": ref(p, r'cell "RFD-'), "output_name": "receipt_number"}),
        ("finish", lambda p: {"reasoning": "refund issued and receipt read"}),
    ]
    return steps[:upto] if upto else steps


def test_a_read_only_task_can_never_commit_even_if_the_model_tries(discover):
    base = discover_base()
    script = [
        ("type_text", lambda p: {"ref": ref(p, r'textbox.*(name="number"|no\.")'), "text": "A-20002"}),
        ("click", lambda p: {"ref": ref(p, r'button "Continue"')}),
        ("select_option", lambda p: {"ref": ref(p, r'combobox.*name="reason"'), "value": "Weather"}),
        ("click", lambda p: {"ref": ref(p, r'button "Continue"')}),
        ("click", lambda p: {"ref": ref(p, r'button "Confirm"')}),
        ("give_up", lambda p: {"reasoning": "was blocked"}),
    ]
    discover.use(script)
    done = wait_for(discover, start(discover).json()["id"], ("ready", "needs_attention", "failed", "stuck"))
    assert done["status"] == "stuck" and done["error"] == "the model gave up"
    assert effects(base, "appointment.cancel") == [] and storage.list_versions("clinic.appointment_details", discover.adir) == []


def test_a_task_that_misses_an_output_is_not_saved(discover):
    discover.use(read_script()[:3] + [("finish", lambda p: {"reasoning": "done"})])
    done = wait_for(discover, start(discover).json()["id"], ("ready", "needs_attention", "failed", "stuck"))
    assert done["status"] == "needs_attention" and "provider" in done["error"]
    assert storage.list_versions("clinic.appointment_details", discover.adir) == []


def test_verification_catches_a_task_that_only_works_for_the_example(discover):
    discover.use(read_script())
    wrong = {**DETAILS, "verify": {"inputs": {"appointment": "A-99999"}}}  # no such appointment: success is not what happens
    done = wait_for(discover, start(discover, wrong).json()["id"], ("ready", "needs_attention", "failed", "stuck"))
    assert done["status"] == "needs_attention" and done["verify"]["passed"] is False and done["verify"]["status"] == "business_outcome"
    assert done["draft_url"] and storage.list_versions("clinic.appointment_details", discover.adir) == ["1.0.0"]
    # ...while naming the normal answer that second set should end in passes
    discover.use(read_script())
    expected = {**DETAILS, "verify": {"inputs": {"appointment": "A-99999"}, "expect_outcome": "not_found"}}
    again = wait_for(discover, start(discover, expected).json()["id"], ("ready", "needs_attention", "failed", "stuck"))
    assert again["status"] == "ready" and again["version"] == "1.0.1"


def test_a_taught_task_can_be_promoted_and_then_run_by_an_operator(discover):
    discover.use(read_script())
    sid = start(discover).json()["id"]
    wait_for(discover, sid, ("ready",))
    assert discover.post(f"/v1/discover/{sid}/promote", json={"reason": "looks right"}, headers=discover.h("alex")).status_code == 403
    promoted = discover.post(f"/v1/discover/{sid}/promote", json={"reason": "looks right"}, headers=discover.h("dana"))
    assert promoted.status_code == 200 and promoted.json()["status"] == "promoted"
    assert storage.current_version("clinic.appointment_details", discover.adir) == "1.0.0"
    run = discover.post("/v1/runs", json={"capability_id": "clinic.appointment_details", "params": {"appointment": "A-20001"}, "target": "clinic"}, headers=discover.h("alex"))
    assert run.status_code == 202, run.text
    end = time.time() + 60
    while time.time() < end:
        body = discover.get(f"/v1/runs/{run.json()['id']}", headers=discover.h("alex")).json()
        if body["status"] in ("success", "hard_failure", "business_outcome"):
            break
        time.sleep(0.3)
    assert body["status"] == "success" and body["result"]["outputs"]["provider"]
    assert discover.post(f"/v1/discover/{sid}/promote", json={"reason": "again"}, headers=discover.h("dana")).status_code == 409


def test_a_draft_can_be_discarded_and_leaves_nothing_behind(discover):
    discover.use(read_script())
    sid = start(discover).json()["id"]
    wait_for(discover, sid, ("ready",))
    assert discover.post(f"/v1/discover/{sid}/discard", headers=discover.h("dana")).json()["status"] == "discarded"
    assert storage.list_versions("clinic.appointment_details", discover.adir) == []
    assert not (discover.adir / "clinic.appointment_details").exists()


def test_a_commit_step_waits_for_a_supervisor_and_is_recorded_once(discover):
    base = discover_base()
    discover.use(refund_steps())
    sid = start(discover, REFUND).json()["id"]
    waiting = wait_for(discover, sid, ("awaiting_commit",))
    assert "Confirm" in waiting["commit_request"]["description"]
    assert discover.get("/v1/inbox", headers=discover.h("alex")).json()["discovery_commits"][0]["id"] == sid
    assert effects(base) == []  # nothing posted while the question is open
    assert discover.post("/v1/discover", json={"contract": {**REFUND, "task_name": "other_refund"}}, headers=discover.h("root")).status_code == 409  # one at a time
    assert discover.post(f"/v1/discover/{sid}/commit", json={"approve": True, "reason": "sandbox"}, headers=discover.h("alex")).status_code == 403
    assert discover.post(f"/v1/discover/{sid}/commit", json={"approve": True, "reason": "it is the sandbox"}, headers=discover.h("dana")).status_code == 200
    assert discover.post(f"/v1/discover/{sid}/commit", json={"approve": False, "reason": "too late"}, headers=discover.h("dana")).status_code == 409
    done = wait_for(discover, sid, ("ready", "needs_attention", "failed", "stuck"))
    assert done["status"] == "ready", done
    assert len(effects(base)) == 1
    artifact = storage.load_artifact_by_id("clinic.small_refund", discover.adir, version="1.0.0")
    assert [(a.approver, a.mode) for a in artifact.provenance.commit_approvals] == [("dana", "supervised")]
    assert done["verify"] == {"ran": False} and "Not verified" in artifact.provenance.note and "dana" in artifact.provenance.note


def test_a_declined_commit_posts_nothing(discover):
    base = discover_base()
    discover.use(refund_steps(6) + [("give_up", lambda p: {"reasoning": "declined"})])
    sid = start(discover, REFUND).json()["id"]
    wait_for(discover, sid, ("awaiting_commit",))
    discover.post(f"/v1/discover/{sid}/commit", json={"approve": False, "reason": "not now"}, headers=discover.h("dana"))
    done = wait_for(discover, sid, ("stuck", "ready", "failed", "needs_attention"))
    assert done["status"] == "stuck" and effects(base) == []


def test_cancelling_while_a_commit_is_waiting_ends_the_session_without_posting(discover):
    base = discover_base()
    discover.use(refund_steps(6) + [("give_up", lambda p: {"reasoning": "stopped"})])
    sid = start(discover, REFUND).json()["id"]
    wait_for(discover, sid, ("awaiting_commit",))
    assert discover.post(f"/v1/discover/{sid}/cancel", headers=discover.h("dana")).status_code == 200
    done = wait_for(discover, sid, ("cancelled", "stuck", "failed", "ready"))
    assert done["status"] in ("cancelled", "stuck") and effects(base) == []
    # and a new session can start straight away
    discover.use(read_script())
    assert start(discover).status_code == 200


def test_contracts_are_refused_before_anything_runs(discover):
    assert start(discover, who="alex").status_code == 403
    for bad, text in [({"inputs": [{"name": "password", "example": "x"}]}, "password"),
                      ({"inputs": [{"name": "appointment", "example": "nope", "pattern": "^A-[0-9]+$"}]}, "pattern"),
                      ({"start_path": "https://evil.example/x"}, "path"),
                      ({"outputs": []}, "read-only"),
                      ({"verify": {"inputs": {}}}, "required input"),
                      ({"task_name": "login"}, "sign-on"),
                      ({"target": "nowhere"}, "target")]:
        reply = start(discover, {**DETAILS, **bad})
        assert reply.status_code in (409, 422) and text in reply.text, (bad, reply.text)
    assert runtime.default_store().discovery_list() == []
    exists = start(discover, {**DETAILS, "task_name": "patient_lookup"})
    assert exists.status_code == 409 and "already exists" in exists.text


def test_without_a_model_key_nothing_starts(discover, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    reply = start(discover)
    assert reply.status_code == 503 and "GEMINI_API_KEY" in reply.text


def test_sessions_left_active_by_a_dead_server_are_closed(discover):
    store = runtime.default_store()
    row = store.discovery_create("dana", "clinic.x_y", "clinic", "{}")
    store.discovery_update(row["id"], status="running")
    discover_service.DiscoveryService(store, discover.adir, runtime.default_policy()).recover()
    assert store.discovery_get(row["id"])["status"] == "failed" and "restarted" in store.discovery_get(row["id"])["error"]


def test_the_policy_tier_for_promotion_applies_only_to_tasks_that_commit(discover):
    """An unlisted task defaults to supervisor approval for its irreversible step; promoting one that only reads is still an operator's call."""
    discover.use(read_script())
    reads = wait_for(discover, start(discover).json()["id"], ("ready",))
    assert discover.post(f"/v1/artifacts/{reads['capability_id']}/promote/{reads['version']}", json={"reason": "reads only"}, headers=discover.h("alex")).status_code == 200

    discover.use(refund_steps())
    sid = start(discover, REFUND).json()["id"]
    wait_for(discover, sid, ("awaiting_commit",))
    discover.post(f"/v1/discover/{sid}/commit", json={"approve": True, "reason": "sandbox"}, headers=discover.h("dana"))
    commits = wait_for(discover, sid, ("ready",))
    path = f"/v1/artifacts/{commits['capability_id']}/promote/{commits['version']}"
    assert discover.post(path, json={"reason": "x"}, headers=discover.h("alex")).status_code == 403
    assert discover.post(path, json={"reason": "x"}, headers=discover.h("dana")).status_code == 200


# ---- the plain-words flow: describe it, the system drafts the contract, discovery starts from the main page ---------------------

from types import SimpleNamespace  # noqa: E402

from google.genai import types  # noqa: E402


class Proposal:
    """A stand-in for the model that answers the drafting call with a fixed proposal."""

    def __init__(self, **args):
        self.args = args

    def generate(self, contents, tools=None, system_instruction=None):
        part = types.Part.from_function_call(name="propose_task", args=self.args)
        return SimpleNamespace(candidates=[SimpleNamespace(content=types.Content(role="model", parts=[part]))])


PROPOSAL = {
    "name": "Look up an appointment", "task_name": "Appointment Details", "description": "Reads who an appointment is with",
    "goal": "Open the Cancel appointment function, type the appointment number and press Continue. Read the patient and the provider, and change nothing.",
    "inputs": [{"name": "appointment", "example": "A-20002"}], "outputs": [{"name": "patient"}, {"name": "provider"}], "effect": "read_only",
}
REQUEST = "Look up appointment A-20002 and tell me the patient and the provider"


def menu_script():
    return [
        ("click", lambda p: {"ref": ref(p, r'link "Cancel appointment"')}),
        *read_script(),
    ]


def test_a_request_in_plain_words_becomes_a_draft_contract(discover, monkeypatch):
    monkeypatch.setattr(discover_service, "get_model", lambda: Proposal(**PROPOSAL))
    reply = discover.post("/v1/discover/draft", json={"target": "clinic", "request": REQUEST}, headers=discover.h("dana"))
    assert reply.status_code == 200, reply.text
    c = reply.json()["contract"]
    assert c["task_name"] == "appointment_details" and c["effect"] == "read_only" and c["start_path"] is None and c["success_text"] is None
    assert c["inputs"][0]["example"] == "A-20002" and c["verify"] == {"inputs": {}, "expect_outcome": None, "same_as_example": True}
    assert runtime.default_store().discovery_list() == []  # a draft is only a proposal: nothing started, nothing saved


def test_the_draft_asks_instead_of_inventing_an_example(discover, monkeypatch):
    monkeypatch.setattr(discover_service, "get_model", lambda: Proposal(name="Look up an appointment", question="Which appointment number should I use as the example?"))
    reply = discover.post("/v1/discover/draft", json={"target": "clinic", "request": "Look up an appointment for me"}, headers=discover.h("dana"))
    assert reply.json() == {"question": "Which appointment number should I use as the example?"}


def test_an_unreadable_effect_is_treated_as_the_cautious_one_and_secrets_are_refused(discover, monkeypatch):
    monkeypatch.setattr(discover_service, "get_model", lambda: Proposal(**{**PROPOSAL, "effect": "whatever"}))
    drafted = discover.post("/v1/discover/draft", json={"target": "clinic", "request": REQUEST}, headers=discover.h("dana")).json()["contract"]
    assert drafted["effect"] == "irreversible" and drafted["verify"] is None  # it cannot be re-checked with its own values
    monkeypatch.setattr(discover_service, "get_model", lambda: Proposal(**{**PROPOSAL, "inputs": [{"name": "password", "example": "x"}]}))
    refused = discover.post("/v1/discover/draft", json={"target": "clinic", "request": REQUEST}, headers=discover.h("dana"))
    assert refused.status_code == 422 and "password" in refused.text
    assert discover.post("/v1/discover/draft", json={"target": "clinic", "request": REQUEST}, headers=discover.h("alex")).status_code == 403


def test_a_drafted_task_is_discovered_from_the_main_page_and_decides_its_own_success_text(discover, monkeypatch):
    monkeypatch.setattr(discover_service, "get_model", lambda: Proposal(**PROPOSAL))
    contract = discover.post("/v1/discover/draft", json={"target": "clinic", "request": REQUEST}, headers=discover.h("dana")).json()["contract"]
    discover.use(menu_script())
    done = wait_for(discover, start(discover, contract).json()["id"], ("ready", "needs_attention", "failed", "stuck"))
    assert done["status"] == "ready", done
    artifact = storage.load_artifact_by_id("clinic.appointment_details", discover.adir, version="1.0.0")
    assert artifact.steps[0].params["url"] == "/legacy/menu" and artifact.success_checkpoint.value not in ("", "(decided after discovery)")
    assert "A-20002" not in artifact.success_checkpoint.value  # not something that is only on the page for the example
    assert done["verify"]["passed"] and done["verify"]["inputs"] == {"appointment": "A-20002"}
    assert "chose from the page" in artifact.provenance.note and "without the model" in artifact.provenance.note


def test_re_checking_with_its_own_values_is_refused_for_a_task_that_changes_things(discover):
    bad = {**REFUND, "verify": {"same_as_example": True}}
    reply = start(discover, bad)
    assert reply.status_code == 422 and "twice" in reply.text


def test_wandering_is_left_out_of_the_recording(discover, monkeypatch):
    """The model opens a wrong page, fails an action, goes back to the start and does it properly: only that last pass is recorded and it replays."""
    monkeypatch.setattr(discover_service, "get_model", lambda: Proposal(**PROPOSAL))
    contract = discover.post("/v1/discover/draft", json={"target": "clinic", "request": REQUEST}, headers=discover.h("dana")).json()["contract"]
    wander = [
        ("click", lambda p: {"ref": ref(p, r'link "Today')}),
        ("click", lambda p: {"ref": "does-not-exist"}),
        ("click", lambda p: {"ref": ref(p, r'link "Main menu"')}),   # back to the start by clicking, which is all a model without addresses can do
    ]
    discover.use(wander + menu_script())
    done = wait_for(discover, start(discover, contract).json()["id"], ("ready", "needs_attention", "failed", "stuck"))
    assert done["status"] == "ready", done
    artifact = storage.load_artifact_by_id("clinic.appointment_details", discover.adir, version="1.0.0")
    assert done["steps"] == len(artifact.steps) == 6 and "left out of the recording" in artifact.provenance.note  # navigate, click, type, click, two reads
    assert done["verify"]["passed"]


def test_a_recording_that_never_uses_its_input_is_not_ready(discover):
    """The model finds the answer by browsing the schedule, so the recording reads the same cell whatever it is asked: the replay 'works' and the answer is wrong."""
    browse = [
        ("navigate", lambda p: {"url": "/legacy/schedule?date=2026-03-04"}),
        ("extract", lambda p: {"ref": ref(p, r'cell "Brennan'), "output_name": "patient"}),
        ("extract", lambda p: {"ref": ref(p, r'cell "Dr\. A\. Okafor"'), "output_name": "provider"}),
        ("finish", lambda p: {"reasoning": "found it in the schedule"}),
    ]
    discover.use(browse)
    done = wait_for(discover, start(discover).json()["id"], ("ready", "needs_attention", "failed", "stuck"))
    assert done["status"] == "needs_attention" and "never uses the input 'appointment'" in done["error"], done
    assert done["verify"]["passed"] is True  # the replay alone could not see the problem: that is why this check exists


# ---- edge cases found while making discovery work on an unfamiliar system -----------------------------------------------------

def find_script(number="A-20002", typo=None, extra_read=False):
    steps = [("click", lambda p: {"ref": ref(p, r'link "Find appointment"')})]
    if typo:
        steps.append(("type_text", lambda p: {"ref": ref(p, r'textbox.*no\.'), "text": typo}))
    steps += [
        ("type_text", lambda p: {"ref": ref(p, r'textbox.*no\.'), "text": number}),
        ("click", lambda p: {"ref": ref(p, r'button "Continue"')}),
        ("extract", lambda p: {"ref": ref(p, r'cell "[A-Za-z]+, '), "output_name": "patient"}),
        ("extract", lambda p: {"ref": ref(p, r'cell "(Dr|N\.P|[A-Z]\.)'), "output_name": "provider"}),
    ]
    if extra_read:
        steps.append(("extract", lambda p: {"ref": ref(p, r'cell "(scheduled|completed|cancelled)"'), "output_name": "status_text"}))
    return steps + [("finish", lambda p: {"reasoning": "read them"})]


def drafted(discover, monkeypatch, **over):
    monkeypatch.setattr(discover_service, "get_model", lambda: Proposal(**{**PROPOSAL, **over}))
    reply = discover.post("/v1/discover/draft", json={"target": "clinic", "request": REQUEST}, headers=discover.h("dana"))
    assert reply.status_code == 200, reply.text
    return reply.json()["contract"]


def test_a_drafted_name_that_is_already_a_task_gets_a_different_one(discover, monkeypatch):
    assert drafted(discover, monkeypatch, task_name="patient_lookup")["task_name"] == "patient_lookup_2"


def test_a_corrected_typo_and_a_stray_read_do_not_end_up_in_the_recording(discover, monkeypatch):
    contract = drafted(discover, monkeypatch)
    discover.use(find_script(typo="A-2000", extra_read=True))
    done = wait_for(discover, start(discover, contract).json()["id"], ("ready", "needs_attention", "failed", "stuck"))
    assert done["status"] == "ready", (done["error"], done["verify"])
    artifact = storage.load_artifact_by_id("clinic.appointment_details", discover.adir, version="1.0.0")
    assert [s.action.value for s in artifact.steps] == ["navigate", "click", "type", "click", "extract", "extract"]
    assert [s.output_binding for s in artifact.steps if s.output_binding] == ["patient", "provider"]
    assert [s.params["text"] for s in artifact.steps if s.action.value == "type"] == ["{{appointment}}"]


def test_a_task_described_as_committing_that_never_commits_is_not_ready(discover):
    contract = {**REFUND, "task_name": "refund_review", "outputs": [{"name": "summary"}], "commit_approval": "auto_sandbox"}
    steps = refund_steps(5) + [("extract", lambda p: {"ref": ref(p, r'cell "[^"]+"'), "output_name": "summary"}), ("finish", lambda p: {"reasoning": "done"})]
    discover.use(steps)
    done = wait_for(discover, start(discover, contract).json()["id"], ("ready", "needs_attention", "failed", "stuck"))
    assert done["status"] == "needs_attention" and "no commit step was recorded" in done["error"], done
    assert effects(discover_base()) == []


def test_a_recording_made_on_one_appointment_gives_the_right_answer_for_the_others(discover, monkeypatch):
    """The test that finds what the example hid: record on A-20002, promote, then run for many other appointments (other patients, providers and statuses)
    and compare every answer with the clinic's own page."""
    import httpx
    contract = drafted(discover, monkeypatch)
    discover.use(find_script())
    sid = start(discover, contract).json()["id"]
    wait_for(discover, sid, ("ready",))
    assert discover.post(f"/v1/discover/{sid}/promote", json={"reason": "sweep"}, headers=discover.h("dana")).status_code == 200
    page = httpx.Client(follow_redirects=True)
    page.post(f"{discover_base()}/legacy/login", data={"username": "frontdesk", "password": "desk-demo-123"})
    checked, mismatches = 0, []
    for i in range(1, 15):
        number = f"A-200{i:02d}"
        html = page.get(f"{discover_base()}/legacy/appointments/{number}").text
        if "Patient" not in html:
            continue
        run = discover.post("/v1/runs", json={"capability_id": "clinic.appointment_details", "params": {"appointment": number}, "target": "clinic"}, headers=discover.h("alex")).json()["id"]
        end = time.time() + 60
        while time.time() < end:
            body = discover.get(f"/v1/runs/{run}", headers=discover.h("alex")).json()
            if body["status"] in ("success", "hard_failure", "business_outcome", "needs_review"):
                break
            time.sleep(0.3)
        out = (body.get("result") or {}).get("outputs") or {}
        ok = body["status"] == "success" and out.get("provider", "\0") in html and out.get("patient", "\0").split(" (")[0] in html
        checked += 1
        if not ok:
            mismatches.append((number, body["status"], out, (body.get("result") or {}).get("error")))
    assert checked >= 8 and not mismatches, mismatches
