"""The chatbot -- ASSIGNMENT_ORIGINAL.md 3.3: "a minimal conversational front door... that
turns a user request into the right capability invocation(s), calls your API, and clearly
confirms success or reports the error/escalation in plain language, surfacing the structured
result." Kept thin, per the brief's own instruction: no conversation memory across turns (each
message is a fresh, self-contained request to the model), and the result is rendered by a plain
template, not a second LLM call.

Mechanically: one Gemini FunctionDeclaration per capability, generated straight from its own
input_schema -- the same JSON-Schema shape agent/tools.py already hand-builds for the discovery
tool set, generated here instead of hardcoded, which is exactly the payoff of input_schema being
JSON-Schema-shaped in the first place (artifacts_lib/schema.py's own docstring calls this out as
"relevant for the agent-facing capability interface stretch goal" -- this is that). Gemini picks
a capability + typed args from the message; the chosen capability is invoked over real HTTP
against this app's own /capabilities/{id}/invoke -- the chatbot is just another client of the
capability API, not a shortcut around it.

Login-only capabilities (no preconditions) are excluded from the offered tool set: signing in is
a precondition every invocation already handles for itself, never something a chatbot user would
ask for directly.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from google.genai import types

import runtime
from agent.gemini_client import GeminiClient
from artifacts_lib.storage import list_artifacts
from escalation.registry import get_session
from escalation.session_manager import SessionMode

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# The chatbot calls its own /invoke endpoint over real HTTP, not a direct function call -- it's
# meant to be just another client of the capability API, proving that surface is a complete,
# self-sufficient front door on its own, not something only the CLI can drive. Resolved per-call
# (not a module-level constant) so tests can point this at an in-process test server via the env
# var; the default matches this app's own documented demo port.
def _self_base_url() -> str:
    return os.environ.get("CAPABILITY_API_BASE_URL", "http://127.0.0.1:8020")


# The operator console runs as its own server (runtime.ensure_operator_console, port 8010 by
# default -- same port every _invoke() call below implicitly uses, since InvokeRequest has no
# operator_port override). Surfaced as a persistent link on the chat page itself: once the
# chatbot (not the CLI, which prints each run's id to its own terminal) is what's starting runs,
# there's no other way for someone watching the chat page to find where to go approve a pause.
def _operator_console_url() -> str:
    return os.environ.get("OPERATOR_CONSOLE_BASE_URL", "http://127.0.0.1:8010") + "/operator"

_JSON_TO_GEMINI_TYPE = {"string": "STRING", "number": "NUMBER", "integer": "INTEGER", "boolean": "BOOLEAN"}

SYSTEM_INSTRUCTION = (
    "You are a teller-facing assistant for two banking back-office systems: MockBank and "
    "MERIDIAN CORE. Each available tool is one real, callable capability against one of those "
    "systems -- call exactly one tool that matches what the user is asking for, with the exact "
    "argument values they gave (or that are obviously and unambiguously implied). If the "
    "request doesn't match any available capability, or is missing required information, don't "
    "call a tool -- reply in plain text asking for what's missing or explaining it's out of "
    "scope. Never guess at a missing required argument."
)

# In-memory, single global transcript -- purely for display on the page. Never fed back into
# the model: each message is evaluated fresh, on purpose (see module docstring).
#
# A capability-invoking entry additionally carries run_id/capability_id/done=False while its
# background thread (see _run_capability_async) is still in flight -- _display_history() below
# renders its live state (working / paused / done) fresh from the same session registry the
# operator console itself reads, rather than a second hand-maintained status.
_HISTORY: list[dict[str, Any]] = []

# Sticks across messages (until the process restarts) so "watch mode" doesn't reset after every
# send -- demoing several requests in a row shouldn't mean re-ticking the checkbox each time.
_SETTINGS = {"headed": False, "slow_mo": 0}


def _function_name(capability_id: str) -> str:
    """Gemini function names can't contain '.', which every capability_id has."""
    return capability_id.replace(".", "__")


def _json_type_to_gemini(prop_type: Any) -> str:
    if isinstance(prop_type, list):
        prop_type = next((t for t in prop_type if t != "null"), "string")
    return _JSON_TO_GEMINI_TYPE.get(prop_type, "STRING")


