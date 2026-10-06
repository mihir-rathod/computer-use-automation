"""Bugs found by exercising the finished system by hand (a QA pass), each pinned by a test."""
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import pytest

import runtime
from replay.result import ReplayResult, ReplayStatus
from runs.approvals import decide_run, resolve_run
from runs.store import ApprovalError
from safety.config import PolicyConfig

pytestmark = pytest.mark.usefixtures("mockbank_runtime")  # these run MockBank artifacts through the runtime; see conftest


def fake_browser(monkeypatch, delay=0.0):
    calls = []

    def fake(artifact, params, **kw):
        calls.append(params)
        time.sleep(delay)
        now = datetime.now(UTC)
        return ReplayResult(status=ReplayStatus.SUCCESS, capability_id=artifact.capability_id, outputs={"status": "issued"},
                            committed=True, commit_step="s7", started_at=now, finished_at=now)

    monkeypatch.setattr(runtime, "_replay_in_browser", fake)
    return calls


REFUND = {"invoice": "INV-30001", "amount": "10.00", "reason": "billing_error"}


def approved_refund():
    first, _ = runtime.run_replay("clinic.issue_refund", REFUND, target="clinic", requested_by="alex", enable_operator_console=False)
    decide_run(runtime.default_store(), runtime.default_policy(), first.run_id, "approved", "dana.okafor", "ok")
    return first


def test_one_approval_cannot_be_executed_twice_even_concurrently(monkeypatch):
    """Found by hand: four concurrent resumes of ONE approved refund launched four browsers (four refunds)."""
    calls = fake_browser(monkeypatch, delay=0.3)
    first = approved_refund()
    outcomes = []

    def go():
        try:
            runtime.run_replay("clinic.issue_refund", {}, target=None, resume_run_id=first.run_id, enable_operator_console=False)
            outcomes.append("ran")
        except ValueError as exc:
            outcomes.append(str(exc))

    threads = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert len(calls) == 1 and outcomes.count("ran") == 1


def test_resume_refuses_a_run_for_a_different_capability(monkeypatch):
    """Found by hand: `--capability clinic.cancel_appointment --resume <refund run>` silently ran the refund."""
    calls = fake_browser(monkeypatch)
    first = approved_refund()
    with pytest.raises(ValueError, match="is for clinic.issue_refund, not clinic.cancel_appointment"):
        runtime.run_replay("clinic.cancel_appointment", {}, target=None, resume_run_id=first.run_id, enable_operator_console=False)
    assert calls == []


def test_settling_an_ambiguous_commit_needs_someone_on_the_roster():
    """Found by hand: a name not on the roster could mark a needs_review run committed."""
    store, policy = runtime.default_store(), PolicyConfig.load()
    run = store.begin("clinic.issue_refund", "1.0.0", {}, "alex", "k").run["id"]
    now = datetime.now(UTC)
    store.finish(run, ReplayResult(status=ReplayStatus.NEEDS_REVIEW, capability_id="clinic.issue_refund", started_at=now, finished_at=now))
    with pytest.raises(ApprovalError, match="not on the approver roster"):
        resolve_run(store, policy, run, "committed", "alex", "x")
    with pytest.raises(ApprovalError, match="needs a supervisor"):
        resolve_run(store, policy, run, "committed", "sam.reyes", "x")  # refunds are a supervisor-tier capability
    resolve_run(store, policy, run, "committed", "dana.okafor", "in the ledger")


def test_a_settled_commit_is_reported_as_committed_on_a_retry(monkeypatch):
    fake_browser(monkeypatch)
    store = runtime.default_store()
    run = store.begin("mockbank.member_balance_lookup", "2.0.0", {"member_id": "10001"}, "a", "k2").run["id"]
    now = datetime.now(UTC)
    store.finish(run, ReplayResult(status=ReplayStatus.NEEDS_REVIEW, capability_id="mockbank.member_balance_lookup", started_at=now, finished_at=now))
    store.resolve(run, "committed", "dana.okafor", "checked")
    res, _ = runtime.run_replay("mockbank.member_balance_lookup", {"member_id": "10001"}, idempotency_key="k2", enable_operator_console=False)
    assert res.deduplicated and res.committed


def test_an_unreachable_target_closes_the_run_and_frees_its_key(monkeypatch):
    """Found by hand: a browser/connection error left the run 'running' for ever, blocking its idempotency key."""
    def boom(*a, **k):
        raise ConnectionError("net::ERR_CONNECTION_REFUSED at http://localhost:9999/login")

    monkeypatch.setattr(runtime, "_replay_in_browser", boom)
    res, _ = runtime.run_replay("mockbank.member_balance_lookup", {"member_id": "10001"}, idempotency_key="kk", enable_operator_console=False)

    assert res.status == ReplayStatus.HARD_FAILURE and res.error.code == "target_unreachable" and "Couldn't reach" in res.error.message
    assert runtime.default_store().get(res.run_id)["status"] == "hard_failure"

    fake_browser(monkeypatch)
    again, _ = runtime.run_replay("mockbank.member_balance_lookup", {"member_id": "10001"}, idempotency_key="kk", enable_operator_console=False)
    assert again.status == ReplayStatus.SUCCESS  # the key was not left blocked


