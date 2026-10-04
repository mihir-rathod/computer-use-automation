"""Applying an approved repair: write a new artifact version, record who approved it and why, and promote it.

The proposal itself is stored in the run store; this module is the only place that turns one into an artifact
change, and it enforces the policy roster: a repair that touches an irreversible step needs a supervisor."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from artifacts_lib import storage
from artifacts_lib.lint import has_errors, lint_artifact
from repair.propose import RepairProposal
from runs.store import ApprovalError, NotPermitted, RunStore
from safety.config import PolicyConfig, may_approve, role_allows


def approve_repair(
    store: RunStore, policy: PolicyConfig, proposal_id: str, by: str, reason: str,
    artifacts_dir: Path = storage.DEFAULT_ARTIFACTS_DIR, promote: bool = True, role: str | None = None,
) -> dict[str, Any]:
    if not reason.strip():
        raise ApprovalError("approving a repair needs a reason")
    row = store.get_repair(proposal_id)
    if row is None:
        raise ApprovalError(f"unknown repair proposal {proposal_id}")
    if row["status"] != "pending":
        raise ApprovalError(f"repair {proposal_id} is already {row['status']}")
    proposal = RepairProposal.model_validate_json(row["proposal_json"])
    if not proposal.confident or proposal.new_target is None:
        raise ApprovalError("this proposal found no confident match, so there is nothing to approve; fix the artifact by hand or re-discover")

    tier = "supervisor" if proposal.touches_irreversible_step else "operator"
    allowed, why = role_allows(role, tier) if role else may_approve(policy, by, tier)
    if not allowed:
        raise NotPermitted(why)

    base = storage.load_artifact_by_id(proposal.capability_id, artifacts_dir, proposal.base_version)
    current = storage.current_version(proposal.capability_id, artifacts_dir)
    if current != proposal.base_version:
        raise ApprovalError(f"the proposal was made against {proposal.base_version} but {current} is now current; re-run to get a fresh proposal")

    def repaired_step(s):
        if s.step_id != proposal.step_id:
            return s
        update: dict[str, Any] = {"target": proposal.new_target}
        # a checkpoint that reads the very element being repaired (e.g. "the field now holds what was typed") points at the
        # same broken locators; leaving it would just move the failure from the action to its checkpoint
        if s.checkpoint is not None and s.checkpoint.target is not None and s.checkpoint.target.locators == proposal.old_target.locators:
            update["checkpoint"] = s.checkpoint.model_copy(update={"target": proposal.new_target})
        return s.model_copy(update=update)

    steps = [repaired_step(s) for s in base.steps]
    version = storage.next_version(proposal.capability_id, artifacts_dir)
    provenance = base.provenance.model_copy(update={
        "parent_version": base.version, "approved_by": by, "change_note": f"repair of {proposal.step_id}: {reason}",
        "repair_proposal_id": proposal.id, "reviewed": True,
    })
    repaired = base.model_copy(update={"version": version, "steps": steps, "provenance": provenance})
    findings = lint_artifact(repaired)
    if has_errors(findings):
        raise ApprovalError("the repaired artifact fails validation: " + "; ".join(str(f) for f in findings if f.level == "error"))

    try:
        storage.save_artifact(repaired, artifacts_dir, make_current=promote, by=by, reason=f"repair {proposal.id}: {reason}")
    except storage.VersionExists:
        raise ApprovalError("another repair was applied at the same moment; re-run to get a fresh proposal") from None
    store.decide_repair(proposal_id, "approved", by, reason, new_version=version)
    return {"capability_id": proposal.capability_id, "from": base.version, "to": version, "promoted": promote, "step_id": proposal.step_id}


def reject_repair(store: RunStore, proposal_id: str, by: str, reason: str) -> None:
    if not reason.strip():
        raise ApprovalError("rejecting a repair needs a reason")
    row = store.get_repair(proposal_id)
    if row is None or row["status"] != "pending":
        raise ApprovalError(f"repair {proposal_id} is not pending")
    store.decide_repair(proposal_id, "rejected", by, reason)
