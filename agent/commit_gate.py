"""Supervised commit recording. Discovery stops at the first irreversible action because the safety policy
blocks it. A commit gate is how a person (or, in a sandbox, an automatic stand-in) authorises that one
action during discovery, so the commit step is *recorded from a real run* instead of hand-edited into the
artifact afterwards. Every approval is written to the artifact's provenance: who, how, and for which step.

Modes:
  supervised    a person is asked at the terminal, with the page and the exact element in front of them.
  auto_sandbox  approves without asking. Refused unless the target profile is marked `sandbox`, so it cannot
                be pointed at a system where the commit is real.
"""
from __future__ import annotations

import getpass
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from surface.base import Action, ObservedState


@dataclass(frozen=True)
class CommitRequest:
    description: str  # the element about to be activated, as the page names it
    url: str
    page_title: str
    capability_id: str | None
    goal: str | None


@dataclass(frozen=True)
class CommitDecision:
    approved: bool
    approver: str | None = None
    mode: Literal["supervised", "auto_sandbox"] = "supervised"
    note: str | None = None


CommitGate = Callable[[CommitRequest], CommitDecision]


def describe_request(action: Action, observed: ObservedState, capability_id: str | None, goal: str | None) -> CommitRequest:
    element = next((e for e in observed.elements if e.ref == action.ref), None)
    name = (element.name or element.role) if element else f"{action.kind.value} {action.ref}"
    return CommitRequest(f"{element.role} '{name}'" if element else name, observed.url, observed.title, capability_id, goal)


def supervised_gate(ask: Callable[[str], str] = input, default_approver: str | None = None) -> CommitGate:
    approver_default = default_approver or getpass.getuser()

    def gate(req: CommitRequest) -> CommitDecision:
        print("\n--- irreversible step awaiting approval ---")
        print(f"capability: {req.capability_id or '-'}\ngoal:       {req.goal or '-'}\npage:       {req.page_title} ({req.url})\nelement:    {req.description}")
        print("Approving executes this action for real and records it as the artifact's commit step.")
        if ask("Approve? [y/N] ").strip().lower() not in ("y", "yes"):
            return CommitDecision(False, mode="supervised", note="declined at the terminal")
        who = ask(f"Approver name [{approver_default}]: ").strip() or approver_default
        return CommitDecision(True, approver=who, mode="supervised")

    return gate


def auto_sandbox_gate(sandbox: bool) -> CommitGate:
    if not sandbox:
        raise PermissionError("auto_sandbox commit approval is only available for a target profile marked sandbox")

    def gate(req: CommitRequest) -> CommitDecision:
        return CommitDecision(True, approver="auto:sandbox", mode="auto_sandbox")

    return gate
