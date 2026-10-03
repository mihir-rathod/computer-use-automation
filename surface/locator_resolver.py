"""Resolves an artifact's Target (an ordered locator fallback chain) against a live Playwright
page -- the concrete mechanism behind "stable element targeting".
Used by replay directly, and by WebSurface.act() when a discovery-era ref isn't available.
"""
from __future__ import annotations

import re

from playwright.sync_api import Locator as PWLocator
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PWTimeoutError

from artifacts_lib.schema import Locator as SchemaLocator
from artifacts_lib.schema import LocatorStrategy, Target

_ROLE_VALUE_RE = re.compile(r"^(?P<role>[a-zA-Z]+)(?:\[name='(?P<name>[^']*)'\])?$")
RESOLVE_TIMEOUT_MS = 2000


def _build_locator(page: Page, locator: SchemaLocator) -> PWLocator:
    if locator.strategy == LocatorStrategy.ROLE:
        match = _ROLE_VALUE_RE.match(locator.value)
        if not match:
            raise ValueError(f"malformed role locator value: {locator.value!r}")
        role, name = match.group("role"), match.group("name")
        # exact=True: Playwright's default name matching is substring-based, which is unreliable
        # in deeply nested table layouts -- an outer wrapping <td> with no aria-label of its own
        # computes its accessible name as the concatenation of all descendant text, so a substring
        # query for an inner cell's label can spuriously match ancestor cells too. Exact matching
        # is what makes an explicit aria-label a precise, deterministic locator rather than a
        # fuzzy one -- exactly what this schema's locators are meant to be.
        return page.get_by_role(role, name=name, exact=True) if name else page.get_by_role(role)
    if locator.strategy == LocatorStrategy.CSS:
        return page.locator(locator.value)
    if locator.strategy == LocatorStrategy.XPATH:
        return page.locator(f"xpath={locator.value}")
    if locator.strategy == LocatorStrategy.TEXT:
        return page.get_by_text(locator.value)
    raise ValueError(f"unknown locator strategy: {locator.strategy}")


def resolve_target(page: Page, target: Target) -> tuple[PWLocator, SchemaLocator] | None:
    """Try each locator in target.locators, in order. Returns the Playwright Locator plus
    which SchemaLocator entry actually resolved -- replay logs this as evidence of which
    strategy in the chain was needed, useful for spotting drift later.

    A match of more than one element is treated the same as no match (try the next, more
    specific, strategy) rather than guessing which of several elements was meant.
    """
    for locator in target.locators:
        try:
            pw_locator = _build_locator(page, locator)
            pw_locator.first.wait_for(state="attached", timeout=RESOLVE_TIMEOUT_MS)
            if pw_locator.count() == 1:
                return pw_locator, locator
        except PWTimeoutError:
            continue
        except Exception:
            continue
    return None
