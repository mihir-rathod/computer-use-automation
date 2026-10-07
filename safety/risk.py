"""Classifies whether an action is safe/reversible or risky/irreversible. Deliberately
independent of whatever risk_level a saved artifact step *claims*: Surface.act() re-classifies
live, at execution time, rather than trusting a value that could be stale or hand-edited
(defense in depth against a tampered or wrong artifact). Binary by design, matching
Step.risk_level's own two-value model (artifacts_lib/schema.py).
"""
from __future__ import annotations

from artifacts_lib.schema import ActionType, StepRiskLevel

# Reading/observing/navigating is never itself irreversible -- only an action that could
# change state (click/type/select/dismiss) is a candidate.
_ALWAYS_SAFE_ACTIONS = {ActionType.EXTRACT, ActionType.NAVIGATE, ActionType.WAIT_FOR}

# Two keyword tiers, because a keyword means different things depending on where it appears.
#
# A *commit* keyword ("confirm"/"post"/"delete"/"remove") legitimately means "this action
# commits something" wherever it appears -- in the element's name or in the current page path.
#
# A *domain-noun* keyword ("transfer"/"withdraw"/"close account") only describes what a specific
# ELEMENT does, never "we are somewhere in this flow": apps often bake the domain word into
# every step of a flow's route (e.g. /accounts/{id}/transfer, .../transfer/review), so matching
# it against the current path would misclassify the safe "Continue" click that merely reaches the
# *review* page (nothing posted yet, still cancelable) as irreversible -- before the operator
# ever sees the review screen. Domain nouns are therefore matched against the element's own
# description only.
#
# Deliberately NOT included:
# - "submit": generic enough to show up in descriptive text without meaning "this changes state"
#   (a hand-written fixture described a login button as "login submit button", which got the
#   login blocked as irreversible). Artifacts describe elements by their accessible name, so
#   semantic_description stays a reliable classification signal rather than free-text prose.
# - "open account" / "open sub-account": a plain link to the form is a harmless navigation, and
#   it is a CLICK (not a NAVIGATE), so it isn't caught by _ALWAYS_SAFE_ACTIONS. The actual
#   commit button there already carries "confirm".
#
# A classifier built on keywords will miss commit buttons whose label contains none of them -- a
# button that just says "Open Share" or "Apply" is irreversible with no keyword to catch it. That
# is a known limitation of this approach, not a solved problem: it is why risk is also declared
# per capability, and why the planned replacement is per-capability policy configuration rather
# than a global word list.
_COMMIT_KEYWORDS = ("confirm", "post", "delete", "remove")
_DOMAIN_KEYWORDS = ("transfer", "withdraw", "close account")


class RiskClassifier:
    def __init__(self, extra_commit: tuple[str, ...] | list[str] = (), extra_domain: tuple[str, ...] | list[str] = ()):
        """Extra keywords come from safety/policy.yaml; they add to the built-in lists, never replace them."""
        self._commit = _COMMIT_KEYWORDS + tuple(k.lower() for k in extra_commit)
        self._domain = _DOMAIN_KEYWORDS + tuple(k.lower() for k in extra_domain)

    def classify(
        self,
        action_type: ActionType,
        semantic_description: str | None = None,
        current_path: str | None = None,
    ) -> StepRiskLevel:
        if action_type in _ALWAYS_SAFE_ACTIONS:
            return StepRiskLevel.SAFE
        description = (semantic_description or "").lower()
        haystack = f"{description} {current_path or ''}".lower()
        if any(keyword in haystack for keyword in self._commit):
            return StepRiskLevel.IRREVERSIBLE
        if any(keyword in description for keyword in self._domain):
            return StepRiskLevel.IRREVERSIBLE
        return StepRiskLevel.SAFE
