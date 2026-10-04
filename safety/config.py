"""The safety policy as data. `safety/policy.yaml` says, per capability, how its irreversible steps are
authorised and what limits apply; this module loads and validates it and answers the questions the
runtime asks: what approval does this run need, does it break a cap, who may approve."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from artifacts_lib.schema import Artifact, StepRiskLevel

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "policy.yaml"
ApprovalTier = Literal["live", "operator", "supervisor"]
Role = Literal["operator", "supervisor"]
_TIER_RANK = {"live": 0, "operator": 1, "supervisor": 2}


class Caps(BaseModel):
    max_param: dict[str, float] = Field(default_factory=dict, description="Largest value allowed for a numeric input parameter.")
    max_commits_per_day: int | None = Field(default=None, ge=1, description="Committed runs per UTC day, counted from the run store.")


class CapabilityPolicy(BaseModel):
    approval: ApprovalTier = "live"
    caps: Caps = Field(default_factory=Caps)


class RiskKeywords(BaseModel):
    commit: list[str] = Field(default_factory=list)
    domain: list[str] = Field(default_factory=list)


class RedactionConfig(BaseModel):
    patterns: dict[str, str] = Field(default_factory=dict)
    field_names: list[str] = Field(default_factory=list)

    @field_validator("patterns")
    @classmethod
    def patterns_compile(cls, v: dict[str, str]) -> dict[str, str]:
        for name, pattern in v.items():
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"redaction pattern '{name}' does not compile: {exc}") from None
        return v


class PolicyConfig(BaseModel):
    version: Literal[1] = 1
    approvers: dict[str, Role] = Field(default_factory=dict)
    capabilities: dict[str, CapabilityPolicy] = Field(default_factory=dict)
    risk_keywords: RiskKeywords = Field(default_factory=RiskKeywords)
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)

    @classmethod
    def load(cls, path: Path = DEFAULT_POLICY_PATH) -> PolicyConfig:
        return cls.model_validate(yaml.safe_load(Path(path).read_text()) or {})

    def for_capability(self, capability_id: str) -> CapabilityPolicy:
        return self.capabilities.get(capability_id, CapabilityPolicy())


@dataclass(frozen=True)
class PolicyViolation:
    code: str  # policy_cap_exceeded | policy_param_invalid
    message: str


def required_approval(policy: PolicyConfig, artifact: Artifact) -> ApprovalTier | None:
    """None when the capability has no irreversible step, so nothing needs authorising."""
    if not any(s.risk_level == StepRiskLevel.IRREVERSIBLE for s in artifact.steps):
        return None
    return policy.for_capability(artifact.capability_id).approval


def irreversible_step_ids(artifact: Artifact) -> frozenset[str]:
    return frozenset(s.step_id for s in artifact.steps if s.risk_level == StepRiskLevel.IRREVERSIBLE)


def check_params(policy: PolicyConfig, capability_id: str, params: dict[str, Any]) -> list[PolicyViolation]:
    out = []
    for name, limit in policy.for_capability(capability_id).caps.max_param.items():
        if name not in params:
            continue
        try:
            value = float(str(params[name]).replace(",", "").lstrip("$"))
        except ValueError:
            out.append(PolicyViolation("policy_param_invalid", f"'{name}' must be a number to be checked against its cap, got {params[name]!r}"))
            continue
        if value > limit:
            out.append(PolicyViolation("policy_cap_exceeded", f"{name}={value:g} exceeds the policy limit of {limit:g} for {capability_id}"))
    return out


def may_approve(policy: PolicyConfig, approver: str, tier: ApprovalTier) -> tuple[bool, str]:
    role = policy.approvers.get(approver)
    if role is None:
        return False, f"'{approver}' is not on the approver roster"
    if _TIER_RANK[role] < _TIER_RANK[tier]:
        article = "an" if role[0] in "aeiou" else "a"
        return False, f"'{approver}' is {article} {role}; this capability needs {'an' if tier[0] in 'aeiou' else 'a'} {tier} approval"
    return True, ""
