"""Chat over /v1: a conversational way into the same runs the catalog starts.

It is a supplementary front door, and it is deliberately the least trusted one: the model only picks a capability and proposes arguments;
every argument is checked against the capability's schema and refused if it looks invented; and the run it starts is an ordinary run, made
under the caller's own key, with the same validation, caps, idempotency and approvals as one started from a form. The conversation is stored per
user (so it survives navigating around the console and a restart) and can be cleared."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from google.genai import types
from pydantic import BaseModel, Field

import observability
import runtime
from api.chatbot import _check_arguments, _function_name, _json_type_to_gemini
from api.v1 import Principal, adir, executor, require, run_view
from artifacts_lib import storage

router = APIRouter(prefix="/v1/chat", tags=["chat"])

SYSTEM_INSTRUCTION = (
    "You are the assistant inside an operations console for back-office systems (a credit-union core and a clinic's front desk and billing). "
    "Each tool is one recorded, reviewed task. Call exactly one tool when the user asks for something a tool does, using only values the user "
    "actually gave (or that follow unambiguously from the conversation). If a required value is missing, do NOT call a tool: ask for exactly what "
    "is missing. Never invent, default or guess a value; never write words like 'unknown' or 'n/a' as a value. If nothing matches the request, say so "
    "plainly. For updates, include only the fields the user wants to change. Tasks that commit something wait for a person's approval, so do not "
    "tell the user they are done. Keep replies short."
)
HISTORY_TURNS = 8


def get_client() -> Any:
    """The model client. A function so tests can replace it."""
    from agent.gemini_client import GeminiClient
    return GeminiClient()


def build_tools() -> tuple[list[types.Tool], dict[str, Any]]:
    declarations, by_name = [], {}
    hidden = runtime.hidden_apps()
    for artifact in storage.list_artifacts(adir()):
        if artifact.preconditions is None or artifact.target.app_id in hidden:
            continue  # sign-on is plumbing, and a hidden system is not offered in the console
        props = {n: types.Schema(type=_json_type_to_gemini(p.get("type")), description=p.get("description")) for n, p in artifact.input_schema.properties.items()}
        note = " Changes data." if artifact.safety.risk_level.value == "state_changing" else " Read-only."
        declarations.append(types.FunctionDeclaration(name=_function_name(artifact.capability_id), description=f"{artifact.description}{note} (system: {artifact.target.app_id})",
                                                      parameters=types.Schema(type="OBJECT", properties=props, required=artifact.input_schema.required)))
        by_name[_function_name(artifact.capability_id)] = artifact
    return [types.Tool(function_declarations=declarations)], by_name


def default_target(app: str) -> str | None:
    return app if app in runtime.TARGET_PROFILES else None


class Say(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    pace_ms: int = Field(default=0, ge=0, le=3000, description="Watch mode for the run this message starts.")
    show_window: bool = False


def _view(m: dict[str, Any]) -> dict[str, Any]:
    out = {"id": m["id"], "role": m["role"], "text": m["text"], "run_id": m["run_id"], "created_at": m["created_at"]}
    return out


@router.get("")
def history(who: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    return {"messages": [_view(m) for m in runtime.default_store().chat_list(who.name)]}


@router.delete("")
def clear(who: Principal = Depends(require("viewer"))) -> dict[str, Any]:
    return {"cleared": runtime.default_store().chat_clear(who.name)}


@router.post("")
def say(body: Say, who: Principal = Depends(require("operator"))) -> dict[str, Any]:
    from api.v1 import check_watch
    check_watch(body.pace_ms, body.show_window)
    store = runtime.default_store()
    prior = store.chat_list(who.name, HISTORY_TURNS)
    user = store.chat_add(who.name, "user", body.message)
    contents = [types.Content(role="user" if m["role"] == "user" else "model", parts=[types.Part.from_text(text=m["text"])]) for m in prior]
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=body.message)]))
    tools, by_name = build_tools()
    try:
        response = get_client().generate(contents, tools=tools, system_instruction=SYSTEM_INSTRUCTION, tool_choice="AUTO")
    except Exception as exc:  # noqa: BLE001 -- a model outage must not look like a platform outage
        observability.log("chat.model_error", logging.WARNING, error_type=type(exc).__name__)
        reply = store.chat_add(who.name, "assistant", "I couldn't reach the language model just now. You can still run any task from the catalog.")
        return {"messages": [_view(user), _view(reply)]}

    parts = response.candidates[0].content.parts
    call = next((p.function_call for p in parts if p.function_call), None)
    if call is None:
        text = "".join(p.text or "" for p in parts).strip() or "I'm not sure how to help with that. Try the catalog for the full list of tasks."
        return {"messages": [_view(user), _view(store.chat_add(who.name, "assistant", text))]}

    artifact = by_name.get(call.name)
    if artifact is None:
        return {"messages": [_view(user), _view(store.chat_add(who.name, "assistant", "I picked a task that doesn't exist, so I did nothing."))]}
    args = {k: v for k, v in dict(call.args).items() if v not in (None, "")}
    problem = _check_arguments(artifact, args, body.message)
    if problem:
        return {"messages": [_view(user), _view(store.chat_add(who.name, "assistant", problem))]}
    target = default_target(artifact.target.app_id)
    if target is None:
        return {"messages": [_view(user), _view(store.chat_add(who.name, "assistant", f"No sign-in is configured for {artifact.target.app_id}."))]}

    prepared = runtime.prepare_run(artifact.capability_id, args, target=target, requested_by=who.name, idempotency_key=f"chat-{uuid.uuid4()}",
                                   enable_operator_console=False, queued=True, artifacts_dir=adir(),
                                   pace_ms=body.pace_ms, show_window=body.show_window)
    if isinstance(prepared, runtime.Early):
        result = prepared.result
        text = (f"I couldn't start {artifact.name.lower()}: {result.error.message}" if result.error else f"{artifact.name} didn't start ({result.status.value}).")
        return {"messages": [_view(user), _view(store.chat_add(who.name, "assistant", text, result.run_id))]}
    executor().submit(prepared)
    shown = ", ".join(f"{k}: {v}" for k, v in args.items())
    text = f"{artifact.name}" + (f" ({shown})" if shown else "")
    observability.log("chat.run_started", run_id=prepared.run_id, capability_id=artifact.capability_id, by=who.name)
    return {"messages": [_view(user), _view(store.chat_add(who.name, "assistant", text, prepared.run_id))]}