def test_a_crash_after_the_commit_step_is_needs_review_not_a_plain_failure():
    from artifacts_lib.storage import load_artifact_by_id
    from replay.engine import ReplayEngine
    from surface.base import ActionResult

    artifact = load_artifact_by_id("clinic.issue_refund")
    commit_index = next(i for i, st in enumerate(artifact.steps, 1) if st.risk_level == "irreversible")
    error_texts = {r.signal.value for r in [*artifact.error_handling.business_outcomes, *artifact.error_handling.recoverable]}

    class Surface:
        acts = 0

        def act(self, action):
            self.acts += 1
            return ActionResult(success=True, dispatched=True, extracted_value="RFD-1")

        def check_signal(self, signal):
            if self.acts >= commit_index:
                raise RuntimeError("page closed")  # the browser dies right after the commit click
            return signal.value not in error_texts  # every checkpoint holds, no known error page is showing

        def perceive(self, *a, **k):
            raise RuntimeError("page closed")

    result = ReplayEngine(Surface(), checkpoint_timeout_s=0.1, confirmed_steps={artifact.steps[commit_index - 1].step_id}).run(artifact, REFUND)

    assert result.status == ReplayStatus.NEEDS_REVIEW and result.error.code == "ambiguous_commit"
    assert result.commit_step == artifact.steps[commit_index - 1].step_id


def test_a_capability_cannot_be_run_against_the_wrong_app(monkeypatch):
    calls = fake_browser(monkeypatch)
    res, _ = runtime.run_replay("mockbank.member_balance_lookup", {"member_id": "10001"}, target="clinic", enable_operator_console=False)
    assert res.error.code == "target_mismatch" and calls == []


def test_approval_error_grammar():
    from safety.config import may_approve

    ok, why = may_approve(PolicyConfig.load(), "sam.reyes", "supervisor")
    assert why == "'sam.reyes' is an operator; this capability needs a supervisor approval"


def test_an_idempotency_key_cannot_be_reused_for_different_parameters():
    """Found by reasoning about the same-key path: a second request with new parameters got the first one's result."""
    store = runtime.default_store()
    first = store.begin("clinic.issue_refund", "1.0.0", {"amount": "10.00"}, "alex", "same-key")
    now = datetime.now(UTC)
    store.finish(first.run["id"], ReplayResult(status=ReplayStatus.SUCCESS, capability_id="clinic.issue_refund", committed=True, started_at=now, finished_at=now))

    other = store.begin("clinic.issue_refund", "1.0.0", {"amount": "500.00"}, "alex", "same-key")
    assert other.kind == "conflict" and "different parameters" in other.reason
    assert store.begin("clinic.issue_refund", "1.0.0", {"amount": "10.00"}, "alex", "same-key").kind == "replay"


def test_a_headed_browser_is_released_once_the_person_closes_its_windows():
    """Found by the user: after reviewing a headed run and closing the tab or pressing Cmd+Q, Chrome for Testing stayed
    running, because Playwright's driver was never stopped (on macOS it ignores both while attached)."""
    class Page:
        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

        def wait_for_timeout(self, ms):
            time.sleep(ms / 1000)

    page = Page()

    class Browser:
        contexts = [type("Ctx", (), {"pages": [page]})()]

        def is_connected(self):
            return True

    threading.Timer(0.6, lambda: setattr(page, "closed", True)).start()
    started = time.monotonic()
    runtime._wait_until_closed_by_user(Browser(), max_s=30)
    assert 0.5 < time.monotonic() - started < 3  # returned because the window closed, not because of the backstop

    # and the backstop still applies if nobody ever closes it
    page.closed = False
    started = time.monotonic()
    runtime._wait_until_closed_by_user(Browser(), max_s=1.2)
    assert 1.0 < time.monotonic() - started < 3


def test_an_unreachable_system_is_explained_plainly_and_can_be_retried(monkeypatch):
    """Seen in the console: with the clinic not running, a failed run showed a raw Playwright line (net::ERR_CONNECTION_REFUSED)."""
    def refused(*a, **k):
        raise RuntimeError("Page.goto: net::ERR_CONNECTION_REFUSED at http://localhost:8100/legacy/login\nCall log:\n  - navigating")

    monkeypatch.setattr(runtime, "_replay_in_browser", refused)
    res, _ = runtime.run_replay("clinic.patient_lookup", {"mrn": "LK-100001"}, target="clinic", idempotency_key="down-1", enable_operator_console=False)

    assert res.status == ReplayStatus.HARD_FAILURE and res.error.code == "target_unreachable"
    assert "Couldn't reach http://localhost:8100" in res.error.message and "Nothing was changed" in res.error.message and "Playwright" not in res.error.message
    fake_browser(monkeypatch)
    again, _ = runtime.run_replay("clinic.patient_lookup", {"mrn": "LK-100001"}, target="clinic", idempotency_key="down-1", enable_operator_console=False)
    assert again.status == ReplayStatus.SUCCESS  # the key was freed, so retrying once the system is back just works
