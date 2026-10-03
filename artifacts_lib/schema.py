"""The capability artifact schema.

An artifact is what a successful discovery run (agent/) produces and what the replay
engine (replay/) executes without an LLM in the loop. It is the seam between "the model
figured this out once" and "this now runs deterministically, cheaply, on demand".

Design principles:
- `Locator` fallback chains are the one mechanism used everywhere a concrete element needs
  to be found -- both for acting (Step.target) and for checking (Signal.target). Nothing
  else in this schema invents a second way to point at an element.
- `Signal` is similarly one mechanism reused for three different questions: "did this one
  step work" (Step.checkpoint), "is the goal actually achieved" (Artifact.success_checkpoint),
  and "does the current page match a known business/recoverable condition"
  (ErrorHandling.business_outcomes / .recoverable). Three different *purposes*, one *shape*.
- Validators encode real invariants (unique step ids, extract steps declare where their
  output goes and that binding exists in output_schema, action-specific required params)
  so "typed inputs/outputs" and "reviewable" are enforced, not just aspirational.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# --------------------------------------------------------------------------------------
# Locating elements -- the single mechanism used for both acting and checking.
# --------------------------------------------------------------------------------------

class LocatorStrategy(str, Enum):
    ROLE = "role"      # accessibility role + accessible name, e.g. "textbox[name='Member ID']"
    CSS = "css"        # CSS selector -- present today, not guaranteed stable across tenants/versions
    XPATH = "xpath"     # structural fallback, most brittle
    TEXT = "text"       # visible-text anchor -- for legacy surfaces with no reliable roles/ids at all


class Locator(BaseModel):
    strategy: LocatorStrategy
    value: str
    note: str | None = Field(
        default=None,
        description="Why this locator was chosen / how robust it is expected to be -- "
                     "the reasoning about robustness.",
    )


class Target(BaseModel):
    """What to act on or check, expressed as an ordered fallback chain.

    Replay tries locators[0] first; if it fails to resolve, it tries locators[1], etc.
    This -- not any single selector -- is the concrete mechanism behind "stable element
    targeting".
    """
    semantic_description: str
    locators: list[Locator]

    @field_validator("locators")
    @classmethod
    def at_least_one_locator(cls, v: list[Locator]) -> list[Locator]:
        if not v:
            raise ValueError("Target must declare at least one locator")
        return v


# --------------------------------------------------------------------------------------
# Signal -- "how do we know a condition is true", reused for checkpoints and error rules.
# --------------------------------------------------------------------------------------

class SignalType(str, Enum):
    URL_MATCHES = "url_matches"
    ELEMENT_VISIBLE = "element_visible"
    ELEMENT_HIDDEN = "element_hidden"
    ELEMENT_VALUE_EQUALS = "element_value_equals"
    TEXT_PRESENT = "text_present"
    DIALOG_PRESENT = "dialog_present"
    REDIRECTED_TO = "redirected_to"


_ELEMENT_SIGNAL_TYPES = {SignalType.ELEMENT_VISIBLE, SignalType.ELEMENT_HIDDEN, SignalType.ELEMENT_VALUE_EQUALS}


class Signal(BaseModel):
    type: SignalType
    value: str = Field(description="Expected content: a URL glob, exact value, or substring to match.")
    target: Target | None = Field(
        default=None,
        description="Required for element_visible/element_hidden/element_value_equals -- "
                     "which element the check applies to. Absent for url/text/dialog/redirect "
                     "checks, which are page-level, not element-level.",
    )
    description: str | None = None

    @model_validator(mode="after")
    def target_required_for_element_signals(self) -> Signal:
        needs_target = self.type in _ELEMENT_SIGNAL_TYPES
        if needs_target and self.target is None:
            raise ValueError(f"signal type '{self.type}' requires a target")
        if not needs_target and self.target is not None:
            raise ValueError(f"signal type '{self.type}' does not use a target")
        return self


# --------------------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------------------

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    EXTRACT = "extract"
    WAIT_FOR = "wait_for"
    DISMISS_DIALOG = "dismiss_dialog"


_TARGET_REQUIRED_ACTIONS = {ActionType.CLICK, ActionType.TYPE, ActionType.SELECT, ActionType.EXTRACT}


class StepRiskLevel(str, Enum):
    """Binary by design -- the job is to distinguish safe/reversible from risky/irreversible,
    not to build a finer-grained taxonomy."""
    SAFE = "safe"
    IRREVERSIBLE = "irreversible"


class Step(BaseModel):
    step_id: str
    action: ActionType
    target: Target | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    output_binding: str | None = Field(
        default=None,
        description="For extract steps: which output_schema property this step's extracted "
                     "value populates.",
    )
    checkpoint: Signal | None = Field(
        default=None,
        description="Per-step verification -- confirms this one action worked. Distinct from "
                     "Artifact.success_checkpoint, which confirms the overall goal was reached.",
    )
    risk_level: StepRiskLevel = StepRiskLevel.SAFE
    idempotent: bool = Field(
        default=True,
        description="False for e.g. a final submit that creates a record. Replay must never "
                     "auto-retry a non-idempotent step after an ambiguous failure -- that's how "
                     "you avoid double-submitting a real transaction.",
    )

    @model_validator(mode="after")
    def action_requirements(self) -> Step:
        if self.action in _TARGET_REQUIRED_ACTIONS and self.target is None:
            raise ValueError(f"action '{self.action}' requires a target")
        if self.action == ActionType.EXTRACT and not self.output_binding:
            raise ValueError("extract action requires output_binding")
        if self.action == ActionType.NAVIGATE and "url" not in self.params:
            raise ValueError("navigate action requires params.url")
        if self.action == ActionType.TYPE and "text" not in self.params:
            raise ValueError("type action requires params.text")
        if self.action == ActionType.SELECT and "value" not in self.params:
            raise ValueError("select action requires params.value")
        return self


# --------------------------------------------------------------------------------------
# Target application, preconditions
# --------------------------------------------------------------------------------------

class SurfaceType(str, Enum):
    WEB = "web"
    LEGACY_WEB = "legacy_web"
    DESKTOP = "desktop"


class CapabilityTarget(BaseModel):
    app_id: str
    surface_type: SurfaceType
    base_url: str
    vendor_product: str
    tenant_id: str | None = Field(
        default=None,
        description="Null for a base/reference capability. Set when a capability has been "
                     "specialized for one tenant.",
    )


class Preconditions(BaseModel):
    requires_capability: str | None = Field(
        default=None,
        description="e.g. 'mockbank.login' -- models auth as its own reusable capability "
                     "instead of duplicating login steps in every flow.",
    )
    note: str | None = None


# --------------------------------------------------------------------------------------
# Typed input/output -- a minimal JSON-Schema-shaped object, not a full JSON Schema
# implementation. Deliberately compatible with how LLM tool-calling APIs (Anthropic/
# Gemini/OpenAI) already describe function parameters, so a capability's input_schema
# can be handed to a tool-calling model directly (the chatbot does exactly this).
# --------------------------------------------------------------------------------------

class JSONSchemaObject(BaseModel):
    type: Literal["object"] = "object"
    properties: dict[str, dict[str, Any]] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def required_fields_are_declared(self) -> JSONSchemaObject:
        unknown = set(self.required) - set(self.properties.keys())
        if unknown:
            raise ValueError(f"required field(s) {sorted(unknown)} not present in properties")
        return self


# --------------------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------------------

class RecoveryAction(str, Enum):
    RETRY = "retry"
    DISMISS_AND_CONTINUE = "dismiss_and_continue"
    REAUTHENTICATE_AND_RESUME = "reauthenticate_and_resume"


class BusinessOutcomeRule(BaseModel):
    """A page state that is a legitimate answer, not a failure -- e.g. 'no such member'."""
    signal: Signal
    outcome: str = Field(description="Value written into output_field when this rule matches.")
    output_field: str = Field(
        default="status",
        description="Which output_schema property receives `outcome`. Explicit rather than the "
                     "replay engine hardcoding a 'status' field name -- keeps the engine generic "
                     "across artifacts that name this field differently or have more than one.",
    )


class RecoverableRule(BaseModel):
    """A page state that's transient/known and should be handled, then re-checked."""
    signal: Signal
    action: RecoveryAction
    max_attempts: int = 1
    backoff_ms: int = 0
    recovery_target: Target | None = Field(
        default=None,
        description="Element to act on for the recovery itself -- e.g. a dialog's dismiss "
                     "button. Required for dismiss_and_continue; unused (and must be absent) "
                     "for retry/reauthenticate_and_resume, which don't target a specific element.",
    )

    @model_validator(mode="after")
    def retry_bounds(self) -> RecoverableRule:
        if self.action == RecoveryAction.RETRY and self.max_attempts < 1:
            raise ValueError("retry action requires max_attempts >= 1")
        return self

    @model_validator(mode="after")
    def recovery_target_matches_action(self) -> RecoverableRule:
        needs_target = self.action == RecoveryAction.DISMISS_AND_CONTINUE
        if needs_target and self.recovery_target is None:
            raise ValueError("dismiss_and_continue requires recovery_target")
        if not needs_target and self.recovery_target is not None:
            raise ValueError(f"recovery action '{self.action}' does not use recovery_target")
        return self


