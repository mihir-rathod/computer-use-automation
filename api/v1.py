"""The v1 API: authenticated, asynchronous, and complete enough to run the whole platform from a UI or an agent.

    POST /v1/runs                      submit a run (returns at once with its id; Idempotency-Key header supported)
    GET  /v1/runs[/{id}]               list / inspect
    GET  /v1/runs/{id}/events          server-sent events: log lines and status changes until the run settles
    POST /v1/runs/{id}/cancel|approve|reject|resolve
    GET  /v1/approvals                 runs waiting for a decision
    GET  /v1/repairs[/{id}]  POST .../approve|reject
    GET  /v1/capabilities[/{id}]       schemas plus risk metadata, policy tier, versions, canary state
    GET  /v1/artifacts/{id}/versions   POST .../promote|rollback   GET .../diff
    GET  /v1/me  /v1/targets  /v1/health

Identity comes from the API key: its name is recorded as the requester or approver, and its role (viewer, operator, supervisor,
admin) decides what it may do. A run's requester can never approve it. Every state-changing path goes through the same
`prepare_run` the CLI uses, so policy, caps, idempotency and approvals cannot be skipped here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

import observability
import runtime
from artifacts_lib import storage
from artifacts_lib.diff import diff_artifacts
from artifacts_lib.lint import has_errors, lint_artifact
from evidence_lib.redaction import is_sensitive_field
from repair.apply import approve_repair, reject_repair
from repair.propose import RepairProposal
from replay.result import ReplayStatus
from runs.approvals import decide_run, resolve_run
from runs.executor import RunExecutor
from runs.store import ApprovalError, NotPermitted, RunError
from safety.config import ROLE_RANK, irreversible_step_ids, required_approval, role_allows

router = APIRouter(prefix="/v1", tags=["v1"])
_executor: RunExecutor | None = None
_FINAL = {s.value for s in ReplayStatus} - {ReplayStatus.PENDING_APPROVAL.value} | {"abandoned", "rejected"}
_SETTLED_FOR_STREAM = _FINAL | {"pending_approval"}


def adir() -> Path:
    """Where artifacts live; ARTIFACTS_DIR overrides it (tests point this at a copy)."""
    return Path(os.environ.get("ARTIFACTS_DIR", storage.DEFAULT_ARTIFACTS_DIR))


def executor() -> RunExecutor:
    global _executor
    if _executor is None:
        _executor = RunExecutor()
    return _executor


# ---- identity ---------------------------------------------------------------------------------------------------

@dataclass
class Principal:
    name: str
    role: str


_bearer = HTTPBearer(auto_error=False, description="An API key from `cli.py keys create`.")


def principal(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    if credentials is None:
        observability.log("auth.rejected", logging.WARNING, reason="no_key", path=request.url.path)
        raise HTTPException(401, "send your API key as 'Authorization: Bearer <key>'", headers={"WWW-Authenticate": "Bearer"})
    row = runtime.default_store().authenticate(credentials.credentials.strip())
    if row is None:
        observability.log("auth.rejected", logging.WARNING, reason="unknown_or_revoked_key", path=request.url.path)
        raise HTTPException(401, "unknown or revoked API key", headers={"WWW-Authenticate": "Bearer"})
    request.state.principal = row["name"]
    return Principal(row["name"], row["role"])


def require(min_role: str):
    def dep(p: Principal = Depends(principal)) -> Principal:
        if ROLE_RANK[p.role] < ROLE_RANK[min_role]:
            raise HTTPException(403, f"this needs the {min_role} role or higher (your key is {p.role})")
        return p
    return dep


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, NotPermitted):
        return HTTPException(403, str(exc))
    if isinstance(exc, (ApprovalError, RunError, ValueError)):
        return HTTPException(409, str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, str(exc))
    raise exc


# ---- views ---------------------------------------------------------------------------------------------------------

def _mask(params: dict[str, Any]) -> dict[str, Any]:
    return {k: ("***" if is_sensitive_field(k) else v) for k, v in params.items()}


def run_view(row: dict[str, Any], detail: bool = False) -> dict[str, Any]:
    store = runtime.default_store()
    out = {k: row[k] for k in ("id", "capability_id", "version", "status", "requested_by", "target", "idempotency_key", "committed",
                               "commit_step", "error_code", "created_at", "started_at", "finished_at", "resolution")}
    out["committed"] = bool(out["committed"])
    out["pace_ms"] = int(row.get("pace_ms") or 0)
    out["show_window"] = bool(row.get("show_window"))
    out["params"] = _mask(json.loads(row["params_json"]))
    out["evidence"] = Path(row["evidence_dir"]).name if row.get("evidence_dir") else None
    out["has_trace"] = bool(row.get("evidence_dir")) and (Path(row["evidence_dir"]) / "trace.zip").exists()
    if detail:
        out["approvals"] = store.approvals_for(row["id"])
        result = store.stored_result(row)
        out["result"] = result.model_dump(mode="json") if result else None
        out["repairs"] = [{"id": r["id"], "status": r["status"], "step_id": r["step_id"], "confident": bool(r["confident"])}
                          for r in store.list_repairs() if r["run_id"] == row["id"]]
    return out


def _get_run(run_id: str) -> dict[str, Any]:
    row = runtime.default_store().get(run_id)
    if row is None:
        raise HTTPException(404, f"unknown run {run_id}")
    return row


# ---- runs -------------------------------------------------------------------------------------------------------------

MAX_PACE_MS = 3000


def window_allowed() -> bool:
    """A visible browser window opens on the *server's* screen, so it only makes sense when the server is the machine in front of you. Off unless enabled."""
    return os.environ.get("CUA_ALLOW_WINDOW") == "1"


