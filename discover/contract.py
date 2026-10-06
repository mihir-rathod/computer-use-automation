"""What a person fills in to discover a new task, and how it becomes the capability contract discovery works from.

The model is only ever told *how to drive the screens toward a goal*; everything that makes a capability safe and reviewable (typed inputs and
outputs, the success signal, what counts as a normal answer, its risk) is decided here by a human, exactly as it is for the capabilities that
were written in agent/catalog*.py. Nothing in the contract is inferred by the model."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from agent.catalog import CapabilitySpec
from artifacts_lib.schema import (
    BusinessOutcomeRule, CapabilityRiskLevel, CapabilityTarget, ErrorHandling, JSONSchemaObject, Preconditions, RecoverableRule,
    RecoveryAction, SafetyMeta, Signal, SignalType, SurfaceType,
)
from evidence_lib.redaction import is_sensitive_field

PENDING_SUCCESS_TEXT = "(decided after discovery)"
SNAKE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
Effect = Literal["read_only", "changes_data", "irreversible"]


def _snake(value: str, what: str) -> str:
    if not SNAKE.match(value):
        raise ValueError(f"{what} must be lowercase letters, digits and underscores, starting with a letter (for example 'appointment_number')")
    return value


class InputField(BaseModel):
    name: str
    kind: Literal["text", "number"] = "text"
    required: bool = True
    example: str = Field(min_length=2, max_length=200, description="The value used while discovery. It is replaced by whatever the caller supplies at run time.")
    description: str | None = Field(default=None, max_length=200)
    pattern: str | None = Field(default=None, max_length=200, description="A regular expression the value must match, to catch typos before a run starts.")
    choices: list[str] | None = Field(default=None, description="If given, the value must be one of these.")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        _snake(v, "an input name")
        if is_sensitive_field(v):
            raise ValueError("a task cannot take a password, secret or token as an input: those would be shown to the model. Sign-in is handled by the target's own profile")
        return v

    @model_validator(mode="after")
    def _example_fits(self) -> InputField:
        if self.pattern:
            try:
                ok = re.search(self.pattern, self.example)
            except re.error as exc:
                raise ValueError(f"the pattern for '{self.name}' is not a valid expression: {exc}") from None
            if not ok:
                raise ValueError(f"the example for '{self.name}' does not match its own pattern")
        if self.choices is not None:
            self.choices = [c for c in self.choices if c.strip()]
            if not self.choices:
                raise ValueError(f"'{self.name}' has an empty list of choices")
            if self.example not in self.choices:
                raise ValueError(f"the example for '{self.name}' must be one of its choices")
        if self.kind == "number":
            try:
                float(self.example.replace(",", ""))
            except ValueError:
                raise ValueError(f"the example for '{self.name}' is not a number") from None
        return self


class OutputField(BaseModel):
    name: str
    description: str | None = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        _snake(v, "an output name")
        if v == "status":
            raise ValueError("'status' is reserved: it holds how the task ended")
        return v


class Outcome(BaseModel):
    when_text: str = Field(min_length=3, max_length=160, description="Text the page shows when this happens, for example 'RECORD NOT FOUND'.")
    outcome: str

    @field_validator("outcome")
    @classmethod
    def _outcome(cls, v: str) -> str:
        return _snake(v, "an outcome name")


class Verify(BaseModel):
    inputs: dict[str, str] = Field(default_factory=dict, description="A second, different set of inputs. After discovery, the recorded task is replayed once with these to prove it works for more than the example.")
    expect_outcome: str | None = Field(default=None, description="If the second set should end in a normal answer (for example not_found) rather than success, name it.")
    same_as_example: bool = Field(default=False, description="Replay once with the example values themselves. Proves the recording replays on its own, without a model; read-only tasks only, since a task that commits would commit twice.")


class DiscoveryContract(BaseModel):
    task_name: str = Field(description="Part of the capability id, for example 'appointment_details' makes clinic.appointment_details.")
    name: str = Field(min_length=3, max_length=80)
    description: str = Field(min_length=5, max_length=300)
    target: str
    goal: str = Field(min_length=20, max_length=1500, description="Plain steps to do on the screens, as you would tell a new colleague.")
    start_path: str | None = Field(default=None, description="The page to start from, for example /legacy/fn/cancel. Left empty, the system's main page: the model finds its own way.")
    inputs: list[InputField] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    success_text: str | None = Field(default=None, min_length=4, max_length=160, description="Text that is on the page when the task has worked. Left empty, it is decided from the page the model read the answer from, and checked by a replay.")
    success_status: str = "done"
    outcomes: list[Outcome] = Field(default_factory=list)
    effect: Effect = "read_only"
    commit_approval: Literal["auto_sandbox", "supervisor"] = "auto_sandbox"
    hint: str | None = Field(default=None, max_length=500, description="Extra guidance for a retry, for example after the model got stuck.")
    verify: Verify | None = None
    retry_on_text: str | None = Field(default=None, max_length=120, description="An error banner worth retrying a couple of times, for example 'APPLICATION ERROR'.")
    timeout_text: str | None = Field(default=None, max_length=120, description="Text shown when the session has timed out, so a run signs in again and restarts.")

    @field_validator("task_name")
    @classmethod
    def _task(cls, v: str) -> str:
        return _snake(v, "the task name")

    @field_validator("success_status")
    @classmethod
    def _status(cls, v: str) -> str:
        return _snake(v, "the success status")

    @field_validator("start_path")
    @classmethod
    def _path(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not v.startswith("/") or v.startswith("//") or " " in v:
            raise ValueError("the start page must be a path on the target, like /legacy/patients")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> DiscoveryContract:
        names = [i.name for i in self.inputs]
        if len(set(names)) != len(names):
            raise ValueError("two inputs have the same name")
        examples = [i.example.strip().lower() for i in self.inputs]
        if len(set(examples)) != len(examples):
            raise ValueError("two inputs have the same example value, so the recording could not tell which one a step typed. Give each a different example")
        outs = [o.name for o in self.outputs]
        if len(set(outs)) != len(outs):
            raise ValueError("two outputs have the same name")
        outcomes = [o.outcome for o in self.outcomes]
        if len(set(outcomes)) != len(outcomes) or self.success_status in outcomes:
            raise ValueError("outcome names must be different from each other and from the success status")
        if self.effect == "read_only" and not self.outputs:
            raise ValueError("a read-only task exists to give something back: name at least one output to read")
        if self.verify and self.verify.same_as_example:
            if self.effect != "read_only":
                raise ValueError("only a read-only task can be re-checked with its own example values: one that changes data would change it twice")
        elif self.verify:
            unknown = set(self.verify.inputs) - set(names)
            if unknown:
                raise ValueError(f"the verification uses inputs the task does not have: {sorted(unknown)}")
            missing = [i.name for i in self.inputs if i.required and i.name not in self.verify.inputs]
            if missing:
                raise ValueError(f"the verification must give every required input; missing {missing}")
            if self.verify.expect_outcome and self.verify.expect_outcome not in outcomes:
                raise ValueError("the expected outcome for verification is not one of the task's outcomes")
        return self

    # ---- what discovery is given ---------------------------------------------------------------------------------------

    def parameters(self) -> dict[str, str]:
        return {i.name: i.example for i in self.inputs}

    def goal_for_model(self) -> str:
        goal = self.goal.strip()
        if self.effect == "read_only":
            goal += ("\n\nThis task only READS. Do not press anything that saves, submits, confirms, cancels, updates, sends or otherwise changes data. "
                     "Read the values asked for, then finish.")
        if self.hint:
            goal += f"\n\nHint from the person discovery you (the previous attempt got stuck): {self.hint.strip()}"
        return goal

    def verify_inputs(self) -> dict[str, Any] | None:
        if not self.verify:
            return None
        if self.verify.same_as_example:
            return {f.name: (float(f.example.replace(",", "")) if f.kind == "number" else f.example) for f in self.inputs}
        out: dict[str, Any] = {}
        for field in self.inputs:
            if field.name in self.verify.inputs:
                v = self.verify.inputs[field.name]
                out[field.name] = float(v.replace(",", "")) if field.kind == "number" else v
        return out

    # ---- the contract discovery and the recorder work from ---------------------------------------------------------------

    def capability_id(self, app_id: str) -> str:
        return f"{app_id}.{self.task_name}"

    def to_spec(self, profile: dict[str, Any], version: str = "1.0.0") -> CapabilitySpec:
        app = profile["app_id"]
        props: dict[str, dict[str, Any]] = {}
        for f in self.inputs:
            p: dict[str, Any] = {"type": "number" if f.kind == "number" else "string"}
            if f.description:
                p["description"] = f.description
            if f.pattern:
                p["pattern"] = f.pattern
            if f.choices:
                p["enum"] = f.choices
            if f.kind == "text":
                p["minLength"] = 1
            props[f.name] = p
        statuses = [self.success_status, *[o.outcome for o in self.outcomes]]
        out_props: dict[str, dict[str, Any]] = {"status": {"type": "string", "enum": statuses}}
        for o in self.outputs:
            out_props[o.name] = {"type": ["string", "null"], **({"description": o.description} if o.description else {})}
        text = lambda v: Signal(type=SignalType.TEXT_PRESENT, value=v)  # noqa: E731
        recoverable: list[RecoverableRule] = []
        if self.retry_on_text:
            recoverable.append(RecoverableRule(signal=text(self.retry_on_text), action=RecoveryAction.RETRY, max_attempts=2, backoff_ms=300, backoff_multiplier=2.0))
        if self.timeout_text:
            recoverable.append(RecoverableRule(signal=text(self.timeout_text), action=RecoveryAction.REAUTHENTICATE_AND_RESUME))
        return CapabilitySpec(
            capability_id=self.capability_id(app), version=version, name=self.name, description=self.description,
            goal=self.goal_for_model(), start_path=self.start_path or profile.get("home_path", "/"),
            target=CapabilityTarget(app_id=app, surface_type=SurfaceType.WEB, base_url=profile["base_url"], vendor_product=profile.get("vendor_product", app)),
            input_schema=JSONSchemaObject(properties=props, required=[f.name for f in self.inputs if f.required]),
            output_schema=JSONSchemaObject(properties=out_props, required=["status"]),
            success_checkpoint=text(self.success_text or PENDING_SUCCESS_TEXT),
            error_handling=ErrorHandling(business_outcomes=[BusinessOutcomeRule(signal=text(o.when_text), outcome=o.outcome) for o in self.outcomes], recoverable=recoverable),
            safety=SafetyMeta(risk_level=CapabilityRiskLevel.READ_ONLY if self.effect == "read_only" else CapabilityRiskLevel.STATE_CHANGING,
                              requires_confirmation=self.effect == "irreversible"),
            preconditions=Preconditions(requires_capability=profile["login_capability"], note="Assumes a signed-on session."),
            success_output_defaults={"status": self.success_status},
        )
