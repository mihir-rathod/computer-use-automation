"""Parses Playwright's `locator.aria_snapshot(mode="ai")` output into a flat, filtered list
of ObservedElement -- the LLM-facing "structured element list".

That snapshot is a YAML-ish dump of the accessibility tree with `[ref=eN]` element references
baked in (`page.locator("aria-ref=eN")` resolves one back to a live element). Two things this
module does beyond a bare parse:

1. Drops pure layout wrappers (table/rowgroup/row/generic with no name or text) -- exactly the
   noise MockBank's deliberately nested-table legacy markup adds -- while keeping anything
   independently actionable (textbox/button/link/etc.) or carrying a label/value.
2. When a real HTML5 <dialog>-style modal is open (role="dialog"), scopes perception to *only*
   that dialog's subtree. MockBank's "Terms Updated" interstitial is a plain CSS overlay, not a
   native <dialog>, so the browser doesn't make the background inert the way it would for a real
   modal -- without this, the flattened list would offer the agent background controls it
   shouldn't be able to reach yet, undermining the "recoverable: dismiss_and_continue" model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from surface.base import ObservedElement

_LINE_RE = re.compile(
    r'^-\s*(?P<role>[a-zA-Z][\w-]*)'
    r'(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?'
    r'(?P<attrs>(?:\s+\[[a-zA-Z]+(?:=[^\]]*)?\])*)'
    r'\s*(?::\s*(?P<value>.*))?$'
)
_ATTR_RE = re.compile(r'\[([a-zA-Z]+)(?:=([^\]]*))?\]')

_ALWAYS_INTERESTING_ROLES = {"textbox", "button", "link", "checkbox", "radio", "combobox", "dialog"}
_SUPPRESSED_ROLES = {"option"}  # consumed by their owning combobox, never listed standalone


@dataclass
class _RawNode:
    role: str
    name: str | None
    ref: str | None
    value: str | None
    attrs: dict[str, str]
    children: list[_RawNode] = field(default_factory=list)


def _parse_tree(snapshot_text: str) -> list[_RawNode]:
    roots: list[_RawNode] = []
    stack: list[tuple[int, _RawNode]] = []
    for raw_line in snapshot_text.splitlines():
        if not raw_line.strip():
            continue
        stripped = raw_line.lstrip(" ")
        indent = len(raw_line) - len(stripped)
        match = _LINE_RE.match(stripped)
        if not match:
            continue  # e.g. a "- /url: ..." child line -- not needed for perception
        attrs = dict(_ATTR_RE.findall(match.group("attrs") or ""))
        ref = attrs.pop("ref", None)
        node = _RawNode(
            role=match.group("role"),
            name=match.group("name"),
            ref=ref,
            value=match.group("value") or None,
            attrs=attrs,
        )
        while stack and stack[-1][0] >= indent:
            stack.pop()
        (stack[-1][1].children if stack else roots).append(node)
        stack.append((indent, node))
    return roots


def _find_dialogs(nodes: list[_RawNode]) -> list[_RawNode]:
    found: list[_RawNode] = []
    for node in nodes:
        if node.role == "dialog":
            found.append(node)
        else:
            found.extend(_find_dialogs(node.children))
    return found


def _flatten(nodes: list[_RawNode], out: list[ObservedElement]) -> None:
    for node in nodes:
        if node.role in _SUPPRESSED_ROLES:
            continue
        if node.role == "combobox":
            selected = next((c.name for c in node.children if c.role == "option" and "selected" in c.attrs), None)
            options = [c.name for c in node.children if c.role == "option" and c.name]
            if node.ref:
                out.append(ObservedElement(
                    ref=node.ref, role=node.role, name=node.name,
                    value=selected, options=options or None, state=node.attrs,
                ))
            continue  # never recurse into a combobox's own <option> children
        worth_showing = node.role in _ALWAYS_INTERESTING_ROLES or bool(node.name) or bool(node.value)
        if worth_showing and node.ref:
            out.append(ObservedElement(ref=node.ref, role=node.role, name=node.name, value=node.value, state=node.attrs))
        _flatten(node.children, out)


def parse_aria_snapshot(snapshot_text: str) -> list[ObservedElement]:
    roots = _parse_tree(snapshot_text)
    dialogs = _find_dialogs(roots)
    scope = dialogs if dialogs else roots
    out: list[ObservedElement] = []
    _flatten(scope, out)
    return out
