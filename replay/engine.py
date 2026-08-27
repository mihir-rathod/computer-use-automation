"""The deterministic replay executor -- ASSIGNMENT_ORIGINAL.md 3.3, the production execution
path. No LLM in the loop: every decision here is either "run the next step" or a classification
against artifact-declared signals, never a model call.

Classification order on any checkpoint failure (PROJECT_PLAN.md Section 3, and this is the
central judgment call the whole error-handling design rests on):
  1. Does current state match a business_outcomes signal?  -> stop, return that business outcome.
  2. Does it match a recoverable signal?                    -> apply the recovery, re-check.
  3. Otherwise                                               -> hard failure, stop, report clearly.

Business outcomes always win over recoverable, which always wins over hard failure -- a page
that happens to say both "No member found" and "Service temporarily unavailable" should be
read as the former (a real answer), not endlessly retried.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from artifacts_lib.schema import (
    ActionType,
    Artifact,
    RecoverableRule,
    RecoveryAction,
    Step,
)
from artifacts_lib.storage import DEFAULT_ARTIFACTS_DIR, load_artifact_by_id
from escalation.session_manager import SessionManager
from evidence_lib.logger import EvidenceLogger
from replay.coercion import coerce_output
from replay.result import ReplayError, ReplayResult, ReplayStatus
from replay.templating import substitute, substitute_signal
from replay.validation import validate_input
from surface.base import Action, Surface

MAX_RECOVERY_ATTEMPTS_PER_STEP = 5
MAX_ARTIFACT_RESTARTS = 1


class _RestartArtifact(Exception):
    """Raised when reauthenticate_and_resume determines it's safe (no non-idempotent step has
    completed yet) to restart the whole artifact from its first step after re-logging in."""


class _BusinessOutcome(Exception):
    def __init__(self, outcome: str, output_field: str, steps_completed: list[str]):
        self.outcome = outcome
        self.output_field = output_field
        self.steps_completed = steps_completed


class _HardFailure(Exception):
    def __init__(self, error: ReplayError, steps_completed: list[str]):
        self.error = error
        self.steps_completed = steps_completed


class ReplayEngine:
    def __init__(
        self,
        surface: Surface,
        evidence_logger: EvidenceLogger | None = None,
        artifacts_dir: Any = DEFAULT_ARTIFACTS_DIR,
        reauth_credentials: dict[str, str] | None = None,
        session_manager: SessionManager | None = None,
    ):
        self.surface = surface
        self.evidence_logger = evidence_logger
        self.artifacts_dir = artifacts_dir
        self.reauth_credentials = reauth_credentials
        # On a failure with no known business/recoverable signal, this is what pauses for a
        # human instead of failing immediately -- exactly once per step (see
        # _run_step_with_recovery's depth==0 guard), ASSIGNMENT_ORIGINAL.md 3.6.
        self.session_manager = session_manager

    def run(self, artifact: Artifact, inputs: dict[str, Any]) -> ReplayResult:
        started_at = datetime.now(UTC)
        input_errors = validate_input(artifact.input_schema, inputs)
        if input_errors:
            return self._finish(artifact, started_at, ReplayResult(
                status=ReplayStatus.HARD_FAILURE, capability_id=artifact.capability_id,
                error=ReplayError(message="; ".join(input_errors)),
                started_at=started_at, finished_at=datetime.now(UTC),
            ))

        # Backfill declared-but-not-required input_schema properties the caller omitted, with
        # an empty string -- found via a real crash: memo is genuinely optional on MERIDIAN's
        # own Funds Transfer form (no asterisk), but a step's params still reference {{memo}},
        # and substitute() has no notion of "this template variable is allowed to be missing."
        # Only fills gaps for properties the artifact itself declares optional; validate_input
        # above already enforces required ones strictly, so this can't mask a real caller error.
        variables = {
            k: "" for k in artifact.input_schema.properties if k not in artifact.input_schema.required
        }
        variables.update(inputs)
        restarts = 0
        while True:
            try:
                completed, outputs = self._execute_steps(artifact, variables)
                break
            except _RestartArtifact:
                restarts += 1
                if restarts > MAX_ARTIFACT_RESTARTS:
                    return self._finish(artifact, started_at, ReplayResult(
                        status=ReplayStatus.HARD_FAILURE, capability_id=artifact.capability_id,
                        error=ReplayError(message="exceeded max artifact restarts after reauthentication"),
                        started_at=started_at, finished_at=datetime.now(UTC),
                    ))
                continue
            except _BusinessOutcome as bo:
                return self._finish(artifact, started_at, ReplayResult(
                    status=ReplayStatus.BUSINESS_OUTCOME, capability_id=artifact.capability_id,
                    business_outcome=bo.outcome,
                    outputs=self._build_business_outputs(artifact, bo.outcome, bo.output_field),
                    steps_completed=bo.steps_completed,
                    started_at=started_at, finished_at=datetime.now(UTC),
                ))
            except _HardFailure as hf:
                return self._finish(artifact, started_at, ReplayResult(
                    status=ReplayStatus.HARD_FAILURE, capability_id=artifact.capability_id,
                    error=hf.error, steps_completed=hf.steps_completed,
                    started_at=started_at, finished_at=datetime.now(UTC),
                ))

        final_outputs = {**artifact.success_output_defaults, **outputs}
        return self._finish(artifact, started_at, ReplayResult(
            status=ReplayStatus.SUCCESS, capability_id=artifact.capability_id,
            outputs=final_outputs, steps_completed=completed,
            started_at=started_at, finished_at=datetime.now(UTC),
        ))

    # ---- step loop --------------------------------------------------------------------

    def _execute_steps(self, artifact: Artifact, variables: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        completed: list[str] = []
        outputs: dict[str, Any] = {}
        non_idempotent_done = False

        for step in artifact.steps:
            extracted = self._run_step_with_recovery(artifact, step, variables, completed, non_idempotent_done)
            completed.append(step.step_id)
            if step.risk_level == "irreversible" or not step.idempotent:
                non_idempotent_done = True
            if step.output_binding:
                outputs[step.output_binding] = coerce_output(
                    extracted, artifact.output_schema.properties.get(step.output_binding)
                )

        if not self.surface.check_signal(substitute_signal(artifact.success_checkpoint, variables)):
            outcome = self._classify(artifact, variables)
            if outcome is None:
                if self.session_manager is not None:
                    self.session_manager.update_observed(self.surface.perceive())
                    self.session_manager.pause(reason="all steps completed but success_checkpoint was not met", step_id=None)
                    if not self.surface.check_signal(substitute_signal(artifact.success_checkpoint, variables)):
                        raise _HardFailure(ReplayError(message="success_checkpoint still not met after human intervention"), completed)
                    return completed, outputs
                raise _HardFailure(ReplayError(
                    message="all steps completed but success_checkpoint was not met",
                ), completed)
            self._handle_classified(artifact, outcome, None, variables, completed, non_idempotent_done)
            # a recoverable rule that clears is only meaningful if it changes whether the
            # checkpoint now passes -- re-check once, then give up rather than loop forever here
            if not self.surface.check_signal(substitute_signal(artifact.success_checkpoint, variables)):
                raise _HardFailure(ReplayError(
                    message="recovered from a known condition but success_checkpoint still not met",
                ), completed)

        return completed, outputs

    def _run_step_with_recovery(
        self, artifact: Artifact, step: Step, variables: dict[str, Any],
        completed_so_far: list[str], non_idempotent_done: bool, depth: int = 0,
    ) -> str | None:
        if depth > MAX_RECOVERY_ATTEMPTS_PER_STEP:
            raise _HardFailure(ReplayError(step_id=step.step_id, message="exceeded max recovery attempts"), completed_so_far)

        result = self.surface.act(self._build_action(step, variables))

        # Classify proactively, even after a nominally successful action -- not only on
        # checkpoint failure. A checkpoint like url_matches can pass on a broken page that
        # loaded at the right URL with the wrong content (e.g. a "service unavailable" banner
        # instead of the real page), so checking known signals first is what actually catches
        # that -- a weak checkpoint alone wouldn't.
        outcome = self._classify(artifact, variables)
        if outcome is not None:
            self._handle_classified(artifact, outcome, step, variables, completed_so_far, non_idempotent_done)
            return self._run_step_with_recovery(artifact, step, variables, completed_so_far, non_idempotent_done, depth + 1)

        checkpoint_ok = result.success and (step.checkpoint is None or self.surface.check_signal(substitute_signal(step.checkpoint, variables)))
        if checkpoint_ok:
            return result.extracted_value

        failure_message = result.error or "checkpoint failed and no known business/recoverable signal matched"

        # Escalate to a human exactly once per step: depth==0 means this is the first time
        # we've hit this specific failure (not a retry after a resume that failed again).
        # Covers both a genuine unmatched failure AND a safety block (e.g. an irreversible
        # action needing confirmation) -- either way, the human can act on the SAME live
        # session (including performing the exact blocked action with the confirmed checkbox)
        # via the operator console, then resume.
        if self.session_manager is not None and depth == 0:
            self.session_manager.update_observed(self.surface.perceive())
            self.session_manager.pause(reason=failure_message, step_id=step.step_id)

            # Don't blindly redo the original action on resume -- the human may already have
            # performed it manually (or something equivalent) via the operator console. Check
            # first, so a successful manual action isn't silently repeated (a second click on
            # a non-idempotent step would be a real double-submit, not just a wasted retry).
            post_outcome = self._classify(artifact, variables)
            if post_outcome is not None:
                self._handle_classified(artifact, post_outcome, step, variables, completed_so_far, non_idempotent_done)
                return self._run_step_with_recovery(artifact, step, variables, completed_so_far, non_idempotent_done, depth + 1)
            already_satisfied = (
                step.action != ActionType.EXTRACT
                and step.checkpoint is not None
                and self.surface.check_signal(substitute_signal(step.checkpoint, variables))
            )
            if already_satisfied:
                return None
            return self._run_step_with_recovery(artifact, step, variables, completed_so_far, non_idempotent_done, depth + 1)

        raise _HardFailure(ReplayError(step_id=step.step_id, message=failure_message), completed_so_far)

    def _handle_classified(self, artifact, outcome, step, variables, completed_so_far, non_idempotent_done) -> None:
        kind, payload = outcome
        if kind == "business_outcome":
            raise _BusinessOutcome(payload["outcome"], payload["output_field"], completed_so_far)

        rule: RecoverableRule = payload["rule"]
        if rule.action == RecoveryAction.REAUTHENTICATE_AND_RESUME:
            if non_idempotent_done:
                raise _HardFailure(ReplayError(
                    step_id=step.step_id if step else None,
                    message="session expired after a non-idempotent step already completed -- cannot safely auto-resume",
                ), completed_so_far)
            self._reauthenticate(artifact)
            raise _RestartArtifact()

        recovered = self._apply_bounded_recovery(rule, step, variables)
        if not recovered:
            raise _HardFailure(ReplayError(
                step_id=step.step_id if step else None,
                message=f"recovery action '{rule.action.value}' did not clear the triggering condition within {rule.max_attempts} attempt(s)",
            ), completed_so_far)

    def _classify(self, artifact: Artifact, variables: dict[str, Any]):
        for rule in artifact.error_handling.business_outcomes:
            if self.surface.check_signal(substitute_signal(rule.signal, variables)):
                return "business_outcome", {"outcome": rule.outcome, "output_field": rule.output_field}
        for rule in artifact.error_handling.recoverable:
            if self.surface.check_signal(substitute_signal(rule.signal, variables)):
                return "recoverable", {"rule": rule}
        return None

    def _apply_bounded_recovery(self, rule: RecoverableRule, step: Step | None, variables: dict[str, Any]) -> bool:
        for _attempt in range(rule.max_attempts):
            if rule.backoff_ms:
                time.sleep(rule.backoff_ms / 1000)
            if rule.action == RecoveryAction.RETRY and step is not None:
                self.surface.act(self._build_action(step, variables))
            elif rule.action == RecoveryAction.DISMISS_AND_CONTINUE:
                self.surface.act(Action(kind=ActionType.DISMISS_DIALOG, target=rule.recovery_target, actor="replay"))
            if not self.surface.check_signal(substitute_signal(rule.signal, variables)):
                return True
        return False

    def _reauthenticate(self, artifact: Artifact) -> None:
        if self.reauth_credentials is None or artifact.preconditions is None or not artifact.preconditions.requires_capability:
            raise _HardFailure(ReplayError(message="session expired but no reauthentication capability/credentials configured"), [])
        login_artifact = load_artifact_by_id(artifact.preconditions.requires_capability, directory=self.artifacts_dir)
        login_result = self.run(login_artifact, self.reauth_credentials)
        if login_result.status != ReplayStatus.SUCCESS:
            raise _HardFailure(ReplayError(
                message=f"reauthentication via '{login_artifact.capability_id}' did not succeed (status={login_result.status.value})",
            ), [])

    def _build_action(self, step: Step, variables: dict[str, Any]) -> Action:
        params = substitute(step.params, variables)
        return Action(kind=step.action, target=step.target, params=params, actor="replay")

    def _build_business_outputs(self, artifact: Artifact, outcome: str, output_field: str) -> dict[str, Any]:
        outputs: dict[str, Any] = {k: None for k in artifact.output_schema.properties}
        outputs[output_field] = outcome
        return outputs

    def _finish(self, artifact: Artifact, started_at: datetime, result: ReplayResult) -> ReplayResult:
        if self.evidence_logger is not None:
            self.evidence_logger.log(
                "replay", "result",
                capability_id=artifact.capability_id, status=result.status.value,
                outputs=result.outputs, business_outcome=result.business_outcome,
                error=result.error.model_dump() if result.error else None,
                steps_completed=result.steps_completed,
                duration_s=(result.finished_at - started_at).total_seconds(),
            )
        return result
