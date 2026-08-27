"""WebSurface -- the only Surface implementation built (ASSIGNMENT_ORIGINAL.md 3.7 asks for a
credible design story for other surfaces, not that we build them; see REPORT.md heading 4).

Backed by Playwright. The accessibility tree (surface/aria.py, via aria_snapshot) is the
primary perception channel; a screenshot is captured alongside for evidence/debugging, but the
structured element list -- not pixels -- is what acting is driven from.

Note on `params`: callers pass already-substituted values (e.g. a real member id, not
"{{member_id}}"). Template substitution is the replay engine's job (Phase 5), not this layer's --
Surface only knows how to act on a live page, not how an artifact's parameters map onto it.
Likewise, EXTRACT returns a raw string; coercing it to the output_schema's declared type is also
the replay engine's job -- Surface doesn't know about output_schema at all.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Page

from artifacts_lib.schema import ActionType, LocatorStrategy, Signal, SignalType, Target
from artifacts_lib.schema import Locator as SchemaLocator
from evidence_lib.logger import EvidenceLogger
from evidence_lib.redaction import redact_type_params
from safety.policy import SafetyPolicy
from surface.aria import parse_aria_snapshot
from surface.base import Action, ActionResult, ObservedElement, ObservedState, Surface
from surface.locator_resolver import resolve_target

_ACTIONABLE_KINDS = {ActionType.CLICK, ActionType.TYPE, ActionType.SELECT, ActionType.EXTRACT, ActionType.WAIT_FOR, ActionType.DISMISS_DIALOG}


class WebSurface(Surface):
    def __init__(
        self,
        page: Page,
        base_url: str,
        screenshot_dir: Path | None = None,
        evidence_logger: EvidenceLogger | None = None,
        safety_policy: SafetyPolicy | None = None,
    ):
        self.page = page
        self.base_url = base_url
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        self.evidence_logger = evidence_logger
        self.safety_policy = safety_policy
        self._last_elements: dict[str, ObservedElement] = {}
        self._screenshot_seq = 0

    # ---- perceive ---------------------------------------------------------------------

    def perceive(self, actor: str = "system") -> ObservedState:
        snapshot_text = self.page.locator("body").aria_snapshot(mode="ai")
        elements = parse_aria_snapshot(snapshot_text)
        self._last_elements = {el.ref: el for el in elements}
        screenshot_path = self._capture_screenshot()
        state = ObservedState(
            url=self.page.url, title=self.page.title(), elements=elements,
            screenshot_path=screenshot_path, raw_snapshot=snapshot_text,
        )
        if self.evidence_logger is not None:
            self.evidence_logger.log(
                actor, "perceive", url=state.url, title=state.title,
                element_count=len(elements), screenshot=screenshot_path,
            )
        return state

    def _capture_screenshot(self) -> str | None:
        if self.screenshot_dir is None:
            return None
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._screenshot_seq += 1
        path = self.screenshot_dir / f"{self._screenshot_seq:03d}.png"
        self.page.screenshot(path=str(path))
        return str(path)

    # ---- recording ----------------------------------------------------------------------

    def compute_target(self, ref: str) -> Target:
        element = self._last_elements.get(ref)
        if element is None:
            raise ValueError(f"ref '{ref}' is not from the most recent perceive() call")
        locators = [SchemaLocator(
            strategy=LocatorStrategy.ROLE,
            value=f"{element.role}[name='{element.name}']" if element.name else element.role,
            note="Primary: accessible role + name, computed at discovery time.",
        )]
        pw_locator = self.page.locator(f"aria-ref={ref}")
        css_id = pw_locator.get_attribute("id")
        if css_id:
            locators.append(SchemaLocator(
                strategy=LocatorStrategy.CSS, value=f"#{css_id}",
                note="Fallback: element id present at discovery time, not guaranteed stable across tenants.",
            ))
        elif not element.name:
            # No accessible name AND no id -- found against MERIDIAN CORE, whose legacy
            # table-layout forms have no <label>/aria-label at all: the primary role locator
            # above degrades to a bare role (e.g. "textbox") with nothing to disambiguate it,
            # which matches every same-role field on the page and is treated as no match at all
            # by resolve_target's "ambiguous match = no match" rule -- replay failed here with
            # "could not resolve element" before this fallback existed. The HTML `name`
            # attribute is a stable identifier even without a label: it's literally what the
            # server keys the submitted value off.
            html_name = pw_locator.get_attribute("name")
            if html_name:
                locators.append(SchemaLocator(
                    strategy=LocatorStrategy.CSS, value=f"[name='{html_name}']",
                    note="Fallback: no accessible name or id at discovery time -- the HTML name "
                         "attribute is the only stable identifier this element has.",
                ))
        return Target(semantic_description=element.name or element.role, locators=locators)

    # ---- check_signal -------------------------------------------------------------------

    def check_signal(self, signal: Signal) -> bool:
        if signal.type in (SignalType.URL_MATCHES, SignalType.REDIRECTED_TO):
            # Path only, not the full URL -- these signals mean "which page/route are we on",
            # and matching the full URL is actively wrong: a redirect to "/login?next=/search"
            # would spuriously satisfy a "**/search" checkpoint (the query string happens to end
            # in "/search") while simultaneously failing to match a "**/login" signal (the full
            # string ends in "/search", not "/login") -- exactly backwards on both counts.
            path = urlsplit(self.page.url).path
            return fnmatch.fnmatchcase(path, signal.value)
        if signal.type == SignalType.TEXT_PRESENT:
            return self.page.get_by_text(signal.value).count() > 0
        if signal.type == SignalType.DIALOG_PRESENT:
            return self.page.get_by_role("dialog", name=signal.value, exact=True).count() > 0

        assert signal.target is not None  # enforced by Signal's own validator
        resolved = resolve_target(self.page, signal.target)
        if signal.type == SignalType.ELEMENT_VISIBLE:
            return resolved is not None and resolved[0].is_visible()
        if signal.type == SignalType.ELEMENT_HIDDEN:
            return resolved is None or not resolved[0].is_visible()
        if signal.type == SignalType.ELEMENT_VALUE_EQUALS:
            if resolved is None:
                return False
            pw_locator, _ = resolved
            try:
                current = pw_locator.input_value()
            except Exception:
                current = pw_locator.inner_text()
            return current.strip() == signal.value
        return False

    # ---- act ------------------------------------------------------------------------------

    def act(self, action: Action) -> ActionResult:
        try:
            result = self._act(action)
        except Exception as exc:
            result = ActionResult(success=False, error=str(exc))
        self._log_action(action, result)
        return result

    def _act(self, action: Action) -> ActionResult:
        if self.safety_policy is not None:
            decision = self.safety_policy.evaluate(
                url=self._url_for_safety_check(action),
                action_type=action.kind,
                semantic_description=self._semantic_for_safety_check(action),
                current_path=urlsplit(self.page.url).path,
                confirmed=action.confirmed,
            )
            if not decision.allowed:
                return ActionResult(success=False, error=decision.reason)

        if action.kind == ActionType.NAVIGATE:
            url = action.params["url"]
            full_url = url if url.startswith("http") else urljoin(self.base_url, url)
            self.page.goto(full_url)
            return ActionResult(success=True)

        resolved = self._resolve(action)
        if resolved is None:
            return ActionResult(success=False, error="could not resolve element")
        pw_locator, resolved_target, resolved_strategy = resolved

        if action.kind in (ActionType.CLICK, ActionType.DISMISS_DIALOG):
            pw_locator.click()
        elif action.kind == ActionType.TYPE:
            pw_locator.fill(action.params["text"])
        elif action.kind == ActionType.SELECT:
            pw_locator.select_option(value=action.params["value"])
        elif action.kind == ActionType.EXTRACT:
            value = pw_locator.inner_text().strip()
            return ActionResult(success=True, resolved_target=resolved_target, resolved_strategy=resolved_strategy, extracted_value=value)
        elif action.kind == ActionType.WAIT_FOR:
            pw_locator.wait_for(state=action.params.get("state", "visible"), timeout=action.params.get("timeout_ms", 5000))
        else:
            return ActionResult(success=False, error=f"unsupported action kind: {action.kind}")

        return ActionResult(success=True, resolved_target=resolved_target, resolved_strategy=resolved_strategy)

    def _url_for_safety_check(self, action: Action) -> str:
        """The URL the allowlist should evaluate: the *destination* for navigate (that's how
        an action would escape the allowed route set), the *current* page for everything else
        (that's where the action actually happens)."""
        if action.kind == ActionType.NAVIGATE:
            url = action.params.get("url", "")
            return url if url.startswith("http") else urljoin(self.base_url, url)
        return self.page.url

    def _semantic_for_safety_check(self, action: Action) -> str | None:
        if action.target is not None:
            return action.target.semantic_description
        if action.ref is not None and action.ref in self._last_elements:
            return self._last_elements[action.ref].name
        return None

    def _resolve(self, action: Action):
        if action.kind not in _ACTIONABLE_KINDS:
            return None
        if action.ref is not None:
            pw_locator = self.page.locator(f"aria-ref={action.ref}")
            if pw_locator.count() != 1:
                return None
            return pw_locator, self.compute_target(action.ref), LocatorStrategy.ROLE
        if action.target is not None:
            resolved = resolve_target(self.page, action.target)
            if resolved is None:
                return None
            pw_locator, schema_locator = resolved
            return pw_locator, action.target, schema_locator.strategy
        return None

    # ---- evidence ---------------------------------------------------------------------

    def _log_action(self, action: Action, result: ActionResult) -> None:
        if self.evidence_logger is None:
            return
        params = action.params
        if action.kind == ActionType.TYPE:
            semantic = action.target.semantic_description if action.target else (
                self._last_elements[action.ref].name if action.ref in self._last_elements else None
            )
            params = redact_type_params(action.params, semantic)
        error_screenshot = self._capture_screenshot() if not result.success else None
        self.evidence_logger.log(
            action.actor, "action",
            action_kind=action.kind.value,
            ref=action.ref,
            target=action.target.model_dump() if action.target else None,
            params=params,
            confirmed=action.confirmed,
            success=result.success,
            resolved_target=result.resolved_target.model_dump() if result.resolved_target else None,
            resolved_strategy=result.resolved_strategy.value if result.resolved_strategy else None,
            extracted_value=result.extracted_value,
            error=result.error,
            error_screenshot=error_screenshot,
            url=self.page.url,
        )
