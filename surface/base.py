"""The Surface interface -- the seam between "how we perceive/act on a surface" and "the
recorded flow". WebSurface (surface/web.py, Playwright) is the only implementation built here;
LegacyWebSurface and DesktopSurface would be a design extension, not built, but this is the
interface they'd implement.

Both the discovery agent and the replay engine drive a Surface through the same two calls
(perceive/act) and the same Action/ActionResult shapes -- there is no separate "replay mode"
API. What differs between discovery and replay is *how an Action's target is chosen*: discovery
points at a `ref` from the most recent perceive() (an ephemeral id, meaningless outside that one
run); replay points at a `Target` (the portable locator fallback chain saved in an artifact).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from artifacts_lib.schema import ActionType, LocatorStrategy, Signal, Target


@dataclass
class ObservedElement:
    ref: str
    role: str
    name: str | None = None
    value: str | None = None
    options: list[str] | None = None  # combobox only: available option labels
    state: dict[str, str] = field(default_factory=dict)  # raw aria flags, e.g. {"disabled": ""}


@dataclass
class ObservedState:
    url: str
    title: str
    elements: list[ObservedElement]
    screenshot_path: str | None = None
    raw_snapshot: str | None = None

    def to_prompt_text(self) -> str:
        """The numbered list the LLM actually reasons over each turn -- structure, not pixels."""
        lines = [f"URL: {self.url}", f"Title: {self.title}", "", "Interactive elements:"]
        if not self.elements:
            lines.append("  (none)")
        for el in self.elements:
            label = f'{el.role} "{el.name}"' if el.name else el.role
            suffix = ""
            if el.value:
                suffix += f" = {el.value!r}"
            if el.options:
                suffix += f" [options: {', '.join(el.options)}]"
            lines.append(f"  {el.ref}: {label}{suffix}")
        return "\n".join(lines)


@dataclass
class Action:
    kind: ActionType
    ref: str | None = None
    target: Target | None = None
    params: dict[str, Any] = field(default_factory=dict)
    actor: str = "system"  # "agent" | "replay" | "human" -- who/what is driving this action
    confirmed: bool = False  # explicit approval for an irreversible action (safety/policy.py)

    def __post_init__(self) -> None:
        if self.kind != ActionType.NAVIGATE and self.ref is None and self.target is None:
            raise ValueError(f"action '{self.kind}' needs either ref (discovery) or target (replay)")
        if self.ref is not None and self.target is not None:
            raise ValueError("action cannot specify both ref and target -- pick one")


@dataclass
class ActionResult:
    success: bool
    resolved_target: Target | None = None
    resolved_strategy: LocatorStrategy | None = None
    extracted_value: str | None = None
    error: str | None = None


class Surface(ABC):
    @abstractmethod
    def perceive(self) -> ObservedState: ...

    @abstractmethod
    def act(self, action: Action) -> ActionResult: ...

    @abstractmethod
    def compute_target(self, ref: str) -> Target:
        """Portable locator fallback chain for a ref from the most recent perceive() call --
        used to turn a discovery action into something an artifact step can save."""
        ...

    @abstractmethod
    def check_signal(self, signal: Signal) -> bool:
        """Does the current state match this signal, right now? One mechanism for "is this
        condition true" used for step/success checkpoints and for error_handling's
        business_outcome/recoverable rules alike -- replay never touches Playwright (or any
        other surface's underlying tech) directly, only this and act()."""
        ...