def _build_tools() -> tuple[list[types.Tool], dict[str, Any]]:
    """One FunctionDeclaration per non-login capability, generated from its own input_schema.
    Returns the tool list plus a lookup from Gemini function name back to the real artifact."""
    declarations = []
    by_function_name = {}
    for artifact in list_artifacts():
        if artifact.preconditions is None:
            continue  # login-only capability -- not something a chatbot user asks for directly
        fn_name = _function_name(artifact.capability_id)
        properties = {
            name: types.Schema(type=_json_type_to_gemini(prop.get("type")), description=prop.get("description"))
            for name, prop in artifact.input_schema.properties.items()
        }
        declarations.append(types.FunctionDeclaration(
            name=fn_name,
            description=f"{artifact.description} (target system: {artifact.target.app_id})",
            parameters=types.Schema(type="OBJECT", properties=properties, required=artifact.input_schema.required),
        ))
        by_function_name[fn_name] = artifact
    return [types.Tool(function_declarations=declarations)], by_function_name


def _invoke(capability_id: str, args: dict[str, Any], target: str, evidence_dir: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "params": args, "target": target,
        "headed": _SETTINGS["headed"], "slow_mo": _SETTINGS["slow_mo"],
    }
    if evidence_dir:
        # Lets the caller know the run's id *before* it starts (see chat_send) -- e.g. to look
        # it up in the session registry while still in flight, the same id run_replay() would
        # otherwise generate internally.
        body["evidence_dir"] = evidence_dir
    # Test-only escape hatch: points a run at an in-process test target instance instead of the
    # profile's real base_url, the same override InvokeRequest.base_url already exists for.
    override = os.environ.get("CAPABILITY_TARGET_BASE_URL_OVERRIDE")
    if override:
        body["base_url"] = override
    # Generous, not tuned for page-load UX: this call now always runs on a background thread
    # (see _run_capability_async), so a slow human approval no longer blocks anyone's browser --
    # _display_history() reflects the live paused state independently, from the session registry.
    resp = httpx.post(f"{_self_base_url()}/capabilities/{capability_id}/invoke", json=body, timeout=600.0)
    resp.raise_for_status()
    return resp.json()


def _render_result(capability_id: str, result: dict[str, Any]) -> str:
    """Plain-language rendering of a structured invoke response -- a template, not a second LLM
    call, per the brief's "keep it thin" instruction. Always surfaces the real structured
    values (confirmation numbers, balances, the actual error), never just a vague "done".

    ASSIGNMENT_ORIGINAL.md 3.3 asks the chatbot to "report the escalation in plain language" as
    a distinct outcome, not just success/business_outcome/error -- a run a human had to approve
    via the operator console reads identically to an unescalated run unless called out here."""
    status = result["status"]
    outputs = result.get("outputs") or {}
    details = ", ".join(f"{k}: {v}" for k, v in outputs.items() if v is not None)
    prefix = "(needed a human to approve on the operator console first) " if result.get("escalated") else ""

    if status == "success":
        return f"{prefix}Done -- {capability_id} completed successfully. {details}".rstrip()
    if status == "business_outcome":
        return f"{prefix}{capability_id} came back as “{result['business_outcome']}”, not a system error. {details}".rstrip()

    err = result.get("error") or {}
    run_id = Path(result["evidence_dir"]).name
    # By the time a response comes back at all, any pause already got resolved (see chat_send's
    # TimeoutException handling below for the *still paused* case) -- so this is a definitive
    # outcome, not "it's paused right now"; escalated just means a human already tried and it
    # still didn't clear.
    if result.get("escalated"):
        return (
            f"{capability_id} was escalated to a human on the operator console (run {run_id}) but still "
            f"could not complete: {err.get('message', 'unknown error')}."
        )
    return f"{capability_id} could not complete: {err.get('message', 'unknown error')}. Run: {run_id}."


def _display_history() -> list[dict[str, str]]:
    """What the page actually renders -- a still-in-flight entry's text is computed fresh here
    from the same session registry the operator console reads (escalation/registry.py), not a
    second, hand-maintained "is it paused" tracker that could drift from the truth. This is what
    lets the chat page show "paused, needs a human" *while it's actually paused*, instead of the
    page just sitting there loading with no explanation until the whole run finishes."""
    display = []
    for entry in _HISTORY:
        if entry.get("done", True):
            display.append({"role": entry["role"], "text": entry["text"], "pending": False})
            continue
        session = get_session(entry["run_id"])
        if session is not None and session.snapshot()["mode"] == SessionMode.PAUSED.value:
            reason = session.snapshot()["pause_reason"] or "needs a human"
            text = f"{entry['capability_id']} is paused -- needs a human to approve on the operator console. {reason}"
        else:
            text = entry["text"]  # not registered yet (still logging in) or between finish and done=True
        display.append({"role": entry["role"], "text": text, "pending": True})
    return display


