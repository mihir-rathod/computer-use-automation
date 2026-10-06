"""Decides the text that shows a task has worked, when the person did not say. The model has just read the answer off a page, so that page is the evidence:
a heading the entry page did not have, or the label printed next to the value that was read. The candidate is checked against the live page before it is accepted,
and the verification replay then has to see it again."""
from __future__ import annotations

from agent.loop import RecordedAction
from artifacts_lib.schema import ActionType, Signal, SignalType
from surface.base import ObservedElement, Surface


def derive_success_text(transcript: list[RecordedAction], surface: Surface, example_values: list[str] | None = None) -> str | None:
    extracts = [r for r in transcript if r.action.kind == ActionType.EXTRACT and r.result.success]
    if not extracts:
        return None
    last = extracts[-1]
    page = last.observed_before.elements
    # what the pages before the answer already showed: text that was there from the start proves nothing
    earlier = {e.name for r in transcript[: transcript.index(extracts[0])] for e in r.observed_before.elements if e.name}
    headings = [e for e in page if e.role == "heading" and _usable(e)]
    labels = [_label_before(page, r.action.ref) for r in extracts]
    candidates = [e.name for e in headings if e.name not in earlier]
    candidates += [n for n in labels if n and n not in earlier]
    candidates += [e.name for e in headings]
    candidates += [n for n in labels if n]
    seen: set[str] = set()
    # anything the model read off the page belongs to that record, so it would not be on the page for the next one
    examples = [v.lower() for v in (example_values or []) if v] + [str(r.result.extracted_value).strip().lower() for r in extracts if r.result.extracted_value]
    for text in candidates:
        if not text or text in seen:
            continue
        if any(v and (v in text.lower() or text.lower() in v) for v in examples):
            continue  # it would only be on the page for this one example, so every other run would fail its own success check
        seen.add(text)
        try:
            if surface.check_signal(Signal(type=SignalType.TEXT_PRESENT, value=text)):
                return text
        except Exception:  # noqa: BLE001 -- a candidate that cannot be checked is not a candidate
            continue
    return None


def _usable(e: ObservedElement) -> bool:
    return bool(e.name) and 4 <= len(e.name.strip()) <= 80


def _label_before(page: list[ObservedElement], ref: str | None) -> str | None:
    """The cell printed just before the one that was read, when it looks like a label ("Provider")."""
    index = next((i for i, e in enumerate(page) if e.ref == ref), None)
    if index is None or index == 0:
        return None
    before = page[index - 1]
    name = (before.name or "").strip()
    return name if before.role in ("cell", "columnheader", "rowheader", "term") and 4 <= len(name) <= 40 and not any(ch.isdigit() for ch in name) else None