def check_watch(pace_ms: int, show_window: bool) -> None:
    if show_window and not window_allowed():
        raise HTTPException(422, "showing a browser window is not enabled on this server (set CUA_ALLOW_WINDOW=1 when it runs on the machine you are using)")


class SubmitRun(BaseModel):
    capability_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    target: str = Field(description="A target profile name, see GET /v1/targets. The caller cannot supply a URL or credentials.")
    version: str | None = Field(default=None, description="A specific artifact version; default is the current one.")
    dry_run: bool = Field(default=False, description="Run up to, not including, the first irreversible step; nothing is committed.")
    pace_ms: int = Field(default=0, ge=0, le=MAX_PACE_MS, description="Watch mode: pause this long around each action and outline the element it touches, so a person can follow the run.")
    show_window: bool = Field(default=False, description="Also open a visible browser window on the machine the server runs on. Only when the server allows it (see /v1/features).")


class Decision(BaseModel):
    reason: str = Field(min_length=1)
    execute: bool = Field(default=True, description="On approve: start the run straight away.")


class Resolution(BaseModel):
    outcome: Literal["committed", "not_committed"]
    reason: str = Field(min_length=1)


def _start(prepared: Any) -> dict[str, Any]:
    executor().submit(prepared)
    return run_view(_get_run(prepared.run_id))


@router.post("/runs", status_code=202)
def submit_run(body: SubmitRun, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
               who: Principal = Depends(require("operator"))) -> dict[str, Any]:
    """202 with the run id when it was accepted for execution; 200 when it was settled without a browser (waiting for an
    approval, refused by policy or validation, or answered from an earlier run with the same idempotency key)."""
    from fastapi.responses import JSONResponse

    if body.target not in runtime.TARGET_PROFILES:
        raise HTTPException(422, f"unknown target '{body.target}'; known: {sorted(runtime.TARGET_PROFILES)}")
    check_watch(body.pace_ms, body.show_window)
    try:
        prepared = runtime.prepare_run(body.capability_id, body.params, target=body.target, requested_by=who.name, idempotency_key=idempotency_key,
                                       dry_run=body.dry_run, enable_operator_console=False, queued=True, version=body.version, artifacts_dir=adir(),
                                       pace_ms=body.pace_ms, show_window=body.show_window)
    except (FileNotFoundError, storage.UnknownVersion):
        raise HTTPException(404, f"unknown capability or version '{body.capability_id}' {body.version or ''}".strip()) from None
    if isinstance(prepared, runtime.Early):
        result = prepared.result
        view = run_view(_get_run(result.run_id), detail=True) if result.run_id else {"status": result.status.value}
        view["result"] = result.model_dump(mode="json")
        return JSONResponse(view, status_code=200)  # type: ignore[return-value]
    return _start(prepared)


