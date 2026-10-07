"""The test kit: what makes this a testable target rather than just an app.

- an audit-log API as ground truth (what the server did, regardless of what a screen showed)
- an idempotent seed-and-reset
- chaos rules (latency, 500s, maintenance page, session expiry, rate limiting) and a duplicate-guard switch
- UI drift mode (renamed ids/classes, changed labels, renamed form fields)

When CLINIC_TEST_TOKEN is set (as it should be on any public deployment) every endpoint here
requires it, via the X-Test-Token header, a `clinic_test_token` cookie, or a `token` query param.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from clinic.errors import PermissionDenied, ValidationFailed
from clinic.state import World
from clinic.ui import UI

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def world_of(request: Request) -> World:
    return request.app.state.world


def require_token(request: Request) -> None:
    expected = world_of(request).settings.test_token
    if not expected:
        return
    supplied = (request.headers.get("x-test-token") or request.cookies.get("clinic_test_token")
                or request.query_params.get("token"))
    if supplied != expected:
        raise PermissionDenied("A valid test token is required.")


router = APIRouter(prefix="/_test", dependencies=[Depends(require_token)])
panel_router = APIRouter(prefix="/_test")


class ResetBody(BaseModel):
    today: str | None = None


class ChaosBody(BaseModel):
    kind: str
    params: dict[str, Any] = {}
    method: str | None = None
    path_glob: str | None = None
    remaining: int | None = 1


class GuardBody(BaseModel):
    enabled: bool


class DriftBody(BaseModel):
    level: int
    seed: str = "0"


def _state(world: World) -> dict[str, Any]:
    return {
        "clinic_date": world.clock.today().isoformat(), "counts": world.clinic.stats(),
        "chaos": world.chaos.snapshot(), "drift": world.ui.describe(),
        "audit_last_id": world.db.scalar("SELECT COALESCE(MAX(id),0) FROM audit"),
    }


@router.get("/fixtures")
def fixtures(request: Request) -> dict[str, Any]:
    return world_of(request).fixtures


@router.get("/state")
def state(request: Request) -> dict[str, Any]:
    return _state(world_of(request))


@router.post("/reset")
def reset(request: Request, body: ResetBody | None = None) -> dict[str, Any]:
    world = world_of(request)
    try:
        fixtures = world.reset(body.today if body else None)
    except ValueError:
        raise ValidationFailed("today must be an ISO date, e.g. 2026-03-02") from None
    return {"fixtures": fixtures, **_state(world)}


@router.get("/audit")
def audit(request: Request, since_id: int = 0, actor: str = "", action: str = "", entity_id: str = "",
          outcome: str = "", effects_only: bool = False, limit: int = 500) -> dict[str, Any]:
    items = world_of(request).audit.list(since_id, actor or None, action or None, entity_id or None,
                                         outcome or None, effects_only, min(limit, 2000))
    return {"items": items, "last_id": items[-1]["id"] if items else since_id}


@router.get("/chaos")
def chaos_state(request: Request) -> dict[str, Any]:
    return world_of(request).chaos.snapshot()


@router.post("/chaos")
def chaos_add(body: ChaosBody, request: Request) -> dict[str, Any]:
    try:
        rule = world_of(request).chaos.add(body.kind, body.params, body.method, body.path_glob, body.remaining)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from None
    return {"rule": {"id": rule.id, "kind": rule.kind, "remaining": rule.remaining}}


@router.delete("/chaos")
def chaos_clear(request: Request) -> dict[str, Any]:
    world_of(request).chaos.clear()
    return world_of(request).chaos.snapshot()


@router.delete("/chaos/{rule_id}")
def chaos_remove(rule_id: int, request: Request) -> dict[str, bool]:
    return {"removed": world_of(request).chaos.remove(rule_id)}


@router.post("/chaos/duplicate-guard")
def duplicate_guard(body: GuardBody, request: Request) -> dict[str, bool]:
    world_of(request).chaos.duplicate_guard = body.enabled
    return {"duplicate_guard": body.enabled}


@router.get("/drift")
def drift_state(request: Request) -> dict[str, Any]:
    return world_of(request).ui.describe()


@router.post("/drift")
def drift_set(body: DriftBody, request: Request) -> dict[str, Any]:
    world = world_of(request)
    world.ui = UI(body.level, body.seed)
    return world.ui.describe()


@router.delete("/drift")
def drift_clear(request: Request) -> dict[str, Any]:
    world = world_of(request)
    world.ui = UI()
    return world.ui.describe()


@panel_router.get("/panel", response_class=HTMLResponse)
def panel(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "testkit/panel.html", {"needs_token": bool(world_of(request).settings.test_token)})
