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
from safety.config import ROLE_RANK, irreversible_step_ids, role_allows

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

class SubmitRun(BaseModel):
    capability_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    target: str = Field(description="A target profile name, see GET /v1/targets. The caller cannot supply a URL or credentials.")
    version: str | None = Field(default=None, description="A specific artifact version; default is the current one.")
    dry_run: bool = Field(default=False, description="Run up to, not including, the first irreversible step; nothing is committed.")


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
    try:
        prepared = runtime.prepare_run(body.capability_id, body.params, target=body.target, requested_by=who.name, idempotency_key=idempotency_key,
                                       dry_run=body.dry_run, enable_operator_console=False, queued=True, version=body.version, artifacts_dir=adir())
    except (FileNotFoundError, storage.UnknownVersion):
        raise HTTPException(404, f"unknown capability or version '{body.capability_id}' {body.version or ''}".strip()) from None
    if isinstance(prepared, runtime.Early):
        result = prepared.result
        view = run_view(_get_run(result.run_id), detail=True) if result.run_id else {"status": result.status.value}
        view["result"] = result.model_dump(mode="json")
        return JSONResponse(view, status_code=200)  # type: ignore[return-value]
    return _start(prepared)


@router.get("/runs")
def list_runs(status: str | None = None, capability_id: str | None = None, limit: int = Query(50, ge=1, le=500),
              _: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    return {"runs": [run_view(r) for r in runtime.default_store().list_runs(capability_id, status, limit)]}


@router.get("/runs/{run_id}")
def get_run(run_id: str, _: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    return run_view(_get_run(run_id), detail=True)


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
        out["provenance"] = artifact.provenance.model_dump(mode="json")
        out["history"] = storage.history(artifact.capability_id, adir())
    return out


@router.get("/capabilities")
def capabilities(_: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    return {"capabilities": [capability_view(a) for a in storage.list_artifacts(adir())]}


@router.get("/capabilities/{capability_id}")
def capability(capability_id: str, version: str | None = None, _: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    try:
        return capability_view(storage.load_artifact_by_id(capability_id, adir(), version=version), detail=True)
    except (FileNotFoundError, storage.UnknownVersion):
        raise HTTPException(404, f"unknown capability or version '{capability_id}' {version or ''}".strip()) from None


@router.get("/targets")
def targets(_: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    return {"targets": [{"name": n, "app": p.get("app_id"), "base_url": p["base_url"], "sandbox": bool(p.get("sandbox")),
                         "signs_in_as": p["username"]} for n, p in runtime.TARGET_PROFILES.items()]}


@router.get("/me")
def me(who: Principal = Depends(principal)) -> dict[str, str]:
    return {"name": who.name, "role": who.role}


@router.get("/health")
def health() -> dict[str, Any]:
    store = runtime.default_store()
    return {"ok": True, "browser_pool": runtime.get_pool().stats(), "runs_active": len(store.list_runs(status="running", limit=500)) + len(store.list_runs(status="queued", limit=500))}


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


def _promoter_ok(who: Principal, capability_id: str) -> None:
    tier = runtime.default_policy().for_capability(capability_id).approval
    ok, why = role_allows(who.role, "operator" if tier == "live" else tier)
    if not ok:
        raise HTTPException(403, why)


@router.post("/artifacts/{capability_id}/promote/{version}")
def promote(capability_id: str, version: str, body: Decision, who: Principal = Depends(require("operator"))) -> dict[str, Any]:
    _promoter_ok(who, capability_id)
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
