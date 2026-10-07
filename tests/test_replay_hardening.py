"""Replay hardening against the clinic target: what the engine does when an irreversible step's outcome
is uncertain, when a run runs out of time, and when the page is still settling.

The clinic's audit log is the ground truth. `effects` below counts rows where state really changed, so
"the refund posted exactly once" does not depend on anything the page said.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from artifacts_lib.schema import Artifact
from evidence_lib.logger import EvidenceLogger
from replay.engine import ReplayEngine
from replay.result import ReplayStatus
from surface.base import ActionResult
from surface.web import SettleConfig, WebSurface
from tests.clinic_support import chaos, effects

AMOUNT = "20.00"  # small enough that two of them still fit inside what the seeded invoice paid


def _loc(strategy: str, value: str) -> dict:
    return {"strategy": strategy, "value": value}


def _target(desc: str, *locators: dict) -> dict:
    return {"semantic_description": desc, "locators": list(locators)}


def refund_artifact(*, retry_on_error: bool = False, reauth_on_timeout: bool = False) -> Artifact:
    """Issue a refund through the legacy skin: function entry -> form -> review -> confirm. A refund is the
    flow where a second post really moves money twice once the target's duplicate guard is off."""
    recoverable = []
    if retry_on_error:
        recoverable.append({"signal": {"type": "text_present", "value": "APPLICATION ERROR"}, "action": "retry", "max_attempts": 2})
    if reauth_on_timeout:
        recoverable.append({"signal": {"type": "text_present", "value": "YOUR SESSION HAS TIMED OUT"}, "action": "reauthenticate_and_resume"})
    return Artifact.model_validate({
        "capability_id": "clinic.issue_refund", "version": "1.0.0", "name": "Issue refund",
        "description": "Refunds part of a paid invoice.",
        "target": {"app_id": "clinic", "surface_type": "legacy_web", "base_url": "http://localhost:8100", "vendor_product": "larkspur"},
        "input_schema": {"type": "object", "properties": {"invoice": {"type": "string"}, "amount": {"type": "string"}, "reason": {"type": "string"}},
                         "required": ["invoice", "amount", "reason"]},
        "output_schema": {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]},
        "success_checkpoint": {"type": "text_present", "value": "REFUND ISSUED"},
        "success_output_defaults": {"status": "issued"},
        "steps": [
            {"step_id": "s1", "action": "navigate", "params": {"url": "/legacy/fn/refund"},
             "checkpoint": {"type": "text_present", "value": "Invoice no."}},
            {"step_id": "s2", "action": "type", "params": {"text": "{{invoice}}"},
             "target": _target("Invoice number", _loc("css", "input[name='number']"))},
            {"step_id": "s3", "action": "click", "target": _target("Continue", _loc("role", "button[name='Continue']")),
             "checkpoint": {"type": "text_present", "value": "Refundable"}},
            {"step_id": "s4a", "action": "type", "params": {"text": "{{amount}}"},
             "target": _target("Amount", _loc("css", "input[name='amount']"))},
            {"step_id": "s4", "action": "select", "params": {"value": "{{reason}}"},
             "target": _target("Reason", _loc("css", "select[name='reason']"))},
            {"step_id": "s5", "action": "click", "target": _target("Continue", _loc("role", "button[name='Continue']")),
             "checkpoint": {"type": "text_present", "value": "cannot be reversed"}},
            {"step_id": "s6", "action": "click", "target": _target("Confirm", _loc("role", "button[name='Confirm']")),
             "checkpoint": {"type": "text_present", "value": "REFUND ISSUED"},
             "risk_level": "irreversible", "idempotent": False},
        ],
        "error_handling": {"business_outcomes": [], "recoverable": recoverable},
        "safety": {"risk_level": "state_changing", "requires_confirmation": True},
        "provenance": {"discovered_by": "hand_written", "discovery_run_id": "test", "created_at": "2026-01-01T00:00:00Z", "reviewed": True},
    })


def engine(page, base, tmp_path, **kw) -> ReplayEngine:
    surface = WebSurface(page, base_url=base, screenshot_dir=tmp_path / "shots")
    return ReplayEngine(surface, evidence_logger=EvidenceLogger(tmp_path), **kw)


# ---- the commit step ------------------------------------------------------------------------

def test_clean_run_commits_exactly_once(signed_in, clinic, tmp_path, params):
    result = engine(signed_in, clinic, tmp_path).run(refund_artifact(), params)

    assert result.status == ReplayStatus.SUCCESS, result.error
    assert result.committed and result.commit_step == "s6"
    assert result.outputs == {"status": "issued"}
    assert len(effects(clinic)) == 1


