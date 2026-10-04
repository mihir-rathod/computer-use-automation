"""`artifact validate`: checks a saved artifact for the problems schema validation can't see, the ones
that make an artifact unsafe to promote or hard to review. Errors block promotion; warnings don't."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from artifacts_lib.schema import (
    ActionType, Artifact, CapabilityRiskLevel, LocatorStrategy, Signal, SignalType, StepRiskLevel, Target,
)

_TEMPLATE = re.compile(r"\{\{(\w+)\}\}")
_SENSITIVE_WORDS = ("password", "secret", "token", "passcode")
_RECORD_ID = re.compile(r"^(\d+|[A-Za-z]{1,4}-\d+)$")


@dataclass(frozen=True)
class Finding:
    level: Literal["error", "warning"]
    code: str
    message: str
    step_id: str | None = None

    def __str__(self) -> str:
        where = f" [{self.step_id}]" if self.step_id else ""
        return f"{self.level.upper():7} {self.code}{where}: {self.message}"


def _template_vars(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(_TEMPLATE.findall(value))
    if isinstance(value, dict):
        return set().union(*[_template_vars(v) for v in value.values()]) if value else set()
    if isinstance(value, list):
        return set().union(*[_template_vars(v) for v in value]) if value else set()
    return set()


def _signals(artifact: Artifact) -> list[Signal]:
    found = [artifact.success_checkpoint]
    found += [s.checkpoint for s in artifact.steps if s.checkpoint]
    found += [r.signal for r in artifact.error_handling.business_outcomes]
    found += [r.signal for r in artifact.error_handling.recoverable]
    return found


def _ambiguous(target: Target) -> bool:
    return all(
        loc.strategy == LocatorStrategy.ROLE and "[name=" not in loc.value for loc in target.locators
    )


def lint_artifact(artifact: Artifact) -> list[Finding]:
    out: list[Finding] = []
    err = lambda code, msg, step=None: out.append(Finding("error", code, msg, step))  # noqa: E731
    warn = lambda code, msg, step=None: out.append(Finding("warning", code, msg, step))  # noqa: E731

    if artifact.steps[0].action != ActionType.NAVIGATE:
        warn("no-start-navigation", "the first step is not a navigation, so replay depends on whatever page the browser is already on", artifact.steps[0].step_id)

    irreversible = [s for s in artifact.steps if s.risk_level == StepRiskLevel.IRREVERSIBLE]
    if irreversible and not artifact.safety.requires_confirmation:
        err("irreversible-unconfirmed", "has irreversible steps but safety.requires_confirmation is false")
    if irreversible and artifact.safety.risk_level == CapabilityRiskLevel.READ_ONLY:
        err("readonly-has-irreversible", "is declared read_only but contains irreversible steps")
    for step in irreversible:
        if step.idempotent:
            err("irreversible-idempotent", "irreversible step is marked idempotent, so it could be auto-retried and double-post", step.step_id)
        if not artifact.provenance.commit_approvals and artifact.provenance.discovered_by != "hand_written":
            warn("commit-not-approved", "irreversible step has no recorded approval in provenance", step.step_id)
    if artifact.safety.risk_level == CapabilityRiskLevel.STATE_CHANGING and not artifact.error_handling.business_outcomes:
        warn("no-business-outcomes", "state-changing capability declares no business outcomes (not found, denied, rejected...)")

    if artifact.canary is not None and (irreversible or artifact.safety.risk_level != CapabilityRiskLevel.READ_ONLY):
        err("canary-not-read-only", "a canary replays on a schedule without approval, so only a read-only capability with no irreversible step may have one")

    declared = set(artifact.input_schema.properties)
    used: set[str] = set()
    for step in artifact.steps:
        used |= _template_vars(step.params)
    for signal in _signals(artifact):
        used |= _template_vars(signal.value)
    for name in sorted(used - declared):
        err("undeclared-template-variable", f"'{{{{{name}}}}}' is used but not declared in input_schema")
    for name in sorted(declared - used):
        warn("unused-input", f"input '{name}' is declared but no step or signal uses it")

    produced = {s.output_binding for s in artifact.steps if s.output_binding}
    produced |= {r.output_field for r in artifact.error_handling.business_outcomes} | set(artifact.success_output_defaults)
    for name in sorted(set(artifact.output_schema.properties) - produced):
        warn("output-never-set", f"output '{name}' is declared but nothing sets it")

    for step in artifact.steps:
        if step.action == ActionType.TYPE and step.target:
            desc = step.target.semantic_description.lower()
            if any(w in desc for w in _SENSITIVE_WORDS) and "{{" not in str(step.params.get("text", "")):
                err("secret-literal", "types a literal value into a password-like field instead of a {{parameter}}", step.step_id)
        if step.target:
            if _ambiguous(step.target):
                warn("ambiguous-locator", f"only role-based locators without an accessible name ({step.target.semantic_description!r}); likely ambiguous on pages with several of them", step.step_id)
            elif len(step.target.locators) == 1 and step.target.locators[0].strategy in (LocatorStrategy.CSS, LocatorStrategy.XPATH):
                warn("no-fallback-locator", "a single CSS/XPath locator with no semantic fallback", step.step_id)

    for signal_owner, signal in [(None, artifact.success_checkpoint)] + [(s.step_id, s.checkpoint) for s in artifact.steps if s.checkpoint]:
        if signal.type == SignalType.URL_MATCHES and any(_RECORD_ID.match(seg) for seg in signal.value.split("/")):
            warn("url-checkpoint-record-id", f"URL checkpoint {signal.value!r} names one specific record; it will fail for any other", signal_owner)

    sc = artifact.success_checkpoint
    if sc.type == SignalType.TEXT_PRESENT and len(sc.value.strip()) < 6:
        warn("weak-success-checkpoint", f"success checkpoint text {sc.value!r} is short enough to appear on unrelated pages")
    if not artifact.provenance.reviewed:
        warn("unreviewed", "has not been human-reviewed")
    return out


def has_errors(findings: list[Finding]) -> bool:
    return any(f.level == "error" for f in findings)
