"""MockBank -- a small, deliberately legacy-flavored bank back-office app.

Stands in for a real core-banking/servicing screen: a proxy target that exercises a
non-trivial multi-step flow.
Server-rendered Jinja2, nested-table layout, no data-testid attributes -- but real <label>/
<table><th> semantics, so the accessibility tree still carries meaningful roles and names
even though the raw markup is ugly. That gap (ugly DOM, meaningful a11y tree) is the point.

Flow: login -> member search -> member detail -> open sub-account -> confirm -> success.

Fault injection model:
- Business outcomes (not_found, permission_denied, validation_error) are pure functions of
  the input data the caller supplies (member id, form fields) -- no server state needed.
- Environmental conditions (slow, unavailable, terms_modal, expire_session) represent
  transient runtime/session state, not business data. They're armed one-shot via
  GET /_debug/simulate?condition=... and consumed by whichever request comes next. This
  keeps them invisible to the discovery agent (it never sees a "sim" control in the UI)
  while still being fully deterministic and reproducible for evidence generation.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mockbank import data

app = FastAPI(title="MockBank")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

SESSION_COOKIE = "mb_session"
SLOW_DELAY_SECONDS = 4.0


def _get_session(request: Request) -> dict | None:
    return data.get_session(request.cookies.get(SESSION_COOKIE))


# Exposed to templates so the header can show "Log Out" based on an actual live session,
# not just cookie presence -- the cookie can outlive the server-side session (e.g. a
# process restart clears in-memory SESSIONS but the browser keeps the stale cookie).
templates.env.globals["is_logged_in"] = lambda request: _get_session(request) is not None


async def apply_environmental(request: Request, session: dict) -> tuple[Response | None, bool]:
    """Consume the session's one-shot pending_sim flag, if any.

    Returns (early_response, show_terms_modal). If early_response is not None, the caller
    must return it immediately instead of rendering its normal template.
    """
    condition = session.get("pending_sim")
    session["pending_sim"] = None
    if condition == "expire_session":
        data.destroy_session(request.cookies.get(SESSION_COOKIE))
        resp = RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp, False
    if condition == "unavailable":
        return templates.TemplateResponse(request, "error_unavailable.html", {}, status_code=200), False
    if condition == "slow":
        await asyncio.sleep(SLOW_DELAY_SECONDS)
        return None, False
    if condition == "terms_modal":
        return None, True
    return None, False


@app.get("/_debug/simulate", response_class=HTMLResponse)
async def debug_simulate(request: Request, condition: str):
    """Test-only lever: arms a one-shot environmental condition for this session.

    Not part of the user-facing flow -- the discovery agent is never pointed at this route.
    An evidence-generation script or the replay harness navigates here deliberately before
    the request that should be affected.
    """
    session = _get_session(request)
    if session is None:
        return HTMLResponse("<p>No active session -- log in first, then arm a simulation.</p>", status_code=400)
    if condition not in data.ENVIRONMENTAL_CONDITIONS:
        return HTMLResponse(f"<p>Unknown condition: {condition}</p>", status_code=400)
    session["pending_sim"] = condition
    return HTMLResponse(f"<p>Simulation armed: {condition}. It fires on the next page load.</p>")


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse("/search" if _get_session(request) else "/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/search"):
    if _get_session(request):
        return RedirectResponse(next, status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None, "next": next})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/search")):
    if username != data.MOCK_USERNAME or password != data.MOCK_PASSWORD:
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid username or password.", "next": next}, status_code=401,
        )
    session_id = data.create_session(username)
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/search"
    response = RedirectResponse(safe_next, status_code=303)
    response.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="lax")
    return response


@app.post("/logout")
async def logout(request: Request):
    data.destroy_session(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/search", response_class=HTMLResponse)
async def search_form(request: Request):
    session = _get_session(request)
    if session is None:
        return RedirectResponse("/login?next=/search", status_code=303)
    early, show_modal = await apply_environmental(request, session)
    if early is not None:
        return early
    return templates.TemplateResponse(
        request, "search.html",
        {"result_status": None, "searched_id": None, "show_terms_modal": show_modal, "modal_return_to": str(request.url)},
    )


@app.post("/search", response_class=HTMLResponse)
async def search_submit(request: Request, member_id: str = Form(...)):
    session = _get_session(request)
    if session is None:
        return RedirectResponse("/login?next=/search", status_code=303)
    early, show_modal = await apply_environmental(request, session)
    if early is not None:
        return early

    member_id = member_id.strip()
    member = data.MEMBERS.get(member_id)
    if member is None:
        return templates.TemplateResponse(
            request, "search.html",
            {"result_status": "not_found", "searched_id": member_id, "show_terms_modal": show_modal, "modal_return_to": str(request.url)},
        )
    if member.restricted:
        return templates.TemplateResponse(
            request, "search.html",
            {"result_status": "permission_denied", "searched_id": member_id, "show_terms_modal": show_modal, "modal_return_to": str(request.url)},
        )
    return RedirectResponse(f"/member/{member_id}", status_code=303)


def _resolve_member(member_id: str) -> tuple[data.Member | None, str | None]:
    """Returns (member, error_status). error_status is "not_found" or "permission_denied" when
    member is None, else None -- callers must not collapse the two into a single "missing"
    case (a real bug found via testing: several routes here used to do exactly that, always
    reporting "not_found" even for a restricted member)."""
    member = data.MEMBERS.get(member_id)
    if member is None:
        return None, "not_found"
    if member.restricted:
        return None, "permission_denied"
    return member, None


@app.get("/member/{member_id}", response_class=HTMLResponse)
async def member_detail(request: Request, member_id: str):
    session = _get_session(request)
    if session is None:
        return RedirectResponse(f"/login?next=/member/{member_id}", status_code=303)
    early, show_modal = await apply_environmental(request, session)
    if early is not None:
        return early

    member = data.MEMBERS.get(member_id)
    if member is None or member.restricted:
        status = "not_found" if member is None else "permission_denied"
        return templates.TemplateResponse(
            request, "search.html",
            {"result_status": status, "searched_id": member_id, "show_terms_modal": show_modal, "modal_return_to": str(request.url)},
        )
    return templates.TemplateResponse(
        request, "member_detail.html",
        {"member": member, "show_terms_modal": show_modal, "modal_return_to": str(request.url)},
    )


@app.get("/member/{member_id}/open-subaccount", response_class=HTMLResponse)
async def open_subaccount_form(request: Request, member_id: str):
    session = _get_session(request)
    if session is None:
        return RedirectResponse(f"/login?next=/member/{member_id}/open-subaccount", status_code=303)
    early, show_modal = await apply_environmental(request, session)
    if early is not None:
        return early

    member, error_status = _resolve_member(member_id)
    if member is None:
        return templates.TemplateResponse(
            request, "search.html",
            {"result_status": error_status, "searched_id": member_id, "show_terms_modal": show_modal, "modal_return_to": str(request.url)},
        )
    return templates.TemplateResponse(
        request, "open_subaccount.html",
        {"member": member, "error": None, "account_type": "", "initial_deposit": "", "show_terms_modal": show_modal, "modal_return_to": str(request.url)},
    )


@app.post("/member/{member_id}/open-subaccount", response_class=HTMLResponse)
async def open_subaccount_submit(
    request: Request, member_id: str,
    account_type: str = Form(...), initial_deposit: str = Form(""),
):
    session = _get_session(request)
    if session is None:
        return RedirectResponse(f"/login?next=/member/{member_id}/open-subaccount", status_code=303)
    early, show_modal = await apply_environmental(request, session)
    if early is not None:
        return early

    member, error_status = _resolve_member(member_id)
    if member is None:
        return templates.TemplateResponse(
            request, "search.html",
            {"result_status": error_status, "searched_id": member_id, "show_terms_modal": show_modal, "modal_return_to": str(request.url)},
        )

    error = None
    deposit_value: float | None = None
    try:
        deposit_value = float(initial_deposit)
        if deposit_value <= 0:
            raise ValueError
    except ValueError:
        error = "Initial deposit amount is required and must be a positive number."

    if error:
        return templates.TemplateResponse(
            request, "open_subaccount.html",
            {
                "member": member, "error": error, "account_type": account_type,
                "initial_deposit": initial_deposit, "show_terms_modal": show_modal,
                "modal_return_to": str(request.url),
            },
            status_code=200,
        )

    session["pending_subaccount"] = {
        "member_id": member_id, "account_type": account_type, "initial_deposit": deposit_value,
    }
    return RedirectResponse(f"/member/{member_id}/open-subaccount/confirm", status_code=303)


@app.get("/member/{member_id}/open-subaccount/confirm", response_class=HTMLResponse)
async def open_subaccount_confirm_form(request: Request, member_id: str):
    session = _get_session(request)
    if session is None:
        return RedirectResponse(f"/login?next=/member/{member_id}/open-subaccount/confirm", status_code=303)
    early, show_modal = await apply_environmental(request, session)
    if early is not None:
        return early

    pending = session.get("pending_subaccount")
    if not pending or pending["member_id"] != member_id:
        return RedirectResponse(f"/member/{member_id}/open-subaccount", status_code=303)

    member, error_status = _resolve_member(member_id)
    if member is None:
        return templates.TemplateResponse(
            request, "search.html",
            {"result_status": error_status, "searched_id": member_id, "show_terms_modal": show_modal, "modal_return_to": str(request.url)},
        )
    return templates.TemplateResponse(
        request, "confirm_subaccount.html",
        {"member": member, "pending": pending, "show_terms_modal": show_modal, "modal_return_to": str(request.url)},
    )


@app.post("/member/{member_id}/open-subaccount/confirm", response_class=HTMLResponse)
async def open_subaccount_confirm_submit(request: Request, member_id: str, action: str = Form(...)):
    session = _get_session(request)
    if session is None:
        return RedirectResponse(f"/login?next=/member/{member_id}/open-subaccount/confirm", status_code=303)
    early, show_modal = await apply_environmental(request, session)
    if early is not None:
        return early

    pending = session.get("pending_subaccount")
    if not pending or pending["member_id"] != member_id:
        return RedirectResponse(f"/member/{member_id}/open-subaccount", status_code=303)

    if action == "cancel":
        session["pending_subaccount"] = None
        return RedirectResponse(f"/member/{member_id}", status_code=303)

    member, error_status = _resolve_member(member_id)
    session["pending_subaccount"] = None
    if member is None:
        return templates.TemplateResponse(
            request, "search.html",
            {"result_status": error_status, "searched_id": member_id, "show_terms_modal": show_modal, "modal_return_to": str(request.url)},
        )

    confirmation_number = data.next_confirmation_number()
    member.sub_accounts.append(data.SubAccount(
        account_type=pending["account_type"],
        balance=pending["initial_deposit"],
        confirmation_number=confirmation_number,
    ))
    return templates.TemplateResponse(
        request, "success.html",
        {
            "member": member, "confirmation_number": confirmation_number, "pending": pending,
            "show_terms_modal": show_modal, "modal_return_to": str(request.url),
        },
    )


@app.post("/dismiss-modal")
async def dismiss_modal(request: Request, return_to: str = Form(...)):
    safe_return = return_to if return_to.startswith("/") and not return_to.startswith("//") else "/search"
    return RedirectResponse(safe_return, status_code=303)
