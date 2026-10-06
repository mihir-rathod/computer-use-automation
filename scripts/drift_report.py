#!/usr/bin/env python
"""Measures how the checked-in clinic artifacts fare under each UI drift level, and how far the human-approved repair
loop gets. Not a claim, a measurement: run it, read the table.

For every capability and drift level it replays against a running clinic, and whenever the replay fails on an
unresolved locator it approves the platform's repair proposal (as the right tier of approver) and replays again,
up to MAX_ROUNDS. It reports how many proposals that took, whether each was confident, and how it ended.

    uv run uvicorn clinic.app:app --port 8100 &
    uv run python scripts/drift_report.py [--levels 1 2 3] [--capabilities clinic.issue_refund ...]
"""
from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path

import httpx

os.environ["RUN_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "drift_report.db")
import runtime  # noqa: E402  (after RUN_DB_PATH)
from repair.apply import approve_repair  # noqa: E402
from repair.propose import RepairProposal  # noqa: E402
from replay.result import ReplayStatus  # noqa: E402
from runs.approvals import decide_run  # noqa: E402
from safety.config import PolicyConfig  # noqa: E402

BASE = os.environ.get("CLINIC_BASE_URL", "http://localhost:8100")
MAX_ROUNDS = 14
REPO = Path(__file__).resolve().parent.parent
CASES = {
    "clinic.patient_lookup": ("clinic", {"mrn": "LK-100002"}),
    "clinic.update_patient_contact": ("clinic", {"mrn": "LK-100002", "phone": "206-555-0142", "email": "x.y@example.test", "address": "9 Fir Ave, Oakridge, WA 98021"}),
    "clinic.reschedule_appointment": ("clinic", {"appointment": "A-20003", "date": "2026-03-12", "time": "11:30"}),
    "clinic.cancel_appointment": ("clinic", {"appointment": "A-20002", "reason": "patient_request"}),
    "clinic.issue_refund": ("clinic", {"invoice": "INV-30001", "amount": "25.00", "reason": "duplicate_payment"}),
    "clinic.submit_claim": ("clinic", {"invoice": "INV-30002", "amount": "124.00"}),
    "clinic.write_off_balance": ("clinic_supervisor", {"invoice": "INV-30002", "amount": "40.00", "reason": "uncollectible"}),
}
EFFECT = {"clinic.cancel_appointment": "appointment.cancel", "clinic.issue_refund": "refund.issue", "clinic.submit_claim": "claim.submit",
          "clinic.write_off_balance": "writeoff.apply", "clinic.update_patient_contact": "patient.update_contact", "clinic.reschedule_appointment": "appointment.reschedule"}


def effects(action: str) -> int:
    return len(httpx.get(f"{BASE}/_test/audit", params={"action": action, "effects_only": True}, timeout=5).json()["items"])


def one(cap: str, level: int, tmp: Path) -> dict:
    target, params = CASES[cap]
    httpx.post(f"{BASE}/_test/reset", timeout=5)
    httpx.post(f"{BASE}/_test/chaos/duplicate-guard", json={"enabled": False}, timeout=5)
    httpx.post(f"{BASE}/_test/drift", json={"level": level, "seed": f"d{level}"}, timeout=5)
    arts = tmp / f"{cap}-{level}"
    shutil.copytree(REPO / "artifacts", arts)
    store, policy = runtime.default_store(), PolicyConfig.load()
    row = {"capability": cap, "level": level, "repairs": 0, "confident": 0, "unconfident": 0, "outcome": "?", "effects": None, "repaired_steps": []}
    for round_ in range(MAX_ROUNDS):
        res, _ = runtime.run_replay(cap, params, target=target, base_url=BASE, enable_operator_console=False, artifacts_dir=arts,
                                    requested_by="drift-report", evidence_dir=tmp / f"ev-{cap}-{level}-{round_}")
        if res.status == ReplayStatus.PENDING_APPROVAL:
            decide_run(store, policy, res.run_id, "approved", "dana.okafor", "drift report")
            res, _ = runtime.run_replay(cap, {}, target=None, base_url=BASE, enable_operator_console=False, artifacts_dir=arts,
                                        resume_run_id=res.run_id, evidence_dir=tmp / f"ev-{cap}-{level}-{round_}b")
        if res.status in (ReplayStatus.SUCCESS, ReplayStatus.BUSINESS_OUTCOME):
            row["outcome"] = res.status.value
            break
        if not res.repair_proposal_id:
            row["outcome"] = f"stuck: {res.error.code if res.error else res.status.value}"
            break
        proposal = RepairProposal.model_validate_json(store.get_repair(res.repair_proposal_id)["proposal_json"])
        row["repairs"] += 1
        if not proposal.confident:
            row["unconfident"] += 1
            row["outcome"] = f"no confident match at {proposal.capability_id} {proposal.step_id}"
            break
        row["confident"] += 1
        row["repaired_steps"].append(f"{proposal.capability_id.split('.')[-1]}.{proposal.step_id}")
        approve_repair(store, policy, proposal.id, "dana.okafor", "drift report", arts)
    else:
        row["outcome"] = f"gave up after {MAX_ROUNDS} rounds"
    if cap in EFFECT:
        row["effects"] = effects(EFFECT[cap])
    httpx.delete(f"{BASE}/_test/drift", timeout=5)
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", type=int, nargs="*", default=[1, 2, 3])
    ap.add_argument("--capabilities", nargs="*", default=list(CASES))
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp())
    print("| capability | drift | proposals | confident | outcome | state changes | repaired steps |\n|---|---|---|---|---|---|---|")
    for cap in args.capabilities:
        for level in args.levels:
            r = one(cap, level, tmp)
            print(f"| {cap} | {level} | {r['repairs']} | {r['confident']} | {r['outcome']} | {r['effects'] if r['effects'] is not None else '-'} | {', '.join(r['repaired_steps']) or '-'} |", flush=True)
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
