"""Chat over /v1: a conversational way into the same runs the catalog starts.

It is a supplementary front door, and it is deliberately the least trusted one: the model only picks a capability and proposes arguments;
every argument is checked against the capability's schema and refused if it looks invented; and the run it starts is an ordinary run, made
under the caller's own key, with the same validation, caps, idempotency and approvals as one started from a form. The conversation is stored per
user (so it survives navigating around the console and a restart) and can be cleared."""
from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from google.genai import types
from pydantic import BaseModel, Field

import observability
import runtime
from api.v1 import Principal, adir, executor, require, run_view
from artifacts_lib import storage

router = APIRouter(prefix="/v1/chat", tags=["chat"])

_JSON_TO_GEMINI_TYPE = {"string": "STRING", "number": "NUMBER", "integer": "INTEGER", "boolean": "BOOLEAN"}
# What a model writes when it has no value and fills the field anyway. Found live: asked to change only a phone number, it filled email and address with "unknown".
_PLACEHOLDERS = {"unknown", "n/a", "na", "none", "null", "tbd", "not provided", "not specified", "unspecified", "?", "-", "--", "placeholder", "example", "test"}


def _function_name(capability_id: str) -> str:
    """Gemini function names can't contain '.', which every capability_id has."""
    return capability_id.replace(".", "__")


def _json_type_to_gemini(prop_type: Any) -> str:
    if isinstance(prop_type, list):
        prop_type = next((t for t in prop_type if t != "null"), "string")
    return _JSON_TO_GEMINI_TYPE.get(prop_type, "STRING")


def _check_arguments(artifact: Any, args: dict[str, Any], message: str) -> str | None:
    """Returns a plain-language question when the model's arguments cannot be trusted, else None: required values missing, placeholder words standing in for
    values, or anything the capability's own input schema rejects."""
    from replay.validation import validate_input

    missing = [f for f in artifact.input_schema.required if f not in args or args[f] in (None, "")]
    invented = [k for k, v in args.items() if isinstance(v, str) and v.strip().lower() in _PLACEHOLDERS]
    if missing or invented:
        needed = missing + invented
        return (f"To {artifact.name.lower()} I still need: {', '.join(needed)}. "
                "Please include the exact value(s) in your message; I won't guess at them.")
    problems = validate_input(artifact.input_schema, args)
    if problems:
        return f"I can't run {artifact.name.lower()} with that: {'; '.join(problems)}."
    return None

SYSTEM_INSTRUCTION = (
    "You are the assistant inside an operations console for back-office systems (a clinic's front desk and billing). "
    "Each tool is one recorded, reviewed task. Call exactly one tool when the user asks for something a tool does, using only values the user "
    "actually gave (or that follow unambiguously from the conversation). If a required value is missing, do NOT call a tool: ask for exactly what "
    "is missing. Write each value in the format its tool asks for: converting what the user wrote is not guessing (4PM becomes 16:00; a date written with slashes "
    "is month/day/year, so 03/04/2026 becomes 2026-03-04). Never invent, default or guess a value; never write words like 'unknown' or 'n/a' as a value. If nothing matches the request, say so "
    "plainly. For updates, include only the fields the user wants to change. Tasks that commit something wait for a person's approval, so do not "
    "tell the user they are done. Keep replies short."
)
HISTORY_TURNS = 8


def _today() -> str:
    """Without it the model answers "tomorrow" from its own idea of the date. Found live: it wrote a date a year in the past, which fits the pattern, so
    nothing refused it."""
    now = datetime.now()
    return f"Today is {now:%A}, {now:%Y-%m-%d}. Work out words like 'tomorrow' or 'next Tuesday' from that."


_DATE_PATTERN, _TIME_PATTERN = "^[0-9]{4}-[0-9]{2}-[0-9]{2}$", "^[0-9]{2}:[0-9]{2}$"
_RELATIVE = re.compile(r"\b(today|tomorrow|tonight|yesterday|monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|weekend|next|this)\b")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_DATES_AND_IDS = re.compile(r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Za-z]+-\d+")


def _says(number: int, text: str) -> bool:
    return re.search(rf"(?<!\d)0?{number}(?!\d)", text) is not None or any(w for w, n in _NUMBER_WORDS.items() if n == number and re.search(rf"\b{w}\b", text))


