"""Approving or rejecting a run: the store checks the mechanics (open request, a reason, not your own run);
this adds the policy check that the approver is on the roster at a high enough tier. Every front door
(CLI today, the API later) goes through `decide_run` so none of them can skip the roster check."""
from __future__ import annotations

import logging
from typing import Any, Literal

import observability

from runs.store import ApprovalError, NotPermitted, RunStore
from safety.config import PolicyConfig, may_approve, role_allows


def may_act_on(policy: PolicyConfig, tier: str, requested_by: str, by: str, role: str | None) -> tuple[bool, str]:
    """Who may decide a request, or operate the page of a run waiting for one: someone at the request's tier, who is not the person who made it. An admin may act on
    their own request: they answer for everything anyway, so a second person adds nothing."""
    allowed, why = role_allows(role, tier) if role else may_approve(policy, by, tier)
    if not allowed:
        return False, why
    if by == requested_by and role != "admin":
        return False, "the person who requested a run cannot approve it"
    return True, ""


def resolve_run(store: RunStore, policy: PolicyConfig, run_id: str, outcome: Literal["committed", "not_committed"], by: str, reason: str,
                role: str | None = None) -> dict[str, Any]:
    """Settling an unresolved commit decides whether a key is closed or free to retry, so it needs the same roster check
    as an approval: at the capability's own tier, and never below an operator."""
    run = store.get(run_id)
    if run is None:
        raise ApprovalError(f"unknown run {run_id}")
    tier = policy.for_capability(run["capability_id"]).approval
    allowed, why = role_allows(role, "operator" if tier == "live" else tier) if role else may_approve(policy, by, "operator" if tier == "live" else tier)
    if not allowed:
        raise NotPermitted(why)
    settled = store.resolve(run_id, outcome, by, reason)
    observability.log("run.resolved", logging.WARNING, run_id=run_id, capability_id=run["capability_id"], outcome=outcome, resolved_by=by)
    return settled


def decide_run(store: RunStore, policy: PolicyConfig, run_id: str, decision: Literal["approved", "rejected"], by: str, reason: str,
               role: str | None = None) -> dict[str, Any]:
    approvals = store.approvals_for(run_id)
    open_request = next((a for a in reversed(approvals) if a["decision"] is None), None)
    if open_request is None:
        raise ApprovalError(f"run {run_id} has no open approval request")
    allowed, why = role_allows(role, open_request["tier"]) if role else may_approve(policy, by, open_request["tier"])
    if not allowed:
        raise NotPermitted(why)
    run = store.decide(run_id, decision, by, reason, allow_self=role == "admin")
    observability.log("approval.decided", run_id=run_id, capability_id=run["capability_id"], decision=decision, decided_by=by,
                      role=role or policy.approvers.get(by), tier=open_request["tier"], requested_by=run["requested_by"])
    return run


def decide_at_step(store: RunStore, policy: PolicyConfig, session: Any, run_id: str, decision: Literal["approved", "rejected", "completed", "resumed"], by: str, reason: str,
                   role: str | None = None) -> dict[str, Any]:
    """A decision on a run that is waiting just before its irreversible step: approve it (the run does the step), reject it, or say the step was done by hand on the
    page. The same rules as `decide_run`: the right tier, a reason, and not the person who asked (unless an admin). The waiting run is told first, because that is what
    settles it: if it already timed out or was stopped, the decision is refused, so a record never says "approved" for a step that did not go ahead."""
    asked = (session.snapshot().get("approval") if session is not None else None)
    if asked is None:
        raise ApprovalError(f"run {run_id} is not waiting for an approval; it may have finished, timed out or been stopped")
    allowed, why = may_act_on(policy, asked["tier"], asked["requested_by"], by, role)
    if not allowed:
        if "requested a run" in why:
            raise ApprovalError(why)
        raise NotPermitted(why)
    if not reason.strip():
        raise ApprovalError("a decision needs a reason")
    if not session.decide(decision, by, reason):
        raise ApprovalError(f"run {run_id} is no longer waiting for an approval; it may have timed out or been stopped")
    row = store.decide_at_step(run_id, decision, by, reason, allow_self=role == "admin")
    observability.log("approval.decided", run_id=run_id, decision=decision, decided_by=by, role=role or policy.approvers.get(by), tier=asked["tier"],
                      requested_by=asked["requested_by"], at_step=True)
    return row
