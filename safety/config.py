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
    approve_at: Literal["run", "step"] = Field(
        default="run", description="When an operator or supervisor approval is asked for. run: before the run starts. step: the run goes as far as the irreversible "
                                   "step and waits there with the page in front of the approver. Only runs started from the console, chat or API can wait; one started "
                                   "by an AI assistant over MCP is approved before it starts either way.")
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


class TracingConfig(BaseModel):
    """Playwright traces (a timeline of DOM snapshots, network calls and screenshots) are the best debugging evidence a failed run can
    leave, but they record everything typed, including secrets, and cannot be redacted. So by default they are kept for failures only,
    and only against sandbox targets."""
    mode: Literal["off", "failures", "always"] = "failures"
    non_sandbox: bool = Field(default=False, description="Also trace targets that are not marked sandbox. Off by default: a trace contains typed passwords.")


class EvidenceConfig(BaseModel):
    """Whether replay keeps a screenshot after every action ("every_action") or only when an action fails ("errors"). Screenshots cannot be
    redacted, so every_action applies to sandbox targets only unless non_sandbox is set."""
    screenshots: Literal["errors", "every_action"] = "errors"
    non_sandbox: bool = False


class DiscoveryConfig(BaseModel):
    """Discovering a new task from the console. A model drives a real browser and sees what the page shows, so it is limited to sandbox
    targets unless a policy owner opts in, and then only for tasks that read."""
    non_sandbox_read_only: bool = False
    max_steps: int = Field(default=25, ge=3, le=60)
    timeout_s: int = Field(default=300, ge=30, le=1800)
    commit_wait_s: int = Field(default=900, ge=30, le=7200, description="How long a discovery session waits for a supervisor to approve its commit step.")


class EscalationConfig(BaseModel):
    """A run that gets stuck can pause for a person instead of failing. A paused run holds one browser worker, so it only waits so long, and only
    this many may wait at once (always leaving at least one worker free for other runs)."""
    enabled: bool = True
    wait_s: int = Field(default=600, ge=30, le=3600, description="How long a paused run waits for a person before it is stopped.")
    max_paused: int = Field(default=1, ge=1, le=5, description="How many runs may be waiting for a person at the same time.")
    approval_wait_s: int = Field(default=900, ge=30, le=7200, description="How long a run waits at its irreversible step for an approval before it is stopped, with nothing committed.")
    max_awaiting_approval: int = Field(default=3, ge=1, le=10, description="How many runs may be waiting at an irreversible step at the same time (a browser worker is always left free).")


class PolicyConfig(BaseModel):
    version: Literal[1] = 1
    default_approval: ApprovalTier = Field(default="supervisor", description="For a capability not listed below that has an irreversible step: who must approve it. A newly discovered task lands here.")
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)
    evidence: EvidenceConfig = Field(default_factory=EvidenceConfig)
    tracing: TracingConfig = Field(default_factory=TracingConfig)
    approvers: dict[str, Role] = Field(default_factory=dict)
    capabilities: dict[str, CapabilityPolicy] = Field(default_factory=dict)
    risk_keywords: RiskKeywords = Field(default_factory=RiskKeywords)
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)

    @classmethod
    def load(cls, path: Path = DEFAULT_POLICY_PATH) -> PolicyConfig:
        return cls.model_validate(yaml.safe_load(Path(path).read_text()) or {})

    def for_capability(self, capability_id: str) -> CapabilityPolicy:
        return self.capabilities.get(capability_id) or CapabilityPolicy(approval=self.default_approval)


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


ROLE_RANK = {"viewer": 0, "operator": 1, "supervisor": 2, "admin": 3}


def role_allows(role: str, tier: str) -> tuple[bool, str]:
    """For API callers, whose role comes from their key rather than the policy roster."""
    needed = "supervisor" if tier == "supervisor" else "operator"
    if ROLE_RANK.get(role, -1) < ROLE_RANK[needed]:
        article = "an" if needed[0] in "aeiou" else "a"
        return False, f"this needs {article} {needed} (your key is {role})"
    return True, ""


def may_approve(policy: PolicyConfig, approver: str, tier: ApprovalTier) -> tuple[bool, str]:
    role = policy.approvers.get(approver)
    if role is None:
        return False, f"'{approver}' is not on the approver roster"
    if _TIER_RANK[role] < _TIER_RANK[tier]:
        article = "an" if role[0] in "aeiou" else "a"
        return False, f"'{approver}' is {article} {role}; this capability needs {'an' if tier[0] in 'aeiou' else 'a'} {tier} approval"
    return True, ""
