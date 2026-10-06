"""The LLM-driven observe -> decide -> act loop. The model
decides WHAT to click/type/extract to accomplish the goal against real, live observed state
(the structured element list from surface/aria.py, not pixels). Turning the resulting
transcript into a typed, reusable artifact -- checkpoints, parameterization, error_handling --
is agent/recorder.py's job, deliberately kept separate: the model discovers, the code
structures.

Stopping conditions (max steps, timeout, dead-end): a step
budget, a wall-clock budget, and dead-end detection (the exact same tool call -- name and args --
repeated back to back, meaning the model is clicking something that isn't doing anything). All
three feed the same DiscoveryResult.stop_reason, which is exactly the kind of context
an escalation/pause path needs -- this is where that context originates.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from google.genai import types

from agent.commit_gate import CommitDecision, CommitGate, describe_request
from agent.gemini_client import GeminiClient
from agent.tools import ALL_TOOLS, CLICK_ONLY_TOOLS, ToolCall, is_terminal, to_action
from artifacts_lib.schema import ActionType
from escalation.session_manager import SessionCancelled, SessionManager
from evidence_lib.logger import EvidenceLogger
from evidence_lib.redaction import is_sensitive_field
from surface.base import Action, ActionResult, ObservedState, Surface

DEFAULT_MAX_STEPS = 25
DEFAULT_TIMEOUT_SECONDS = 300
DEAD_END_THRESHOLD = 3
REVISIT_NUDGE_AFTER = 3  # visits to one page before the model is told it is going round in circles
_UNREACHABLE = re.compile(r"net::ERR_(CONNECTION_REFUSED|CONNECTION_RESET|CONNECTION_CLOSED|EMPTY_RESPONSE|NAME_NOT_RESOLVED|ADDRESS_UNREACHABLE|TIMED_OUT)")

StopReason = Literal["finished", "give_up", "max_steps", "timeout", "dead_end", "error", "cancelled"]
# Stop reasons that mean "the model is stuck," not "the model is done" -- these are what
# escalate to a human rather than just ending the run.
_STUCK_REASONS = {"give_up", "dead_end", "max_steps", "timeout"}  # ("cancelled" is a decision, not being stuck)

SYSTEM_INSTRUCTION = (
    "You are an automation agent operating a web application through a structured element "
    "list, not pixels. Each turn you are given the current URL and a numbered list of "
    "interactive elements (ref id, role, accessible name, current value/options). Call "
    "exactly one tool per turn to make progress toward the stated goal -- prefer the most "
    "direct path. Use extract() to record any piece of data the goal asks you to read. Call "
    "finish() only once the goal is genuinely and visibly achieved. Call give_up() if you are "
    "stuck: no element matches what you need, or recent actions haven't changed anything. "
    "Move around by clicking the links and buttons in the element list; never guess a URL. If no link is named exactly for what you need, "
    "open the closest-sounding one and look: function names are often not what the request calls them. "
    "Use the parameter values exactly as given. If the record they refer to cannot be found, call give_up and say it was not found: "
    "never read a different record instead, and never find the answer by browsing a list when a lookup by that value exists."
)


@dataclass
class RecordedAction:
    action: Action
    result: ActionResult
    observed_before: ObservedState
    observed_after: ObservedState | None = None
    output_name: str | None = None
    commit_approval: CommitDecision | None = None  # set when a person authorised this irreversible action


@dataclass
class DiscoveryResult:
    stop_reason: StopReason
    reasoning: str | None
    transcript: list[RecordedAction] = field(default_factory=list)
    escalated: bool = False


class DiscoveryLoop:
    def __init__(
        self,
        surface: Surface,
        gemini_client: GeminiClient,
        evidence_logger: EvidenceLogger | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        session_manager: SessionManager | None = None,
        commit_gate: CommitGate | None = None,
        capability_id: str | None = None,
        should_stop: Callable[[], bool] | None = None,
        allow_navigate: bool = True,
    ):
        self.surface = surface
        # Asked, instead of failing the step, when the safety policy blocks an irreversible action. No gate
        # means the old behaviour: the block is reported back to the model and discovery cannot commit.
        self.commit_gate = commit_gate
        self.should_stop = should_stop  # polled each turn so a person can cancel a discovery session
        self.tools = ALL_TOOLS if allow_navigate else CLICK_ONLY_TOOLS
        self.capability_id = capability_id
        self.gemini_client = gemini_client
        self.evidence_logger = evidence_logger
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        # On a "stuck" stop reason (give_up/dead_end/max_steps/timeout -- see _STUCK_REASONS),
        # this is what pauses and hands the live session to a human before the run ends,
        # instead of just returning a failed DiscoveryResult.
        self.session_manager = session_manager

    def run(self, goal: str, parameters: dict[str, str], start_path: str | None = None, output_names: list[str] | None = None) -> DiscoveryResult:
        """`start_path`, if given, is navigated to explicitly *before* the model takes over --
        recorded as the transcript's first entry, not left to chance. Without this, a
        discovered artifact only works by accident: if the browser happened to already be on
        the right page when discovery started (e.g. straight after a login helper), the model
        never needs to navigate there itself, and the resulting artifact has no starting-point
        step at all -- replay would then assume whatever page the browser happens to be on,
        which isn't guaranteed. Found via a real replay failure during testing: a discovered
        artifact with no navigate step failed at its first step when replayed against a page
        left on a different URL by the discovery run itself.
        """
        started = time.monotonic()
        transcript: list[RecordedAction] = []
        recent_calls: list[str] = []
        visits: dict[str, int] = {}
        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part.from_text(text=self._initial_prompt(goal, parameters, output_names))])
        ]

        if start_path is not None:
            observed_before = self.surface.perceive(actor="agent")
            start_action = Action(kind=ActionType.NAVIGATE, params={"url": start_path}, actor="agent")
            start_result = self.surface.act(start_action)
            transcript.append(RecordedAction(action=start_action, result=start_result, observed_before=observed_before))
            if not start_result.success:
                return self._finish(transcript, "error", f"could not navigate to start_path {start_path!r}: {start_result.error}")

        for step_index in range(self.max_steps):
            if self.should_stop is not None and self.should_stop():
                return self._finish(transcript, "cancelled", "stopped by a person")
            if time.monotonic() - started > self.timeout_seconds:
                return self._finish(transcript, "timeout", None)

            observed = self.surface.perceive(actor="agent")
            if transcript and transcript[-1].observed_after is None:
                transcript[-1].observed_after = observed

            page = urlsplit(observed.url).path
            visits[page] = visits.get(page, 0) + 1
            prompt = observed.to_prompt_text()
            if visits[page] >= REVISIT_NUDGE_AFTER and len(transcript) >= visits[page]:
                prompt += (f"\n\nNote: you have been on this page {visits[page]} times and have not finished. Going round again will not help: "
                           "pick a link or button you have not tried yet, or call give_up and say what is missing.")
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))

            response = self.gemini_client.generate(contents, tools=self.tools, system_instruction=SYSTEM_INSTRUCTION)
            model_content = response.candidates[0].content
            contents.append(model_content)

            call_part = next((p for p in model_content.parts if p.function_call), None)
            if call_part is None:
                return self._finish(transcript, "error", "model did not return a tool call")
            tool_call = ToolCall(name=call_part.function_call.name, args=dict(call_part.function_call.args))

            if self.evidence_logger is not None:
                self.evidence_logger.log("agent", "decide", tool=tool_call.name, args=self._loggable_args(tool_call, observed), step=step_index)

            # Dead-end: the exact same tool call (name + args) repeated back to back -- e.g.
            # clicking a ref that isn't doing anything. Deliberately NOT "did the page state
            # change": a sequence of distinct extract() calls legitimately never changes the
            # page (extraction is read-only by design), and hashing raw page state flagged that
            # as a false dead-end during testing against the real model.
            call_signature = f"{tool_call.name}:{sorted(tool_call.args.items())}"
            recent_calls.append(call_signature)
            if len(recent_calls) >= DEAD_END_THRESHOLD and len(set(recent_calls[-DEAD_END_THRESHOLD:])) == 1:
                return self._finish(transcript, "dead_end", f"the same tool call ({tool_call.name}) repeated {DEAD_END_THRESHOLD} times in a row without progress")

            if is_terminal(tool_call):
                reasoning = tool_call.args.get("reasoning")
                return self._finish(transcript, "finished" if tool_call.name == "finish" else "give_up", reasoning)

            action = to_action(tool_call)
            action.actor = "agent"
            result = self.surface.act(action)
            if result.error and _UNREACHABLE.search(result.error):
                # the system went away (a restart, a crash): everything after this would be the model guessing at an error page
                transcript.append(RecordedAction(action=action, result=result, observed_before=observed))
                return self._finish(transcript, "error", "the system could not be reached while the model was working (it may have restarted). Nothing was recorded; try again")
            if result.applied_params:
                action.params.update(result.applied_params)  # record what was really applied, so the recorder parameterizes the real value
            approval: CommitDecision | None = None
            if result.blocked == "irreversible_unconfirmed" and self.commit_gate is not None:
                request = describe_request(action, observed, self.capability_id, goal)
                waited_from = time.monotonic()
                decision = self.commit_gate(request)
                started += time.monotonic() - waited_from  # time spent waiting for a person's approval does not count against the model's budget
                if self.evidence_logger is not None:
                    self.evidence_logger.log("human" if decision.mode == "supervised" else "system", "commit_decision",
                                             approved=decision.approved, approver=decision.approver, mode=decision.mode,
                                             element=request.description, url=request.url)
                if decision.approved:
                    action.confirmed = True
                    result = self.surface.act(action)
                    approval = decision
                else:
                    result.error = "the supervisor declined this irreversible step; do not retry it -- finish or give_up"
            transcript.append(RecordedAction(
                action=action, result=result, observed_before=observed,
                output_name=tool_call.args.get("output_name"), commit_approval=approval,
            ))

            contents.append(types.Content(role="user", parts=[types.Part.from_function_response(
                name=tool_call.name,
                response={"success": result.success, "error": result.error, "extracted_value": result.extracted_value},
            )]))

        return self._finish(transcript, "max_steps", None)

    @staticmethod
    def _loggable_args(tool_call: ToolCall, observed: ObservedState) -> dict:
        """Typed text goes into evidence, so a value typed into a password-like field must not."""
        if tool_call.name != "type_text":
            return tool_call.args
        element = next((e for e in observed.elements if e.ref == tool_call.args.get("ref")), None)
        label = " ".join(filter(None, [element.name if element else None, element.html_name if element else None]))
        return {**tool_call.args, "text": "***REDACTED***"} if is_sensitive_field(label) else tool_call.args

    def _finish(self, transcript: list[RecordedAction], stop_reason: StopReason, reasoning: str | None) -> DiscoveryResult:
        if transcript and transcript[-1].observed_after is None:
            transcript[-1].observed_after = self.surface.perceive(actor="agent")
        if self.evidence_logger is not None:
            self.evidence_logger.log("agent", "discovery_result", stop_reason=stop_reason, reasoning=reasoning, step_count=len(transcript))

        escalated = False
        if self.session_manager is not None and stop_reason in _STUCK_REASONS:
            if self.session_manager.latest_observed is None:
                self.session_manager.update_observed(self.surface.perceive(actor="agent"))
            # Discovery never retries after this pause either way (it already gave up before
            # reaching here) -- cancelling just means skip straight to returning, same as a
            # normal resume would.
            try:
                self.session_manager.pause(reason=f"discovery stuck: {stop_reason} -- {reasoning or 'no reasoning given'}")
            except SessionCancelled:
                pass
            escalated = True

        return DiscoveryResult(stop_reason=stop_reason, reasoning=reasoning, transcript=transcript, escalated=escalated)

    def _initial_prompt(self, goal: str, parameters: dict[str, str], output_names: list[str] | None = None) -> str:
        param_lines = "\n".join(f"- {k} = {v!r}" for k, v in parameters.items()) or "(none)"
        prompt = f"Goal: {goal}\n\nUse these exact parameter values where the goal calls for them:\n{param_lines}"
        if output_names:
            prompt += ("\n\nWhen you extract() a value, use exactly one of these output_name values (no others, no variations): "
                       + ", ".join(output_names))
        return prompt