def _supported_by(value: str, pattern: str | None, text: str) -> bool:
    """A date or time the model wrote must come from words the user wrote. Found live: asked to move an appointment 'to 4PM', the model supplied tomorrow's
    date itself, and for 'to 03/12/2026' it supplied 12:00. Nothing refused either, because both are valid. Anything else is left to the schema."""
    text = text.lower()
    if pattern == _DATE_PATTERN:
        year, month, day = (int(p) for p in value.split("-"))
        return value in text or _RELATIVE.search(text) is not None or (_says(day, text) and (_says(month, text) or _MONTHS[month - 1] in text))
    if pattern == _TIME_PATTERN:
        hour, minute = (int(p) for p in value.split(":"))
        if (hour, minute) == (12, 0) and re.search(r"\b(noon|midday)\b", text) or (hour, minute) == (0, 0) and "midnight" in text:
            return True
        text = _DATES_AND_IDS.sub(" ", text)  # the 12 in 03/12/2026 is a day, and the 20 in A-20002 is part of a number
        return any(_says(h, text) for h in {hour, hour % 12 or 12}) and (minute == 0 or _says(minute, text))
    return True


def _drop_unsupported(artifact: Any, args: dict[str, Any], said: str) -> dict[str, Any]:
    props = artifact.input_schema.properties
    return {k: v for k, v in args.items() if not isinstance(v, str) or _supported_by(v, props.get(k, {}).get("pattern"), said)}


LOOKUP_WAIT_S = 90  # a sleeping target can take most of this to wake up
_UNFINISHED = ("queued", "running")


def _lookup_candidates(artifact: Any, missing: list[str], by_name: dict[str, Any]) -> dict[str, Any]:
    """Read-only tasks that can return every value the user left out."""
    return {n: a for n, a in by_name.items() if a is not artifact and a.safety.risk_level.value == "read_only"
            and set(missing) <= set(a.output_schema.properties)}


def _fill_from_lookup(who: Principal, contents: list[types.Content], tools: list[types.Tool], by_name: dict[str, Any], artifact: Any,
                      args: dict[str, Any], missing: list[str], target: str) -> tuple[dict[str, Any] | None, str | None]:
    """The user changed only part of something (a new time, no date) and a read-only task can say what the rest is now: run that task, and let the model
    complete the original call from what it found. Returns (the completed arguments, a line saying what was looked up) or (None, why not). Only the read-only
    task runs here; the call that changes something still goes through the checks and approvals every other call does."""
    candidates = _lookup_candidates(artifact, missing, by_name)
    if not candidates:
        return None, None
    declarations = [d for d in tools[0].function_declarations if d.name in candidates]
    hint = (f"{SYSTEM_INSTRUCTION} {_today()} The user did not give: {', '.join(missing)}. Call the lookup tool that can find them, using only what the user already gave.")
    first = get_client().generate(contents, tools=[types.Tool(function_declarations=declarations)], system_instruction=hint, tool_choice="ANY")
    lookup_call = next((p.function_call for p in first.candidates[0].content.parts if p.function_call), None)
    lookup = candidates.get(lookup_call.name) if lookup_call else None
    if lookup is None:
        return None, None
    lookup_args = {k: v for k, v in dict(lookup_call.args).items() if v not in (None, "")}
    if _check_arguments(lookup, lookup_args, ""):
        return None, None
    prepared = runtime.prepare_run(lookup.capability_id, lookup_args, target=target, requested_by=who.name, idempotency_key=f"chat-{uuid.uuid4()}",
                                   enable_operator_console=False, queued=True, artifacts_dir=adir(), pause_for_human=False)
    if isinstance(prepared, runtime.Early):
        return None, f"I tried to {lookup.name.lower()} to fill in the {', '.join(missing)}, but it didn't start."
    executor().submit(prepared)
    store = runtime.default_store()
    deadline = time.time() + LOOKUP_WAIT_S
    row = store.get(prepared.run_id)
    while row and row["status"] in _UNFINISHED and time.time() < deadline:
        time.sleep(0.3)
        row = store.get(prepared.run_id)
    result = store.stored_result(row) if row and row["status"] not in _UNFINISHED else None
    if result is None or row["status"] != "success":
        return None, f"I tried to {lookup.name.lower()} to fill in the {', '.join(missing)}, but it didn't work. Please give me the exact value(s)."
    found = {k: v for k, v in (result.outputs or {}).items() if k != "status"}
    # the model's own turn goes back as it came (Gemini rejects a function call rebuilt without its thought signature)
    context = contents + [first.candidates[0].content,
                          types.Content(role="user", parts=[types.Part.from_function_response(name=lookup_call.name, response={"found": found})])]
    complete = (f"{SYSTEM_INSTRUCTION} {_today()} The lookup is done. Now make the user's original request: change only what they asked for and take "
                f"every other value from what the lookup found.")
    second = get_client().generate(context, tools=tools, system_instruction=complete, tool_choice="AUTO")
    call = next((p.function_call for p in second.candidates[0].content.parts if p.function_call), None)
    if call is None or _function_name(artifact.capability_id) != call.name:
        return None, None
    return {k: v for k, v in dict(call.args).items() if v not in (None, "")}, f"I looked up {', '.join(map(str, lookup_args.values()))} to fill in the {', '.join(missing)}."