def _run_capability_async(entry: dict[str, Any], capability_id: str, target: str, args: dict[str, Any], evidence_dir: str) -> None:
    """Runs on a background thread, spawned by chat_send -- lets the request return immediately
    instead of blocking the page for however long a pause takes to get approved. `entry` is the
    same dict object already appended to _HISTORY (not a copy), mutated in place once the real
    outcome is known; _display_history() shows its live "paused" state in the meantime by
    looking the run up in the session registry via entry['run_id'], not by polling this thread."""
    try:
        result = _invoke(capability_id, args, target, evidence_dir=evidence_dir)
        entry["text"] = _render_result(capability_id, result)
    except httpx.TimeoutException:
        entry["text"] = (
            f"{capability_id} hasn't responded in several minutes -- it may still be in progress. "
            f"Check the operator console or evidence/{Path(evidence_dir).name}/ directly."
        )
    except httpx.HTTPError as exc:
        entry["text"] = f"Couldn't reach the capability API: {exc}"
    finally:
        entry["done"] = True


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    pending = bool(_HISTORY) and not _HISTORY[-1].get("done", True)
    return templates.TemplateResponse(request, "chat.html", {
        "history": _display_history(), "settings": _SETTINGS,
        "operator_console_url": _operator_console_url(), "pending": pending,
    })


@router.get("/chat/status")
def chat_status() -> dict[str, bool]:
    """Polled by the page's own auto-refresh script (see templates/chat.html) so it can reload
    while a run is in flight without the user ever having to refresh manually -- same pattern
    the operator console's own status endpoint already uses."""
    return {"pending": bool(_HISTORY) and not _HISTORY[-1].get("done", True)}


@router.post("/chat")
def chat_send(message: str = Form(...), headed: bool = Form(False), slow_mo: int = Form(0)):
    """Plain `def`, not `async def` -- the Gemini call below is blocking; FastAPI runs a sync
    handler in a thread pool automatically, so this doesn't stall other requests.

    The capability invoke itself does NOT happen here -- it's handed off to a background thread
    (_run_capability_async) so this handler returns as soon as Gemini has picked a capability,
    instead of blocking the whole page load for however long a pause takes to get approved.
    Found the previous blocking version genuinely confusing to watch: the page just looked
    frozen with no explanation for up to two minutes if something paused -- _display_history()
    now shows the live "paused, needs a human" state while chat_page keeps returning immediately.

    `headed`/`slow_mo` come straight from the page's own toggle + speed select -- a fast headless
    replay is correct for production but too fast to actually watch, even with a visible browser
    window (found via a real demo-rehearsal complaint, not guessed at)."""
    _SETTINGS["headed"] = headed
    _SETTINGS["slow_mo"] = slow_mo
    _HISTORY.append({"role": "user", "text": message, "done": True})

    client = GeminiClient()
    tools, by_function_name = _build_tools()
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=message)])]
    response = client.generate(contents, tools=tools, system_instruction=SYSTEM_INSTRUCTION, tool_choice="AUTO")
    call_part = next((p for p in response.candidates[0].content.parts if p.function_call), None)

    if call_part is None:
        text_reply = "".join(p.text or "" for p in response.candidates[0].content.parts)
        _HISTORY.append({"role": "assistant", "text": text_reply or "I'm not sure how to help with that.", "done": True})
        return RedirectResponse("/chat", status_code=303)

    fn = call_part.function_call
    artifact = by_function_name.get(fn.name)
    if artifact is None:
        _HISTORY.append({"role": "assistant", "text": f"Model picked an unrecognized tool ('{fn.name}').", "done": True})
        return RedirectResponse("/chat", status_code=303)

    # Computed here, not left to run_replay's own default, so the run's id (== its operator
    # console session id) is known before it starts -- same reasoning as cli.py's cmd_replay.
    run_id = runtime.run_id("replay_run")
    evidence_dir = str(runtime.EVIDENCE_ROOT / run_id)
    entry: dict[str, Any] = {
        "role": "assistant", "text": f"Working on {artifact.capability_id}...",
        "run_id": run_id, "capability_id": artifact.capability_id, "done": False,
    }
    _HISTORY.append(entry)
    threading.Thread(
        target=_run_capability_async,
        args=(entry, artifact.capability_id, artifact.target.app_id, dict(fn.args), evidence_dir),
        daemon=True,
    ).start()
    return RedirectResponse("/chat", status_code=303)
