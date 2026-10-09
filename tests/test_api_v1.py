"""The v1 API end to end: keys and roles, async runs, SSE, approvals with real identities, idempotency, repairs and versions.
Real browser, real clinic; the clinic's audit log is the ground truth for what actually posted."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import runtime
from api.app import app
from tests.clinic_support import chaos, copy_artifacts, effects, reset

REAL = Path(__file__).resolve().parent.parent / "artifacts"


@pytest.fixture
def api(tmp_path, monkeypatch, clinic_base_url):
    monkeypatch.setenv("ARTIFACTS_DIR", str(copy_artifacts(tmp_path)))
    monkeypatch.setitem(runtime.TARGET_PROFILES["clinic"], "base_url", clinic_base_url)
    monkeypatch.setitem(runtime.TARGET_PROFILES["clinic_supervisor"], "base_url", clinic_base_url)
    reset(clinic_base_url)
    store = runtime.default_store()
    keys = {n: store.create_key(n, r) for n, r in [("alex", "operator"), ("sam", "operator"), ("dana", "supervisor"), ("dee", "supervisor"), ("vic", "viewer"), ("root", "admin")]}
    with TestClient(app) as client:
        client.keys = keys
        client.h = lambda who: {"Authorization": f"Bearer {keys[who]}"}
        yield client
    httpx.delete(f"{clinic_base_url}/_test/chaos", timeout=5)
    httpx.delete(f"{clinic_base_url}/_test/drift", timeout=5)


def submit(api, who, capability, params, target="clinic", key=None, **extra):
    headers = api.h(who) | ({"Idempotency-Key": key} if key else {})
    return api.post("/v1/runs", json={"capability_id": capability, "params": params, "target": target, **extra}, headers=headers)


def wait(api, run_id, until=("success", "business_outcome", "hard_failure", "needs_review", "dry_run"), timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        body = api.get(f"/v1/runs/{run_id}", headers=api.h("vic")).json()
        if body["status"] in until:
            return body
        time.sleep(0.4)
    raise AssertionError(f"run {run_id} did not settle: {body['status']}")


REFUND = {"invoice": "INV-30001", "amount": "15.00", "reason": "duplicate_payment"}


# ---- identity -----------------------------------------------------------------------------------------------

def test_requests_need_a_valid_key_and_the_right_role(api):
    assert api.get("/v1/me").status_code == 401
    assert api.get("/v1/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert api.get("/v1/me", headers=api.h("dana")).json() == {"name": "dana", "role": "supervisor"}
    assert api.get("/v1/health").status_code == 200  # open
    denied = submit(api, "vic", "clinic.patient_lookup", {"mrn": "LK-100001"})
    assert denied.status_code == 403 and "operator" in denied.json()["detail"]
    assert api.get("/v1/runs", headers=api.h("vic")).status_code == 200


def test_a_revoked_key_stops_working(api):
    runtime.default_store().revoke_key("sam")
    assert api.get("/v1/me", headers=api.h("sam")).status_code == 401


def test_keys_are_stored_hashed():
    store = runtime.default_store()
    key = store.create_key("zed", "viewer")
    rows = store._rows("SELECT * FROM api_keys WHERE name='zed'")
    assert key not in json.dumps(rows) and rows[0]["key_hash"] != key


# ---- async runs and events --------------------------------------------------------------------------------------

def test_a_run_is_accepted_at_once_and_settles_in_the_background(api):
    started = time.time()
    r = submit(api, "alex", "clinic.patient_lookup", {"mrn": "LK-100002"})
    assert r.status_code == 202 and time.time() - started < 5
    body = r.json()
    assert body["status"] in ("queued", "running") and body["requested_by"] == "alex"

    done = wait(api, body["id"])
    assert done["status"] == "success" and done["result"]["outputs"]["patient_name"] == "Pell, Jordan"
    assert api.get("/v1/runs", headers=api.h("vic")).json()["runs"][0]["id"] == body["id"]


def test_events_stream_log_lines_and_status_until_the_run_settles(api):
    run_id = submit(api, "alex", "clinic.patient_lookup", {"mrn": "LK-100001"}).json()["id"]
    events = []
    with api.stream("GET", f"/v1/runs/{run_id}/events", headers=api.h("vic")) as stream:
        kind = None
        for line in stream.iter_lines():
            if line.startswith("event:"):
                kind = line.split(":", 1)[1].strip()
                events.append(kind)
            if kind == "done" and line.startswith("data:"):
                break
    assert events[0] == "status" and "log" in events and events[-1] == "done"
    assert events.count("done") == 1


def test_an_invalid_request_is_refused_without_queueing(api):
    r = submit(api, "alex", "clinic.issue_refund", {"invoice": "INV-30001", "amount": "x", "reason": "vibes"})
    assert r.status_code == 200 and r.json()["result"]["error"]["code"] == "input_invalid"
    assert api.get("/v1/approvals", headers=api.h("dana")).json()["approvals"] == []
    assert submit(api, "alex", "clinic.nope", {}).status_code == 404
    assert submit(api, "alex", "clinic.patient_lookup", {"mrn": "LK-100001"}, target="nowhere").status_code == 422


def test_the_caller_cannot_choose_a_url_or_credentials(api):
    r = api.post("/v1/runs", json={"capability_id": "clinic.patient_lookup", "params": {"mrn": "LK-100001"}, "target": "clinic",
                                   "base_url": "http://example.com", "username": "x"}, headers=api.h("alex"))
    assert r.status_code == 202  # unknown fields are ignored; the run used the profile's own URL
    done = wait(api, r.json()["id"])
    assert done["status"] == "success"
    targets = api.get("/v1/targets", headers=api.h("vic")).json()["targets"]
    assert "password" not in json.dumps(targets).lower()


def test_idempotency_key_header_returns_the_first_run(api):
    first = submit(api, "alex", "clinic.patient_lookup", {"mrn": "LK-100001"}, key="k-1")
    wait(api, first.json()["id"])
    again = submit(api, "alex", "clinic.patient_lookup", {"mrn": "LK-100001"}, key="k-1")
    assert again.status_code == 200 and again.json()["result"]["deduplicated"] and again.json()["id"] == first.json()["id"]
    other = submit(api, "alex", "clinic.patient_lookup", {"mrn": "LK-100002"}, key="k-1")
    assert other.json()["result"]["error"]["code"] == "idempotency_conflict"


def test_a_running_run_can_be_cancelled(api, clinic_base_url):
    chaos(clinic_base_url, "latency", params={"ms": 4000}, method="GET", path_glob="/legacy/patients*", remaining=None)
    run_id = submit(api, "alex", "clinic.patient_lookup", {"mrn": "LK-100001"}).json()["id"]
    time.sleep(2.5)
    assert api.post(f"/v1/runs/{run_id}/cancel", headers=api.h("alex")).status_code == 200
    done = wait(api, run_id)
    assert done["status"] == "hard_failure" and done["error_code"] == "cancelled"
    assert api.post(f"/v1/runs/{run_id}/cancel", headers=api.h("alex")).status_code == 409


# ---- approvals with real identities ----------------------------------------------------------------------------------
# These start every run with pause_for_human=False, as an AI assistant does: it cannot wait at a screen, so its runs are approved before they start. Runs that
# wait at the step itself are in tests/test_approve_at_step.py.

def test_a_refund_waits_for_a_supervisor_key_and_posts_once(api, clinic_base_url):
    r = submit(api, "alex", "clinic.issue_refund", REFUND, key="refund-1", pause_for_human=False)
    run_id = r.json()["id"]
    assert r.status_code == 200 and r.json()["status"] == "pending_approval"
    assert effects(clinic_base_url) == []

    queue = api.get("/v1/approvals", headers=api.h("dana")).json()["approvals"]
    assert [(a["run_id"], a["tier"], a["requested_by"]) for a in queue] == [(run_id, "supervisor", "alex")]

    assert api.post(f"/v1/runs/{run_id}/approve", json={"reason": "ok"}, headers=api.h("sam")).status_code == 403  # operator, not supervisor
    assert api.post(f"/v1/runs/{run_id}/approve", json={"reason": "ok"}, headers=api.h("vic")).status_code == 403
    assert api.post(f"/v1/runs/{run_id}/approve", json={"reason": ""}, headers=api.h("dana")).status_code == 422

    approved = api.post(f"/v1/runs/{run_id}/approve", json={"reason": "invoice checked"}, headers=api.h("dana"))
    assert approved.status_code == 202
    done = wait(api, run_id)
    assert done["status"] == "success" and done["committed"]
    assert [(a["decided_by"], a["decision"], a["reason"]) for a in done["approvals"]] == [("dana", "approved", "invoice checked")]
    assert len(effects(clinic_base_url)) == 1

    assert api.post(f"/v1/runs/{run_id}/approve", json={"reason": "again"}, headers=api.h("dee")).status_code == 409  # already decided
    retry = submit(api, "alex", "clinic.issue_refund", REFUND, key="refund-1", pause_for_human=False)
    assert retry.json()["result"]["deduplicated"] and len(effects(clinic_base_url)) == 1


def test_a_requester_cannot_approve_their_own_run_even_as_a_supervisor(api):
    run_id = submit(api, "dana", "clinic.issue_refund", REFUND, pause_for_human=False).json()["id"]
    r = api.post(f"/v1/runs/{run_id}/approve", json={"reason": "me"}, headers=api.h("dana"))
    assert r.status_code == 409 and "cannot approve" in r.json()["detail"]
    assert api.post(f"/v1/runs/{run_id}/approve", json={"reason": "second pair of eyes"}, headers=api.h("dee")).status_code == 202
    assert wait(api, run_id)["status"] == "success"  # let it finish: a run still in flight would leak into the next test


def test_a_rejected_run_never_executes(api, clinic_base_url):
    run_id = submit(api, "alex", "clinic.issue_refund", REFUND, pause_for_human=False).json()["id"]
    assert api.post(f"/v1/runs/{run_id}/reject", json={"reason": "wrong invoice"}, headers=api.h("dana")).json()["status"] == "rejected"
    assert api.post(f"/v1/runs/{run_id}/approve", json={"reason": "late"}, headers=api.h("dee")).status_code == 409
    assert effects(clinic_base_url) == []


def test_an_unclear_commit_is_settled_through_the_api_by_a_supervisor(api, clinic_base_url):
    chaos(clinic_base_url, "error500_after", method="POST", path_glob="/legacy/transactions/confirm")
    run_id = submit(api, "alex", "clinic.issue_refund", REFUND, key="lost-1", pause_for_human=False).json()["id"]
    api.post(f"/v1/runs/{run_id}/approve", json={"reason": "ok"}, headers=api.h("dana"))
    done = wait(api, run_id)
    assert done["status"] == "needs_review" and len(effects(clinic_base_url)) == 1

    blocked = submit(api, "alex", "clinic.issue_refund", REFUND, key="lost-1", pause_for_human=False)
    assert blocked.json()["result"]["error"]["code"] == "idempotency_conflict"
    body = {"outcome": "committed", "reason": "refund is in the ledger"}
    assert api.post(f"/v1/runs/{run_id}/resolve", json=body, headers=api.h("sam")).status_code == 403
    assert api.post(f"/v1/runs/{run_id}/resolve", json=body, headers=api.h("dana")).status_code == 200
    assert submit(api, "alex", "clinic.issue_refund", REFUND, key="lost-1", pause_for_human=False).json()["result"]["deduplicated"]
    assert len(effects(clinic_base_url)) == 1


def test_dry_run_through_the_api_commits_nothing(api, clinic_base_url):
    r = submit(api, "alex", "clinic.issue_refund", REFUND, dry_run=True)
    done = wait(api, r.json()["id"])
    assert done["status"] == "dry_run" and effects(clinic_base_url) == []


# ---- catalog, repairs, versions ------------------------------------------------------------------------------------

def test_capabilities_expose_schemas_and_risk_metadata(api):
    body = api.get("/v1/capabilities", headers=api.h("vic")).json()["capabilities"]
    by_id = {c["capability_id"]: c for c in body}
    refund = by_id["clinic.issue_refund"]
    assert refund["risk"]["approval_required"] == "supervisor" and refund["risk"]["has_irreversible_step"]
    assert refund["risk"]["caps"]["max_param"] == {"amount": 1000.0}
    assert by_id["clinic.patient_lookup"]["risk"]["approval_required"] is None and by_id["clinic.patient_lookup"]["has_canary"]
    contact = by_id["clinic.update_patient_contact"]["input_schema"]
    assert contact["required"] == ["mrn"] and contact["at_least_one_of"] == ["phone", "email", "address"]
    detail = api.get("/v1/capabilities/clinic.issue_refund", headers=api.h("vic")).json()
    assert detail["provenance"]["commit_approvals"] and detail["history"]
    assert api.get("/v1/capabilities/clinic.nope", headers=api.h("vic")).status_code == 404


def test_drift_produces_a_repair_proposal_that_an_operator_approves_through_the_api(api, clinic_base_url):
    httpx.post(f"{clinic_base_url}/_test/drift", json={"level": 2, "seed": "v1"}, timeout=5)
    run_id = submit(api, "alex", "clinic.patient_lookup", {"mrn": "LK-100001"}).json()["id"]
    failed = wait(api, run_id)
    assert failed["status"] == "hard_failure" and failed["error_code"] == "login_failed"
    [proposal] = failed["repairs"]
    assert proposal["confident"]

    detail = api.get(f"/v1/repairs/{proposal['id']}", headers=api.h("vic")).json()
    assert detail["status"] == "pending" and detail["proposal"]["capability_id"] == "clinic.login"
    assert api.post(f"/v1/repairs/{proposal['id']}/approve", json={"reason": "relabelled"}, headers=api.h("vic")).status_code == 403
    done = api.post(f"/v1/repairs/{proposal['id']}/approve", json={"reason": "sign-on button was relabelled"}, headers=api.h("sam"))
    assert done.status_code == 200 and done.json()["to"] == "1.0.1"

    versions = api.get("/v1/artifacts/clinic.login/versions", headers=api.h("vic")).json()
    assert versions["current"] == "1.0.1" and versions["versions"] == ["1.0.0", "1.0.1"]
    diff = api.get("/v1/artifacts/clinic.login/diff", params={"from": "1.0.0", "to": "1.0.1"}, headers=api.h("vic")).json()
    assert [s["step"] for s in diff["steps"]] == ["s4"]
    assert api.post("/v1/artifacts/clinic.login/rollback", json={"reason": "back out"}, headers=api.h("sam")).json()["current"] == "1.0.0"
    assert api.post("/v1/artifacts/clinic.login/rollback", json={"reason": "x"}, headers=api.h("vic")).status_code == 403


# ---- housekeeping ------------------------------------------------------------------------------------------------------

def test_runs_left_running_by_a_dead_process_stop_blocking_their_key():
    store = runtime.default_store()
    cheap = store.begin("clinic.patient_lookup", "1.0.0", {"mrn": "LK-100001"}, "alex", "k-a").run["id"]
    commit = store.begin("clinic.issue_refund", "1.0.0", REFUND, "alex", "k-b").run["id"]
    recovered = store.recover_interrupted(lambda cap: cap == "clinic.issue_refund")
    assert sorted(recovered) == sorted([cheap, commit])
    assert store.get(cheap)["status"] == "hard_failure" and store.get(commit)["status"] == "needs_review"
    assert store.get(commit)["error_code"] == "interrupted"
    assert store.begin("clinic.patient_lookup", "1.0.0", {"mrn": "LK-100001"}, "alex", "k-a").kind == "new"
    assert store.begin("clinic.issue_refund", "1.0.0", REFUND, "alex", "k-b").kind == "conflict"  # unsettled: stays blocked
