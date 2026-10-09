"""Approval at the step: a run goes as far as its irreversible step and waits there for a person, who sees the page and the values entered before approving or
rejecting. Real browser, real clinic; the clinic's audit log is the ground truth for what actually posted."""
from __future__ import annotations

import time

import pytest

import runtime
from escalation.session_manager import SessionCancelled, SessionManager
from tests.clinic_support import effects
from tests.test_api_v1 import REFUND, api, submit, wait  # noqa: F401  (the fixture and shared helpers)

SETTLED = ("success", "business_outcome", "hard_failure", "needs_review", "dry_run", "rejected")


def waiting(api, run_id, timeout=60):
    """The run has reached its irreversible step and is waiting for an approval."""
    end = time.time() + timeout
    while time.time() < end:
        body = api.get(f"/v1/runs/{run_id}", headers=api.h("vic")).json()
        if body["awaiting_approval"]:
            return body
        assert body["status"] not in SETTLED, f"run settled without waiting: {body['status']}"
        time.sleep(0.3)
    raise AssertionError("the run never reached its approval")


def settle_all(api, *run_ids):
    """Finish what a test leaves waiting: a run still in flight would leak into the next test."""
    for run_id in run_ids:
        if api.get(f"/v1/runs/{run_id}", headers=api.h("vic")).json()["awaiting_approval"]:
            api.post(f"/v1/runs/{run_id}/approval/reject", json={"reason": "test cleanup"}, headers=api.h("dee"))
        wait(api, run_id, until=SETTLED)


def patch_policy(monkeypatch, **escalation):
    policy = runtime.default_policy()
    patched = policy.model_copy(update={"escalation": policy.escalation.model_copy(update=escalation)})
    monkeypatch.setattr(runtime, "default_policy", lambda: patched)


# ---- the whole path ---------------------------------------------------------------------------------------------------

def test_a_refund_stops_at_the_step_and_posts_once_after_a_supervisor_approves(api, clinic_base_url):
    r = submit(api, "alex", "clinic.issue_refund", REFUND, key="step-1")
    run_id = r.json()["id"]
    assert r.status_code == 202  # it started: nothing waited for an approval before the run

    ask = waiting(api, run_id)["awaiting_approval"]
    assert (ask["tier"], ask["requested_by"]) == ("supervisor", "alex") and ask["description"] and ask["stops_in_s"] > 0
    assert effects(clinic_base_url) == [], "it must stop before the irreversible step"

    queue = api.get("/v1/approvals", headers=api.h("dana")).json()["approvals"]
    assert [(a["run_id"], a["tier"], a["at_step"]) for a in queue] == [(run_id, "supervisor", True)]
    assert api.get("/v1/inbox", headers=api.h("dana")).json()["counts"]["approvals"] == 1

    # what the approver sees: the page, and the values that were entered
    view = api.get(f"/v1/runs/{run_id}/approval", headers=api.h("dana")).json()
    assert view["params"]["amount"] == "15.00" and view["has_screenshot"]
    assert "INV-30001" in view["page_text"] and "15.00" in view["page_text"], view["page_text"]  # the review page, as a person reads it
    assert api.get(f"/v1/runs/{run_id}/approval/screenshot", headers=api.h("dana")).headers["content-type"] == "image/png"
    assert api.get(f"/v1/runs/{run_id}/approval", headers=api.h("vic")).status_code == 403  # a viewer cannot see the page

    assert api.post(f"/v1/runs/{run_id}/approval/approve", json={"reason": "ok"}, headers=api.h("sam")).status_code == 403  # an operator, not a supervisor
    assert api.post(f"/v1/runs/{run_id}/approval/approve", json={"reason": "ok"}, headers=api.h("vic")).status_code == 403
    assert api.post(f"/v1/runs/{run_id}/approval/approve", json={"reason": ""}, headers=api.h("dana")).status_code == 422
    assert effects(clinic_base_url) == []

    assert api.post(f"/v1/runs/{run_id}/approval/approve", json={"reason": "invoice checked"}, headers=api.h("dana")).status_code == 200
    done = wait(api, run_id)
    assert done["status"] == "success" and done["committed"] and done["awaiting_approval"] is None
    assert [(a["decided_by"], a["decision"], a["reason"], a["tier"]) for a in done["approvals"]] == [("dana", "approved", "invoice checked", "supervisor")]
    assert len(effects(clinic_base_url)) == 1

    assert api.post(f"/v1/runs/{run_id}/approval/approve", json={"reason": "again"}, headers=api.h("dee")).status_code == 409  # nothing is waiting any more
    assert submit(api, "alex", "clinic.issue_refund", REFUND, key="step-1").json()["result"]["deduplicated"] and len(effects(clinic_base_url)) == 1