def test_response_lost_after_commit_is_needs_review_and_never_retried(signed_in, clinic, tmp_path, params):
    """The server issues the refund and then answers 500. The artifact even carries a RETRY rule for
    that page; the engine must still not re-issue the commit."""
    chaos(clinic, "error500_after", method="POST", path_glob="/legacy/transactions/confirm")

    result = engine(signed_in, clinic, tmp_path).run(refund_artifact(retry_on_error=True), params)

    assert result.status == ReplayStatus.NEEDS_REVIEW
    assert result.error.code == "ambiguous_commit" and result.error.step_id == "s6"
    assert not result.committed  # not confirmed by the page
    assert result.commit_step == "s6"  # but it was issued
    assert result.steps_completed == ["s1", "s2", "s3", "s4a", "s4", "s5"]
    assert len(effects(clinic)) == 1  # ground truth: the refund did land, once


def test_second_confirm_would_double_post_if_it_were_retried(clinic, signed_in, params):
    """Sanity check on the test above: with the target's guard off, a blind re-confirm does post twice.
    This is what the engine's refusal to retry is protecting against."""
    page = signed_in
    page.goto(f"{clinic}/legacy/fn/refund")
    page.locator('input[name="number"]').fill(params["invoice"])
    page.get_by_role("button", name="Continue").click()
    page.locator('input[name="amount"]').fill(AMOUNT)
    page.locator('select[name="reason"]').select_option(params["reason"])
    page.get_by_role("button", name="Continue").click()
    token = page.locator('input[name="txn"]').first.get_attribute("value")
    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    for _ in range(2):
        httpx.post(f"{clinic}/legacy/transactions/confirm", data={"txn": token}, cookies=cookies, timeout=5)
    assert len(effects(clinic)) == 2


def test_recoverable_page_after_commit_is_needs_review_not_reauth(signed_in, clinic, tmp_path, params):
    """The session lapses as the commit is submitted. The page that comes back is a known recoverable
    condition, but after an issued irreversible step the engine cannot know the effect did not happen."""
    chaos(clinic, "expire_session", method="POST", path_glob="/legacy/transactions/confirm")

    result = engine(signed_in, clinic, tmp_path, reauth_credentials={"username": "x", "password": "y"}).run(
        refund_artifact(reauth_on_timeout=True), params)

    assert result.status == ReplayStatus.NEEDS_REVIEW
    assert result.error.code == "ambiguous_commit"
    assert len(effects(clinic)) == 0  # in fact nothing posted; the engine is conservative on purpose


def test_dry_run_stops_before_the_irreversible_step(signed_in, clinic, tmp_path, params):
    result = engine(signed_in, clinic, tmp_path, dry_run=True).run(refund_artifact(), params)

    assert result.status == ReplayStatus.DRY_RUN and result.dry_run
    assert result.steps_completed == ["s1", "s2", "s3", "s4a", "s4", "s5"]
    assert result.error.step_id == "s6" and result.error.code == "dry_run"
    assert not result.committed and result.commit_step is None
    assert effects(clinic) == []


def test_unconfirmed_commit_is_blocked_by_the_safety_policy_and_confirmed_one_runs(signed_in, clinic, tmp_path, params):
    from safety.allowlist import AllowlistConfig, AllowlistPolicy
    from safety.policy import SafetyPolicy

    config = AllowlistConfig(allowed_base_urls=[clinic], allowed_route_patterns=["/legacy/*"],
                             allowed_action_types=["navigate", "click", "type", "select", "extract", "wait_for", "dismiss_dialog"])
    surface = WebSurface(signed_in, base_url=clinic, screenshot_dir=tmp_path / "shots",
                         safety_policy=SafetyPolicy(AllowlistPolicy(config)))

    blocked = ReplayEngine(surface).run(refund_artifact(), params)
    assert blocked.status == ReplayStatus.HARD_FAILURE
    assert blocked.error.code == "blocked" and blocked.error.step_id == "s6"
    assert effects(clinic) == []

    httpx.post(f"{clinic}/_test/reset", timeout=5)
    httpx.post(f"{clinic}/_test/chaos/duplicate-guard", json={"enabled": False}, timeout=5)
    signed_in.goto(f"{clinic}/legacy/login")
    signed_in.locator('input[name="username"]').fill("frontdesk")
    signed_in.locator('input[name="password"]').fill("desk-demo-123")
    signed_in.get_by_role("button", name="Sign In").click()
    signed_in.get_by_text("Main menu").first.wait_for()

    ok = ReplayEngine(surface, confirmed_steps={"s6"}).run(refund_artifact(), params)
    assert ok.status == ReplayStatus.SUCCESS, ok.error
    assert len(effects(clinic)) == 1


# ---- time budget and cancellation --------------------------------------------------------------

def test_run_deadline_stops_before_the_next_action(signed_in, clinic, tmp_path, params):
    chaos(clinic, "latency", params={"ms": 1500}, method="GET", path_glob="/legacy/fn/refund")

    result = engine(signed_in, clinic, tmp_path, deadline_s=1.0).run(refund_artifact(), params)

    assert result.status == ReplayStatus.HARD_FAILURE
    assert result.error.code == "timeout"
    assert result.steps_completed == ["s1"]
    assert effects(clinic) == []


