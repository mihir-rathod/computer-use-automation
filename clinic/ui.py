"""UI strings, element ids, css classes and form field names, with a drift mode.

Drift simulates a real vendor release changing the UI under an automation:
  level 0  baseline
  level 1  element ids and css classes are renamed
  level 2  + visible labels change (button, link and heading text)
  level 3  + form field names change, so even name-attribute locators break
The seed makes renames differ between drift runs. Server-side code reads form values through
`read()`, so the app keeps working at every level; only the surface an automation sees changes.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

DRIFT_TEXT: dict[str, tuple[str, str]] = {
    "Sign In": ("Log in", "Continue to portal"),
    "Search": ("Find patient", "Look up"),
    "Patient lookup": ("Find a patient", "Patients"),
    "Today's schedule": ("Daily schedule", "Calendar"),
    "Approvals": ("Pending approvals", "Review queue"),
    "Continue": ("Next", "Proceed"),
    "Confirm": ("Submit transaction", "Post it"),
    "Cancel appointment": ("Cancel visit", "Void appointment"),
    "Reschedule": ("Move appointment", "Change time"),
    "Submit claim": ("File claim", "Send to payer"),
    "Issue refund": ("Refund payment", "Return funds"),
    "Write off": ("Adjust off", "Clear balance"),
    "Approve": ("Authorize", "Okay it"),
    "Deny": ("Reject", "Decline"),
    "Save changes": ("Update", "Apply"),
    "Back": ("Return", "Go back"),
    "Sign out": ("Log out", "Exit"),
    "Update contact": ("Edit contact details", "Change contact"),
    "Select": ("Open", "View"),
    "Claim": ("File", "Bill payer"),
    "Refund": ("Return money", "Credit"),
    "Cancel": ("Void", "Remove"),
    "Prev": ("Previous", "Older"),
    "Next": ("More", "Newer"),
    "Show": ("Display", "Load"),
}


class UI:
    def __init__(self, level: int = 0, seed: str = "0"):
        self.level = max(0, min(3, level))
        self.seed = seed

    def _suffix(self, kind: str, key: str) -> str:
        return hashlib.sha1(f"{self.seed}:{kind}:{key}".encode()).hexdigest()[:5]

    def t(self, text: str) -> str:
        if self.level >= 2 and text in DRIFT_TEXT:
            return DRIFT_TEXT[text][0 if self.level == 2 else 1]
        return text

    def i(self, key: str) -> str:
        return key if self.level < 1 else f"{key}_{self._suffix('id', key)}"

    def c(self, key: str) -> str:
        return key if self.level < 1 else f"c{self._suffix('cls', key)}"

    def n(self, key: str) -> str:
        return key if self.level < 3 else f"f{self._suffix('name', key)}"

    def read(self, form: Mapping[str, Any], key: str, default: str = "") -> str:
        value = form.get(self.n(key))
        return default if value is None else str(value)

    def describe(self) -> dict[str, Any]:
        return {"level": self.level, "seed": self.seed}
