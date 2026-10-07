"""WebSurface -- the only Surface implementation built; other surfaces (legacy web,
desktop) are a design extension, not built.

Backed by Playwright. The accessibility tree (surface/aria.py, via aria_snapshot) is the
primary perception channel; a screenshot is captured alongside for evidence/debugging, but the
structured element list -- not pixels -- is what acting is driven from.

Note on `params`: callers pass already-substituted values (e.g. a real member id, not
"{{member_id}}"). Template substitution is the replay engine's job, not this layer's --
Surface only knows how to act on a live page, not how an artifact's parameters map onto it.
Likewise, EXTRACT returns a raw string; coercing it to the output_schema's declared type is also
the replay engine's job -- Surface doesn't know about output_schema at all.
"""
from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
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

@dataclass
class SettleConfig:
    """After an action that can change the page, wait for it to stop changing before anything reads
    it. Pending network requests and DOM mutations both count as "still changing"; both waits are
    bounded, so a page that never goes quiet slows an action down rather than failing it."""
    enabled: bool = True
    quiet_ms: int = 150
    timeout_ms: int = 2500


_SETTLE_AFTER = {ActionType.NAVIGATE, ActionType.CLICK, ActionType.SELECT, ActionType.DISMISS_DIALOG}
_DISPATCHING = {ActionType.NAVIGATE, ActionType.CLICK, ActionType.SELECT, ActionType.TYPE, ActionType.DISMISS_DIALOG}

_DOM_QUIET_JS = """([quiet, max]) => new Promise((resolve) => {
  let timer = setTimeout(done, quiet);
  const hard = setTimeout(done, max);
  const obs = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(done, quiet); });
  obs.observe(document.documentElement, {subtree: true, childList: true, attributes: true, characterData: true});
  function done() { obs.disconnect(); clearTimeout(timer); clearTimeout(hard); resolve(true); }
})"""

# For a table cell: is its accessible name just its own text (so it is *data*, which differs between
# records), and what is the label cell to its left?
_CELL_FACTS_JS = """(el) => {
  const text = (n) => (n && n.innerText ? n.innerText : '').trim();
  let label = null;
  if (el.tagName === 'TD' || el.tagName === 'TH') {
    let prev = el.previousElementSibling;
    while (prev && !text(prev)) prev = prev.previousElementSibling;
    if (prev) label = text(prev);
  }
  return {dataValued: !el.getAttribute('aria-label'), label};
}"""

# "Watching" a run: the element about to be acted on gets an amber outline (and keeps it, so the screenshot taken after the action shows
# what was touched), and the previous one is cleared.
_HIGHLIGHT_JS = """(el) => {
  document.querySelectorAll('[data-cua-hl]').forEach((n) => { n.style.outline = n.dataset.cuaPrev || ''; n.style.outlineOffset = ''; delete n.dataset.cuaHl; delete n.dataset.cuaPrev; });
  el.dataset.cuaPrev = el.style.outline || ''; el.dataset.cuaHl = '1';
  el.style.outline = '3px solid #f5a524'; el.style.outlineOffset = '2px';
  if (el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
}"""

_ACTIONABLE_KINDS = {ActionType.CLICK, ActionType.TYPE, ActionType.SELECT, ActionType.EXTRACT, ActionType.WAIT_FOR, ActionType.DISMISS_DIALOG}


