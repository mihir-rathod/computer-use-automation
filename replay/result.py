"""The replay result contract: a clear, structured result -- success (with outputs), a known
business outcome, or a failure with enough detail to debug. Three distinct statuses, not two --
collapsing business_outcome into either success or failure is the most common design mistake
in this kind of system.
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
    # An irreversible step was issued and the run cannot tell whether it took effect (the page
    # errored, the session expired, a checkpoint never appeared). The engine does not retry it
    # and does not guess; a human settles it against the target's own records.
    NEEDS_REVIEW = "needs_review"
    # Stopped on purpose before the first irreversible step; nothing was committed.
    DRY_RUN = "dry_run"
    # The run is recorded but has not started: policy requires a recorded approval first.
    PENDING_APPROVAL = "pending_approval"


class ReplayError(BaseModel):
    step_id: str | None = None
    message: str
    # Machine-readable cause: timeout | cancelled | blocked | ambiguous_commit | max_recovery |
    # checkpoint_failed | input_invalid | no_reauth | unsafe_resume. None for older call sites.
    code: str | None = None
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
    committed: bool = Field(
        default=False, description="An irreversible step was issued and its checkpoint confirmed it. "
                                   "Stays True on a later failure, so a caller never re-runs a committed capability.",
    )
    commit_step: str | None = Field(default=None, description="The irreversible step that was issued (confirmed or ambiguous).")
    dry_run: bool = False
    run_id: str | None = Field(default=None, description="Id in the run store.")
    deduplicated: bool = Field(default=False, description="Answered from an earlier run with the same idempotency key; no browser was launched.")
    approval_tier: str | None = None
    repair_proposal_id: str | None = Field(default=None, description="A locator failure produced this repair proposal; nothing is changed until it is approved.")
    started_at: datetime
    finished_at: datetime
