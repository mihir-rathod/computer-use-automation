"""Scheduled read-only canary replays.

A canary is a known-good invocation (`Artifact.canary`: params plus the outputs they must produce) replayed on a
schedule against the live target, so UI drift is found by a harmless read at 3 a.m. rather than by a real request
at 9 a.m. Only capabilities with no irreversible step may have one; a canary never needs an approval and never
changes state. A failing canary records its repair proposal (if the failure was an unresolved locator) so a person
can approve the fix before real traffic hits the broken step."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import runtime
from artifacts_lib import storage
from artifacts_lib.schema import Artifact, StepRiskLevel
from replay.result import ReplayStatus
from runs.store import RunStore


@dataclass
class CanaryOutcome:
    capability_id: str
    version: str
    ok: bool
    status: str
    detail: str
    run_id: str | None = None
    repair_proposal_id: str | None = None


def canary_capabilities(artifacts_dir: Path = storage.DEFAULT_ARTIFACTS_DIR) -> list[Artifact]:
    return [a for a in storage.list_artifacts(artifacts_dir)
            if a.canary is not None and not any(s.risk_level == StepRiskLevel.IRREVERSIBLE for s in a.steps)]


def _mismatches(expect: dict[str, Any], outputs: dict[str, Any] | None) -> list[str]:
    outputs = outputs or {}
    return [f"{k}: expected {v!r}, got {outputs.get(k)!r}" for k, v in expect.items() if outputs.get(k) != v]


def run_canary(artifact: Artifact, *, target: str, store: RunStore | None = None, **run_kwargs: Any) -> CanaryOutcome:
    assert artifact.canary is not None
    store = store or runtime.default_store()
    result, _ = runtime.run_replay(
        artifact.capability_id, artifact.canary.params, target=target, requested_by="canary", enable_operator_console=False,
        store=store, **run_kwargs)
    problems = _mismatches(artifact.canary.expect, result.outputs)
    ok = result.status in (ReplayStatus.SUCCESS, ReplayStatus.BUSINESS_OUTCOME) and not problems
    detail = "ok" if ok else (result.error.message if result.error else "; ".join(problems) or result.status.value)
    outcome = CanaryOutcome(artifact.capability_id, artifact.version, ok, result.status.value, detail, result.run_id, result.repair_proposal_id)
    store.record_canary(outcome.capability_id, outcome.version, ok, outcome.status, detail, outcome.run_id, outcome.repair_proposal_id)
    return outcome


def run_all(*, target: str, store: RunStore | None = None, artifacts_dir: Path = storage.DEFAULT_ARTIFACTS_DIR, **run_kwargs: Any) -> list[CanaryOutcome]:
    return [run_canary(a, target=target, store=store, artifacts_dir=artifacts_dir, **run_kwargs) for a in canary_capabilities(artifacts_dir)]


def loop(every_s: float, *, target: str, **run_kwargs: Any) -> None:  # pragma: no cover -- a plain scheduler
    while True:
        for outcome in run_all(target=target, **run_kwargs):
            print(f"{'ok  ' if outcome.ok else 'FAIL'} {outcome.capability_id} {outcome.version}: {outcome.detail}"
                  + (f" (repair proposal {outcome.repair_proposal_id})" if outcome.repair_proposal_id else ""))
        time.sleep(every_s)
