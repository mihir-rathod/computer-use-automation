"""Turns a run's evidence log and its artifact into the step timeline the UI shows: what each step was meant to do, whether it ran, what the
page looked like afterwards, and, for the step that failed, what was expected against what actually happened."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from artifacts_lib.schema import Artifact, Signal, SignalType, Step


def describe_signal(signal: Signal | None) -> str | None:
    if signal is None:
        return None
    what = {
        SignalType.TEXT_PRESENT: f"the page shows “{signal.value}”",
        SignalType.URL_MATCHES: f"the address matches {signal.value}",
        SignalType.REDIRECTED_TO: f"the browser was sent to {signal.value}",
        SignalType.DIALOG_PRESENT: f"a dialog named “{signal.value}” is open",
        SignalType.ELEMENT_VISIBLE: f"{signal.target.semantic_description if signal.target else 'the element'} is visible",
        SignalType.ELEMENT_HIDDEN: f"{signal.target.semantic_description if signal.target else 'the element'} is hidden",
        SignalType.ELEMENT_VALUE_EQUALS: f"{signal.target.semantic_description if signal.target else 'the field'} holds “{signal.value}”",
    }
    return what.get(signal.type, signal.type.value)


def describe_step(step: Step) -> str:
    target = step.target.semantic_description if step.target else None
    verb = {"navigate": "Open", "click": "Click", "type": "Type into", "select": "Choose in", "extract": "Read", "wait_for": "Wait for",
            "dismiss_dialog": "Dismiss"}.get(step.action.value, step.action.value)
    if step.action.value == "navigate":
        return f"Open {step.params.get('url', '')}"
    return f"{verb} {target}" if target else verb


def read_events(evidence_dir: Path) -> list[dict[str, Any]]:
    log = evidence_dir / "log.jsonl"
    if not log.exists():
        return []
    events = []
    for line in log.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def build(evidence_dir: Path | None, artifact: Artifact | None, result: dict[str, Any] | None) -> dict[str, Any]:
    events = read_events(evidence_dir) if evidence_dir else []
    shots: dict[str, str | None] = {}
    outcomes: dict[str, str] = {}
    errors: dict[str, str] = {}
    paused: list[dict[str, Any]] = []
    error = (result or {}).get("error") or {}
    # A failure while signing on belongs to the sign-on capability, not to the requested task's steps.
    sign_on_failed = error.get("code") == "login_failed"
    failed_step = None if sign_on_failed else error.get("step_id")
    sign_on: dict[str, Any] | None = None
    own = artifact.capability_id if artifact else None
    for e in events:
        data = e.get("data", {})
        belongs = data.get("capability_id") in (None, own)  # events from before actions were tagged are taken as the task's own
        if e.get("event_type") == "action" and data.get("step_id") and not belongs and data.get("error_screenshot") and sign_on_failed:
            sign_on = {"status": "failed", "capability_id": data.get("capability_id"), "step_id": data["step_id"], "screenshot": Path(data["error_screenshot"]).name,
                       "actual": data.get("error") or error.get("message")}
        elif e.get("event_type") == "action" and data.get("step_id") and belongs:
            if data.get("error_screenshot"):
                shots[data["step_id"]] = Path(data["error_screenshot"]).name
            if data.get("error"):
                errors[data["step_id"]] = data["error"]
        elif e.get("event_type") == "step" and belongs:
            outcomes[data["step_id"]] = data["status"]
        elif e.get("event_type") in ("pause", "resume", "cancel"):
            paused.append({"type": e["event_type"], "at": e["ts"], "reason": data.get("reason"), "step_id": data.get("step_id")})

    items = []
    if artifact is not None:
        for index, step in enumerate(artifact.steps, 1):
            status = outcomes.get(step.step_id, "not_run")
            if status == "not_run" and step.step_id == failed_step:
                status = "failed"
            item: dict[str, Any] = {
                "n": index, "step_id": step.step_id, "action": step.action.value, "description": describe_step(step),
                "risk": step.risk_level.value, "optional_input": step.when_present, "status": status,
                "expected": describe_signal(step.checkpoint), "screenshot": shots.get(step.step_id),
            }
            if status == "failed":
                item["actual"] = errors.get(step.step_id) or error.get("message")
                item["error_code"] = error.get("code")
            items.append(item)
    if sign_on_failed and sign_on is None:
        sign_on = {"status": "failed", "capability_id": None, "step_id": error.get("step_id"), "screenshot": None, "actual": error.get("message")}
    return {"sign_on": sign_on, "steps": items, "pauses": paused, "event_count": len(events),
            "success_expectation": describe_signal(artifact.success_checkpoint) if artifact else None}


def screenshot_names(evidence_dir: Path | None) -> list[str]:
    folder = evidence_dir / "screenshots" if evidence_dir else None
    return sorted(p.name for p in folder.glob("*.png")) if folder and folder.exists() else []