class ErrorHandling(BaseModel):
    business_outcomes: list[BusinessOutcomeRule] = Field(default_factory=list)
    recoverable: list[RecoverableRule] = Field(default_factory=list)
    hard_failure_default: Literal["stop_and_escalate"] = "stop_and_escalate"


# --------------------------------------------------------------------------------------
# Safety, provenance
# --------------------------------------------------------------------------------------

class CapabilityRiskLevel(str, Enum):
    READ_ONLY = "read_only"
    STATE_CHANGING = "state_changing"


class SafetyMeta(BaseModel):
    risk_level: CapabilityRiskLevel
    requires_confirmation: bool = Field(
        default=False,
        description="True gates unattended replay -- a human must approve before this "
                     "capability's irreversible step(s) run. Set False only after human "
                     "review of the artifact.",
    )


class Provenance(BaseModel):
    discovered_by: str = Field(description="Model id (e.g. a Gemini model id), or 'hand_written' for fixtures.")
    discovery_run_id: str
    created_at: datetime
    reviewed: bool = False
    note: str | None = None


# --------------------------------------------------------------------------------------
# Artifact
# --------------------------------------------------------------------------------------

_CAPABILITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class Artifact(BaseModel):
    artifact_schema_version: Literal["1.0"] = Field(
        default="1.0",
        description="Format version -- lets the replay engine know how to interpret older "
                     "artifacts as the schema evolves. Distinct from `version`, which is this "
                     "capability's own recorded-flow version.",
    )
    capability_id: str = Field(description="'<app_id>.<name>', e.g. 'mockbank.member_balance_lookup'.")
    version: str = Field(description="Semver of this capability's recorded flow, e.g. '1.0.0'.")
    name: str
    description: str
    target: CapabilityTarget
    preconditions: Preconditions | None = None
    input_schema: JSONSchemaObject
    output_schema: JSONSchemaObject
    success_checkpoint: Signal = Field(
        description="Confirms the overall goal was reached -- distinct from each step's own "
                     "checkpoint, which only confirms that one action worked. A replay can "
                     "complete every step and still not have achieved the goal (e.g. an "
                     "unexpected redirect along the way).",
    )
    steps: list[Step]
    error_handling: ErrorHandling
    safety: SafetyMeta
    provenance: Provenance
    success_output_defaults: dict[str, str] = Field(
        default_factory=dict,
        description="Literal values merged into outputs when the run completes via "
                     "success_checkpoint rather than a business outcome -- e.g. {'status': "
                     "'found'}. Keeps the replay engine generic instead of hardcoding a field name.",
    )

    @field_validator("capability_id")
    @classmethod
    def capability_id_format(cls, v: str) -> str:
        if not _CAPABILITY_ID_RE.match(v):
            raise ValueError("capability_id must be '<app_id>.<name>' in lowercase snake_case")
        return v

    @field_validator("version")
    @classmethod
    def version_format(cls, v: str) -> str:
        if not _SEMVER_RE.match(v):
            raise ValueError("version must be semver: x.y.z")
        return v

    @model_validator(mode="after")
    def steps_non_empty_with_unique_ids(self) -> Artifact:
        if not self.steps:
            raise ValueError("artifact must have at least one step")
        ids = [s.step_id for s in self.steps]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate step_id(s): {dupes}")
        return self

    @model_validator(mode="after")
    def output_fields_are_declared(self) -> Artifact:
        bound = {s.output_binding for s in self.steps if s.output_binding}
        bound |= {r.output_field for r in self.error_handling.business_outcomes}
        bound |= set(self.success_output_defaults.keys())
        declared = set(self.output_schema.properties.keys())
        unknown = bound - declared
        if unknown:
            raise ValueError(
                f"output field(s) {sorted(unknown)} (from step output_binding, "
                f"business_outcomes.output_field, or success_output_defaults) not declared in "
                f"output_schema.properties"
            )
        return self
