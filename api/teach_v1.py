"""Teaching new tasks from the console.

    GET  /v1/teach/options                  what can be taught where, and whether a model is configured
    POST /v1/teach                          start a session from a contract (supervisor or above); one at a time
    GET  /v1/teach  /v1/teach/{id}          sessions, and one session with the model's turns so far
    POST /v1/teach/{id}/cancel|commit|discard|promote
    GET  /v1/teach/{id}/screenshots/{name}

Teaching only ever creates a draft: a version nobody can run until a supervisor promotes it. Promotion goes through the same policy
tier as any other promotion.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError

import observability
import runtime
from api.v1 import Principal, adir, require
from artifacts_lib import storage
from artifacts_lib.lint import has_errors, lint_artifact
from safety.config import role_allows
from teach import turns
from teach.contract import TeachContract
from teach.service import TeachError, TeachService, model_available, teachable_targets

router = APIRouter(prefix="/v1/teach", tags=["teach"])
_service: TeachService | None = None
_SHOT = re.compile(r"^[0-9]{3,}\.png$")


def service() -> TeachService:
    """Built per store/artifacts directory, so tests that point those elsewhere get their own."""
    global _service
    store, directory = runtime.default_store(), adir()
    if _service is None or _service.store is not store or _service.adir != directory:
        _service = TeachService(store, directory, runtime.default_policy())
    _service.policy = runtime.default_policy()  # the file is re-read on every use, like everywhere else
    return _service


def _view(row: dict[str, Any], detail: bool = False) -> dict[str, Any]:
    status = row["status"]
    if status in ("ready", "needs_attention") and row.get("version") and storage.current_version(row["capability_id"], adir()) == row["version"]:
        status = "promoted"
    out = {k: row[k] for k in ("id", "created_by", "created_at", "finished_at", "capability_id", "version", "target", "stop_reason", "reasoning", "steps", "error", "retry_of")}
    out["status"] = status
    contract = TeachContract.model_validate_json(row["contract_json"])
    out["name"] = contract.name
    out["effect"] = contract.effect
    out["verify"] = json.loads(row["verify_json"]) if row.get("verify_json") else None
    if detail:
        out["contract"] = json.loads(row["contract_json"])
        out["lint"] = json.loads(row["lint_json"]) if row.get("lint_json") else None
        out["commit_request"] = json.loads(row["commit_request_json"]) if row.get("commit_request_json") else None
        out["turns"] = turns.build(Path(row["evidence_dir"]) if row.get("evidence_dir") else None)
        out["draft_url"] = f"/artifact/?id={row['capability_id']}&version={row['version']}" if row.get("version") and status in ("ready", "needs_attention", "promoted") else None
    return out


def _get(sid: str) -> dict[str, Any]:
    row = runtime.default_store().teach_get(sid)
    if row is None:
        raise HTTPException(404, f"unknown teaching session {sid}")
    return row


def _raise(exc: TeachError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@router.get("/options")
def options(_: Principal = Depends(require("supervisor"))) -> dict[str, Any]:
    policy = runtime.default_policy()
    active = runtime.default_store().teach_active()
    return {"targets": teachable_targets(policy), "model_available": model_available(), "busy": bool(active),
            "limits": policy.teaching.model_dump(), "effects": ["read_only", "changes_data", "irreversible"]}


class DraftRequest(BaseModel):
    target: str
    request: str = Field(min_length=10, max_length=1500)


@router.post("/draft")
def draft(body: DraftRequest, _: Principal = Depends(require("supervisor"))) -> dict[str, Any]:
    """Plain words in, a proposed contract out. Nothing is started and nothing is saved: the person confirms or edits it first."""
    from teach.draft import draft_contract
    from teach.service import get_model
    policy = runtime.default_policy()
    if body.target not in {t["name"] for t in teachable_targets(policy)}:
        raise HTTPException(422, f"'{body.target}' is not a target tasks can be taught on")
    if not model_available():
        raise HTTPException(503, "no model key is configured on the server (GEMINI_API_KEY)")
    try:
        from teach.peek import peek_home
        profile = runtime.TARGET_PROFILES[body.target]
        drafted = draft_contract(get_model(), body.request, body.target, profile, menu=peek_home(profile, policy, adir()))
        if "contract" in drafted:
            base = drafted["contract"]["task_name"]
            taken = {c for c in storage.capability_ids(adir()) if storage.current_version(c, adir()) is not None}
            n = 1
            while f"{profile['app_id']}.{drafted['contract']['task_name']}" in taken:
                n += 1
                drafted["contract"]["task_name"] = f"{base}_{n}"[:40]
        return drafted
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("")
def start(body: dict[str, Any], who: Principal = Depends(require("supervisor"))) -> dict[str, Any]:
    try:
        contract = TeachContract.model_validate(body.get("contract", body))
    except ValidationError as exc:
        raise HTTPException(422, [{"field": ".".join(str(p) for p in e["loc"]),
                                   "msg": ".".join(str(p) for p in e["loc"] if not isinstance(p, int)) + ": " + e["msg"].removeprefix("Value error, ")} for e in exc.errors()]) from None
    try:
        row = service().start(contract, who.name, retry_of=body.get("retry_of"))
    except TeachError as exc:
        raise _raise(exc) from None
    return _view(row)


@router.get("")
def list_sessions(_: Principal = Depends(require("supervisor"))) -> dict[str, Any]:
    return {"sessions": [_view(r) for r in runtime.default_store().teach_list()]}


@router.get("/{sid}")
def get_session(sid: str, _: Principal = Depends(require("supervisor"))) -> dict[str, Any]:
    return _view(_get(sid), detail=True)


@router.get("/{sid}/screenshots/{name}")
def screenshot(sid: str, name: str, _: Principal = Depends(require("supervisor"))):
    row = _get(sid)
    path = Path(row["evidence_dir"]) / "screenshots" / name if row.get("evidence_dir") and _SHOT.match(name) else None
    if path is None or not path.exists():
        raise HTTPException(404, "no such screenshot")
    return FileResponse(path, media_type="image/png")


@router.post("/{sid}/cancel")
def cancel(sid: str, _: Principal = Depends(require("supervisor"))) -> dict[str, Any]:
    _get(sid)
    try:
        service().cancel(sid)
    except TeachError as exc:
        raise _raise(exc) from None
    return {"ok": True}


class CommitAnswer(BaseModel):
    approve: bool
    reason: str = Field(min_length=1, max_length=300)


@router.post("/{sid}/commit")
def commit(sid: str, body: CommitAnswer, who: Principal = Depends(require("supervisor"))) -> dict[str, Any]:
    """Answers the session's request to take its one irreversible step on the sandbox, which then becomes the recorded commit step."""
    _get(sid)
    try:
        row = service().decide_commit(sid, body.approve, who.name, body.reason)
    except TeachError as exc:
        raise _raise(exc) from None
    observability.log("teach.commit_decided", session_id=sid, approved=body.approve, by=who.name)
    return _view(row)


