"""The deterministic replay executor -- the production execution path. No LLM in the loop: every decision here is either "run the next step" or a classification
against artifact-declared signals, never a model call.

Classification order on any checkpoint failure (the central judgment call the whole
error-handling design rests on):
  1. Does current state match a business_outcomes signal?  -> stop, return that business outcome.
  2. Does it match a recoverable signal?                    -> apply the recovery, re-check.
  3. Otherwise                                               -> hard failure, stop, report clearly.

Business outcomes always win over recoverable, which always wins over hard failure -- a page
that happens to say both "No member found" and "Service temporarily unavailable" should be
read as the former (a real answer), not endlessly retried.
"""
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

from artifacts_lib.schema import (
    ActionType,
    Artifact,
    RecoverableRule,
    RecoveryAction,
    Step,
    StepRiskLevel,
)
from artifacts_lib.storage import DEFAULT_ARTIFACTS_DIR, load_artifact_by_id
from escalation.session_manager import SessionCancelled, SessionManager
from evidence_lib.logger import EvidenceLogger
from replay.coercion import coerce_output
from replay.result import ReplayError, ReplayResult, ReplayStatus
from replay.templating import substitute, substitute_signal
from replay.validation import validate_input
from surface.base import Action, Surface

MAX_RECOVERY_ATTEMPTS_PER_STEP = 5
MAX_ARTIFACT_RESTARTS = 1
# How long a step/success checkpoint may take to become true after the action and the surface's own
# settle wait. Polling, not sleeping: a checkpoint that is already true costs nothing.
DEFAULT_CHECKPOINT_TIMEOUT_S = 1.5
CHECKPOINT_POLL_S = 0.1


class _RestartArtifact(Exception):
    """Raised when reauthenticate_and_resume determines it's safe (no non-idempotent step has
    completed yet) to restart the whole artifact from its first step after re-logging in."""


class _BusinessOutcome(Exception):
    def __init__(self, outcome: str, output_field: str, steps_completed: list[str]):
        self.outcome = outcome
        self.output_field = output_field
        self.steps_completed = steps_completed


class _NeedsReview(Exception):
    def __init__(self, error: ReplayError, steps_completed: list[str]):
        self.error = error
        self.steps_completed = steps_completed


