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
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from google.genai import types

from agent.gemini_client import GeminiClient
from artifacts_lib.storage import list_artifacts

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# The chatbot calls its own /invoke endpoint over real HTTP, not a direct function call -- it's
# meant to be just another client of the capability API, proving that surface is a complete,
# self-sufficient front door on its own, not something only the CLI can drive. Resolved per-call
# (not a module-level constant) so tests can point this at an in-process test server via the env
# var; the default matches this app's own documented demo port.
def _self_base_url() -> str:
    return os.environ.get("CAPABILITY_API_BASE_URL", "http://127.0.0.1:8020")

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
_HISTORY: list[dict[str, str]] = []


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


def _invoke(capability_id: str, args: dict[str, Any], target: str) -> dict[str, Any]:
    body: dict[str, Any] = {"params": args, "target": target}
    # Test-only escape hatch: points a run at an in-process test target instance instead of the
    # profile's real base_url, the same override InvokeRequest.base_url already exists for.
    override = os.environ.get("CAPABILITY_TARGET_BASE_URL_OVERRIDE")
    if override:
        body["base_url"] = override
    resp = httpx.post(f"{_self_base_url()}/capabilities/{capability_id}/invoke", json=body, timeout=120.0)
    resp.raise_for_status()
    return resp.json()


def _render_result(capability_id: str, result: dict[str, Any]) -> str:
    """Plain-language rendering of a structured invoke response -- a template, not a second LLM
    call, per the brief's "keep it thin" instruction. Always surfaces the real structured
    values (confirmation numbers, balances, the actual error), never just a vague "done"."""
    status = result["status"]
    outputs = result.get("outputs") or {}
    details = ", ".join(f"{k}: {v}" for k, v in outputs.items() if v is not None)

    if status == "success":
        return f"Done -- {capability_id} completed successfully. {details}".rstrip()
    if status == "business_outcome":
        return f"{capability_id} came back as “{result['business_outcome']}”, not a system error. {details}".rstrip()

    err = result.get("error") or {}
    run_id = Path(result["evidence_dir"]).name
    return (
        f"{capability_id} could not complete: {err.get('message', 'unknown error')}. "
        f"If this needed a human, it's paused for escalation -- check the operator console for run {run_id}."
    )


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    return templates.TemplateResponse(request, "chat.html", {"history": _HISTORY})


@router.post("/chat")
def chat_send(message: str = Form(...)):
    """Plain `def`, not `async def` -- both the Gemini call and the invoke call below are
    blocking; FastAPI runs a sync handler in a thread pool automatically, so this doesn't stall
    other requests, same reasoning as api/app.py's invoke handler."""
    _HISTORY.append({"role": "user", "text": message})

    client = GeminiClient()
    tools, by_function_name = _build_tools()
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=message)])]
    response = client.generate(contents, tools=tools, system_instruction=SYSTEM_INSTRUCTION, tool_choice="AUTO")
    call_part = next((p for p in response.candidates[0].content.parts if p.function_call), None)

    if call_part is None:
        text_reply = "".join(p.text or "" for p in response.candidates[0].content.parts)
        _HISTORY.append({"role": "assistant", "text": text_reply or "I'm not sure how to help with that."})
    else:
        fn = call_part.function_call
        artifact = by_function_name.get(fn.name)
        if artifact is None:
            _HISTORY.append({"role": "assistant", "text": f"Model picked an unrecognized tool ('{fn.name}')."})
        else:
            try:
                result = _invoke(artifact.capability_id, dict(fn.args), artifact.target.app_id)
                _HISTORY.append({"role": "assistant", "text": _render_result(artifact.capability_id, result)})
            except httpx.HTTPError as exc:
                _HISTORY.append({"role": "assistant", "text": f"Couldn't reach the capability API: {exc}"})

    return RedirectResponse("/chat", status_code=303)
