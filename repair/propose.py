"""Locator repair proposals.

When a replay fails because no locator in a step's chain matches anything on the page (a vendor release renamed a
button, an id, a form field), this module looks at the live page where the step failed and proposes which element
the step most likely meant. It is a *proposal*: it changes nothing until a person with the right tier approves it
(repair/apply.py), and the replay engine never calls it.

How candidates are scored, with no model involved: among elements of the same role, compare the recorded hints
(name, HTML field name, neighbouring labels, position among same-role elements) with each candidate. A candidate is
only offered when it clearly beats the runner-up; otherwise the proposal is "no confident match", which is the honest
answer when the page has changed too much to tell. An LLM ranker can be plugged in for that case (repair/llm.py);
it only ever picks among the same real candidates and its pick is still a proposal.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any, Literal

from pydantic import BaseModel, Field

from artifacts_lib.schema import Artifact, Locator, Step, Target
from surface.base import ObservedElement, ObservedState
from surface.web import WebSurface

MIN_SCORE = 0.5
MIN_MARGIN = 0.15
_ROLE_RE = re.compile(r"^(?P<role>[a-zA-Z]+)(?:\[name='(?P<name>[^']*)'\])?$")


class Candidate(BaseModel):
    ref: str
    role: str
    name: str | None = None
    html_name: str | None = None
    score: float
    why: dict[str, float] = Field(default_factory=dict)


class RepairProposal(BaseModel):
    id: str
    capability_id: str
    base_version: str
    step_id: str
    run_id: str | None = None
    method: Literal["heuristic", "llm"] = "heuristic"
    confident: bool
    reason: str
    old_target: Target
    new_target: Target | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    screenshot: str | None = None
    page_url: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    touches_irreversible_step: bool = False


def _sim(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _old_role_and_names(step: Step) -> tuple[str | None, list[str]]:
    target = step.target
    assert target is not None
    role = target.hints.get("role")
    names = [n for n in (target.hints.get("name"), target.semantic_description) if n]
    for loc in target.locators:
        match = _ROLE_RE.match(loc.value) if loc.strategy.value == "role" else None
        if match:
            role = role or match["role"]
            if match["name"]:
                names.append(match["name"])
    return role, names


def _neighbours(elements: list[ObservedElement], element: ObservedElement) -> set[str]:
    at = elements.index(element)
    near = [e.name for e in elements[max(0, at - 3):at] if e.name][-2:] + [e.name for e in elements[at + 1:at + 4] if e.name][:2]
    return {n.lower() for n in near}


def score_candidates(step: Step, state: ObservedState) -> list[Candidate]:
    """Position among same-role elements is the base signal (a button that was the second button is still the second
    button after a relabel), and evidence that the element itself is recognisable adds to it: a similar name, the same
    HTML field name, the same neighbouring labels. Signals that *disagree* are ignored rather than counted against a
    candidate, because a rename is exactly why the locators broke; what separates candidates is who has evidence."""
    role, names = _old_role_and_names(step)
    hints = step.target.hints  # type: ignore[union-attr]
    same_role = [e for e in state.elements if role is None or e.role == role]
    old_neighbours = {n.lower() for n in [*hints.get("before", []), *hints.get("after", [])]}
    out = []
    for position, el in enumerate(same_role):
        parts: dict[str, float] = {}
        if "ordinal" in hints and position == hints["ordinal"]:
            parts["ordinal"] = 0.5 if hints.get("same_role_count") == len(same_role) else 0.2
        name = max((_sim(el.name, n) for n in names), default=0.0)
        if name >= 0.5:
            parts["name"] = 0.5 * name
        html = _sim(el.html_name, hints.get("html_name")) if hints.get("html_name") else 0.0
        if html >= 0.8:
            parts["html_name"] = 0.15 * html
        near = _neighbours(state.elements, el)
        if near & old_neighbours:
            parts["neighbours"] = 0.2 * len(near & old_neighbours) / len(near | old_neighbours)
        score = min(1.0, sum(parts.values()))
        out.append(Candidate(ref=el.ref, role=el.role, name=el.name, html_name=el.html_name, score=round(score, 3), why={k: round(v, 3) for k, v in parts.items()}))
    return sorted(out, key=lambda c: c.score, reverse=True)


def propose_repair(
    artifact: Artifact, step_id: str, surface: WebSurface, proposal_id: str, run_id: str | None = None,
    llm_pick: Callable[[Step, list[Candidate]], str | None] | None = None,
) -> RepairProposal | None:
    """Looks at the live page where `step_id` failed. Returns None when the step has no target to repair."""
    step = next((s for s in artifact.steps if s.step_id == step_id), None)
    if step is None or step.target is None:
        return None
    state = surface.perceive(actor="system")
    cands = score_candidates(step, state)
    base = dict(id=proposal_id, capability_id=artifact.capability_id, base_version=artifact.version, step_id=step_id, run_id=run_id,
                old_target=step.target, candidates=cands[:5], screenshot=state.screenshot_path, page_url=state.url,
                touches_irreversible_step=step.risk_level == "irreversible")

    chosen: Candidate | None = None
    method: Literal["heuristic", "llm"] = "heuristic"
    if cands and cands[0].score >= MIN_SCORE and (len(cands) == 1 or cands[0].score - cands[1].score >= MIN_MARGIN):
        chosen = cands[0]
    elif llm_pick is not None and cands:
        picked = llm_pick(step, cands[:5])
        chosen = next((c for c in cands if c.ref == picked), None)
        method = "llm"
    if chosen is None:
        return RepairProposal(**base, confident=False, reason="no candidate on the page clearly matches the recorded element; a person needs to look")

    fresh = surface.compute_target(chosen.ref)
    # new locators first; the old ones stay behind them as fallbacks, so a rollback of the vendor release still resolves
    merged: list[Locator] = [*fresh.locators, *[l for l in step.target.locators if l not in fresh.locators]]
    new_target = Target(semantic_description=step.target.semantic_description, locators=merged, hints=fresh.hints)
    why = ", ".join(f"{k}={v}" for k, v in chosen.why.items())
    return RepairProposal(**base, method=method, confident=True, new_target=new_target,
                          reason=f"best match is {chosen.role} {chosen.name!r} (score {chosen.score}: {why})")