@router.get("/runs")
def list_runs(status: str | None = None, capability_id: str | None = None, requested_by: str | None = None, q: str | None = None,
              limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), _: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    return {"runs": [run_view(r) for r in runtime.default_store().list_runs(capability_id, status, limit, requested_by, q, offset)]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, _: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    return run_view(_get_run(run_id), detail=True)


@router.get("/runs/{run_id}/timeline")
def timeline(run_id: str, _: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    """The run's steps as the artifact defined them, with each one's outcome, expected result and (when kept) screenshot. For the step that
    failed: what was expected against what actually happened."""
    from api import timeline as tl
    row = _get_run(run_id)
    try:
        artifact = storage.load_artifact_by_id(row["capability_id"], adir(), version=row["version"])
    except (FileNotFoundError, storage.UnknownVersion):
        artifact = None
    stored = runtime.default_store().stored_result(row)
    evidence = Path(row["evidence_dir"]) if row.get("evidence_dir") else None
    out = tl.build(evidence, artifact, stored.model_dump(mode="json") if stored else None)
    out["screenshots"] = tl.screenshot_names(evidence)
    return out


_SHOT = __import__("re").compile(r"^[0-9]{3,}\.png$")


@router.get("/runs/{run_id}/screenshots/{name}")
def screenshot(run_id: str, name: str, _: Principal = Depends(require("operator"))):
    """A screenshot kept as evidence. Operators and above: it shows whatever the page showed, and cannot be redacted."""
    from fastapi.responses import FileResponse
    row = _get_run(run_id)
    if not _SHOT.match(name) or not row.get("evidence_dir"):
        raise HTTPException(404, "no such screenshot")
    path = Path(row["evidence_dir"]) / "screenshots" / name
    if not path.exists():
        raise HTTPException(404, "no such screenshot")
    return FileResponse(path, media_type="image/png")


@router.get("/runs/{run_id}/trace")
def trace(run_id: str, who: Principal = Depends(require("operator"))):
    """The run's Playwright trace (kept for failures against sandbox targets by default). Open it with `playwright show-trace`.
    Operators and above only: it records everything that was typed."""
    from fastapi.responses import FileResponse
    row = _get_run(run_id)
    path = Path(row["evidence_dir"]) / "trace.zip" if row.get("evidence_dir") else None
    if path is None or not path.exists():
        raise HTTPException(404, f"run {run_id} has no trace (policy keeps them for failures on sandbox targets)")
    observability.log("trace.downloaded", run_id=run_id, by=who.name)
    return FileResponse(path, media_type="application/zip", filename=f"{run_id}-trace.zip")


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, who: Principal = Depends(require("operator"))) -> dict[str, Any]:
    row = _get_run(run_id)
    if row["status"] not in ("queued", "running"):
        raise HTTPException(409, f"run {run_id} is {row['status']}; only a queued or running run can be cancelled")
    if not executor().cancel(run_id):
        raise HTTPException(409, f"run {run_id} is not running in this server process")
    return {"id": run_id, "cancelling": True, "by": who.name}


@router.get("/approvals")
def approvals(_: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    store = runtime.default_store()
    items = []
    for a in store.pending_approvals():
        items.append({"run_id": a["run_id"], "capability_id": a["capability_id"], "tier": a["tier"], "requested_by": a["requested_by"],
                      "requested_at": a["requested_at"], "params": _mask(json.loads(a["params_json"]))})
    return {"approvals": items}


@router.post("/runs/{run_id}/approve", status_code=202)
def approve(run_id: str, body: Decision, who: Principal = Depends(require("operator"))) -> dict[str, Any]:
    store, policy = runtime.default_store(), runtime.default_policy()
    try:
        decide_run(store, policy, run_id, "approved", who.name, body.reason, role=who.role)
        if not body.execute:
            return run_view(_get_run(run_id), detail=True)
        prepared = runtime.prepare_run("", {}, target=None, resume_run_id=run_id, enable_operator_console=False, queued=True, artifacts_dir=adir())
        return _start(prepared)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/runs/{run_id}/reject")
def reject(run_id: str, body: Decision, who: Principal = Depends(require("operator"))) -> dict[str, Any]:
    try:
        decide_run(runtime.default_store(), runtime.default_policy(), run_id, "rejected", who.name, body.reason, role=who.role)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return run_view(_get_run(run_id), detail=True)


@router.post("/runs/{run_id}/resolve")
def resolve(run_id: str, body: Resolution, who: Principal = Depends(require("operator"))) -> dict[str, Any]:
    try:
        resolve_run(runtime.default_store(), runtime.default_policy(), run_id, body.outcome, who.name, body.reason, role=who.role)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return run_view(_get_run(run_id), detail=True)


@router.get("/runs/{run_id}/events")
async def events(run_id: str, request: Request, last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
                 _: Principal = Depends(require("viewer"))) -> StreamingResponse:
    """Server-sent events. `log` events carry the run's evidence lines (id = line number, so a reconnect resumes with
    Last-Event-ID); `status` events carry each status change; `done` closes the stream when the run settles."""
    row = _get_run(run_id)
    evidence = Path(row["evidence_dir"]) / "log.jsonl" if row.get("evidence_dir") else None

    async def stream():
        sent = (last_event_id or 0)
        last_status = None
        while True:
            if await request.is_disconnected():
                return
            current = runtime.default_store().get(run_id) or row
            if current["status"] != last_status:
                last_status = current["status"]
                yield f"event: status\ndata: {json.dumps({'status': last_status, 'committed': bool(current['committed']), 'error_code': current['error_code']})}\n\n"
            if evidence and evidence.exists():
                lines = evidence.read_text().splitlines()
                for number in range(sent, len(lines)):
                    yield f"id: {number + 1}\nevent: log\ndata: {lines[number]}\n\n"
                sent = len(lines)
            if last_status in _SETTLED_FOR_STREAM:
                yield f"event: done\ndata: {json.dumps({'status': last_status})}\n\n"
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- capabilities and targets ------------------------------------------------------------------------------------------

def capability_view(artifact: Any, detail: bool = False) -> dict[str, Any]:
    policy, store = runtime.default_policy(), runtime.default_store()
    cap = policy.for_capability(artifact.capability_id)
    irreversible = sorted(irreversible_step_ids(artifact))
    canary = store.canary_history(artifact.capability_id, 1)
    out: dict[str, Any] = {
        "capability_id": artifact.capability_id, "name": artifact.name, "description": artifact.description, "version": artifact.version,
        "versions": storage.list_versions(artifact.capability_id, adir()), "app": artifact.target.app_id,
        "risk": {"level": artifact.safety.risk_level.value, "has_irreversible_step": bool(irreversible), "irreversible_steps": irreversible,
                 "approval_required": cap.approval if irreversible else None,
                 "caps": cap.caps.model_dump() if irreversible else None},
        "login": artifact.preconditions.requires_capability if artifact.preconditions else None,
        "reviewed": artifact.provenance.reviewed, "has_canary": artifact.canary is not None,
        "last_canary": ({"ok": bool(canary[0]["ok"]), "at": canary[0]["at"], "detail": canary[0]["detail"]} if canary else None),
        "input_schema": artifact.input_schema.model_dump(), "output_schema": artifact.output_schema.model_dump(),
    }
    if detail:
        from api import timeline as tl
        out["provenance"] = artifact.provenance.model_dump(mode="json")
        out["history"] = storage.history(artifact.capability_id, adir())
        out["steps"] = [{
            "step_id": s.step_id, "action": s.action.value, "description": tl.describe_step(s), "risk": s.risk_level.value, "idempotent": s.idempotent,
            "optional_input": s.when_present, "expected": tl.describe_signal(s.checkpoint),
            "locators": [{"strategy": l.strategy.value, "value": l.value} for l in (s.target.locators if s.target else [])],
            "output": s.output_binding} for s in artifact.steps]
        out["success_when"] = tl.describe_signal(artifact.success_checkpoint)
        out["business_outcomes"] = [{"when": tl.describe_signal(r.signal), "outcome": r.outcome} for r in artifact.error_handling.business_outcomes]
        out["recoverable"] = [{"when": tl.describe_signal(r.signal), "action": r.action.value, "attempts": r.max_attempts} for r in artifact.error_handling.recoverable]
    return out


@router.get("/capabilities")
def capabilities(_: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    hidden = runtime.hidden_apps()
    return {"capabilities": [capability_view(a) for a in storage.list_artifacts(adir()) if a.target.app_id not in hidden]}


@router.get("/capabilities/{capability_id}")
def capability(capability_id: str, version: str | None = None, _: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    try:
        return capability_view(storage.load_artifact_by_id(capability_id, adir(), version=version), detail=True)
    except (FileNotFoundError, storage.UnknownVersion):
        raise HTTPException(404, f"unknown capability or version '{capability_id}' {version or ''}".strip()) from None


@router.get("/targets")
def targets(_: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    return {"targets": [{"name": n, "app": p.get("app_id"), "base_url": p["base_url"], "sandbox": bool(p.get("sandbox")),
                         "signs_in_as": p["username"]} for n, p in runtime.TARGET_PROFILES.items() if not p.get("hidden")]}


@router.get("/me")
def me(who: Principal = Depends(principal)) -> dict[str, str]:
    return {"name": who.name, "role": who.role}


@router.get("/features")
def features(_: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    """What this server can do for the console: whether it may open a visible browser window, and the watch-mode pace limits."""
    return {"show_window": window_allowed(), "max_pace_ms": MAX_PACE_MS, "watch_presets": {"slow": 700, "step_by_step": 1800}}


@router.get("/health")
def health() -> dict[str, Any]:
    store = runtime.default_store()
    return {"ok": True, "browser_pool": runtime.get_pool().stats(), "runs_active": len(store.list_runs(status="running", limit=500)) + len(store.list_runs(status="queued", limit=500))}


# ---- policy and keys ---------------------------------------------------------------------------------------------------

@router.get("/policy")
def policy_view(_: Principal = Depends(require("operator"))) -> dict[str, Any]:
    """The safety policy as it is right now (the file is re-read on every run). Read-only here: it is changed by editing safety/policy.yaml."""
    p = runtime.default_policy()
    return {
        "approvers": p.approvers,
        "capabilities": {k: {"approval": v.approval, "caps": v.caps.model_dump()} for k, v in p.capabilities.items() if k.split(".")[0] not in runtime.hidden_apps()},
        "default_approval": p.default_approval, "teaching": p.teaching.model_dump(),
        "tracing": p.tracing.model_dump(), "evidence": p.evidence.model_dump(),
        "risk_keywords": p.risk_keywords.model_dump(),
        "redaction": {"patterns": sorted(p.redaction.patterns), "field_names": p.redaction.field_names},
        "approval_tiers": {"live": "blocked until a person confirms it on the operator console, mid-run",
                           "operator": "needs a recorded approval from an operator or above, other than the requester",
                           "supervisor": "needs a recorded approval from a supervisor or admin, other than the requester"},
    }


class NewKey(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    role: Literal["viewer", "operator", "supervisor", "admin"]


@router.get("/keys")
def keys(_: Principal = Depends(require("admin"))) -> dict[str, Any]:
    return {"keys": runtime.default_store().list_keys()}


@router.post("/keys", status_code=201)
def create_key(body: NewKey, who: Principal = Depends(require("admin"))) -> dict[str, Any]:
    """The key is returned once and only its hash is kept."""
    key = runtime.default_store().create_key(body.name, body.role)
    observability.log("key.created", name=body.name, role=body.role, created_by=who.name)
    return {"name": body.name, "role": body.role, "key": key}


@router.post("/keys/{name}/revoke")
def revoke_key(name: str, who: Principal = Depends(require("admin"))) -> dict[str, Any]:
    if name == who.name:
        raise HTTPException(409, "you cannot revoke the key you are using")
    n = runtime.default_store().revoke_key(name)
    observability.log("key.revoked", name=name, revoked_by=who.name, count=n)
    return {"name": name, "revoked": n}


# ---- metrics ----------------------------------------------------------------------------------------------------------

@router.get("/metrics")
def metrics(since_hours: float | None = Query(None, gt=0), capability_id: str | None = None,
            _: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    """Per-capability success, failure, escalation and latency figures computed from the run store (definitions in runs/metrics.py)."""
    from runs import metrics as m
    out = m.compute(runtime.default_store(), since_hours, capability_id)
    out["browser_pool"] = runtime.get_pool().stats()
    return out


@router.get("/metrics.prom")
def metrics_prom(_: Principal = Depends(require("viewer"))):
    from fastapi.responses import PlainTextResponse
    from runs import metrics as m
    pool = runtime.get_pool().stats()
    return PlainTextResponse(m.prometheus(m.compute(runtime.default_store()), pool), media_type="text/plain; version=0.0.4")


# ---- inbox -----------------------------------------------------------------------------------------------------------------

@router.get("/inbox")
def inbox(_: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    """Everything waiting for a person: approvals, runs whose commit outcome is unknown, and repair proposals. One call, so the console can
    show a badge and the inbox page from the same data."""
    store = runtime.default_store()
    approvals_ = [{"run_id": a["run_id"], "capability_id": a["capability_id"], "tier": a["tier"], "requested_by": a["requested_by"],
                   "requested_at": a["requested_at"], "params": _mask(json.loads(a["params_json"]))} for a in store.pending_approvals()]
    reviews = [run_view(r) for r in store.unresolved_reviews()]
    repairs_ = []
    for r in store.list_repairs("pending"):
        p = RepairProposal.model_validate_json(r["proposal_json"])
        repairs_.append({"id": r["id"], "capability_id": r["capability_id"], "step_id": r["step_id"], "run_id": r["run_id"], "created_at": r["created_at"],
                         "confident": bool(r["confident"]), "reason": p.reason, "touches_irreversible_step": p.touches_irreversible_step,
                         "old_locators": [{"strategy": l.strategy.value, "value": l.value} for l in p.old_target.locators],
                         "new_locators": [{"strategy": l.strategy.value, "value": l.value} for l in (p.new_target.locators if p.new_target else [])],
                         "candidates": [c.model_dump() for c in p.candidates[:4]], "has_screenshot": bool(p.screenshot), "page_url": p.page_url,
                         "description": p.old_target.semantic_description})
    teach_commits = [{"id": t["id"], "capability_id": t["capability_id"], "created_by": t["created_by"], "request": json.loads(t["commit_request_json"]) if t["commit_request_json"] else None}
                     for t in store.teach_active() if t["status"] == "awaiting_commit"]
    return {"approvals": approvals_, "needs_review": reviews, "repairs": repairs_, "teach_commits": teach_commits,
            "counts": {"approvals": len(approvals_), "needs_review": len(reviews), "repairs": len(repairs_), "teach_commits": len(teach_commits),
                       "total": len(approvals_) + len(reviews) + len(repairs_) + len(teach_commits)}}


# ---- repairs ---------------------------------------------------------------------------------------------------------

@router.get("/repairs")
def repairs(status: str | None = None, capability_id: str | None = None, _: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    rows = runtime.default_store().list_repairs(status, capability_id)
    return {"repairs": [{k: r[k] for k in ("id", "capability_id", "base_version", "step_id", "run_id", "status", "created_at", "decided_by", "new_version")}
                        | {"confident": bool(r["confident"])} for r in rows]}


@router.get("/repairs/{repair_id}")
def repair(repair_id: str, _: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    row = runtime.default_store().get_repair(repair_id)
    if row is None:
        raise HTTPException(404, f"unknown repair {repair_id}")
    return {"status": row["status"], "decided_by": row["decided_by"], "reason": row["reason"], "new_version": row["new_version"],
            "proposal": RepairProposal.model_validate_json(row["proposal_json"]).model_dump(mode="json")}


@router.get("/repairs/{repair_id}/screenshot")
def repair_screenshot(repair_id: str, _: Principal = Depends(require("operator"))):
    """The page as it looked where the step failed, for the person reviewing the proposal."""
    from fastapi.responses import FileResponse
    row = runtime.default_store().get_repair(repair_id)
    if row is None:
        raise HTTPException(404, f"unknown repair {repair_id}")
    path = Path(RepairProposal.model_validate_json(row["proposal_json"]).screenshot or "")
    run = runtime.default_store().get(row["run_id"]) if row["run_id"] else None
    allowed = Path(run["evidence_dir"]).resolve() if run and run.get("evidence_dir") else None
    if not path.is_file() or allowed is None or not path.resolve().is_relative_to(allowed):
        raise HTTPException(404, "this proposal has no screenshot")
    return FileResponse(path, media_type="image/png")


@router.post("/repairs/{repair_id}/approve")
def approve_repair_endpoint(repair_id: str, body: Decision, who: Principal = Depends(require("operator"))) -> dict[str, Any]:
    try:
        return approve_repair(runtime.default_store(), runtime.default_policy(), repair_id, who.name, body.reason, artifacts_dir=adir(), role=who.role)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc


@router.post("/repairs/{repair_id}/reject")
def reject_repair_endpoint(repair_id: str, body: Decision, who: Principal = Depends(require("operator"))) -> dict[str, Any]:
    try:
        reject_repair(runtime.default_store(), repair_id, who.name, body.reason)
    except Exception as exc:  # noqa: BLE001
        raise _fail(exc) from exc
    return {"id": repair_id, "status": "rejected"}


# ---- artifact versions -------------------------------------------------------------------------------------------------

@router.get("/artifacts/{capability_id}/versions")
def versions(capability_id: str, _: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    vs = storage.list_versions(capability_id, adir())
    if not vs:
        raise HTTPException(404, f"unknown capability '{capability_id}'")
    return {"current": storage.current_version(capability_id, adir()), "versions": vs, "history": storage.history(capability_id, adir())}


@router.get("/artifacts/{capability_id}/diff")
def diff(capability_id: str, a: str = Query(alias="from"), b: str = Query(alias="to"), _: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    try:
        return diff_artifacts(storage.load_artifact_by_id(capability_id, adir(), version=a), storage.load_artifact_by_id(capability_id, adir(), version=b)).to_dict()
    except (FileNotFoundError, storage.UnknownVersion) as exc:
        raise HTTPException(404, str(exc)) from None


def _promoter_ok(who: Principal, capability_id: str, version: str | None = None) -> None:
    """Changing what runs for a capability that commits something needs the tier its runs need; one that only reads needs an operator."""
    policy = runtime.default_policy()
    try:
        tier = required_approval(policy, storage.load_artifact_by_id(capability_id, adir(), version=version)) or "live"
    except (FileNotFoundError, storage.UnknownVersion):
        tier = "live"  # the caller reports the missing capability or version right after
    ok, why = role_allows(who.role, "operator" if tier == "live" else tier)
    if not ok:
        raise HTTPException(403, why)


@router.post("/artifacts/{capability_id}/promote/{version}")
def promote(capability_id: str, version: str, body: Decision, who: Principal = Depends(require("operator"))) -> dict[str, Any]:
    _promoter_ok(who, capability_id, version)
    try:
        findings = lint_artifact(storage.load_artifact_by_id(capability_id, adir(), version=version))
        if has_errors(findings):
            raise HTTPException(409, "validation errors: " + "; ".join(str(f) for f in findings if f.level == "error"))
        storage.set_current(capability_id, version, adir(), by=who.name, reason=body.reason)
    except (FileNotFoundError, storage.UnknownVersion) as exc:
        raise HTTPException(404, str(exc)) from None
    return {"capability_id": capability_id, "current": storage.current_version(capability_id, adir())}


@router.post("/artifacts/{capability_id}/rollback")
def rollback(capability_id: str, body: Decision, who: Principal = Depends(require("operator"))) -> dict[str, Any]:
    _promoter_ok(who, capability_id)
    try:
        previous = storage.rollback(capability_id, adir(), by=who.name, reason=body.reason)
    except storage.UnknownVersion as exc:
        raise HTTPException(409, str(exc)) from None
    return {"capability_id": capability_id, "current": previous}