def test_the_person_who_asked_cannot_approve_even_as_a_supervisor(api, clinic_base_url):
    run_id = submit(api, "dana", "clinic.issue_refund", REFUND).json()["id"]
    waiting(api, run_id)
    refused = api.post(f"/v1/runs/{run_id}/approval/approve", json={"reason": "me"}, headers=api.h("dana"))
    assert refused.status_code == 409 and "cannot approve" in refused.json()["detail"]
    assert effects(clinic_base_url) == []
    assert api.post(f"/v1/runs/{run_id}/approval/approve", json={"reason": "second pair of eyes"}, headers=api.h("dee")).status_code == 200
    assert wait(api, run_id)["status"] == "success"


def test_a_claim_needs_only_an_operator_other_than_the_requester(api, clinic_base_url):
    run_id = submit(api, "alex", "clinic.submit_claim", {"invoice": "INV-30002", "amount": "124.00"}).json()["id"]
    assert waiting(api, run_id)["awaiting_approval"]["tier"] == "operator"
    assert api.post(f"/v1/runs/{run_id}/approval/approve", json={"reason": "mine"}, headers=api.h("alex")).status_code == 409
    assert api.post(f"/v1/runs/{run_id}/approval/approve", json={"reason": "checked the claim"}, headers=api.h("sam")).status_code == 200
    assert wait(api, run_id)["status"] == "success"
    assert len(effects(clinic_base_url, "claim.submit")) == 1


def test_a_rejected_step_is_never_issued_and_the_run_says_so(api, clinic_base_url):
    run_id = submit(api, "alex", "clinic.issue_refund", REFUND).json()["id"]
    waiting(api, run_id)
    assert api.post(f"/v1/runs/{run_id}/approval/reject", json={"reason": "wrong invoice"}, headers=api.h("dana")).status_code == 200
    done = wait(api, run_id)
    assert done["status"] == "hard_failure" and done["error_code"] == "approval_rejected" and not done["committed"]
    assert "dana rejected it" in done["result"]["error"]["message"] and "did not commit it" in done["result"]["error"]["message"]
    assert [(a["decision"], a["decided_by"], a["reason"]) for a in done["approvals"]] == [("rejected", "dana", "wrong invoice")]
    assert effects(clinic_base_url) == []


def test_nobody_deciding_in_time_stops_the_run_with_nothing_committed(api, clinic_base_url, monkeypatch):
    patch_policy(monkeypatch, approval_wait_s=2)
    run_id = submit(api, "alex", "clinic.issue_refund", REFUND).json()["id"]
    done = wait(api, run_id, until=SETTLED)
    assert done["status"] == "hard_failure" and done["error_code"] == "approval_timeout" and not done["committed"]
    assert [(a["decision"], a["decided_by"]) for a in done["approvals"]] == [("expired", "system")]  # the record never shows it as still waiting
    assert effects(clinic_base_url) == []
    late = api.post(f"/v1/runs/{run_id}/approval/approve", json={"reason": "late"}, headers=api.h("dana"))
    assert late.status_code == 409  # a decision after the timeout is not applied
    assert effects(clinic_base_url) == []


