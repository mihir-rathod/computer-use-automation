"""Idempotency keys, approvals, caps, policy loading and PII redaction. No browser except where noted:
the browser path is replaced by a counter, so "no browser was launched" is something a test can assert."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import runtime
from artifacts_lib.storage import load_artifact
from evidence_lib.redaction import Redactor
from replay.result import ReplayError, ReplayResult, ReplayStatus
from runs.store import ABANDONED, APPROVED, PENDING_APPROVAL, ApprovalError, RunError, RunStore
from safety.config import PolicyConfig, check_params, may_approve, required_approval

pytestmark = pytest.mark.usefixtures("mockbank_runtime")  # these run MockBank artifacts through the runtime; see conftest

FIXTURES = Path(__file__).parent / "fixtures"


def result(status: ReplayStatus, committed: bool = False, code: str | None = None, **kw) -> ReplayResult:
    now = datetime.now(UTC)
    return ReplayResult(status=status, capability_id="clinic.issue_refund", committed=committed,
                        commit_step="s6" if committed else None, started_at=now, finished_at=now,
                        error=ReplayError(message="x", code=code) if code else None, **kw)


@pytest.fixture
def store() -> RunStore:
    return RunStore()


def begin(store: RunStore, key: str | None = "k1", status: str = "running"):
    return store.begin("clinic.issue_refund", "1.0.0", {"amount": "20.00"}, "alice", key, status=status)


# ---- idempotency --------------------------------------------------------------------------------

def test_same_key_after_success_is_answered_from_the_first_run(store):
    first = begin(store)
    store.finish(first.run["id"], result(ReplayStatus.SUCCESS, committed=True, outputs={"receipt": "R-1"}))

    again = begin(store)

    assert again.kind == "replay" and again.run["id"] == first.run["id"]
    assert store.stored_result(again.run).outputs == {"receipt": "R-1"}
    assert len(store.list_runs()) == 1


def test_same_key_while_running_conflicts(store):
    first = begin(store)
    assert begin(store).kind == "conflict" and begin(store).run["id"] == first.run["id"]


def test_different_keys_and_no_key_never_deduplicate(store):
    store.finish(begin(store, "a").run["id"], result(ReplayStatus.SUCCESS, committed=True))
    assert begin(store, "b").kind == "new"
    assert begin(store, None).kind == "new"
    assert begin(store, None).kind == "new"


def test_failure_before_commit_frees_the_key(store):
    first = begin(store)
    store.finish(first.run["id"], result(ReplayStatus.HARD_FAILURE, code="checkpoint_failed"))
    assert begin(store).kind == "new"


def test_failure_after_commit_does_not_free_the_key(store):
    first = begin(store)
    store.finish(first.run["id"], result(ReplayStatus.HARD_FAILURE, committed=True, code="timeout"))
    assert begin(store).kind == "replay"


def test_needs_review_blocks_the_key_until_a_person_settles_it(store):
    first = begin(store)
    store.finish(first.run["id"], result(ReplayStatus.NEEDS_REVIEW, code="ambiguous_commit"))

    blocked = begin(store)
    assert blocked.kind == "conflict" and "resolve" in blocked.reason

    with pytest.raises(RunError):
        store.resolve(first.run["id"], "not_committed", "dana", "  ")
    store.resolve(first.run["id"], "not_committed", "dana", "checked the refund ledger: nothing posted")
    assert store.get(first.run["id"])["status"] == ABANDONED
    assert begin(store).kind == "new"


def test_resolving_as_committed_keeps_the_key_closed(store):
    first = begin(store)
    store.finish(first.run["id"], result(ReplayStatus.NEEDS_REVIEW, code="ambiguous_commit"))
    store.resolve(first.run["id"], "committed", "dana", "refund is in the ledger")
    assert begin(store).kind == "replay"
    with pytest.raises(RunError):
        store.resolve(first.run["id"], "committed", "dana", "again")  # already settled


def test_two_concurrent_claims_create_one_run(tmp_path):
    import threading

    path = tmp_path / "shared.db"
    stores = [RunStore(path) for _ in range(6)]
    kinds: list[str] = []

    def go(s: RunStore) -> None:
        kinds.append(s.begin("clinic.issue_refund", "1.0.0", {}, "a", "same-key").kind)

    threads = [threading.Thread(target=go, args=(s,)) for s in stores]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert sorted(kinds) == ["conflict"] * 5 + ["new"]


def test_daily_commit_count_only_counts_committed_runs_today(store):
    for committed in (True, True, False):
        r = begin(store, None)
        store.finish(r.run["id"], result(ReplayStatus.SUCCESS if committed else ReplayStatus.HARD_FAILURE, committed=committed))
    assert store.committed_today("clinic.issue_refund") == 2
    assert store.committed_today("clinic.cancel_appointment") == 0


# ---- approvals ----------------------------------------------------------------------------------

def pending(store: RunStore, requester: str = "alice") -> str:
    run = store.begin("clinic.issue_refund", "1.0.0", {}, requester, None, status=PENDING_APPROVAL).run["id"]
    store.request_approval(run, "supervisor", requester)
    return run


def test_approval_records_who_when_and_why(store):
    run = pending(store)
    store.decide(run, "approved", "suzie.visor", "invoice checked, duplicate payment confirmed")

    approval = store.approvals_for(run)[0]
    assert (approval["decision"], approval["decided_by"]) == ("approved", "suzie.visor")
    assert approval["reason"].startswith("invoice checked") and approval["decided_at"]
    assert store.get(run)["status"] == APPROVED


def test_requester_cannot_approve_their_own_run(store):
    run = pending(store, requester="suzie.visor")
    with pytest.raises(ApprovalError, match="cannot approve"):
        store.decide(run, "approved", "suzie.visor", "me")
    assert store.get(run)["status"] == PENDING_APPROVAL


def test_decision_needs_a_reason_and_can_only_be_made_once(store):
    run = pending(store)
    with pytest.raises(ApprovalError, match="reason"):
        store.decide(run, "approved", "suzie.visor", "")
    store.decide(run, "rejected", "suzie.visor", "amount does not match the claim")
    with pytest.raises(ApprovalError, match="not waiting"):
        store.decide(run, "approved", "suzie.visor", "changed my mind")


def test_tiers_follow_the_roster():
    policy = PolicyConfig.load()
    assert may_approve(policy, "suzie.visor", "supervisor")[0]
    assert may_approve(policy, "smooth.operator", "operator")[0]
    ok, why = may_approve(policy, "smooth.operator", "supervisor")
    assert not ok and "needs a supervisor" in why
    assert not may_approve(policy, "nobody", "operator")[0]
    assert may_approve(policy, "suzie.visor", "operator")[0]  # a supervisor outranks an operator tier


# ---- policy -------------------------------------------------------------------------------------

def test_shipped_policy_loads_and_has_the_expected_shape():
    policy = PolicyConfig.load()
    assert policy.for_capability("clinic.issue_refund").approval == "supervisor"
    assert policy.for_capability("clinic.issue_refund").caps.max_param == {"amount": 1000.0}
    assert policy.for_capability("something.unlisted").approval == "supervisor"  # an unlisted capability with a commit step needs a supervisor


def test_bad_policy_is_rejected_at_load(tmp_path):
    bad = tmp_path / "p.yaml"
    bad.write_text("capabilities:\n  a.b:\n    approval: whenever\n")
    with pytest.raises(ValueError):
        PolicyConfig.load(bad)
    bad.write_text("redaction:\n  patterns:\n    x: '('\n")
    with pytest.raises(ValueError, match="does not compile"):
        PolicyConfig.load(bad)


def test_caps_reject_an_oversized_or_non_numeric_amount():
    policy = PolicyConfig.load()
    assert check_params(policy, "clinic.issue_refund", {"amount": "150.00"}) == []
    assert check_params(policy, "clinic.issue_refund", {"amount": "$1,000"}) == []
    assert check_params(policy, "clinic.issue_refund", {"amount": "1000.01"})[0].code == "policy_cap_exceeded"
    assert check_params(policy, "clinic.issue_refund", {"amount": "lots"})[0].code == "policy_param_invalid"
    assert check_params(policy, "mockbank.login", {"amount": "9999"}) == []


def test_approval_is_only_required_when_the_capability_has_an_irreversible_step():
    policy = PolicyConfig.load()
    assert required_approval(policy, load_artifact(FIXTURES / "mockbank.member_balance_lookup.json")) is None


def test_policy_keywords_extend_the_classifier():
    from artifacts_lib.schema import ActionType, StepRiskLevel
    from safety.risk import RiskClassifier

    assert RiskClassifier().classify(ActionType.CLICK, "Issue refund") == StepRiskLevel.SAFE
    assert RiskClassifier(extra_domain=["refund"]).classify(ActionType.CLICK, "Issue refund") == StepRiskLevel.IRREVERSIBLE
    assert RiskClassifier(extra_domain=["refund"]).classify(ActionType.CLICK, "Confirm") == StepRiskLevel.IRREVERSIBLE  # built-ins stay


# ---- redaction ----------------------------------------------------------------------------------

def test_redactor_scrubs_patterns_and_sensitive_fields_recursively():
    r = Redactor.from_config(PolicyConfig.load().redaction)
    out = r.scrub({"params": {"amount": "20", "address": "1 Main St", "dob": "1990-01-01"},
                   "text": "call 555-123-4567, mail a@b.com, ssn 123-45-6789, card 4111 1111 1111 1111",
                   "rows": [{"note": "ok"}, {"note": "x@y.org"}]})
    assert out["params"] == {"amount": "20", "address": "[REDACTED:field]", "dob": "[REDACTED:field]"}
    for leaked in ("555-123-4567", "a@b.com", "123-45-6789", "4111"):
        assert leaked not in json.dumps(out)
    assert out["rows"][1]["note"] == "[REDACTED:email]"


def test_evidence_logger_redacts_what_it_writes(tmp_path):
    from evidence_lib.logger import EvidenceLogger

    with EvidenceLogger(tmp_path, redactor=Redactor.from_config(PolicyConfig.load().redaction)) as log:
        log.log("replay", "action", params={"phone": "555-123-4567"}, note="mail a@b.com")
    line = (tmp_path / "log.jsonl").read_text()
    assert "555-123-4567" not in line and "a@b.com" not in line and "[REDACTED:phone]" in line


# ---- run_replay: policy, approval and dedupe before any browser -----------------------------------

@pytest.fixture
def no_browser(monkeypatch):
    calls: list[dict] = []

    def fake(artifact, params, **kw):
        calls.append({"params": params, **kw})
        now = datetime.now(UTC)
        irreversible = any(s.risk_level == "irreversible" for s in artifact.steps)
        return ReplayResult(status=ReplayStatus.SUCCESS, capability_id=artifact.capability_id, outputs={"status": "ok"},
                            committed=irreversible, commit_step="s6" if irreversible else None, started_at=now, finished_at=now)

    monkeypatch.setattr(runtime, "_replay_in_browser", fake)
    return calls


def test_read_only_run_with_a_key_runs_once_then_is_deduplicated(no_browser, tmp_path):
    kw = dict(idempotency_key="lookup-1", evidence_dir=tmp_path / "e1")
    first, _ = runtime.run_replay("mockbank.member_balance_lookup", {"member_id": "10001"}, **kw)
    second, _ = runtime.run_replay("mockbank.member_balance_lookup", {"member_id": "10001"}, **{**kw, "evidence_dir": tmp_path / "e2"})

    assert first.status == ReplayStatus.SUCCESS and not first.deduplicated
    assert second.deduplicated and second.run_id == first.run_id
    assert len(no_browser) == 1


def test_oversized_amount_is_refused_before_any_browser(no_browser, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "required_approval", lambda *a: None)  # isolate the cap check from approval
    policy = PolicyConfig.load()
    policy.capabilities["mockbank.open_subaccount"] = policy.for_capability("clinic.issue_refund")

    res, _ = runtime.run_replay("mockbank.open_subaccount", {"member_id": "10001", "account_type": "savings", "initial_deposit": 250.0},
                                policy=policy, evidence_dir=tmp_path / "e")
    # the cap is on `amount`; this capability's parameter is named differently, so it is not capped: proves caps are per-parameter
    assert res.status == ReplayStatus.SUCCESS

    policy.capabilities["mockbank.open_subaccount"].caps.max_param = {"initial_deposit": 200.0}
    res, _ = runtime.run_replay("mockbank.open_subaccount", {"member_id": "10001", "account_type": "savings", "initial_deposit": 250.0},
                                policy=policy, evidence_dir=tmp_path / "e2")
    assert res.status == ReplayStatus.HARD_FAILURE and res.error.code == "policy_cap_exceeded"
    assert len(no_browser) == 1  # only the first, uncapped call got a browser


def test_supervisor_tier_waits_for_approval_then_runs_with_the_commit_step_confirmed(no_browser, tmp_path):
    policy = PolicyConfig.load()
    policy.capabilities["mockbank.open_subaccount"] = policy.for_capability("clinic.issue_refund")
    policy.capabilities["mockbank.open_subaccount"].caps.max_param = {}
    store = runtime.default_store()
    params = {"member_id": "10001", "account_type": "savings", "initial_deposit": 50.0}

    first, evidence = runtime.run_replay("mockbank.open_subaccount", params, policy=policy, requested_by="alice",
                                         idempotency_key="open-1", evidence_dir=tmp_path / "e")
    assert first.status == ReplayStatus.PENDING_APPROVAL and first.approval_tier == "supervisor"
    assert no_browser == []
    assert [e["tier"] for e in store.pending_approvals()] == ["supervisor"]
    assert (evidence / "result.json").exists()

    # a retry while it waits does not create a second run
    again, _ = runtime.run_replay("mockbank.open_subaccount", params, policy=policy, requested_by="alice", idempotency_key="open-1")
    assert again.error.code == "idempotency_conflict" and again.run_id == first.run_id

    store.decide(first.run_id, "approved", "suzie.visor", "member confirmed by phone")
    done, _ = runtime.run_replay("mockbank.open_subaccount", {}, policy=policy, resume_run_id=first.run_id)

    assert done.status == ReplayStatus.SUCCESS and done.run_id == first.run_id
    assert no_browser[0]["confirmed_steps"] and no_browser[0]["params"] == params  # the approved params, not the empty ones passed on resume
    assert store.get(first.run_id)["status"] == "success"

    replayed, _ = runtime.run_replay("mockbank.open_subaccount", params, policy=policy, requested_by="alice", idempotency_key="open-1")
    assert replayed.deduplicated and len(no_browser) == 1


def test_rejected_run_cannot_be_resumed(no_browser, tmp_path):
    policy = PolicyConfig.load()
    policy.capabilities["mockbank.open_subaccount"] = policy.for_capability("clinic.issue_refund")
    policy.capabilities["mockbank.open_subaccount"].caps.max_param = {}
    first, _ = runtime.run_replay("mockbank.open_subaccount", {"member_id": "10001", "account_type": "savings", "initial_deposit": 5.0},
                                  policy=policy, requested_by="alice", evidence_dir=tmp_path / "e")
    runtime.default_store().decide(first.run_id, "rejected", "suzie.visor", "not authorised by the member")
    with pytest.raises(ValueError, match="not approved"):
        runtime.run_replay("mockbank.open_subaccount", {}, policy=policy, resume_run_id=first.run_id)
    assert no_browser == []


def test_daily_commit_cap_refuses_the_next_run(no_browser, tmp_path):
    policy = PolicyConfig.load()
    cap = policy.for_capability("clinic.issue_refund").model_copy(deep=True)
    cap.approval, cap.caps.max_param, cap.caps.max_commits_per_day = "live", {}, 1
    policy.capabilities["mockbank.open_subaccount"] = cap
    params = {"member_id": "10001", "account_type": "savings", "initial_deposit": 5.0}

    first, _ = runtime.run_replay("mockbank.open_subaccount", params, policy=policy, evidence_dir=tmp_path / "a")
    second, _ = runtime.run_replay("mockbank.open_subaccount", params, policy=policy, evidence_dir=tmp_path / "b")

    assert first.status == ReplayStatus.SUCCESS and first.committed
    assert second.status == ReplayStatus.HARD_FAILURE and second.error.code == "policy_cap_exceeded"
    assert len(no_browser) == 1


def test_evidence_written_by_run_replay_is_redacted_but_the_returned_result_is_not(no_browser, tmp_path, monkeypatch):
    def fake(artifact, params, **kw):
        now = datetime.now(UTC)
        return ReplayResult(status=ReplayStatus.SUCCESS, capability_id=artifact.capability_id, outputs={"status": "found", "email": "a@b.com"},
                            started_at=now, finished_at=now)

    monkeypatch.setattr(runtime, "_replay_in_browser", fake)
    res, evidence = runtime.run_replay("mockbank.member_balance_lookup", {"member_id": "10001"}, evidence_dir=tmp_path / "e")

    assert res.outputs["email"] == "a@b.com"
    assert "a@b.com" not in (evidence / "result.json").read_text()


def test_decide_run_enforces_the_roster_and_tier(store):
    from runs.approvals import decide_run

    policy = PolicyConfig.load()
    run = pending(store, requester="alice")  # a supervisor-tier request

    with pytest.raises(ApprovalError, match="needs a supervisor"):
        decide_run(store, policy, run, "approved", "smooth.operator", "looks fine")
    with pytest.raises(ApprovalError, match="not on the approver roster"):
        decide_run(store, policy, run, "approved", "mallory", "trust me")
    assert store.get(run)["status"] == PENDING_APPROVAL

    decide_run(store, policy, run, "approved", "suzie.visor", "checked the invoice")
    assert store.get(run)["status"] == APPROVED


def test_secret_parameter_values_are_scrubbed_from_free_text():
    r = Redactor.from_config(PolicyConfig.load().redaction).with_secrets_from({"password": "hunter2-longer", "username": "frontdesk"})
    out = r.scrub({"reasoning": "I typed hunter2-longer into the password box for frontdesk"})
    assert "hunter2-longer" not in json.dumps(out) and "frontdesk" in json.dumps(out)


def test_a_database_from_before_the_rename_keeps_its_discovery_sessions(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE teach_sessions (id TEXT PRIMARY KEY, created_by TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, capability_id TEXT NOT NULL,"
               " version TEXT, target TEXT NOT NULL, contract_json TEXT NOT NULL, evidence_dir TEXT, stop_reason TEXT, reasoning TEXT, steps INTEGER, error TEXT, verify_json TEXT,"
               " lint_json TEXT, commit_request_json TEXT, commit_decision TEXT, commit_decided_by TEXT, commit_reason TEXT, retry_of TEXT)")
    db.execute("INSERT INTO teach_sessions(id, created_by, created_at, status, capability_id, target, contract_json) VALUES('teach_old_1','dana','2026-10-05T00:00:00','ready','clinic.x_y','clinic','{}')")
    db.commit()
    db.close()
    store = RunStore(path)
    assert store.discovery_get("teach_old_1")["capability_id"] == "clinic.x_y"
    assert store.discovery_create("dana", "clinic.z_z", "clinic", "{}")["id"].startswith("disc_")
