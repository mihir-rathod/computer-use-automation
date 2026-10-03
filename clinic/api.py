"""The JSON API. The modern skin is a client of this, and it is also the "by API" way to drive every
flow. Errors are JSON with a stable `error` code; see `register_error_handlers`."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from clinic.audit import Actor
from clinic.errors import (
    AlreadyProcessed,
    ClinicError,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    SessionExpired,
    ValidationFailed,
)
from clinic.services import CANCEL_REASONS, REFUND_REASONS, WRITEOFF_REASONS
from clinic.state import World
from clinic.ui import DRIFT_TEXT

router = APIRouter(prefix="/api")

_STATUS = {
    NotFound: 404, ValidationFailed: 400, PermissionDenied: 403, NotAuthenticated: 401,
    SessionExpired: 401, AlreadyProcessed: 409,
}


def world_of(request: Request) -> World:
    return request.app.state.world


def skin_of(request: Request) -> str:
    return "modern" if request.headers.get("x-clinic-client") == "modern-ui" else "api"


def current_actor(request: Request) -> Actor:
    world = world_of(request)
    user = world.auth.resolve(request.cookies.get(world.auth.cookie_name))
    return Actor(user["username"], user["role"], skin_of(request), request.state.request_id)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ClinicError)
    async def _clinic_error(request: Request, exc: ClinicError) -> JSONResponse:
        body: dict[str, Any] = {"error": exc.code, "message": exc.message}
        if exc.problems:
            body["problems"] = exc.problems
        if isinstance(exc, AlreadyProcessed):
            body["result"] = exc.result
        return JSONResponse(body, status_code=_STATUS.get(type(exc), 400))


class LoginBody(BaseModel):
    username: str
    password: str


class ContactBody(BaseModel):
    phone: str
    email: str
    address: str


class RescheduleBody(BaseModel):
    date: str
    time: str


class CancelReview(BaseModel):
    appointment: str
    reason: str
    notes: str = ""


class ClaimReview(BaseModel):
    invoice: str
    amount_cents: int


class RefundReview(BaseModel):
    invoice: str
    amount_cents: int
    reason: str
    notes: str = ""


class WriteoffReview(RefundReview):
    pass


class DecisionBody(BaseModel):
    decision: str
    note: str = ""


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    world = world_of(request)
    result = world.auth.login(body.username, body.password, skin_of(request), request.state.request_id)
    if result is None:
        raise PermissionDenied("Invalid username or password.")
    sid, user = result
    response.set_cookie(world.auth.cookie_name, sid, httponly=True, samesite="lax", path="/")
    return {"user": {"username": user["username"], "display_name": user["display_name"], "role": user["role"]}}


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
    world = world_of(request)
    world.auth.logout(request.cookies.get(world.auth.cookie_name), skin_of(request), request.state.request_id)
    response.delete_cookie(world.auth.cookie_name, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(request: Request, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    user = world_of(request).db.one("SELECT username, display_name, role FROM users WHERE username=?", (actor.username,))
    return {"user": user}


@router.get("/ui")
def ui_config(request: Request) -> dict[str, Any]:
    ui = world_of(request).ui
    return {**ui.describe(), "text": {base: ui.t(base) for base in DRIFT_TEXT}}


@router.get("/meta")
def meta(request: Request) -> dict[str, Any]:
    world = world_of(request)
    return {
        "today": world.clock.today().isoformat(), "providers": world.clinic.providers(),
        "refund_cap_cents": world.settings.refund_cap_cents,
        "cancel_reasons": CANCEL_REASONS, "refund_reasons": REFUND_REASONS, "writeoff_reasons": WRITEOFF_REASONS,
    }


@router.get("/patients")
def search_patients(request: Request, mrn: str = "", last_name: str = "", dob: str = "", page: int = 1,
                    actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    return world_of(request).clinic.search_patients(actor, mrn, last_name, dob, page)


@router.get("/patients/{patient_id}")
def patient_detail(patient_id: int, request: Request, invoice_page: int = 1,
                   actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    return world_of(request).clinic.patient_detail(actor, patient_id, invoice_page)


@router.patch("/patients/{patient_id}/contact")
def update_contact(patient_id: int, body: ContactBody, request: Request,
                   actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    return world_of(request).clinic.update_contact(actor, patient_id, body.phone, body.email, body.address)


@router.get("/appointments/{number}/slots")
def slots(number: str, date: str, request: Request, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    return {"slots": world_of(request).clinic.available_slots(number, date)}


@router.post("/appointments/{number}/reschedule")
def reschedule(number: str, body: RescheduleBody, request: Request, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    return world_of(request).clinic.reschedule(actor, number, body.date, body.time)


@router.post("/transactions/cancel/review")
def review_cancel(body: CancelReview, request: Request, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    return world_of(request).clinic.review_cancel(actor, body.appointment, body.reason, body.notes)


@router.post("/transactions/claim/review")
def review_claim(body: ClaimReview, request: Request, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    return world_of(request).clinic.review_claim(actor, body.invoice, body.amount_cents)


@router.post("/transactions/refund/review")
def review_refund(body: RefundReview, request: Request, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    return world_of(request).clinic.review_refund(actor, body.invoice, body.amount_cents, body.reason, body.notes)


@router.post("/transactions/writeoff/review")
def review_writeoff(body: WriteoffReview, request: Request, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    return world_of(request).clinic.review_writeoff(actor, body.invoice, body.amount_cents, body.reason, body.notes)


@router.post("/transactions/{token}/confirm")
def confirm(token: str, request: Request, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    return world_of(request).clinic.confirm(actor, token)


@router.post("/transactions/{token}/discard")
def discard(token: str, request: Request, actor: Actor = Depends(current_actor)) -> dict[str, bool]:
    world_of(request).clinic.discard(actor, token)
    return {"ok": True}


@router.get("/approvals")
def approvals(request: Request, status: str = "pending", actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    return {"items": world_of(request).clinic.list_approvals(actor, status)}


@router.post("/approvals/{number}/decision")
def decide(number: str, body: DecisionBody, request: Request, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    return world_of(request).clinic.decide_approval(actor, number, body.decision, body.note)


@router.get("/schedule")
def schedule(request: Request, date: str, provider: str = "", actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    return {"items": world_of(request).clinic.schedule(actor, date, provider)}


@router.get("/schedule.csv")
def schedule_csv(request: Request, date: str, provider: str = "", actor: Actor = Depends(current_actor)) -> Response:
    text = world_of(request).clinic.schedule_csv(actor, date, provider)
    return Response(text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="schedule-{date}.csv"'})