class WebSurface(Surface):
    def __init__(
        self,
        page: Page,
        base_url: str,
        screenshot_dir: Path | None = None,
        evidence_logger: EvidenceLogger | None = None,
        safety_policy: SafetyPolicy | None = None,
        settle: SettleConfig | None = None,
        capture_every_action: bool = False,
        pace_ms: int = 0,
    ):
        self.page = page
        # Watch mode: a deliberate pause before and after each action, and an outline on the element being touched. This is our own pacing,
        # not Playwright's slow_mo, which delays every internal call (resolving, polling, settling) and makes a run crawl.
        self.pace_ms = max(0, int(pace_ms))
        # Evidence screenshots after every action (not just failures). Screenshots are not redactable, so this is a policy decision.
        self.capture_every_action = capture_every_action
        self.settle = settle or SettleConfig()
        self._inflight = 0
        self._last_net_activity = time.monotonic()
        self._dispatched = False
        page.on("request", self._net_start)
        page.on("requestfinished", self._net_end)
        page.on("requestfailed", self._net_end)
        self.base_url = base_url
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        self.evidence_logger = evidence_logger
        self.safety_policy = safety_policy
        self._last_elements: dict[str, ObservedElement] = {}
        self._screenshot_seq = 0

    # ---- settling ---------------------------------------------------------------------

    def _net_start(self, _request: object) -> None:
        self._inflight += 1
        self._last_net_activity = time.monotonic()

    def _net_end(self, _request: object) -> None:
        self._inflight = max(0, self._inflight - 1)
        self._last_net_activity = time.monotonic()

    def _settle(self) -> None:
        if not self.settle.enabled:
            return
        deadline = time.monotonic() + self.settle.timeout_ms / 1000
        try:
            self.page.wait_for_load_state("load", timeout=self.settle.timeout_ms)
        except Exception:
            pass
        quiet = self.settle.quiet_ms / 1000
        while time.monotonic() < deadline:
            if self._inflight == 0 and time.monotonic() - self._last_net_activity >= quiet:
                break
            self.page.wait_for_timeout(25)
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        try:
            self.page.evaluate(_DOM_QUIET_JS, [self.settle.quiet_ms, max(remaining_ms, self.settle.quiet_ms)])
        except Exception:
            pass  # the page navigated mid-wait and the old context is gone; the next read will wait for the new one

    def _pause(self, factor: float) -> None:
        if self.pace_ms:
            self.page.wait_for_timeout(int(self.pace_ms * factor))

    def _highlight(self, pw_locator: Any) -> None:
        try:
            pw_locator.evaluate(_HIGHLIGHT_JS)
        except Exception:
            pass  # a decoration: never let it break the action

    # ---- perceive ---------------------------------------------------------------------

    def perceive(self, actor: str = "system") -> ObservedState:
        snapshot_text = self.page.locator("body").aria_snapshot(mode="ai")
        elements = parse_aria_snapshot(snapshot_text)
        self._name_unlabeled_controls(elements)
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

    _FORM_ROLES = {"textbox", "searchbox", "combobox", "listbox", "spinbutton", "checkbox", "radio"}

    def _name_unlabeled_controls(self, elements: list[ObservedElement]) -> None:
        """Legacy forms often have no <label> at all, so several textboxes look identical. The HTML `name`
        attribute is what the server keys the value on; surfacing it lets discovery tell them apart."""
        for el in elements:
            if el.name or el.role not in self._FORM_ROLES:
                continue
            try:
                el.html_name = self.page.locator(f"aria-ref={el.ref}").get_attribute("name", timeout=500)
            except Exception:
                el.html_name = None

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
        pw_locator = self.page.locator(f"aria-ref={ref}")
        anchored = self._label_anchored_cell(element, pw_locator)
        if anchored is not None:
            return anchored
        locators = [SchemaLocator(
            strategy=LocatorStrategy.ROLE,
            value=f"{element.role}[name='{element.name}']" if element.name else element.role,
            note="Primary: accessible role + name, computed at discovery time.",
        )]
        css_id = pw_locator.get_attribute("id")
        if css_id:
            locators.append(SchemaLocator(
                strategy=LocatorStrategy.CSS, value=f"#{css_id}",
                note="Fallback: element id present at discovery time, not guaranteed stable across tenants.",
            ))
        if not element.name:
            # No accessible name: found against legacy table-layout forms with no <label>/aria-label at all. The
            # primary role locator degrades to a bare role (e.g. "textbox") that matches every same-role field,
            # which resolve_target treats as no match. The HTML `name` attribute is what the server keys the
            # submitted value off, so it is the stable identifier. It is added even when an id exists, because the
            # id is the first thing a vendor release renames (clinic drift level 1 breaks login without this).
            html_name = pw_locator.get_attribute("name")
            if html_name:
                locators.append(SchemaLocator(
                    strategy=LocatorStrategy.CSS, value=f"[name='{html_name}']",
                    note="Fallback: no accessible name or id at discovery time -- the HTML name "
                         "attribute is the only stable identifier this element has.",
                ))
        description = element.name or (f"{element.role} (form field {element.html_name})" if element.html_name else element.role)
        return Target(semantic_description=description, locators=locators, hints=self._hints_for(element, css_id))

    def _hints_for(self, element: ObservedElement, css_id: str | None) -> dict[str, Any]:
        """Where this element sat when it was recorded, so that if every locator later breaks a repair proposal
        can find the same element again: its role, its position among same-role elements, and what was around it."""
        ordered = list(self._last_elements.values())
        same_role = [e for e in ordered if e.role == element.role]
        at = ordered.index(element)
        named = lambda items: [e.name for e in items if e.name][:]  # noqa: E731
        return {
            "role": element.role,
            "name": element.name,
            "ordinal": same_role.index(element),
            "same_role_count": len(same_role),
            "before": named(ordered[max(0, at - 3):at])[-2:],
            "after": named(ordered[at + 1:at + 4])[:2],
            "html_name": element.html_name,
            "id": css_id,
        }

    def _label_anchored_cell(self, element: ObservedElement, pw_locator: Any) -> Target | None:
        """A value cell on a legacy page ('Phone' | '(206) 555-0111') is named by its own text, which is
        different for every record: a role+name locator would hold one patient's phone number and either fail on
        the next record or, worse, match some other cell that happens to show the same text. Anchor it to the
        label cell beside it instead, which is the same on every record."""
        if element.role not in ("cell", "gridcell"):
            return None
        try:
            facts = pw_locator.evaluate(_CELL_FACTS_JS)
        except Exception:
            return None
        label = facts.get("label")
        if not facts.get("dataValued") or not label or '"' in label or any(ch.isdigit() for ch in label) or len(label) > 40:
            return None  # the cell beside it is more record data (an id, a time), not a label: anchoring to it would only work for this record
        xpath = f'(//*[self::td or self::th][normalize-space(.)="{label}"]/following-sibling::*[self::td or self::th][1])[1]'
        return Target(
            semantic_description=f"value cell beside the '{label}' label",
            locators=[SchemaLocator(
                strategy=LocatorStrategy.XPATH, value=xpath,
                note="Anchored to the label cell to its left: a cell's own text is record data and changes between runs.",
            )],
            hints={"label": label},
        )

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
        self._dispatched = False
        try:
            result = self._act(action)
        except Exception as exc:
            result = ActionResult(success=False, error=str(exc))
        result.dispatched = result.dispatched or self._dispatched
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
                return ActionResult(success=False, error=decision.reason, blocked=decision.block_kind)

        if action.kind == ActionType.NAVIGATE:
            url = action.params["url"]
            full_url = url if url.startswith("http") else urljoin(self.base_url, url)
            self._dispatched = True
            self.page.goto(full_url)
            self._settle()
            self._pause(0.5)
            return ActionResult(success=True)

        resolved = self._resolve(action)
        if resolved is None:
            return ActionResult(success=False, error="could not resolve element", unresolved=True)
        pw_locator, resolved_target, resolved_strategy = resolved

        if self.pace_ms:
            self._highlight(pw_locator)
            self._pause(1.0)
        if action.kind in _DISPATCHING:
            self._dispatched = True
        if action.kind in (ActionType.CLICK, ActionType.DISMISS_DIALOG):
            pw_locator.click()
        elif action.kind == ActionType.TYPE:
            pw_locator.fill(action.params["text"])
        elif action.kind == ActionType.SELECT:
            wanted = action.params["value"]
            try:
                pw_locator.select_option(value=wanted, timeout=1500)
            except Exception:
                # a model often names the option by its visible label ("Weather") rather than its value ("weather")
                try:
                    pw_locator.select_option(label=wanted, timeout=1500)
                except Exception:
                    offered = pw_locator.evaluate("el => Array.from(el.options).filter(o => o.value).map(o => o.value)")
                    self._dispatched = False  # nothing was selected, so nothing was sent
                    return ActionResult(success=False, resolved_target=resolved_target, resolved_strategy=resolved_strategy, dispatched=False,
                                        error=f"option {wanted!r} is not offered; the list has: {', '.join(offered)}")
            actual = pw_locator.evaluate("el => el.value")
            return ActionResult(success=True, resolved_target=resolved_target, resolved_strategy=resolved_strategy,
                                applied_params={"value": actual} if actual != wanted else None)
        elif action.kind == ActionType.EXTRACT:
            value = pw_locator.inner_text().strip()
            return ActionResult(success=True, resolved_target=resolved_target, resolved_strategy=resolved_strategy, extracted_value=value)
        elif action.kind == ActionType.WAIT_FOR:
            pw_locator.wait_for(state=action.params.get("state", "visible"), timeout=action.params.get("timeout_ms", 5000))
        else:
            return ActionResult(success=False, error=f"unsupported action kind: {action.kind}")

        if action.kind in _SETTLE_AFTER:
            self._settle()
        self._pause(0.5)
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
            element = self._last_elements.get(action.ref) if action.ref else None
            semantic = " ".join(filter(None, [
                action.target.semantic_description if action.target else None,
                element.name if element else None,
                element.html_name if element else None,
                result.resolved_target.semantic_description if result.resolved_target else None,
            ])) or None
            params = redact_type_params(action.params, semantic)
        error_screenshot = self._capture_screenshot() if (not result.success or self.capture_every_action) else None
        self.evidence_logger.log(
            action.actor, "action",
            action_kind=action.kind.value,
            ref=action.ref,
            step_id=action.step_id,
            capability_id=action.capability_id,
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