class _DryRunStop(Exception):
    def __init__(self, step_id: str, steps_completed: list[str], outputs: dict[str, Any]):
        self.step_id = step_id
        self.steps_completed = steps_completed
        self.outputs = outputs


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
        *,
        deadline_s: float | None = None,
        cancel_event: threading.Event | None = None,
        dry_run: bool = False,
        confirmed_steps: set[str] | frozenset[str] = frozenset(),
        checkpoint_timeout_s: float = DEFAULT_CHECKPOINT_TIMEOUT_S,
    ):
        self.surface = surface
        # Whole-run budget, checked before every action and inside every recovery wait. It cannot
        # interrupt an action already in flight (that is bounded by the page's own action timeout),
        # and it is never checked between an irreversible step and its verification.
        self.deadline_s = deadline_s
        self.cancel_event = cancel_event
        self.dry_run = dry_run
        # Step ids a human or approval flow has authorised to commit. Only these steps get
        # Action.confirmed=True; the safety policy blocks every other irreversible step.
        self.confirmed_steps = frozenset(confirmed_steps)
        self.checkpoint_timeout_s = checkpoint_timeout_s
        self._deadline_at: float | None = None
        self._commit_step: str | None = None  # irreversible step issued (ambiguous or confirmed)
        self._committed = False
        self._skipped: list[str] = []
        self._current_capability: str | None = None
        self.evidence_logger = evidence_logger
        self.artifacts_dir = artifacts_dir
        self.reauth_credentials = reauth_credentials
        # On a failure with no known business/recoverable signal, this is what pauses for a
        # human instead of failing immediately -- exactly once per step (see
        # _run_step_with_recovery's depth==0 guard).
        self.session_manager = session_manager
        # Accumulate, never reset mid-run: _reauthenticate() below calls self.run() recursively
        # on this same instance for the login sub-capability, and the outer call's final result
        # must still reflect anything that happened before reauthentication kicked in. Set only
        # to True at the moment each condition actually fires (the dashboard needs
        # "recoverable"/"escalated" as run statuses in their own right, distinct from the
        # underlying ReplayStatus).
        self._escalated = False
        self._recovered = False

    def run(self, artifact: Artifact, inputs: dict[str, Any]) -> ReplayResult:
        # run() recurses for the re-login sub-capability: actions are tagged with the artifact they belong to, and restored afterwards
        previous, self._current_capability = self._current_capability, artifact.capability_id
        try:
            return self._run(artifact, inputs)
        finally:
            self._current_capability = previous

    def _run(self, artifact: Artifact, inputs: dict[str, Any]) -> ReplayResult:
        started_at = datetime.now(UTC)
        # run() recurses for the re-login sub-capability; that must spend the outer run's budget,
        # not restart the clock.
        if self._deadline_at is None and self.deadline_s is not None:
            self._deadline_at = time.monotonic() + self.deadline_s
        input_errors = validate_input(artifact.input_schema, inputs)
        if input_errors:
            return self._finish(artifact, started_at, ReplayResult(
                status=ReplayStatus.HARD_FAILURE, capability_id=artifact.capability_id,
                error=ReplayError(message="; ".join(input_errors), code="input_invalid"),
                started_at=started_at, finished_at=datetime.now(UTC),
            ))

        # Backfill declared-but-not-required input_schema properties the caller omitted, with
        # an empty string -- found via a real crash: a field can be genuinely optional on the
        # target's own form, but a step's params still reference {{name}},
        # and substitute() has no notion of "this template variable is allowed to be missing."
        # Only fills gaps for properties the artifact itself declares optional; validate_input
        # above already enforces required ones strictly, so this can't mask a real caller error.
        conditional = {s.when_present for s in artifact.steps if s.when_present}
        variables = {
            k: "" for k in artifact.input_schema.properties if k not in artifact.input_schema.required and k not in conditional
        }
        # An input an artifact marks `when_present` is genuinely optional: omitted (or null) means "leave that field alone",
        # so it is neither backfilled nor sent. An empty string, by contrast, is a deliberate "set it to empty".
        variables.update({k: v for k, v in inputs.items() if v is not None or k not in conditional})
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
                        error=ReplayError(message="exceeded max artifact restarts after reauthentication", code="max_recovery"),
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
            except _NeedsReview as nr:
                return self._finish(artifact, started_at, ReplayResult(
                    status=ReplayStatus.NEEDS_REVIEW, capability_id=artifact.capability_id,
                    error=nr.error, steps_completed=nr.steps_completed,
                    started_at=started_at, finished_at=datetime.now(UTC),
                ))
            except _DryRunStop as ds:
                return self._finish(artifact, started_at, ReplayResult(
                    status=ReplayStatus.DRY_RUN, capability_id=artifact.capability_id,
                    outputs=ds.outputs, steps_completed=ds.steps_completed, dry_run=True,
                    error=ReplayError(step_id=ds.step_id, message=f"dry run: stopped before irreversible step {ds.step_id}", code="dry_run"),
                    started_at=started_at, finished_at=datetime.now(UTC),
                ))
            except _HardFailure as hf:
                return self._finish(artifact, started_at, ReplayResult(
                    status=ReplayStatus.HARD_FAILURE, capability_id=artifact.capability_id,
                    error=hf.error, steps_completed=hf.steps_completed,
                    started_at=started_at, finished_at=datetime.now(UTC),
                ))
            except Exception as exc:  # noqa: BLE001 -- the browser died, the page vanished, a locator was malformed...
                # If an irreversible step had already been issued, an unexpected crash is exactly the ambiguous case: report it
                # as needs_review rather than as a plain failure someone might retry.
                issued = self._commit_step is not None
                return self._finish(artifact, started_at, ReplayResult(
                    status=ReplayStatus.NEEDS_REVIEW if issued else ReplayStatus.HARD_FAILURE, capability_id=artifact.capability_id,
                    error=ReplayError(step_id=self._commit_step, code="ambiguous_commit" if issued else "engine_error",
                                      message=f"unexpected {type(exc).__name__} during replay"
                                              + ("; a commit step had already been issued, so it was not retried" if issued else "")),
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
            if step.when_present and step.when_present not in variables:
                self._skipped.append(step.step_id)
                self._log_step(step, "skipped")
                continue
            if self.dry_run and step.risk_level == StepRiskLevel.IRREVERSIBLE:
                raise _DryRunStop(step.step_id, completed, dict(outputs))
            extracted = self._run_step_with_recovery(artifact, step, variables, completed, non_idempotent_done)
            completed.append(step.step_id)
            self._log_step(step, "ok")
            if step.risk_level == StepRiskLevel.IRREVERSIBLE:
                self._committed = True
                self._commit_step = step.step_id
            if step.risk_level == StepRiskLevel.IRREVERSIBLE or not step.idempotent:
                non_idempotent_done = True
            if step.output_binding:
                outputs[step.output_binding] = coerce_output(
                    extracted, artifact.output_schema.properties.get(step.output_binding)
                )

        if not self._wait_signal(substitute_signal(artifact.success_checkpoint, variables)):
            outcome = self._classify(artifact, variables)
            if outcome is None:
                if self.session_manager is not None:
                    self.session_manager.update_observed(self.surface.perceive())
                    try:
                        self.session_manager.pause(reason="all steps completed but success_checkpoint was not met", step_id=None)
                    except SessionCancelled as sc:
                        self._escalated = True
                        raise _HardFailure(ReplayError(message=sc.reason, code=sc.code), completed) from None
                    self._escalated = True
                    if not self.surface.check_signal(substitute_signal(artifact.success_checkpoint, variables)):
                        raise _HardFailure(ReplayError(message="success_checkpoint still not met after human intervention", code="checkpoint_failed"), completed)
                    return completed, outputs
                if self._committed:
                    # every step ran, including the commit, but the page does not confirm the end state
                    raise _NeedsReview(ReplayError(
                        step_id=self._commit_step, code="ambiguous_commit",
                        message="the commit step ran but the success checkpoint was not met; verify against the target's records before retrying",
                    ), completed)
                raise _HardFailure(ReplayError(
                    message="all steps completed but success_checkpoint was not met", code="checkpoint_failed",
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
            raise _HardFailure(ReplayError(step_id=step.step_id, message="exceeded max recovery attempts", code="max_recovery"), completed_so_far)
        self._check_budget(completed_so_far)

        result = self.surface.act(self._build_action(step, variables))
        # A step that must never run twice (irreversible, or declared non-idempotent) and was
        # actually issued: from here on any doubt about the outcome is a human's to settle, never
        # a retry's. A step the safety policy refused, or whose locator never resolved, was not
        # issued, so it stays freely retryable.
        issued = result.dispatched and self._unsafe_to_repeat(step)
        if issued and step.risk_level == StepRiskLevel.IRREVERSIBLE:
            self._commit_step = step.step_id

        # Classify proactively, even after a nominally successful action -- not only on
        # checkpoint failure. A checkpoint like url_matches can pass on a broken page that
        # loaded at the right URL with the wrong content (e.g. a "service unavailable" banner
        # instead of the real page), so checking known signals first is what actually catches
        # that -- a weak checkpoint alone wouldn't.
        outcome = self._classify(artifact, variables)
        if outcome is not None:
            return self._resolve_outcome(artifact, outcome, step, variables, completed_so_far, non_idempotent_done, depth, issued)

        checkpoint_ok = result.success and (step.checkpoint is None or self._wait_signal(substitute_signal(step.checkpoint, variables)))
        if checkpoint_ok:
            return result.extracted_value

        # A known error banner can take longer to render than the action; look once more now that
        # the checkpoint wait has given the page time to finish.
        outcome = self._classify(artifact, variables)
        if outcome is not None:
            return self._resolve_outcome(artifact, outcome, step, variables, completed_so_far, non_idempotent_done, depth, issued)

        failure_message = result.error or "checkpoint failed and no known business/recoverable signal matched"
        if issued:
            raise _NeedsReview(ReplayError(
                step_id=step.step_id, code="ambiguous_commit",
                message=f"step {step.step_id} was issued but its outcome could not be confirmed ({failure_message}); "
                        "it was not retried. Verify against the target's records before running again.",
            ), completed_so_far)
        code = ("blocked" if result.blocked else "locator_unresolved" if result.unresolved
                else "action_failed" if not result.success else "checkpoint_failed")

        # Escalate to a human exactly once per step: depth==0 means this is the first time
        # we've hit this specific failure (not a retry after a resume that failed again).
        # Covers both a genuine unmatched failure AND a safety block (e.g. an irreversible
        # action needing confirmation) -- either way, the human can act on the SAME live
        # session (including performing the exact blocked action with the confirmed checkbox)
        # via the operator console, then resume.
        if self.session_manager is not None and depth == 0 and (code != "blocked" or self.session_manager.pause_on_blocked):
            self.session_manager.update_observed(self.surface.perceive())
            try:
                self.session_manager.pause(reason=failure_message, step_id=step.step_id)
            except SessionCancelled as sc:
                self._escalated = True
                raise _HardFailure(ReplayError(step_id=step.step_id, message=sc.reason, code=sc.code), completed_so_far) from None
            self._escalated = True

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

        raise _HardFailure(ReplayError(step_id=step.step_id, message=failure_message, code=code), completed_so_far)

    def _log_step(self, step: Step, status: str) -> None:
        if self.evidence_logger is not None:
            self.evidence_logger.log("replay", "step", step_id=step.step_id, status=status, capability_id=self._current_capability)

    def _resolve_outcome(self, artifact, outcome, step, variables, completed_so_far, non_idempotent_done, depth, issued) -> str | None:
        kind, _payload = outcome
        if kind == "recoverable" and issued:
            # e.g. the session-expired page or a 500 banner right after the commit click: the
            # server may or may not have processed it, so neither reauthenticating nor retrying is safe.
            raise _NeedsReview(ReplayError(
                step_id=step.step_id, code="ambiguous_commit",
                message=f"step {step.step_id} was issued and the page then showed a recoverable condition; "
                        "the commit may or may not have taken effect, so it was not retried.",
            ), completed_so_far)
        self._handle_classified(artifact, outcome, step, variables, completed_so_far, non_idempotent_done)
        return self._run_step_with_recovery(artifact, step, variables, completed_so_far, non_idempotent_done, depth + 1)

    @staticmethod
    def _unsafe_to_repeat(step: Step) -> bool:
        return step.risk_level == StepRiskLevel.IRREVERSIBLE or not step.idempotent

    # ---- time budget, cancellation, waiting ---------------------------------------------

    def _check_budget(self, completed: list[str]) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise _HardFailure(ReplayError(message="run cancelled", code="cancelled"), completed)
        if self._deadline_at is not None and time.monotonic() >= self._deadline_at:
            raise _HardFailure(ReplayError(message=f"run exceeded its {self.deadline_s:g}s time budget", code="timeout"), completed)

    def _sleep(self, seconds: float, completed: list[str]) -> None:
        """Sleeps in short slices so a cancel or the run deadline interrupts a long backoff."""
        end = time.monotonic() + seconds
        while (remaining := end - time.monotonic()) > 0:
            self._check_budget(completed)
            time.sleep(min(0.05, remaining))

    def _wait_signal(self, signal) -> bool:
        """Polls a checkpoint until it holds or the checkpoint timeout (capped by the run deadline) passes."""
        end = time.monotonic() + self.checkpoint_timeout_s
        if self._deadline_at is not None:
            end = min(end, self._deadline_at)
        while True:
            if self.surface.check_signal(signal):
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(CHECKPOINT_POLL_S)

    def _handle_classified(self, artifact, outcome, step, variables, completed_so_far, non_idempotent_done) -> None:
        kind, payload = outcome
        if kind == "business_outcome":
            raise _BusinessOutcome(payload["outcome"], payload["output_field"], completed_so_far)

        rule: RecoverableRule = payload["rule"]
        if rule.action == RecoveryAction.REAUTHENTICATE_AND_RESUME:
            if non_idempotent_done:
                raise _HardFailure(ReplayError(
                    step_id=step.step_id if step else None, code="unsafe_resume",
                    message="session expired after a non-idempotent step already completed -- cannot safely auto-resume",
                ), completed_so_far)
            self._reauthenticate(artifact)
            self._recovered = True
            raise _RestartArtifact()

        recovered = self._apply_bounded_recovery(rule, step, variables, completed_so_far)
        if not recovered:
            raise _HardFailure(ReplayError(
                step_id=step.step_id if step else None, code="max_recovery",
                message=f"recovery action '{rule.action.value}' did not clear the triggering condition within {rule.max_attempts} attempt(s)",
            ), completed_so_far)
        self._recovered = True

    def _classify(self, artifact: Artifact, variables: dict[str, Any]):
        for rule in artifact.error_handling.business_outcomes:
            if self.surface.check_signal(substitute_signal(rule.signal, variables)):
                return "business_outcome", {"outcome": rule.outcome, "output_field": rule.output_field}
        for rule in artifact.error_handling.recoverable:
            if self.surface.check_signal(substitute_signal(rule.signal, variables)):
                return "recoverable", {"rule": rule}
        return None

    def _apply_bounded_recovery(self, rule: RecoverableRule, step: Step | None, variables: dict[str, Any], completed: list[str]) -> bool:
        for attempt in range(rule.max_attempts):
            if rule.backoff_ms:
                self._sleep(rule.backoff_ms * rule.backoff_multiplier ** attempt / 1000, completed)
            else:
                self._check_budget(completed)
            if rule.action == RecoveryAction.RETRY and step is not None:
                retried = self.surface.act(self._build_action(step, variables))
                if retried.dispatched and self._unsafe_to_repeat(step):
                    # never a second issue of a step that must run once, whatever the rule says
                    raise _NeedsReview(ReplayError(
                        step_id=step.step_id, code="ambiguous_commit",
                        message=f"step {step.step_id} cannot be retried safely; a recovery rule asked to re-run it",
                    ), completed)
            elif rule.action == RecoveryAction.DISMISS_AND_CONTINUE:
                self.surface.act(Action(kind=ActionType.DISMISS_DIALOG, target=rule.recovery_target, actor="replay"))
            if not self.surface.check_signal(substitute_signal(rule.signal, variables)):
                return True
        return False

    def _reauthenticate(self, artifact: Artifact) -> None:
        if self.reauth_credentials is None or artifact.preconditions is None or not artifact.preconditions.requires_capability:
            raise _HardFailure(ReplayError(message="session expired but no reauthentication capability/credentials configured", code="no_reauth"), [])
        login_artifact = load_artifact_by_id(artifact.preconditions.requires_capability, directory=self.artifacts_dir)
        login_result = self.run(login_artifact, self.reauth_credentials)
        if login_result.status != ReplayStatus.SUCCESS:
            raise _HardFailure(ReplayError(
                message=f"reauthentication via '{login_artifact.capability_id}' did not succeed (status={login_result.status.value})", code="no_reauth",
            ), [])

    def _build_action(self, step: Step, variables: dict[str, Any]) -> Action:
        params = substitute(step.params, variables)
        return Action(kind=step.action, target=step.target, params=params, actor="replay", confirmed=step.step_id in self.confirmed_steps, step_id=step.step_id,
                      capability_id=self._current_capability)

    def _build_business_outputs(self, artifact: Artifact, outcome: str, output_field: str) -> dict[str, Any]:
        outputs: dict[str, Any] = {k: None for k in artifact.output_schema.properties}
        outputs[output_field] = outcome
        return outputs

    def _finish(self, artifact: Artifact, started_at: datetime, result: ReplayResult) -> ReplayResult:
        result.escalated = self._escalated
        result.recovered = self._recovered
        result.committed = self._committed
        result.steps_skipped = list(self._skipped)
        result.commit_step = self._commit_step
        if self.evidence_logger is not None:
            self.evidence_logger.log(
                "replay", "result",
                capability_id=artifact.capability_id, status=result.status.value,
                outputs=result.outputs, business_outcome=result.business_outcome,
                error=result.error.model_dump() if result.error else None,
                steps_completed=result.steps_completed,
                escalated=result.escalated, recovered=result.recovered,
                committed=result.committed, commit_step=result.commit_step,
                duration_s=(result.finished_at - started_at).total_seconds(),
            )
        return result
