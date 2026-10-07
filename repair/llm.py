"""Optional model-assisted repair, used only when the heuristic cannot pick confidently and a person has asked for
it (`repair_llm=True`). It is the one place a model is allowed near replay, and it is fenced in: it sees only the
candidate elements the heuristic already found on the live page, it can only choose one of them (or none), and the
result is still just a proposal that waits for a human approval. Replay itself never imports this module."""
from __future__ import annotations

from typing import Any

from artifacts_lib.schema import Step
from repair.propose import Candidate


def make_llm_picker(client: Any):
    from google.genai import types

    choose = types.FunctionDeclaration(
        name="choose",
        description="Pick which candidate is the element the automation step meant, or none if none of them is.",
        parameters=types.Schema(type="OBJECT", properties={"ref": types.Schema(type="STRING", description="candidate ref, or 'none'")}, required=["ref"]),
    )
    tools = [types.Tool(function_declarations=[choose])]

    def pick(step: Step, candidates: list[Candidate]) -> str | None:
        assert step.target is not None
        listing = "\n".join(f"- {c.ref}: {c.role} name={c.name!r} field={c.html_name!r}" for c in candidates)
        prompt = (
            f"An automation recorded this step on a web page: {step.action.value} on \"{step.target.semantic_description}\". "
            f"Recorded facts about the element: {step.target.hints}. The page has since changed and none of its locators match.\n"
            f"Candidate elements on the page now:\n{listing}\nWhich candidate is the same element? Answer 'none' if you cannot tell."
        )
        response = client.generate([types.Content(role="user", parts=[types.Part.from_text(text=prompt)])], tools=tools)
        part = next((p for p in response.candidates[0].content.parts if p.function_call), None)
        ref = dict(part.function_call.args).get("ref") if part else None
        return ref if ref and ref != "none" else None

    return pick
