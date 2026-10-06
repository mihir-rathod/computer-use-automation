"""Turns a plain-words request into a draft contract, so a person does not have to fill in a form to start discovery.

One model call, forced to answer through a single tool so the reply is structured. The draft is only a proposal: it is shown to the person, who confirms or edits it,
and what the safety rules depend on (read-only or not) is theirs to confirm. The model never sees a password; sign-on is the target's own login capability."""
from __future__ import annotations

import re
from typing import Any

from google.genai import types
from pydantic import ValidationError

from teach.contract import TeachContract, Verify

_STR = types.Schema(type="STRING")
PROPOSE = types.FunctionDeclaration(
    name="propose_task",
    description="Propose the task described by the person, as a reusable automation.",
    parameters=types.Schema(type="OBJECT", properties={
        "name": types.Schema(type="STRING", description="Short title, for example 'Look up an appointment'."),
        "task_name": types.Schema(type="STRING", description="lowercase_snake_case id, for example appointment_details."),
        "description": types.Schema(type="STRING", description="One sentence on what the task is for."),
        "goal": types.Schema(type="STRING", description="Plain steps for an operator who has never seen the system, starting from its main page. Refer to inputs by name and say what to read at the end."),
        "inputs": types.Schema(type="ARRAY", items=types.Schema(type="OBJECT", properties={
            "name": types.Schema(type="STRING", description="snake_case"), "example": types.Schema(type="STRING", description="The value the person gave in the request."),
            "description": _STR}, required=["name", "example"])),
        "outputs": types.Schema(type="ARRAY", items=types.Schema(type="OBJECT", properties={"name": types.Schema(type="STRING", description="snake_case"), "description": _STR}, required=["name"])),
        "effect": types.Schema(type="STRING", enum=["read_only", "changes_data", "irreversible"],
                               description="read_only unless the request clearly asks to change, send, cancel, refund or submit something. irreversible if it cannot be undone from the screen."),
        "question": types.Schema(type="STRING", description="Only if the request lacks an example value for an input, or is too vague to act on: the one question to ask the person. Leave everything else empty then."),
    }, required=["name"]),
)
SYSTEM = (
    "You turn a request from an operator into a reusable automation definition for a web application. The operator will be shown your proposal and can correct it. "
    "Use the values they gave as the examples. Never invent an example value: if one is missing, ask with `question`. Never propose a password, secret or token as an input. "
    "Pick read_only unless the request clearly changes something. Keep the goal short and concrete, in the order a person would do it."
)


def draft_contract(model: Any, request: str, target: str, profile: dict[str, Any], menu: list[str] | None = None) -> dict[str, Any]:
    """Returns {"contract": {...}} or {"question": "..."}. Raises ValueError with something a person can act on if the draft is unusable."""
    prompt = (f"The application is '{profile['app_id']}' ({target}). After signing on, the operator lands on its main page ({profile.get('home_path', '/')}).\n"
              + (f"The main page offers exactly these links and buttons: {', '.join(menu)}.\nWrite the goal using those names and only those; do not invent pages, searches or menus that are not listed.\n" if menu else "")
              + f"Request:\n{request.strip()}")
    response = model.generate([types.Content(role="user", parts=[types.Part.from_text(text=prompt)])], tools=[types.Tool(function_declarations=[PROPOSE])], system_instruction=SYSTEM)
    part = next((p for p in response.candidates[0].content.parts if p.function_call), None)
    if part is None:
        raise ValueError("the model did not return a proposal; try rephrasing, or fill the details in yourself")
    args = _plain(dict(part.function_call.args))
    if args.get("question"):
        return {"question": str(args["question"])}
    effect = args.get("effect") if args.get("effect") in ("read_only", "changes_data", "irreversible") else "irreversible"  # an unreadable answer is treated as the cautious one
    task_name = re.sub(r"[^a-z0-9]+", "_", str(args.get("task_name") or args.get("name") or "").lower()).strip("_")
    task_name = re.sub(r"^[0-9]+", "", task_name)[:40]
    try:
        contract = TeachContract.model_validate({
            "task_name": task_name, "name": args.get("name"), "description": args.get("description") or args.get("name"), "target": target,
            "goal": args.get("goal") or "", "inputs": [{"name": i.get("name"), "example": i.get("example"), "description": i.get("description") or None} for i in args.get("inputs") or []],
            "outputs": [{"name": o.get("name"), "description": o.get("description") or None} for o in args.get("outputs") or []],
            "effect": effect, "commit_approval": "supervisor",
            "verify": Verify(same_as_example=True).model_dump() if effect == "read_only" else None,
        })
    except ValidationError as exc:
        why = "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
        raise ValueError(f"the draft was not usable ({why}). Say a bit more about what to read or type, or fill the details in yourself") from None
    return {"contract": contract.model_dump(mode="json")}


def _plain(value: Any) -> Any:
    """Function-call arguments arrive as proto-ish containers; make them plain dicts and lists."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value
