"""The legacy skin: server-rendered, table layout, unlabeled inputs, headerless tables, and results
returned straight from a POST (so a browser refresh resubmits). It is deliberately awkward the way
real back-office screens are; the business rules all live in `clinic.services`."""
from __future__ import annotations

from datetime import date as _date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

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
from clinic.format import fmt_date, fmt_dt, fmt_time, money
from clinic.services import CANCEL_REASONS, REFUND_REASONS, WRITEOFF_REASONS
from clinic.state import World

router = APIRouter(prefix="/legacy")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters.update(money=money, fmt_date=fmt_date, fmt_dt=fmt_dt, fmt_time=fmt_time)

FUNCTIONS = {
    "find": ("Find appointment", "Appointment no."),
    "cancel": ("Cancel appointment", "Appointment no."),
    "reschedule": ("Reschedule appointment", "Appointment no."),
    "claim": ("Submit insurance claim", "Invoice no."),
    "refund": ("Issue refund", "Invoice no."),
    "writeoff": ("Write off balance", "Invoice no."),
}
REASONS = {"cancel": CANCEL_REASONS, "refund": REFUND_REASONS, "writeoff": WRITEOFF_REASONS}


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


def world_of(request: Request) -> World:
    return request.app.state.world


def legacy_actor(request: Request) -> Actor:
    world = world_of(request)
    try:
        user = world.auth.resolve(request.cookies.get(world.auth.cookie_name))
    except SessionExpired:
        raise Redirect("/legacy/login?reason=timeout") from None
    except NotAuthenticated:
        raise Redirect("/legacy/login") from None
    return Actor(user["username"], user["role"], "legacy", request.state.request_id)


def register_legacy(app: FastAPI) -> None:
    app.include_router(router)

    @app.exception_handler(Redirect)
    async def _redirect(request: Request, exc: Redirect) -> RedirectResponse:
        return RedirectResponse(exc.url, status_code=303)


def render(request: Request, name: str, actor: Actor | None = None, status: int = 200, **ctx: Any) -> HTMLResponse:
    world = world_of(request)
    ctx.update(ui=world.ui, actor=actor, today=world.clock.today().isoformat())
    return templates.TemplateResponse(request, f"legacy/{name}", ctx, status_code=status)


def fail(request: Request, actor: Actor | None, exc: ClinicError, back_url: str = "/legacy/menu") -> HTMLResponse:
    if isinstance(exc, NotFound):
        heading = "RECORD NOT FOUND"
    elif isinstance(exc, PermissionDenied):
        heading = exc.message if exc.message.isupper() else "NOT AUTHORIZED"
    elif isinstance(exc, ValidationFailed):
        heading = "Please correct the following:"
    else:
        heading = "TRANSACTION REJECTED"
    return render(request, "error.html", actor, heading=heading, problems=exc.problems, message=exc.message, back_url=back_url)


def parse_amount(text: str) -> int:
    cleaned = (text or "").replace("$", "").replace(",", "").strip()
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValidationFailed("Amount must be a number.") from None
    if not value.is_finite():
        raise ValidationFailed("Amount must be a number.")
    return int((value * 100).to_integral_value())


def to_int(text: str | None, default: int = 1) -> int:
    try:
        return int(text or default)
    except ValueError:
        return default


# ---- session ---------------------------------------------------------------------------