def test_stopping_a_run_that_is_waiting_ends_it_and_closes_the_request(api, clinic_base_url):
    run_id = submit(api, "alex", "clinic.issue_refund", REFUND).json()["id"]
    waiting(api, run_id)
    assert api.post(f"/v1/runs/{run_id}/cancel", headers=api.h("alex")).status_code == 200
    done = wait(api, run_id, until=SETTLED)
    assert done["status"] == "hard_failure" and not done["committed"] and [a["decision"] for a in done["approvals"]] == ["cancelled"]
    assert effects(clinic_base_url) == []


# ---- the approver works on the page ------------------------------------------------------------------------------------

def approval_view(api, run_id, who="dana"):
    return api.get(f"/v1/runs/{run_id}/approval", headers=api.h(who)).json()


def act(api, run_id, who, ref, kind="click", value=None):
    return api.post(f"/v1/runs/{run_id}/approval/act", json={"kind": kind, "ref": ref, "value": value}, headers=api.h(who))


def wait_for_action(api, run_id, who="dana", timeout=30):
    """The page has carried out what the person asked (it runs on the thread that owns the page)."""
    end = time.time() + timeout
    while time.time() < end:
        last = approval_view(api, run_id, who).get("last_action")
        if last and last["kind"] != "note":
            return last
        time.sleep(0.3)
    raise AssertionError("the action was never carried out")


def test_the_approver_does_the_step_on_the_page_and_the_run_does_not_repeat_it(api, clinic_base_url):
    run_id = submit(api, "alex", "clinic.issue_refund", REFUND).json()["id"]
    waiting(api, run_id)
    confirm = next(e for e in approval_view(api, run_id)["elements"] if e["role"] == "button" and e["name"] == "Confirm")

    # only someone who could approve it may work on the page: not an operator for a refund, not the person who asked, not a viewer
    assert act(api, run_id, "sam", confirm["ref"]).status_code == 403
    assert act(api, run_id, "alex", confirm["ref"]).status_code == 403
    assert act(api, run_id, "vic", confirm["ref"]).status_code == 403
    assert act(api, run_id, "dana", "e99999").status_code == 409  # not an element of this page
    assert effects(clinic_base_url) == []

    assert act(api, run_id, "dana", confirm["ref"]).status_code == 202  # the irreversible click, done by a person who answers for it
    assert wait_for_action(api, run_id)["ok"]
    assert len(effects(clinic_base_url)) == 1
    assert api.post(f"/v1/runs/{run_id}/approval/done", json={"reason": "I confirmed the refund"}, headers=api.h("dana")).status_code == 200

    done = wait(api, run_id)
    assert done["status"] == "success" and done["committed"] and any(done["result"]["outputs"].values())  # the run read the receipt off the page the person left it on
    assert [(a["decision"], a["decided_by"]) for a in done["approvals"]] == [("completed", "dana")]
    assert len(effects(clinic_base_url)) == 1, "the run must not click the commit again"


def until(api, run_id, ready, timeout=40, who="dana"):
    """Polls the approval view until `ready(view)` holds."""
    end = time.time() + timeout
    while time.time() < end:
        view = approval_view(api, run_id, who)
        if ready(view):
            return view
        time.sleep(0.3)
    raise AssertionError(f"never happened; last view: {view}")


