"""Turns a discovery session's evidence log into the list of model turns the console shows while it works:
what the page looked like, what the model decided, and what happened."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from api.timeline import read_events

_VERB = {"navigate": "Opened", "click": "Clicked", "type_text": "Typed into", "select_option": "Chose in", "extract": "Read", "wait_for": "Waited for"}


def build(evidence_dir: Path | None) -> list[dict[str, Any]]:
    """One entry per model decision: {n, url, title, screenshot, tool, summary, ok, error, value, reasoning}. The last entry may still be waiting
    for its result while the model is working."""
    if evidence_dir is None:
        return []
    turns: list[dict[str, Any]] = []
    seen: dict[str, Any] | None = None
    current: dict[str, Any] | None = None
    for e in read_events(evidence_dir):
        kind, data = e.get("event_type"), e.get("data", {})
        if e.get("actor") != "agent" and kind not in ("commit_decision",):
            continue
        if kind == "perceive":
            seen = data
        elif kind == "decide":
            args = data.get("args") or {}
            tool = data.get("tool") or ""
            current = {"n": len(turns) + 1, "url": (seen or {}).get("url"), "title": (seen or {}).get("title"),
                       "screenshot": Path(seen["screenshot"]).name if seen and seen.get("screenshot") else None,
                       "tool": tool, "summary": _summarise(tool, args), "ok": None, "error": None, "value": None,
                       "reasoning": args.get("reasoning") if tool in ("finish", "give_up") else None}
            turns.append(current)
        elif kind == "action" and current is not None and current["ok"] is None:
            current["ok"] = bool(data.get("success"))
            current["error"] = data.get("error")
            current["value"] = data.get("extracted_value")
        elif kind == "commit_decision" and current is not None:
            current["commit"] = {"approved": bool(data.get("approved")), "by": data.get("approver"), "mode": data.get("mode")}
    for t in turns:
        if t["tool"] in ("finish", "give_up") and t["ok"] is None:
            t["ok"] = t["tool"] == "finish"
    return turns


def _summarise(tool: str, args: dict[str, Any]) -> str:
    if tool == "finish":
        return "Finished: " + str(args.get("reasoning") or "goal reached")
    if tool == "give_up":
        return "Gave up: " + str(args.get("reasoning") or "no reason given")
    if tool == "extract":
        return f"Read “{args.get('output_name')}”"
    if tool == "navigate":
        return f"Opened {args.get('url', 'a page')}"
    verb = _VERB.get(tool, tool)
    extra = ""
    if tool == "type_text":
        extra = f" (“{args.get('text')}”)"
    elif tool == "select_option":
        extra = f" (“{args.get('value')}”)"
    return f"{verb} element {args.get('ref', '')}{extra}".strip()
