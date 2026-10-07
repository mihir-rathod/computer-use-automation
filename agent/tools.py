"""Tool/function declarations exposed to the model, and the mapping from a chosen tool call
back to a Surface Action. Mirrors Surface's ActionType set except wait_for/dismiss_dialog --
waiting is implicit (each perceive() call gives the model fresh state to react to), and
dismissing a dialog is just a plain click on whatever the (dialog-scoped, per surface/aria.py)
perceive() shows -- the model doesn't need a separate concept for it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google.genai import types

from artifacts_lib.schema import ActionType
from surface.base import Action

NAVIGATE = types.FunctionDeclaration(
    name="navigate",
    description="Navigate to a URL path within the target application.",
    parameters=types.Schema(
        type="OBJECT",
        properties={"url": types.Schema(type="STRING", description="relative or absolute URL")},
        required=["url"],
    ),
)
CLICK = types.FunctionDeclaration(
    name="click",
    description="Click an interactive element by its ref id from the current element list.",
    parameters=types.Schema(type="OBJECT", properties={"ref": types.Schema(type="STRING")}, required=["ref"]),
)
TYPE_TEXT = types.FunctionDeclaration(
    name="type_text",
    description="Type text into a textbox element by its ref id, replacing any existing value.",
    parameters=types.Schema(
        type="OBJECT",
        properties={"ref": types.Schema(type="STRING"), "text": types.Schema(type="STRING")},
        required=["ref", "text"],
    ),
)
SELECT_OPTION = types.FunctionDeclaration(
    name="select_option",
    description="Select an option in a combobox by its ref id. Use the option's underlying "
                 "value, not its visible label, if the element list shows both.",
    parameters=types.Schema(
        type="OBJECT",
        properties={"ref": types.Schema(type="STRING"), "value": types.Schema(type="STRING")},
        required=["ref", "value"],
    ),
)
EXTRACT = types.FunctionDeclaration(
    name="extract",
    description="Read the text content of an element by its ref id and record it under a "
                 "named output (snake_case, e.g. 'savings_balance').",
    parameters=types.Schema(
        type="OBJECT",
        properties={"ref": types.Schema(type="STRING"), "output_name": types.Schema(type="STRING")},
        required=["ref", "output_name"],
    ),
)
FINISH = types.FunctionDeclaration(
    name="finish",
    description="Call this once the goal has been fully achieved and verified on screen.",
    parameters=types.Schema(type="OBJECT", properties={"reasoning": types.Schema(type="STRING")}, required=["reasoning"]),
)
GIVE_UP = types.FunctionDeclaration(
    name="give_up",
    description="Call this if the goal cannot be achieved -- e.g. no element matches what's "
                 "needed, or the last few actions haven't changed anything.",
    parameters=types.Schema(type="OBJECT", properties={"reasoning": types.Schema(type="STRING")}, required=["reasoning"]),
)

ALL_TOOLS = [types.Tool(function_declarations=[NAVIGATE, CLICK, TYPE_TEXT, SELECT_OPTION, EXTRACT, FINISH, GIVE_UP])]
# For a model that has to find its own way: no typing of addresses, only the links and buttons on the page. A small model given the option guesses URLs.
CLICK_ONLY_TOOLS = [types.Tool(function_declarations=[CLICK, TYPE_TEXT, SELECT_OPTION, EXTRACT, FINISH, GIVE_UP])]

_ACTION_TOOL_KINDS = {
    "navigate": ActionType.NAVIGATE,
    "click": ActionType.CLICK,
    "type_text": ActionType.TYPE,
    "select_option": ActionType.SELECT,
    "extract": ActionType.EXTRACT,
}
TERMINAL_TOOLS = {"finish", "give_up"}


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


def is_terminal(tool_call: ToolCall) -> bool:
    return tool_call.name in TERMINAL_TOOLS


def to_action(tool_call: ToolCall) -> Action:
    kind = _ACTION_TOOL_KINDS[tool_call.name]
    ref = tool_call.args.get("ref")
    if kind == ActionType.NAVIGATE:
        return Action(kind=kind, params={"url": tool_call.args["url"]})
    if kind == ActionType.TYPE:
        return Action(kind=kind, ref=ref, params={"text": tool_call.args["text"]})
    if kind == ActionType.SELECT:
        return Action(kind=kind, ref=ref, params={"value": tool_call.args["value"]})
    return Action(kind=kind, ref=ref, params={})
