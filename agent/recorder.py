"""Turns a successful discovery transcript into a saved, typed artifact. The model decided
WHAT to do (agent/loop.py); this module is the deterministic, non-LLM logic that decides HOW
that becomes steps/checkpoints/parameters -- see loop.py's docstring for the full rationale.

Two schema pieces are deliberately caller-provided, not inferred from the transcript:
capability_id/name/description/input_schema/output_schema/success_checkpoint/error_handling/
safety are the *contract* a human (or the code invoking discovery) decides on up front -- what
this capability is for, what it takes, what it returns. The model's job is only to figure out
*how* to make that happen against the live UI. This mirrors a real PRD-driven build: a human
specifies the contract, an agent figures out the implementation -- not the other way around.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from agent.loop import DiscoveryResult, RecordedAction
from artifacts_lib.schema import (
    ActionType,
    Artifact,
    CanarySpec,
    CapabilityTarget,
    CommitApproval,
    ErrorHandling,
    JSONSchemaObject,
    Preconditions,
    Provenance,
    SafetyMeta,
    Signal,
    SignalType,
    Step,
    StepRiskLevel,
)
from safety.risk import RiskClassifier

_risk_classifier = RiskClassifier()


def _parameterize(value: str, parameters: dict[str, str]) -> str:
    """Replace any concrete parameter value that appears literally in `value` with its
    {{name}} placeholder -- e.g. "10001" -> "{{member_id}}". This is the whole mechanism that
    turns one concrete recorded run into a reusable, parameterized capability."""
    # Longest value first, and only where the value stands alone: "20" must not be pulled out of "2026-03-04" or out of another input's value ("A-200021"
    # is not "A-20002"). A blind replace once corrupted an email that happened to contain a member id (WRITEUP incident on the Meridian branch).
    for name, concrete in sorted(parameters.items(), key=lambda kv: -len(kv[1] or "")):
        if concrete:
            value = re.sub(rf"(?<![A-Za-z0-9]){re.escape(concrete)}(?![A-Za-z0-9])", lambda _m, n=name: f"{{{{{n}}}}}", value)
    return value


_RECORD_ID_SEGMENT = re.compile(r"^(\d+|[A-Za-z]{1,4}-\d+)$")


def _generalize_path(path: str) -> str:
    """A URL checkpoint is a statement about *which page*, not which record: /legacy/patients/1 recorded during
    discovery would fail for every other patient. Numeric ids and business numbers (A-20003) become wildcards;
    ones that are inputs were already turned into {{placeholders}} by _parameterize."""
    return "/".join("*" if _RECORD_ID_SEGMENT.match(seg) else seg for seg in path.split("/"))


def _synthesize_checkpoint(recorded: RecordedAction, parameters: dict[str, str]) -> Signal | None:
    """A lightweight, deterministic checkpoint per recorded step -- not LLM-inferred. If the
    action's URL changed (an explicit navigate, or a click that triggered navigation), assert
    the new path. Otherwise, if it typed text, assert the field now holds that value.
    Everything else gets no per-step checkpoint -- Step.checkpoint is optional by design
    (artifacts_lib/schema.py), and Surface.act() already reports its own success/failure."""
    before_path = urlsplit(recorded.observed_before.url).path
    after_path = urlsplit(recorded.observed_after.url).path if recorded.observed_after else before_path
    if after_path != before_path:
        return Signal(type=SignalType.URL_MATCHES, value=f"**{_generalize_path(_parameterize(after_path, parameters))}")
    if recorded.action.kind == ActionType.TYPE and recorded.result.resolved_target is not None:
        return Signal(
            type=SignalType.ELEMENT_VALUE_EQUALS,
            target=recorded.result.resolved_target,
            value=_parameterize(recorded.action.params["text"], parameters),
        )
    return None


def _build_step(step_id: str, recorded: RecordedAction, parameters: dict[str, str], classifier: RiskClassifier = _risk_classifier,
                optional_inputs: frozenset[str] = frozenset()) -> Step:
    action = recorded.action
    target = recorded.result.resolved_target
    params = {k: (_parameterize(v, parameters) if isinstance(v, str) else v) for k, v in action.params.items()}
    risk = classifier.classify(
        action.kind,
        semantic_description=target.semantic_description if target else None,
        current_path=urlsplit(recorded.observed_before.url).path,
    )
    only = re.fullmatch(r"\{\{(\w+)\}\}", str(params.get("text", params.get("value", ""))))
    when_present = only.group(1) if only and action.kind in (ActionType.TYPE, ActionType.SELECT) and only.group(1) in optional_inputs else None
    return Step(
        step_id=step_id,
        when_present=when_present,
        action=action.kind,
        target=target,
        params=params,
        output_binding=recorded.output_name,
        checkpoint=_synthesize_checkpoint(recorded, parameters),
        risk_level=risk,
        idempotent=(risk != StepRiskLevel.IRREVERSIBLE),
    )


def build_artifact(
    result: DiscoveryResult,
    parameters: dict[str, str],
    *,
    capability_id: str,
    version: str,
    name: str,
    description: str,
    target: CapabilityTarget,
    input_schema: JSONSchemaObject,
    output_schema: JSONSchemaObject,
    success_checkpoint: Signal,
    error_handling: ErrorHandling,
    safety: SafetyMeta,
    discovered_by: str,
    discovery_run_id: str,
    preconditions: Preconditions | None = None,
    success_output_defaults: dict[str, str] | None = None,
    risk_classifier: RiskClassifier | None = None,
    canary: CanarySpec | None = None,
) -> Artifact:
    if result.stop_reason != "finished":
        raise ValueError(
            f"cannot build an artifact from a discovery run that did not finish successfully "
            f"(stop_reason={result.stop_reason!r}, reasoning={result.reasoning!r})"
        )
    optional_inputs = frozenset(k for k in input_schema.properties if k not in input_schema.required)
    steps = [_build_step(f"s{i + 1}", recorded, parameters, risk_classifier or _risk_classifier, optional_inputs) for i, recorded in enumerate(result.transcript)]
    approvals = [
        CommitApproval(step_id=f"s{i + 1}", approver=r.commit_approval.approver or "unknown", mode=r.commit_approval.mode, at=datetime.now(UTC))
        for i, r in enumerate(result.transcript) if r.commit_approval is not None and r.commit_approval.approved
    ]
    return Artifact(
        capability_id=capability_id,
        version=version,
        name=name,
        description=description,
        target=target,
        preconditions=preconditions,
        input_schema=input_schema,
        output_schema=output_schema,
        success_checkpoint=success_checkpoint,
        steps=steps,
        error_handling=error_handling,
        safety=safety,
        success_output_defaults=success_output_defaults or {},
        canary=canary,
        provenance=Provenance(
            discovered_by=discovered_by,
            discovery_run_id=discovery_run_id,
            created_at=datetime.now(UTC),
            reviewed=False,
            note="LLM-discovered; not yet human-reviewed.",
            commit_approvals=approvals,
        ),
    )
