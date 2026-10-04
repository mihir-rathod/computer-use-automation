"""Approving or rejecting a run: the store checks the mechanics (open request, a reason, not your own run);
this adds the policy check that the approver is on the roster at a high enough tier. Every front door
(CLI today, the API later) goes through `decide_run` so none of them can skip the roster check."""
from __future__ import annotations

from typing import Any, Literal

from runs.store import ApprovalError, RunStore
from safety.config import PolicyConfig, may_approve


def resolve_run(store: RunStore, policy: PolicyConfig, run_id: str, outcome: Literal["committed", "not_committed"], by: str, reason: str) -> dict[str, Any]:
    """Settling an unresolved commit decides whether a key is closed or free to retry, so it needs the same roster check
    as an approval: at the capability's own tier, and never below an operator."""
    run = store.get(run_id)
    if run is None:
        raise ApprovalError(f"unknown run {run_id}")
    tier = policy.for_capability(run["capability_id"]).approval
    allowed, why = may_approve(policy, by, "operator" if tier == "live" else tier)
    if not allowed:
        raise ApprovalError(why)
    return store.resolve(run_id, outcome, by, reason)


def decide_run(store: RunStore, policy: PolicyConfig, run_id: str, decision: Literal["approved", "rejected"], by: str, reason: str) -> dict[str, Any]:
    approvals = store.approvals_for(run_id)
    open_request = next((a for a in reversed(approvals) if a["decision"] is None), None)
    if open_request is None:
        raise ApprovalError(f"run {run_id} has no open approval request")
    allowed, why = may_approve(policy, by, open_request["tier"])
    if not allowed:
        raise ApprovalError(why)
    return store.decide(run_id, decision, by, reason)
