"""Keeps only the part of a discovery run that is the task. A model working out an unfamiliar system wanders: it opens the wrong page, tries a filter, goes back to
the main page and starts again. Replaying the wandering would fail or waste time, and nothing in it is the task.

Two rules, both mechanical and both checked afterwards by the verification replay:
  * an action that failed was not part of the way there;
  * if the model returned to the page it started from (by address or by clicking back to it) and began again, only the last attempt counts."""
from __future__ import annotations

from urllib.parse import urlsplit

from agent.loop import RecordedAction
from artifacts_lib.schema import ActionType


def _path(action: RecordedAction) -> str:
    return urlsplit(str(action.action.params.get("url", ""))).path


def _back_at(action: RecordedAction, start: str) -> bool:
    """The model ended up on the page it started from again: it went there by address or by clicking the main-menu link."""
    came_from = urlsplit(action.observed_before.url).path
    if came_from == start:
        return False  # typing or choosing on the start page itself is not going back to it
    if action.action.kind == ActionType.NAVIGATE and _path(action) == start:
        return True
    return action.observed_after is not None and urlsplit(action.observed_after.url).path == start


def _same_field(a: RecordedAction, b: RecordedAction) -> bool:
    ta, tb = a.result.resolved_target, b.result.resolved_target
    return a.action.kind == b.action.kind == ActionType.TYPE and ta is not None and tb is not None and ta.model_dump() == tb.model_dump() \
        and a.observed_before.url == b.observed_before.url


def prune(transcript: list[RecordedAction], outputs: set[str] | None = None) -> tuple[list[RecordedAction], int]:
    """Returns the pruned transcript and how many actions were dropped. `outputs`, when given, are the only things the task is meant to read back."""
    kept = [r for r in transcript if r.result.success or r.commit_approval is not None]
    if outputs is not None:
        # a read that is not one of the task's outputs would only be one more thing to break on another record; and a value read twice counts once, the last time
        kept = [r for r in kept if r.output_name is None or r.output_name in outputs]
        last_read = {r.output_name: i for i, r in enumerate(kept) if r.output_name}
        kept = [r for i, r in enumerate(kept) if r.output_name is None or last_read[r.output_name] == i]
    # a field typed more than once (a typo corrected) is typed once, with the value that stuck
    kept = [r for i, r in enumerate(kept) if not (i + 1 < len(kept) and _same_field(r, kept[i + 1]))]
    if not kept:
        return kept, len(transcript)
    first = kept[0]
    if first.action.kind == ActionType.NAVIGATE:
        start = _path(first)
        again = [i for i, r in enumerate(kept) if i > 0 and _back_at(r, start)]
        if again:
            kept = [first, *kept[again[-1] + 1:]]
    return kept, len(transcript) - len(kept)
