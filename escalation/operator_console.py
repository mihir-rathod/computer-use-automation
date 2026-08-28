"""A bare/mock operator console -- ASSIGNMENT_ORIGINAL.md 3.6 explicitly scopes this down:
"a full real-time co-browsing operator console is out of scope... Mock the operator UI if
needed, but make the handoff mechanism and the control-transfer model real." This is that:
plain server-rendered HTML, auto-refreshing while paused, no websockets/co-browsing -- but it
genuinely acts on the SAME live session the automation was using (via
SessionManager.request_action -> the automation thread's own pause() loop), not a fresh one.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from artifacts_lib.schema import ActionType
from escalation.registry import get_session, list_sessions
from escalation.session_manager import SessionMode
from surface.base import Action

app = FastAPI(title="Operator Console")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/operator", response_class=HTMLResponse)
async def operator_index(request: Request):
    """The one predictable entry point -- ASSIGNMENT_ORIGINAL.md 3.3/3.6: once the chatbot (not
    just the CLI, which prints each run's id to its own terminal) is what's starting runs, a
    person watching the chat page has no way to learn a specific run's id while it's paused.
    This lists whatever's currently registered so they can land on the right session without
    needing it in advance."""
    sessions = [
        {"session_id": s.session_id, "capability_id": s.capability_id, "mode": s.snapshot()["mode"], "pause_reason": s.snapshot()["pause_reason"]}
        for s in list_sessions()
    ]
    return templates.TemplateResponse(request, "operator_index.html", {"sessions": sessions})


@app.get("/operator/{session_id}", response_class=HTMLResponse)
async def operator_page(request: Request, session_id: str):
    session = get_session(session_id)
    if session is None:
        return HTMLResponse(f"<p>No session '{session_id}'.</p>", status_code=404)
    snap = session.snapshot()
    observed = snap["observed"]
    return templates.TemplateResponse(request, "operator.html", {
        "session_id": session_id,
        "mode": snap["mode"],
        "pause_reason": snap["pause_reason"],
        "capability_id": snap["capability_id"],
        "goal": snap["goal"],
        "step_id": snap["current_step_id"],
        "url": observed.url if observed else None,
        "has_screenshot": bool(observed and observed.screenshot_path),
        "elements": observed.elements if observed else [],
        "auto_refresh": snap["mode"] != SessionMode.AUTOMATION.value,
        "element_count": len(observed.elements) if observed else 0,
    })


@app.get("/operator/{session_id}/status")
async def operator_status(session_id: str):
    """Polled by the page's own background check (see templates/operator.html) so it can
    reload only when something actually changed, instead of a blind full-page refresh every
    couple of seconds -- which would also blow away anything you were mid-typing into the
    manual-action form."""
    session = get_session(session_id)
    if session is None:
        return {"mode": None}
    snap = session.snapshot()
    return {
        "mode": snap["mode"],
        "pause_reason": snap["pause_reason"],
        "element_count": len(snap["observed"].elements) if snap["observed"] else 0,
    }


@app.get("/operator/{session_id}/screenshot")
async def operator_screenshot(session_id: str):
    session = get_session(session_id)
    if session is None or session.latest_observed is None or session.latest_observed.screenshot_path is None:
        return Response(status_code=404)
    path = Path(session.latest_observed.screenshot_path)
    if not path.exists():
        return Response(status_code=404)
    return Response(content=path.read_bytes(), media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/operator/{session_id}/act")
async def operator_act(
    session_id: str,
    action_kind: str = Form(...),
    ref: str = Form(...),
    value: str = Form(""),
    confirmed: bool = Form(False),
):
    session = get_session(session_id)
    if session is None:
        return HTMLResponse(f"No session '{session_id}'.", status_code=404)
    kind = ActionType(action_kind)
    params: dict = {}
    if kind == ActionType.TYPE:
        params = {"text": value}
    elif kind == ActionType.SELECT:
        params = {"value": value}
    action = Action(kind=kind, ref=ref, params=params, actor="human", confirmed=confirmed)
    session.request_action(action)
    return RedirectResponse(f"/operator/{session_id}", status_code=303)


@app.post("/operator/{session_id}/resume")
async def operator_resume(session_id: str):
    session = get_session(session_id)
    if session is None:
        return HTMLResponse(f"No session '{session_id}'.", status_code=404)
    session.resume()
    return RedirectResponse(f"/operator/{session_id}", status_code=303)


@app.post("/operator/{session_id}/cancel")
async def operator_cancel(session_id: str):
    session = get_session(session_id)
    if session is None:
        return HTMLResponse(f"No session '{session_id}'.", status_code=404)
    session.cancel()
    return RedirectResponse(f"/operator/{session_id}", status_code=303)