@router.post("/{sid}/discard")
def discard(sid: str, _: Principal = Depends(require("supervisor"))) -> dict[str, Any]:
    _get(sid)
    try:
        service().discard(sid)
    except (TeachError, ValueError) as exc:
        raise HTTPException(getattr(exc, "status", 409), str(exc)) from None
    return _view(_get(sid))


class Promotion(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


@router.post("/{sid}/promote")
def promote(sid: str, body: Promotion, who: Principal = Depends(require("supervisor"))) -> dict[str, Any]:
    """Makes a ready draft the runnable version. Same policy tier as any other promotion; the verification must have passed (or not been asked for)."""
    row = _get(sid)
    view = _view(row)
    if view["status"] == "promoted":
        raise HTTPException(409, "already promoted")
    if row["status"] != "ready":
        raise HTTPException(409, f"only a draft that passed its checks can be promoted (this one is {row['status']})")
    tier = runtime.default_policy().for_capability(row["capability_id"]).approval
    ok, why = role_allows(who.role, "operator" if tier == "live" else tier)
    if not ok:
        raise HTTPException(403, why)
    artifact = storage.load_artifact_by_id(row["capability_id"], adir(), version=row["version"])
    findings = lint_artifact(artifact)
    if has_errors(findings):
        raise HTTPException(409, "validation errors: " + "; ".join(str(f) for f in findings if f.level == "error"))
    storage.set_current(row["capability_id"], row["version"], adir(), by=who.name, reason=body.reason)
    observability.log("teach.promoted", session_id=sid, capability_id=row["capability_id"], by=who.name)
    return _view(_get(sid))