def test_cancel_event_stops_the_run(signed_in, clinic, tmp_path, params):
    cancel = threading.Event()
    cancel.set()

    result = engine(signed_in, clinic, tmp_path, cancel_event=cancel).run(refund_artifact(), params)

    assert result.status == ReplayStatus.HARD_FAILURE and result.error.code == "cancelled"
    assert result.steps_completed == []


def test_cancel_interrupts_a_long_backoff_sleep():
    cancel = threading.Event()
    eng = ReplayEngine(SimpleNamespace(), cancel_event=cancel)
    threading.Timer(0.2, cancel.set).start()

    started = time.monotonic()
    with pytest.raises(Exception) as exc:
        eng._sleep(30, [])

    assert time.monotonic() - started < 2
    assert exc.value.error.code == "cancelled"


# ---- backoff and checkpoint polling (no browser) -------------------------------------------------

class FakeSurface:
    """check_signal answers True for the first `true_for` calls (a condition that is present, then
    clears) or, with `late=True`, False for that many calls and True afterwards (a checkpoint that
    shows up late)."""

    def __init__(self, n: int, late: bool = False):
        self.acts = 0
        self.checks = 0
        self.n, self.late = n, late

    def act(self, action):
        self.acts += 1
        return ActionResult(success=True, dispatched=True)

    def check_signal(self, signal):
        self.checks += 1
        return self.checks > self.n if self.late else self.checks <= self.n


def test_backoff_grows_by_the_multiplier(monkeypatch):
    from artifacts_lib.schema import RecoverableRule, Step

    rule = RecoverableRule.model_validate({"signal": {"type": "text_present", "value": "busy"}, "action": "retry",
                                           "max_attempts": 3, "backoff_ms": 100, "backoff_multiplier": 2.0})
    step = Step.model_validate({"step_id": "s1", "action": "navigate", "params": {"url": "/x"}})
    eng = ReplayEngine(FakeSurface(1))
    waits: list[float] = []
    monkeypatch.setattr(eng, "_sleep", lambda seconds, completed: waits.append(round(seconds, 3)))

    assert eng._apply_bounded_recovery(rule, step, {}, []) is True
    assert waits == [0.1, 0.2]


def test_retry_rule_never_reissues_a_non_idempotent_step():
    from artifacts_lib.schema import RecoverableRule, Step

    rule = RecoverableRule.model_validate({"signal": {"type": "text_present", "value": "busy"}, "action": "retry", "max_attempts": 2})
    step = Step.model_validate({"step_id": "s9", "action": "navigate", "params": {"url": "/x"}, "idempotent": False})
    surface = FakeSurface(99)
    eng = ReplayEngine(surface)

    with pytest.raises(Exception) as exc:
        eng._apply_bounded_recovery(rule, step, {}, [])

    assert exc.value.error.code == "ambiguous_commit"
    assert surface.acts == 1  # the first re-issue is detected and stops the loop


def test_checkpoint_poll_waits_for_a_late_signal_and_gives_up_on_a_missing_one():
    late = ReplayEngine(FakeSurface(3, late=True), checkpoint_timeout_s=2)
    assert late._wait_signal(object()) is True

    never = ReplayEngine(FakeSurface(10**9, late=True), checkpoint_timeout_s=0.3)
    started = time.monotonic()
    assert never._wait_signal(object()) is False
    assert 0.25 < time.monotonic() - started < 1.5


# ---- surface settling -------------------------------------------------------------------------

def test_settle_waits_for_a_late_dom_change(page, tmp_path):
    page.set_content("<button onclick=\"setTimeout(() => document.body.append('LATE CONTENT'), 100)\">go</button>")
    from artifacts_lib.schema import ActionType, Locator, LocatorStrategy, Target
    from surface.base import Action

    surface = WebSurface(page, base_url="about:blank", screenshot_dir=tmp_path)
    target = Target(semantic_description="go", locators=[Locator(strategy=LocatorStrategy.ROLE, value="button[name='go']")])
    surface.act(Action(kind=ActionType.CLICK, target=target))

    assert "LATE CONTENT" in page.inner_text("body")  # no wait after act(): settle already covered it


def test_settle_waits_for_an_in_flight_request(page, clinic, tmp_path):
    from artifacts_lib.schema import ActionType, Locator, LocatorStrategy, Target
    from surface.base import Action

    chaos(clinic, "latency", params={"ms": 600}, method="GET", path_glob="/legacy/login")
    page.set_content(
        f"<button onclick=\"fetch('{clinic}/legacy/login', {{mode: 'no-cors'}}).then(() => document.body.append('FETCH DONE'))\">go</button>")
    surface = WebSurface(page, base_url="about:blank", screenshot_dir=tmp_path, settle=SettleConfig(timeout_ms=4000))
    target = Target(semantic_description="go", locators=[Locator(strategy=LocatorStrategy.ROLE, value="button[name='go']")])
    surface.act(Action(kind=ActionType.CLICK, target=target))

    assert "FETCH DONE" in page.inner_text("body")
