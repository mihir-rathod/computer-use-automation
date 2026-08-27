"""The dashboard -- ASSIGNMENT_ORIGINAL.md 3.4: "a lightweight UI to watch the system work: the
capability catalog, run history (discovery and replay), each run's inputs and structured
outputs, its status (success / business outcome / recoverable / failed / escalated), and the
evidence your core already emits (steps, screenshots, DOM snapshots, timings, logs)."

Read-only, server-rendered (Jinja2, matching every other UI in this repo), no new persistence:
evidence already on disk under /evidence/*/ is the source of truth, this just reads it. Three
views: the catalog (from the same list_artifacts() the capability API itself uses), run history
(scans /evidence/*/log.jsonl), and a run detail page (full event timeline + screenshots).

Every run -- discovery or replay -- gets a "system"/"run_start" event logged as soon as its
evidence dir exists (cli.py's cmd_discover, runtime.run_replay()), specifically so this page can
identify what a run *is* even if it crashes or hangs before ever reaching a terminal result
event -- an in-progress or dead run showing up as a blank, unlabeled row would defeat the whole
point of a dashboard a reviewer debugs from.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

import runtime
from artifacts_lib.storage import list_artifacts

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _read_events(run_dir: Path) -> list[dict[str, Any]]:
    log_path = run_dir / "log.jsonl"
    if not log_path.exists():
        return []
    events = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a truncated last line (process killed mid-write) shouldn't break the page
    return events


def _parse_run(run_dir: Path) -> dict[str, Any] | None:
    """One run's summary, derived entirely from its own log.jsonl -- no second source of truth
    to drift from it. `.get(..., default)` throughout: evidence recorded before this sprint's
    escalated/recovered/run_start additions is still valid, just less detailed."""
    events = _read_events(run_dir)
    if not events:
        return None

    start = next((e for e in events if e.get("event_type") == "run_start"), None)
    start_data = start.get("data", {}) if start else {}
    kind = start_data.get("kind") or ("discovery" if run_dir.name.startswith("discovery_run") else "replay")

    replay_result = next((e for e in reversed(events) if e.get("actor") == "replay" and e.get("event_type") == "result"), None)
    discovery_result = next((e for e in reversed(events) if e.get("actor") == "agent" and e.get("event_type") == "discovery_result"), None)
    ever_paused = any(e.get("event_type") == "pause" for e in events)

    capability_id = start_data.get("capability_id")
    status = "running"
    escalated = ever_paused
    recovered = False
    business_outcome = None
    error_message = None
    outputs = None
    finished_at = None

    if replay_result:
        d = replay_result.get("data", {})
        capability_id = capability_id or d.get("capability_id")
        status = d.get("status", "unknown")  # "success" | "business_outcome" | "hard_failure"
        escalated = bool(d.get("escalated", ever_paused))
        recovered = bool(d.get("recovered", False))
        business_outcome = d.get("business_outcome")
        outputs = d.get("outputs")
        error_message = (d.get("error") or {}).get("message")
        finished_at = replay_result.get("ts")
    elif discovery_result:
        d = discovery_result.get("data", {})
        stop_reason = d.get("stop_reason")
        status = "success" if stop_reason == "finished" else "hard_failure"
        error_message = d.get("reasoning") if status == "hard_failure" else None
        finished_at = discovery_result.get("ts")
    elif ever_paused:
        status = "paused"

    return {
        "run_id": run_dir.name,
        "kind": kind,
        "capability_id": capability_id,
        "goal": start_data.get("goal"),
        "target": start_data.get("target"),
        "status": status,
        "escalated": escalated,
        "recovered": recovered,
        "business_outcome": business_outcome,
        "outputs": outputs,
        "error_message": error_message,
        "started_at": start.get("ts") if start else events[0].get("ts"),
        "finished_at": finished_at,
        "step_count": len(events),
    }


def _scan_runs() -> list[dict[str, Any]]:
    if not runtime.EVIDENCE_ROOT.exists():
        return []
    runs = [r for d in runtime.EVIDENCE_ROOT.iterdir() if d.is_dir() and (r := _parse_run(d)) is not None]
    runs.sort(key=lambda r: r["started_at"] or 0, reverse=True)
    return runs


def _screenshot_url(run_id: str, path: str | None) -> str | None:
    if not path:
        return None
    return f"/dashboard/runs/{run_id}/screenshot/{Path(path).name}"


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_catalog(request: Request):
    capabilities = [
        {
            "capability_id": a.capability_id,
            "name": a.name,
            "description": a.description,
            "target_app_id": a.target.app_id,
            "input_props": list(a.input_schema.properties.keys()),
            "risk_level": a.safety.risk_level.value,
            "requires_confirmation": a.safety.requires_confirmation,
        }
        for a in sorted(list_artifacts(), key=lambda a: a.capability_id)
    ]
    return templates.TemplateResponse(request, "dashboard_catalog.html", {"capabilities": capabilities})


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


@router.get("/dashboard/runs", response_class=HTMLResponse)
def dashboard_runs(request: Request):
    runs = _scan_runs()
    for r in runs:
        r["started_at_display"] = _fmt_ts(r["started_at"])
    return templates.TemplateResponse(request, "dashboard_runs.html", {"runs": runs})


@router.get("/dashboard/runs/{run_id}", response_class=HTMLResponse)
def dashboard_run_detail(request: Request, run_id: str):
    run_dir = runtime.EVIDENCE_ROOT / run_id
    if "/" in run_id or ".." in run_id or not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"unknown run '{run_id}'")

    summary = _parse_run(run_dir) or {"run_id": run_id, "status": "unknown"}
    summary["started_at_display"] = _fmt_ts(summary.get("started_at"))
    summary["finished_at_display"] = _fmt_ts(summary.get("finished_at"))
    if summary.get("started_at") is not None and summary.get("finished_at") is not None:
        summary["duration_display"] = f"{summary['finished_at'] - summary['started_at']:.2f}s"
    else:
        summary["duration_display"] = "-"

    events = []
    for e in _read_events(run_dir):
        data = dict(e.get("data", {}))
        screenshot = _screenshot_url(run_id, data.get("screenshot") or data.get("error_screenshot"))
        events.append({
            "ts": e.get("ts"), "ts_display": _fmt_ts(e.get("ts")),
            "actor": e.get("actor"), "event_type": e.get("event_type"),
            "data": data, "screenshot_url": screenshot,
        })
    return templates.TemplateResponse(request, "dashboard_run_detail.html", {"run": summary, "events": events})


@router.get("/dashboard/runs/{run_id}/screenshot/{filename}")
def dashboard_screenshot(run_id: str, filename: str):
    if "/" in run_id or ".." in run_id or "/" in filename or ".." in filename:
        raise HTTPException(status_code=404, detail="not found")
    path = runtime.EVIDENCE_ROOT / run_id / "screenshots" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)