def get_client() -> Any:
    """The model client. A function so tests can replace it."""
    from agent.gemini_client import GeminiClient
    return GeminiClient()


def _describe(prop: dict[str, Any]) -> str | None:
    """The description plus the exact format the capability will accept. Found live: with only the pattern in the schema the model wrote '4PM' and
    '03/04/2026' for a time and date the task takes as '16:00' and '2026-03-04', and the run was refused."""
    note = f"Must match the pattern {prop['pattern']}" if prop.get("pattern") else None
    return " ".join(x for x in (prop.get("description"), note, "Leave this out unless the user gave it.") if x)


def build_tools() -> tuple[list[types.Tool], dict[str, Any]]:
    declarations, by_name = [], {}
    for artifact in storage.list_artifacts(adir()):
        if artifact.preconditions is None:
            continue  # sign-on is plumbing
        props = {n: types.Schema(type=_json_type_to_gemini(p.get("type")), description=_describe(p), pattern=p.get("pattern"))
                 for n, p in artifact.input_schema.properties.items()}
        note = " Changes data." if artifact.safety.risk_level.value == "state_changing" else " Read-only."
        declarations.append(types.FunctionDeclaration(name=_function_name(artifact.capability_id), description=f"{artifact.description}{note} (system: {artifact.target.app_id})",
                                                      parameters=types.Schema(type="OBJECT", properties=props)))  # nothing marked required: told to supply a value, the model invents one (below)
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
        response = get_client().generate(contents, tools=tools, system_instruction=f"{SYSTEM_INSTRUCTION} {_today()}", tool_choice="AUTO")
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
    said = " ".join([m["text"] for m in prior if m["role"] == "user"] + [body.message])
    args = _drop_unsupported(artifact, args, said)
    target = default_target(artifact.target.app_id)
    if target is None:
        return {"messages": [_view(user), _view(store.chat_add(who.name, "assistant", f"No sign-in is configured for {artifact.target.app_id}."))]}
    looked_up = None
    missing = [f for f in artifact.input_schema.required if f not in args]
    if missing and args and not any(isinstance(v, str) and v.strip().lower() in _PLACEHOLDERS for v in args.values()):
        filled, looked_up = _fill_from_lookup(who, contents, tools, by_name, artifact, args, missing, target)
        if filled:
            args = filled
        elif looked_up:
            return {"messages": [_view(user), _view(store.chat_add(who.name, "assistant", looked_up))]}
    problem = _check_arguments(artifact, args, body.message)
    if problem:
        return {"messages": [_view(user), _view(store.chat_add(who.name, "assistant", problem))]}

    prepared = runtime.prepare_run(artifact.capability_id, args, target=target, requested_by=who.name, idempotency_key=f"chat-{uuid.uuid4()}",
                                   enable_operator_console=False, queued=True, artifacts_dir=adir(),
                                   pace_ms=body.pace_ms, show_window=body.show_window, pause_for_human=runtime.default_policy().escalation.enabled)
    if isinstance(prepared, runtime.Early):
        result = prepared.result
        text = (f"I couldn't start {artifact.name.lower()}: {result.error.message}" if result.error else f"{artifact.name} didn't start ({result.status.value}).")
        return {"messages": [_view(user), _view(store.chat_add(who.name, "assistant", text, result.run_id))]}
    executor().submit(prepared)
    shown = ", ".join(f"{k}: {v}" for k, v in args.items())
    text = (f"{looked_up} " if looked_up else "") + f"{artifact.name}" + (f" ({shown})" if shown else "")
    observability.log("chat.run_started", run_id=prepared.run_id, capability_id=artifact.capability_id, by=who.name)
    return {"messages": [_view(user), _view(store.chat_add(who.name, "assistant", text, prepared.run_id))]}
