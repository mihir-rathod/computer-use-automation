"""The replay result contract -- ASSIGNMENT_ORIGINAL.md 3.3: "Report a clear, structured
result: success (with outputs), a known business outcome, or a failure with enough detail to
debug." Three distinct statuses, not two -- collapsing business_outcome into either success or
failure is "the most common design mistake here" per the brief's own glossary.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    HARD_FAILURE = "hard_failure"


class ReplayError(BaseModel):
    step_id: str | None = None
    message: str
    screenshot_path: str | None = None


class ReplayResult(BaseModel):
    status: ReplayStatus
    capability_id: str
    outputs: dict[str, Any] | None = Field(
        default=None, description="Populated for SUCCESS (typed per output_schema) and "
                                    "BUSINESS_OUTCOME (business fields null, outcome field set)."
    )
    business_outcome: str | None = None
    error: ReplayError | None = None
    steps_completed: list[str] = Field(default_factory=list)
    escalated: bool = Field(
        default=False, description="A human paused and acted via the operator console during this run."
    )
    recovered: bool = Field(
        default=False, description="A known recoverable condition fired and was auto-recovered (no human)."
    )
    started_at: datetime
    finished_at: datetime