def test_leaving_the_page_does_not_wreck_the_run_and_resuming_brings_it_back(api, clinic_base_url):
    """The approver clicks Main menu, so the step's button is gone. Approving must not be accepted as if it could work (the run keeps waiting and says why), and
    Resume automation takes the run back through its earlier steps to the page it stopped on, where it asks again."""
    run_id = submit(api, "alex", "clinic.issue_refund", REFUND).json()["id"]
    try:
        waiting(api, run_id)
        view = approval_view(api, run_id)
        assert view["page_moved"] is False
        menu = next(e for e in view["elements"] if e["role"] == "link" and e["name"] == "Main menu")
        assert act(api, run_id, "dana", menu["ref"]).status_code == 202
        until(api, run_id, lambda v: v.get("page_moved") is True)

        assert api.post(f"/v1/runs/{run_id}/approval/approve", json={"reason": "ok"}, headers=api.h("dana")).status_code == 200
        view = until(api, run_id, lambda v: v.get("awaiting") and v.get("last_action") and v["last_action"]["kind"] == "note")
        body = api.get(f"/v1/runs/{run_id}", headers=api.h("vic")).json()
        assert "was not used" in view["last_action"]["error"] and view["page_moved"] is True
        assert body["awaiting_approval"] and body["paused"] is None and body["status"] == "running"  # waiting for a decision, not stuck
        assert effects(clinic_base_url) == []

        assert api.post(f"/v1/runs/{run_id}/approval/resume", json={}, headers=api.h("sam")).status_code == 403  # an operator may not for a refund
        assert api.post(f"/v1/runs/{run_id}/approval/resume", json={}, headers=api.h("dana")).status_code == 200
        back = until(api, run_id, lambda v: v.get("awaiting") and v.get("page_moved") is False and "Confirm" in [e["name"] for e in v["elements"]])
        assert "15.00" in back["page_text"] and effects(clinic_base_url) == []  # back on the review page, nothing committed on the way

        assert api.post(f"/v1/runs/{run_id}/approval/approve", json={"reason": "now it is the right page"}, headers=api.h("dana")).status_code == 200
        done = wait(api, run_id)
        assert done["status"] == "success" and done["committed"] and len(effects(clinic_base_url)) == 1
        assert [a["decision"] for a in done["approvals"]] == ["approved", "resumed", "approved"]  # the first approval could not be used; then resumed; then approved
    finally:
        settle_all(api, run_id)


def test_handing_back_without_doing_the_step_asks_again_and_nothing_is_committed(api, clinic_base_url):
    run_id = submit(api, "alex", "clinic.issue_refund", REFUND).json()["id"]
    waiting(api, run_id)
    assert api.post(f"/v1/runs/{run_id}/approval/done", json={"reason": "done"}, headers=api.h("dana")).status_code == 200

    end = time.time() + 30
    while time.time() < end:  # it looks at the page, sees the step is not done, and asks for a decision again
        view = approval_view(api, run_id)
        if view["awaiting"] and view["last_action"] and view["last_action"]["kind"] == "note":
            break
        time.sleep(0.3)
    assert view["awaiting"] and "does not show that step done yet" in view["last_action"]["error"]
    assert effects(clinic_base_url) == []

    assert api.post(f"/v1/runs/{run_id}/approval/approve", json={"reason": "go on then"}, headers=api.h("dana")).status_code == 200
    done = wait(api, run_id)
    assert done["status"] == "success" and len(effects(clinic_base_url)) == 1
    assert [a["decision"] for a in done["approvals"]] == ["completed", "approved"]


def test_an_admin_may_decide_their_own_request_but_a_supervisor_may_not(api, clinic_base_url):
    own = submit(api, "root", "clinic.issue_refund", REFUND).json()["id"]  # an admin asked
    waiting(api, own)
    assert api.post(f"/v1/runs/{own}/approval/approve", json={"reason": "my own request"}, headers=api.h("root")).status_code == 200
    done = wait(api, own)
    assert done["status"] == "success" and [(a["decided_by"], a["requested_by"]) for a in done["approvals"]] == [("root", "root")]

    other = submit(api, "dana", "clinic.issue_refund", {**REFUND, "amount": "16.00"}).json()["id"]  # a supervisor asked
    waiting(api, other)
    assert api.post(f"/v1/runs/{other}/approval/approve", json={"reason": "mine"}, headers=api.h("dana")).status_code == 409
    settle_all(api, other)
    assert len(effects(clinic_base_url)) == 1


def test_an_admin_may_also_approve_their_own_request_before_the_run_starts(api, clinic_base_url):
    run_id = submit(api, "root", "clinic.issue_refund", REFUND, pause_for_human=False).json()["id"]
    assert api.post(f"/v1/runs/{run_id}/approve", json={"reason": "my own request"}, headers=api.h("root")).status_code == 202
    assert wait(api, run_id)["status"] == "success" and len(effects(clinic_base_url)) == 1