@router.get("/")
async def root(request: Request) -> RedirectResponse:
    world = world_of(request)
    try:
        world.auth.resolve(request.cookies.get(world.auth.cookie_name))
    except (NotAuthenticated, SessionExpired):
        return RedirectResponse("/legacy/login", status_code=303)
    return RedirectResponse("/legacy/menu", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return render(request, "login.html", reason=request.query_params.get("reason"), error=None)


@router.post("/login")
async def login(request: Request) -> Response:
    world, ui = world_of(request), world_of(request).ui
    form = await request.form()
    result = world.auth.login(ui.read(form, "username"), ui.read(form, "password"), "legacy", request.state.request_id)
    if result is None:
        return render(request, "login.html", reason=None, error="Invalid user ID or password.")
    sid, _ = result
    response = RedirectResponse("/legacy/menu", status_code=303)
    response.set_cookie(world.auth.cookie_name, sid, httponly=True, samesite="lax", path="/")
    return response


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    world = world_of(request)
    world.auth.logout(request.cookies.get(world.auth.cookie_name), "legacy", request.state.request_id)
    response = RedirectResponse("/legacy/login", status_code=303)
    response.delete_cookie(world.auth.cookie_name, path="/")
    return response


@router.get("/menu", response_class=HTMLResponse)
async def menu(request: Request, actor: Actor = Depends(legacy_actor)) -> HTMLResponse:
    return render(request, "menu.html", actor)


# ---- patients --------------------------------------------------------------------------

@router.get("/patients", response_class=HTMLResponse)
async def patients(request: Request, actor: Actor = Depends(legacy_actor)) -> HTMLResponse:
    world = world_of(request)
    ui, params = world.ui, request.query_params
    q = {key: params.get(ui.n(key), "") for key in ("mrn", "last", "dob")}
    result, problems, prev_url, next_url = None, [], None, None
    if any(q.values()):
        page = to_int(params.get("page"))
        try:
            result = world.clinic.search_patients(actor, q["mrn"], q["last"], q["dob"], page)
        except ValidationFailed as exc:
            problems = exc.problems
        if result:
            def url(n: int) -> str:
                return "/legacy/patients?" + urlencode({**{ui.n(k): v for k, v in q.items()}, "page": n})
            prev_url = url(result["page"] - 1) if result["page"] > 1 else None
            next_url = url(result["page"] + 1) if result["page"] < result["pages"] else None
    return render(request, "patients.html", actor, q=q, result=result, problems=problems, prev_url=prev_url, next_url=next_url)


@router.get("/patients/{patient_id}", response_class=HTMLResponse)
async def patient(patient_id: int, request: Request, actor: Actor = Depends(legacy_actor)) -> HTMLResponse:
    try:
        detail = world_of(request).clinic.patient_detail(actor, patient_id, to_int(request.query_params.get("inv_page")))
    except ClinicError as exc:
        return fail(request, actor, exc)
    return render(request, "patient.html", actor, d=detail, banner=None)


@router.api_route("/patients/{patient_id}/contact", methods=["GET", "POST"], response_class=HTMLResponse)
async def contact(patient_id: int, request: Request, actor: Actor = Depends(legacy_actor)) -> HTMLResponse:
    world, ui = world_of(request), world_of(request).ui
    try:
        current = world.clinic._patient(patient_id)
    except ClinicError as exc:
        return fail(request, actor, exc)
    form = {"phone": current["phone"], "email": current["email"], "address": current["address"]}
    banner, problems = None, []
    if request.method == "POST":
        posted = await request.form()
        form = {key: ui.read(posted, key) for key in form}
        try:
            result = world.clinic.update_contact(actor, patient_id, form["phone"], form["email"], form["address"])
            current = result["patient"]
            form = {"phone": current["phone"], "email": current["email"], "address": current["address"]}
            banner = "CONTACT INFORMATION UPDATED" if result["changed"] else "NO CHANGES WERE NEEDED"
        except ValidationFailed as exc:
            problems = exc.problems
    return render(request, "contact.html", actor, patient=current, form=form, banner=banner, problems=problems)


@router.get("/schedule", response_class=HTMLResponse)
async def schedule(request: Request, actor: Actor = Depends(legacy_actor)) -> HTMLResponse:
    world, ui = world_of(request), world_of(request).ui
    day = request.query_params.get(ui.n("date")) or world.clock.today().isoformat()
    provider = request.query_params.get(ui.n("provider"), "")
    rows, problems = None, []
    try:
        rows = world.clinic.schedule(actor, day, provider)
    except ValidationFailed as exc:
        problems = exc.problems
    prev_day = next_day = None
    try:
        parsed = _date.fromisoformat(day.strip())
        prev_day, next_day = (parsed - timedelta(days=1)).isoformat(), (parsed + timedelta(days=1)).isoformat()
    except ValueError:
        pass
    return render(request, "schedule.html", actor, date=day, provider=provider, providers=world.clinic.providers(),
                  rows=rows, problems=problems, prev_day=prev_day, next_day=next_day)


# ---- transactions ----------------------------------------------------------------------

def _detail_url(kind: str, number: str) -> str:
    if kind == "find":
        return f"/legacy/appointments/{number}"
    return f"/legacy/appointments/{number}/{kind}" if kind in ("cancel", "reschedule") else f"/legacy/invoices/{number}/{kind}"


@router.get("/fn/{kind}", response_class=HTMLResponse)
async def function_entry(kind: str, request: Request, actor: Actor = Depends(legacy_actor)) -> HTMLResponse:
    if kind not in FUNCTIONS:
        return fail(request, actor, NotFound("Unknown function."))
    title, prompt = FUNCTIONS[kind]
    return render(request, "fn_entry.html", actor, kind=kind, title=title, prompt=prompt, error=None)


@router.post("/fn/{kind}")
async def function_entry_submit(kind: str, request: Request, actor: Actor = Depends(legacy_actor)) -> Response:
    world, ui = world_of(request), world_of(request).ui
    if kind not in FUNCTIONS:
        return fail(request, actor, NotFound("Unknown function."))
    title, prompt = FUNCTIONS[kind]
    number = ui.read(await request.form(), "number").strip().upper()
    try:
        (world.clinic.appointment_info if kind in ("find", "cancel", "reschedule") else world.clinic.invoice_info)(number)
    except NotFound:
        return render(request, "fn_entry.html", actor, kind=kind, title=title, prompt=prompt, error="RECORD NOT FOUND")
    return RedirectResponse(_detail_url(kind, number), status_code=303)


def _txn_form(request: Request, actor: Actor, kind: str, number: str, form: dict[str, str],
              problems: list[str]) -> HTMLResponse:
    clinic = world_of(request).clinic
    try:
        if kind == "cancel":
            a = clinic.appointment_info(number)
            summary = [("Patient", f"{a['patient']['last_name']}, {a['patient']['first_name']} ({a['patient']['mrn']})"),
                       ("Appointment", f"{a['number']} - {fmt_dt(a['starts_at'])}"), ("Provider", a["provider"]),
                       ("Status", a["status"])]
            title = "Cancel appointment"
        else:
            i = clinic.invoice_info(number)
            summary = [("Patient", f"{i['patient']['last_name']}, {i['patient']['first_name']} ({i['patient']['mrn']})"),
                       ("Invoice", f"{i['number']} - {i['description']}"), ("Amount", money(i["amount_cents"])),
                       ("Balance", money(i["balance_cents"])), ("Refundable", money(i["refundable_cents"]))]
            title = FUNCTIONS[kind][0]
            if kind == "claim" and not form.get("amount") and not problems:
                form = {**form, "amount": f"{i['balance_cents'] / 100:.2f}"}
    except ClinicError as exc:
        return fail(request, actor, exc)
    return render(request, "txn_form.html", actor, kind=kind, number=number, title=title, summary=summary,
                  reasons=REASONS.get(kind), form=form, problems=problems)


@router.get("/appointments/{number}", response_class=HTMLResponse)
async def appointment_view(number: str, request: Request, actor: Actor = Depends(legacy_actor)) -> HTMLResponse:
    """Read-only: who, when, with whom. Changing it is a separate step on its own page."""
    try:
        appt = world_of(request).clinic.appointment_info(number.strip().upper())
    except ClinicError as exc:
        return fail(request, actor, exc)
    return render(request, "appointment.html", actor, appt=appt)


@router.get("/appointments/{number}/cancel", response_class=HTMLResponse)
async def cancel_form(number: str, request: Request, actor: Actor = Depends(legacy_actor)) -> HTMLResponse:
    return _txn_form(request, actor, "cancel", number, {}, [])


@router.get("/invoices/{number}/{kind}", response_class=HTMLResponse)
async def invoice_form(number: str, kind: str, request: Request, actor: Actor = Depends(legacy_actor)) -> HTMLResponse:
    if kind not in ("claim", "refund", "writeoff"):
        return fail(request, actor, NotFound("Unknown function."))
    return _txn_form(request, actor, kind, number, {}, [])


@router.post("/transactions/{kind}/review")
async def review(kind: str, request: Request, actor: Actor = Depends(legacy_actor)) -> HTMLResponse:
    world, ui = world_of(request), world_of(request).ui
    posted = await request.form()
    number = ui.read(posted, "number")
    form = {"amount": ui.read(posted, "amount"), "reason": ui.read(posted, "reason"), "notes": ui.read(posted, "notes")}
    clinic = world.clinic
    try:
        if kind == "cancel":
            draft = clinic.review_cancel(actor, number, form["reason"], form["notes"])
        elif kind == "claim":
            draft = clinic.review_claim(actor, number, parse_amount(form["amount"]))
        elif kind == "refund":
            draft = clinic.review_refund(actor, number, parse_amount(form["amount"]), form["reason"], form["notes"])
        elif kind == "writeoff":
            draft = clinic.review_writeoff(actor, number, parse_amount(form["amount"]), form["reason"], form["notes"])
        else:
            return fail(request, actor, NotFound("Unknown function."))
    except ValidationFailed as exc:
        return _txn_form(request, actor, kind, number, form, exc.problems)
    except ClinicError as exc:
        return fail(request, actor, exc)
    return render(request, "review.html", actor, review=draft["review"], token=draft["token"])


@router.post("/transactions/confirm")
async def confirm(request: Request, actor: Actor = Depends(legacy_actor)) -> HTMLResponse:
    world, ui = world_of(request), world_of(request).ui
    token = ui.read(await request.form(), "txn")
    try:
        result = world.clinic.confirm(actor, token)
    except AlreadyProcessed as exc:
        return render(request, "receipt.html", actor, result=exc.result, duplicate=True)
    except ClinicError as exc:
        return fail(request, actor, exc)
    return render(request, "receipt.html", actor, result=result, duplicate=False)


@router.post("/transactions/discard")
async def discard(request: Request, actor: Actor = Depends(legacy_actor)) -> RedirectResponse:
    world, ui = world_of(request), world_of(request).ui
    try:
        world.clinic.discard(actor, ui.read(await request.form(), "txn"))
    except NotFound:
        pass
    return RedirectResponse("/legacy/menu", status_code=303)


@router.api_route("/appointments/{number}/reschedule", methods=["GET", "POST"], response_class=HTMLResponse)
async def reschedule(number: str, request: Request, actor: Actor = Depends(legacy_actor)) -> HTMLResponse:
    world, ui = world_of(request), world_of(request).ui
    try:
        appt = world.clinic.appointment_info(number)
    except ClinicError as exc:
        return fail(request, actor, exc)
    day, slots, banner, problems = "", None, None, []
    if request.method == "GET":
        day = request.query_params.get(ui.n("date"), "")
        if day:
            try:
                slots = world.clinic.available_slots(number, day)
            except ValidationFailed as exc:
                problems = exc.problems
    else:
        posted = await request.form()
        day, slot = ui.read(posted, "date"), ui.read(posted, "time")
        try:
            world.clinic.reschedule(actor, number, day, slot)
            appt = world.clinic.appointment_info(number)
            banner, day = "APPOINTMENT RESCHEDULED", ""
        except ValidationFailed as exc:
            problems = exc.problems
    return render(request, "reschedule.html", actor, appt=appt, date=day, slots=slots, banner=banner, problems=problems)


# ---- approvals -------------------------------------------------------------------------

@router.get("/approvals", response_class=HTMLResponse)
async def approvals(request: Request, actor: Actor = Depends(legacy_actor)) -> HTMLResponse:
    try:
        items = world_of(request).clinic.list_approvals(actor)
    except ClinicError as exc:
        return fail(request, actor, exc)
    return render(request, "approvals.html", actor, items=items, banner=None, error=None)


@router.post("/approvals/decide", response_class=HTMLResponse)
async def decide(request: Request, actor: Actor = Depends(legacy_actor)) -> HTMLResponse:
    world, ui = world_of(request), world_of(request).ui
    posted = await request.form()
    pressed = ui.read(posted, "decision")
    decision = "approve" if pressed == ui.t("Approve") else "deny" if pressed == ui.t("Deny") else pressed
    banner = error = None
    try:
        outcome = world.clinic.decide_approval(actor, ui.read(posted, "number"), decision, ui.read(posted, "note"))
        banner = f"APPROVAL {outcome['approval']} {outcome['status'].upper()}"
    except ClinicError as exc:
        if isinstance(exc, PermissionDenied):
            return fail(request, actor, exc)
        error = exc.message
    try:
        items = world.clinic.list_approvals(actor)
    except ClinicError as exc:
        return fail(request, actor, exc)
    return render(request, "approvals.html", actor, items=items, banner=banner, error=error)
