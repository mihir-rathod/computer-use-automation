"""SessionManager -- pause automation, let a human operate the
SAME live session (not a fresh one), then hand control back. Single-process, in-memory --
justified the same way as the rest of this system ("simpler is fine if justified"): a real
deployment would swap this for a persisted, multi-worker-safe session store, but the
control-transfer *model* (mode, a command queue, a resume signal) is what matters and doesn't
change, only where it lives.

Threading model: automation (DiscoveryLoop / ReplayEngine) runs on the thread that owns the
live Playwright page. The operator console (escalation/operator_console.py) runs in a
*different* thread (a background uvicorn server) and must never touch that page directly --
Playwright's sync API isn't safe to call across threads. So the console only ever calls
`request_action()`, which enqueues an intent; the actual `Surface.act()` call happens inside
`pause()`'s loop, on the automation thread, exactly like every other action in this system.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from evidence_lib.logger import EvidenceLogger
from surface.base import Action, ObservedState, Surface


class SessionMode(str, Enum):
    AUTOMATION = "automation"
    PAUSED = "paused"
    HUMAN_ACTIVE = "human_active"


class SessionCancelled(Exception):
    """Raised out of pause() when an operator gives up on a run instead of resuming it. Only
    ever raised while genuinely blocked in pause()'s own queue-wait loop -- never while a live
    Playwright action is in flight -- so this is safe to interrupt from another thread; nothing
    here touches the page directly (see module docstring)."""

    def __init__(self, reason: str, step_id: str | None = None, code: str = "cancelled"):
        self.reason = reason
        self.step_id = step_id
        self.code = code  # cancelled | escalation_timeout | no_capacity: why the run did not carry on
        super().__init__(reason)


@dataclass
class InterventionRequest:
    reason: str
    capability_id: str | None
    goal: str | None
    step_id: str | None
    screenshot_path: str | None
    observed_state_text: str | None
    created_at: str


@dataclass
class HumanCommand:
    action: Action
    by: str | None = None


class SessionManager:
    def __init__(
        self,
        session_id: str,
        surface: Surface,
        evidence_dir: Path,
        evidence_logger: EvidenceLogger | None = None,
        capability_id: str | None = None,
        goal: str | None = None,
        max_wait_s: float | None = None,
        may_pause: Callable[[SessionManager], bool] | None = None,
        pause_on_blocked: bool = True,
    ):
        self.session_id = session_id
        self.surface = surface
        self.evidence_dir = evidence_dir
        self.evidence_logger = evidence_logger
        self.capability_id = capability_id
        self.goal = goal
        # A paused run holds a browser worker, so it may wait only so long, and only if there is room for it to wait.
        self.max_wait_s = max_wait_s
        self.may_pause = may_pause
        # A safety block (an irreversible step nobody confirmed, a page outside the allowlist) is only worth pausing for if the person can get past it. Someone
        # taking over from the web console cannot, so there the run fails at once instead of holding a browser.
        self.pause_on_blocked = pause_on_blocked
        self.paused_at: float | None = None  # wall-clock (time.time()) when the current pause began
        self.last_human_result: dict[str, Any] | None = None  # how the person's latest action went, so the console can say so
        self._resumed_by: str | None = None

        self.mode = SessionMode.AUTOMATION
        self.pause_reason: str | None = None
        self.current_step_id: str | None = None
        self.latest_observed: ObservedState | None = None

        self._command_queue: queue.Queue[HumanCommand] = queue.Queue()
        self._resume_event = threading.Event()
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()

    # ---- called by the automation thread -------------------------------------------------

    def update_observed(self, observed: ObservedState) -> None:
        with self._lock:
            self.latest_observed = observed

    def pause(self, reason: str, step_id: str | None = None, poll_interval: float = 0.3) -> None:
        """Blocks the calling (automation) thread until a human resumes. While paused, any
        human-submitted action is drained from the queue and executed HERE -- on the thread
        that owns the live page -- never inside the operator console's own request handler.
        """
        if self.may_pause is not None and not self.may_pause(self):
            if self.evidence_logger is not None:
                self.evidence_logger.log("system", "pause_refused", reason=reason, step_id=step_id)
            raise SessionCancelled("this run needs a person, but another run is already waiting for one and there is no room for a second", step_id, code="no_capacity")
        with self._lock:
            self.mode = SessionMode.PAUSED
            self.pause_reason = reason
            self.current_step_id = step_id
            self.paused_at = time.time()
            self.last_human_result = None
        deadline = time.monotonic() + self.max_wait_s if self.max_wait_s else None
        self._write_intervention_request(reason, step_id)
        if self.evidence_logger is not None:
            self.evidence_logger.log("system", "pause", reason=reason, step_id=step_id)
        self._resume_event.clear()
        self._cancel_event.clear()

        while not self._resume_event.is_set() and not self._cancel_event.is_set():
            try:
                command = self._command_queue.get(timeout=poll_interval)
            except queue.Empty:
                if deadline is not None and time.monotonic() > deadline:
                    with self._lock:
                        self.mode = SessionMode.AUTOMATION
                        self.pause_reason = None
                        self.paused_at = None
                    if self.evidence_logger is not None:
                        self.evidence_logger.log("system", "pause_expired", step_id=step_id, waited_s=self.max_wait_s)
                    raise SessionCancelled(f"nobody took over within {int(self.max_wait_s // 60) or 1} minute(s), so the run was stopped", step_id, code="escalation_timeout")
                continue
            with self._lock:
                self.mode = SessionMode.HUMAN_ACTIVE
            result = self.surface.act(command.action)
            if self.evidence_logger is not None:
                self.evidence_logger.log(
                    "human", "action",
                    action_kind=command.action.kind.value, ref=command.action.ref,
                    params=command.action.params, confirmed=command.action.confirmed,
                    success=result.success, error=result.error, by=command.by,
                )
            with self._lock:
                self.latest_observed = self.surface.perceive(actor="human")
                self.last_human_result = {"ok": result.success, "error": result.error, "kind": command.action.kind.value, "at": time.time()}
                self.mode = SessionMode.PAUSED  # still paused, waiting for an explicit Resume
                deadline = time.monotonic() + self.max_wait_s if self.max_wait_s else None  # a person is working on it: the clock starts again

        if self._cancel_event.is_set():
            with self._lock:
                self.mode = SessionMode.AUTOMATION
                self.pause_reason = None
                self.paused_at = None
            if self.evidence_logger is not None:
                self.evidence_logger.log("system", "cancel", step_id=step_id, by=self._resumed_by)
            raise SessionCancelled(reason="cancelled by operator", step_id=step_id)

        with self._lock:
            self.mode = SessionMode.AUTOMATION
            self.pause_reason = None
            self.paused_at = None
        if self.evidence_logger is not None:
            self.evidence_logger.log("system", "resume", step_id=step_id, by=self._resumed_by)

    def request_action(self, action: Action, by: str | None = None) -> None:
        """Called from the operator console's own thread -- only ever enqueues; never touches
        the Playwright page directly (see module docstring)."""
        self._command_queue.put(HumanCommand(action=action, by=by))

    def resume(self, by: str | None = None) -> None:
        self._resumed_by = by
        self._resume_event.set()

    def cancel(self, by: str | None = None) -> None:
        """Give up on this run instead of resuming it. Only interrupts pause()'s own
        queue-wait loop (a plain blocking wait, safe to break from another thread) -- never a
        live Playwright call, which is never safe to touch cross-thread. Raises
        SessionCancelled on the automation thread, out of pause() itself."""
        self._resumed_by = by
        self._cancel_event.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session_id": self.session_id,
                "mode": self.mode.value,
                "pause_reason": self.pause_reason,
                "current_step_id": self.current_step_id,
                "capability_id": self.capability_id,
                "goal": self.goal,
                "observed": self.latest_observed,
                "paused_at": self.paused_at,
                "max_wait_s": self.max_wait_s,
                "last_human_result": self.last_human_result,
            }

    def _write_intervention_request(self, reason: str, step_id: str | None) -> None:
        screenshot = self.latest_observed.screenshot_path if self.latest_observed else None
        request = InterventionRequest(
            reason=reason,
            capability_id=self.capability_id,
            goal=self.goal,
            step_id=step_id,
            screenshot_path=screenshot,
            observed_state_text=self.latest_observed.to_prompt_text() if self.latest_observed else None,
            created_at=datetime.now(UTC).isoformat(),
        )
        interventions_dir = self.evidence_dir / "interventions"
        interventions_dir.mkdir(parents=True, exist_ok=True)
        n = len(list(interventions_dir.glob("*.json"))) + 1
        (interventions_dir / f"{n:03d}.json").write_text(json.dumps(asdict(request), indent=2))