def test_the_page_can_be_operated_while_waiting_and_the_wait_survives_it():
    """The wait carries out what the person asks, on the page, and goes on waiting for the decision."""
    import threading
    from types import SimpleNamespace

    from artifacts_lib.schema import ActionType
    from surface.base import Action, ObservedState

    class Page:
        def __init__(self):
            self.acted = []

        def act(self, action):
            self.acted.append((action.kind, action.ref, action.confirmed))
            return SimpleNamespace(success=True, error=None)

        def perceive(self, actor="system"):
            return ObservedState(url="http://x/review", title="Review", elements=[])

    page = Page()
    s = SessionManager("s", surface=page, evidence_dir=None, approval_wait_s=30)  # type: ignore[arg-type]
    results = []
    t = threading.Thread(target=lambda: results.append(s.await_approval("supervisor", "alex", "Click Confirm", "s7")))
    t.start()
    end = time.time() + 5
    while s.snapshot()["approval"] is None and time.time() < end:
        time.sleep(0.02)
    s.request_action(Action(kind=ActionType.CLICK, ref="e12", actor="human", confirmed=True), by="dana")
    end = time.time() + 5
    while s.snapshot()["last_human_result"] is None and time.time() < end:
        time.sleep(0.02)
    assert page.acted == [(ActionType.CLICK, "e12", True)] and s.human_clicks == 1
    assert s.snapshot()["mode"] == "awaiting_approval"  # still waiting: acting on the page is not deciding
    assert s.decide("completed", "dana", "did it") is True
    t.join(5)
    assert results == [("completed", "dana", "did it")]


# ---- who can wait, and where ------------------------------------------------------------------------------------------

def test_a_client_that_cannot_wait_is_approved_before_the_run_starts(api, clinic_base_url):
    """An AI assistant (MCP) says pause_for_human false: it gets the approval that comes before the run, as it always did."""
    r = submit(api, "alex", "clinic.issue_refund", REFUND, pause_for_human=False)
    run_id = r.json()["id"]
    assert r.status_code == 200 and r.json()["status"] == "pending_approval"
    assert [a["at_step"] for a in api.get("/v1/approvals", headers=api.h("dana")).json()["approvals"]] == [False]
    assert effects(clinic_base_url) == []
    assert api.post(f"/v1/runs/{run_id}/approve", json={"reason": "ok"}, headers=api.h("dana")).status_code == 202
    done = wait(api, run_id)
    assert done["status"] == "success" and len(effects(clinic_base_url)) == 1


def test_a_task_set_to_approve_before_the_run_still_does(api, clinic_base_url, monkeypatch):
    policy = runtime.default_policy()
    capabilities = {**policy.capabilities, "clinic.issue_refund": policy.capabilities["clinic.issue_refund"].model_copy(update={"approve_at": "run"})}
    patched = policy.model_copy(update={"capabilities": capabilities})
    monkeypatch.setattr(runtime, "default_policy", lambda: patched)
    r = submit(api, "alex", "clinic.issue_refund", REFUND)
    assert r.status_code == 200 and r.json()["status"] == "pending_approval"
    api.post(f"/v1/runs/{r.json()['id']}/reject", json={"reason": "test"}, headers=api.h("dana"))


def test_when_too_many_are_already_waiting_the_next_is_approved_before_it_starts(api, clinic_base_url, monkeypatch):
    patch_policy(monkeypatch, max_awaiting_approval=1)
    first = submit(api, "alex", "clinic.issue_refund", REFUND, key="first").json()["id"]
    waiting(api, first)
    second = submit(api, "alex", "clinic.issue_refund", {**REFUND, "amount": "16.00"}, key="second")
    assert second.status_code == 200 and second.json()["status"] == "pending_approval"
    api.post(f"/v1/runs/{second.json()['id']}/reject", json={"reason": "test"}, headers=api.h("dana"))
    settle_all(api, first)
    assert effects(clinic_base_url) == []


