"""A run that gets stuck pauses for a person, who takes over from the console, then hands it back. Real browser, real clinic, the real API.

The stuck step: the recorded locator for "type the MRN" has gone stale, but the step's own checkpoint (the field holds the MRN) still resolves. A person types
the MRN into the field; the engine sees the checkpoint met and carries on, without redoing the step."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import runtime
from artifacts_lib import storage
from tests.test_api_v1 import api, submit, wait  # noqa: F401  (the API fixture and helpers)

MRN = "LK-100002"


@pytest.fixture
def stuck(api):
    """The shared API fixture, with patient_lookup's typing step pointed at a locator that no longer exists."""
    adir = Path(os.environ["ARTIFACTS_DIR"])
    version = storage.current_version("clinic.patient_lookup", adir)
    path = adir / "clinic.patient_lookup" / f"{version}.json"
    raw = json.loads(path.read_text())
    raw["steps"][1]["target"]["locators"] = [{"strategy": "css", "value": "#this-field-was-renamed"}]
    path.write_text(json.dumps(raw))
    return api


def run_until_paused(api, who="alex", **extra):
    run = submit(api, who, "clinic.patient_lookup", {"mrn": MRN}, **extra).json()["id"]
    end = time.time() + 60
    while time.time() < end:
        body = api.get(f"/v1/runs/{run}", headers=api.h("vic")).json()
        if body["paused"] or body["status"] not in ("queued", "running"):
            return run, body
        time.sleep(0.2)
    raise AssertionError(f"run never paused: {body['status']}")


def mrn_box(api, run):
    state = api.get(f"/v1/runs/{run}/escalation", headers=api.h("alex")).json()
    return state, next(e["ref"] for e in state["elements"] if e["role"] == "textbox" and e["name"] == "mrn")


def test_a_stuck_run_pauses_a_person_takes_over_and_it_carries_on(stuck):
    run, body = run_until_paused(stuck)
    assert body["status"] == "running" and body["paused"]["step_id"] == "s2" and body["paused"]["reason"] and body["paused"]["stops_in_s"] > 0

    state, box = mrn_box(stuck, run)
    assert state["url"].endswith("/legacy/patients") and state["has_screenshot"]
    assert stuck.get(f"/v1/runs/{run}/escalation/screenshot", headers=stuck.h("alex")).headers["content-type"] == "image/png"
    assert any(r["id"] == run for r in stuck.get("/v1/inbox", headers=stuck.h("alex")).json()["paused"])

    assert stuck.post(f"/v1/runs/{run}/escalation/act", json={"kind": "type", "ref": box, "value": MRN}, headers=stuck.h("alex")).status_code == 202
    end = time.time() + 20
    while time.time() < end and not (stuck.get(f"/v1/runs/{run}/escalation", headers=stuck.h("alex")).json()["last_action"] or {}).get("ok"):
        time.sleep(0.2)
    assert stuck.post(f"/v1/runs/{run}/escalation/resume", headers=stuck.h("alex")).json() == {"resumed": True}

    done = wait(stuck, run)
    assert done["status"] == "success" and done["result"]["outputs"]["patient_name"] == "Pell, Jordan" and done["result"]["escalated"] and done["paused"] is None
    events = [json.loads(line) for line in (runtime_evidence(stuck, run) / "log.jsonl").read_text().splitlines()]
    assert any(e["event_type"] == "action" and e["actor"] == "human" and e["data"].get("by") == "alex" for e in events)
    assert [e["event_type"] for e in events if e["event_type"] in ("pause", "resume")] == ["pause", "resume"]


def runtime_evidence(api, run) -> Path:
    return Path(runtime.default_store().get(run)["evidence_dir"])


def test_a_run_can_be_stopped_by_the_person_helping(stuck):
    run, _ = run_until_paused(stuck)
    assert stuck.post(f"/v1/runs/{run}/escalation/stop", headers=stuck.h("sam")).status_code == 200
    done = wait(stuck, run)
    assert done["status"] == "hard_failure" and done["result"]["error"]["code"] == "cancelled" and done["paused"] is None


def test_cancelling_a_paused_run_from_the_run_page_ends_the_pause(stuck):
    run, _ = run_until_paused(stuck)
    assert stuck.post(f"/v1/runs/{run}/cancel", headers=stuck.h("alex")).status_code == 200
    assert wait(stuck, run)["status"] == "hard_failure"


def test_only_operators_can_look_or_act_and_only_while_the_run_is_waiting(stuck):
    run, _ = run_until_paused(stuck)
    assert stuck.get(f"/v1/runs/{run}/escalation", headers=stuck.h("vic")).status_code == 403
    assert stuck.post(f"/v1/runs/{run}/escalation/resume", headers=stuck.h("vic")).status_code == 403
    state, box = mrn_box(stuck, run)
    gone = stuck.post(f"/v1/runs/{run}/escalation/act", json={"kind": "click", "ref": "f99e99"}, headers=stuck.h("alex"))
    assert gone.status_code == 409 and "no longer on the page" in gone.json()["detail"]
    assert stuck.post(f"/v1/runs/{run}/escalation/act", json={"kind": "type", "ref": box}, headers=stuck.h("alex")).status_code == 422
    stuck.post(f"/v1/runs/{run}/escalation/stop", headers=stuck.h("alex"))
    wait(stuck, run)
    late = stuck.post(f"/v1/runs/{run}/escalation/resume", headers=stuck.h("alex"))
    assert late.status_code == 409 and "not waiting for a person" in late.json()["detail"]


def test_a_run_that_nobody_helps_is_stopped_after_the_wait(stuck, monkeypatch):
    policy = runtime.default_policy()
    policy.escalation.wait_s = 2  # (set directly: the configured minimum is longer than a test should wait)
    monkeypatch.setattr(runtime, "default_policy", lambda: policy)
    run, _ = run_until_paused(stuck)
    done = wait(stuck, run, timeout=30)
    assert done["status"] == "hard_failure" and done["result"]["error"]["code"] == "escalation_timeout" and "nobody took over" in done["result"]["error"]["message"]


def test_only_one_run_may_wait_at_a_time_so_the_others_still_have_a_browser(stuck):
    first, _ = run_until_paused(stuck)
    second = submit(stuck, "sam", "clinic.patient_lookup", {"mrn": "LK-100001"}).json()["id"]
    done = wait(stuck, second, timeout=60)
    assert done["status"] == "hard_failure" and done["result"]["error"]["code"] == "no_capacity" and "no room" in done["result"]["error"]["message"]
    assert stuck.get(f"/v1/runs/{first}", headers=stuck.h("vic")).json()["paused"]  # the first is still waiting
    stuck.post(f"/v1/runs/{first}/escalation/stop", headers=stuck.h("alex"))
    wait(stuck, first)


def test_a_client_that_cannot_be_helped_gets_a_plain_failure(stuck):
    run = submit(stuck, "alex", "clinic.patient_lookup", {"mrn": MRN}, pause_for_human=False).json()["id"]
    done = wait(stuck, run)
    assert done["status"] == "hard_failure" and done["result"]["error"]["code"] == "locator_unresolved" and done["result"]["escalated"] is False


def test_the_policy_can_turn_pausing_off(stuck, monkeypatch):
    policy = runtime.default_policy()
    policy.escalation.enabled = False
    monkeypatch.setattr(runtime, "default_policy", lambda: policy)
    run = submit(stuck, "alex", "clinic.patient_lookup", {"mrn": MRN}).json()["id"]
    assert wait(stuck, run)["result"]["error"]["code"] == "locator_unresolved"
