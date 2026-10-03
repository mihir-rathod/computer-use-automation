"""Combines the allowlist and risk classifier into the single object Surface.act() consults --
one enforcement point for both discovery and replay (wired into Surface.act() itself, not
into the replay engine alone).

Handling the risky class: this project blocks by default (conservative). An irreversible
action only proceeds if the caller explicitly passes Action.confirmed=True -- i.e. something
upstream (a human, via the operator console; or an orchestration script that has decided to
demonstrate the full flow for evidence) has already obtained approval. Surface itself never
grants that approval on its own.
"""
from __future__ import annotations

from dataclasses import dataclass

from artifacts_lib.schema import ActionType, StepRiskLevel
from safety.allowlist import AllowlistPolicy, AllowlistViolation
from safety.risk import RiskClassifier


@dataclass
class SafetyDecision:
    allowed: bool
    risk_level: StepRiskLevel
    reason: str | None = None


class SafetyPolicy:
    def __init__(
        self,
        allowlist: AllowlistPolicy,
        risk_classifier: RiskClassifier | None = None,
        require_confirmation_for_irreversible: bool = True,
    ):
        self.allowlist = allowlist
        self.risk_classifier = risk_classifier or RiskClassifier()
        self.require_confirmation_for_irreversible = require_confirmation_for_irreversible

    def evaluate(
        self,
        url: str,
        action_type: ActionType,
        semantic_description: str | None,
        current_path: str | None,
        confirmed: bool,
    ) -> SafetyDecision:
        try:
            self.allowlist.check(url, action_type.value)
        except AllowlistViolation as exc:
            return SafetyDecision(allowed=False, risk_level=StepRiskLevel.SAFE, reason=f"blocked by allowlist: {exc}")

        risk = self.risk_classifier.classify(action_type, semantic_description, current_path)
        if risk == StepRiskLevel.IRREVERSIBLE and self.require_confirmation_for_irreversible and not confirmed:
            return SafetyDecision(allowed=False, risk_level=risk, reason="blocked: irreversible action requires confirmation")
        return SafetyDecision(allowed=True, risk_level=risk)