def test_a_run_waiting_for_approval_is_not_one_a_person_can_take_over(api, clinic_base_url):
    run_id = submit(api, "alex", "clinic.issue_refund", REFUND).json()["id"]
    body = waiting(api, run_id)
    assert body["paused"] is None  # not "stuck": nobody works on its page
    for path in ("act", "resume", "stop"):
        sent = api.post(f"/v1/runs/{run_id}/escalation/{path}", json={"kind": "click", "ref": "x"}, headers=api.h("alex"))
        assert sent.status_code == 409, path
    assert api.get("/v1/inbox", headers=api.h("dana")).json()["counts"]["paused"] == 0
    settle_all(api, run_id)
    assert effects(clinic_base_url) == []


def test_limits_and_dry_runs_are_unchanged(api, clinic_base_url):
    over = submit(api, "alex", "clinic.issue_refund", {**REFUND, "amount": "5000.00"})
    assert over.json()["result"]["error"]["code"] == "policy_cap_exceeded"  # refused before any browser, so nothing ever waits
    dry = submit(api, "alex", "clinic.issue_refund", REFUND, dry_run=True)
    assert wait(api, dry.json()["id"])["status"] == "dry_run"
    assert effects(clinic_base_url) == []


def test_the_policy_says_which_tasks_wait_at_the_step(api):
    caps = api.get("/v1/policy", headers=api.h("alex")).json()["capabilities"]
    assert {k: v["approve_at"] for k, v in caps.items()} == {
        "clinic.issue_refund": "step", "clinic.write_off_balance": "step", "clinic.cancel_appointment": "step", "clinic.submit_claim": "step", "clinic.update_patient_contact": "run"}


# ---- the wait itself --------------------------------------------------------------------------------------------------

def test_the_wait_ends_on_a_decision_a_timeout_or_a_stop():
    def session(**kw):
        return SessionManager("s", surface=None, evidence_dir=None, approval_wait_s=kw.pop("wait", None), **kw)  # type: ignore[arg-type]

    s = session()
    results = []
    import threading
    t = threading.Thread(target=lambda: results.append(s.await_approval("supervisor", "alex", "Click Issue refund", "s9")))
    t.start()
    end = time.time() + 5
    while s.snapshot()["approval"] is None and time.time() < end:
        time.sleep(0.02)
    assert s.snapshot()["approval"]["requested_by"] == "alex" and s.snapshot()["mode"] == "awaiting_approval"
    assert s.decide("approved", "dana", "ok") is True and s.decide("rejected", "dee", "late") is False  # the first decision stands
    t.join(5)
    assert results == [("approved", "dana", "ok")] and s.snapshot()["mode"] == "automation" and s.snapshot()["approval"] is None

    quick = session(wait=0.3)
    with pytest.raises(SessionCancelled) as timed_out:
        quick.await_approval("operator", "alex", "x", "s1")
    assert timed_out.value.code == "approval_timeout" and quick.decide("approved", "dana", "late") is False

    full = session(may_await_approval=lambda _s: False)
    with pytest.raises(SessionCancelled) as no_room:
        full.await_approval("operator", "alex", "x", "s1")
    assert no_room.value.code == "no_capacity"

    stopping = session()
    threading.Timer(0.3, stopping.cancel).start()
    with pytest.raises(SessionCancelled) as stopped:
        stopping.await_approval("operator", "alex", "x", "s1")
    assert stopped.value.code == "cancelled"


def test_a_restart_while_waiting_is_a_plain_failure_not_an_unknown_outcome(tmp_path):
    """A run waiting at its step never issued it, so it must not land in the inbox as "outcome unknown" for someone to investigate."""
    from runs.store import RunStore

    store = RunStore(tmp_path / "runs.db")
    waiting = store.begin("clinic.issue_refund", "1.0.0", {}, "alex", None, status="running").run["id"]
    store.request_approval(waiting, "supervisor", "alex")
    committed = store.begin("clinic.issue_refund", "1.0.0", {}, "alex", None, status="running").run["id"]
    store.request_approval(committed, "supervisor", "alex")
    store.decide_at_step(committed, "approved", "dana", "ok")  # approved, then the process died: it may have issued the step

    store.recover_interrupted(lambda _capability: True)

    assert store.get(waiting)["status"] == "hard_failure" and [a["decision"] for a in store.approvals_for(waiting)] == ["cancelled"]
    assert store.get(committed)["status"] == "needs_review"
