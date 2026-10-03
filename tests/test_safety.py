"""Unit tests for the safety module -- no browser needed. Covers: the allowlist blocks an out-of-scope
action, and the risk classifier correctly flags a commit-type action.
"""
from __future__ import annotations

import pytest

from artifacts_lib.schema import ActionType, StepRiskLevel
from safety.allowlist import AllowlistConfig, AllowlistPolicy, AllowlistViolation
from safety.policy import SafetyPolicy
from safety.risk import RiskClassifier

CONFIG = AllowlistConfig(
    allowed_base_urls=["http://localhost:8000"],
    allowed_route_patterns=["/login", "/search", "/member/*"],
    allowed_action_types=["navigate", "click", "type", "extract"],
)


def test_allowlist_allows_in_scope_action():
    AllowlistPolicy(CONFIG).check("http://localhost:8000/search", "navigate")  # should not raise


def test_allowlist_blocks_disallowed_base_url():
    with pytest.raises(AllowlistViolation, match="base URL"):
        AllowlistPolicy(CONFIG).check("http://evil.example.com/search", "navigate")


def test_allowlist_blocks_disallowed_route():
    with pytest.raises(AllowlistViolation, match="route pattern"):
        AllowlistPolicy(CONFIG).check("http://localhost:8000/admin", "navigate")


def test_allowlist_blocks_disallowed_action_type():
    with pytest.raises(AllowlistViolation, match="action type"):
        AllowlistPolicy(CONFIG).check("http://localhost:8000/search", "select")


def test_allowlist_route_pattern_covers_subpaths():
    # /member/* should cover the whole member sub-tree (detail, open-subaccount, confirm)
    AllowlistPolicy(CONFIG).check("http://localhost:8000/member/10001/open-subaccount/confirm", "click")


def test_risk_classifier_flags_confirm_submit_button_as_irreversible():
    classifier = RiskClassifier()
    risk = classifier.classify(ActionType.CLICK, semantic_description="Confirm & Open Account", current_path="/member/10001/open-subaccount/confirm")
    assert risk == StepRiskLevel.IRREVERSIBLE


def test_risk_classifier_does_not_flag_navigational_open_subaccount_link():
    # regression test: "Open Sub-Account" is a plain navigational link, not itself irreversible
    classifier = RiskClassifier()
    risk = classifier.classify(ActionType.CLICK, semantic_description="Open Sub-Account", current_path="/member/10001")
    assert risk == StepRiskLevel.SAFE


def test_risk_classifier_treats_extract_and_navigate_as_always_safe():
    classifier = RiskClassifier()
    assert classifier.classify(ActionType.EXTRACT, semantic_description="confirm delete transfer", current_path="/anything") == StepRiskLevel.SAFE
    assert classifier.classify(ActionType.NAVIGATE, semantic_description=None, current_path="/confirm") == StepRiskLevel.SAFE


def test_risk_classifier_safe_for_ordinary_typing():
    classifier = RiskClassifier()
    risk = classifier.classify(ActionType.TYPE, semantic_description="Member ID search input", current_path="/search")
    assert risk == StepRiskLevel.SAFE


def test_safety_policy_blocks_unconfirmed_irreversible_action():
    policy = SafetyPolicy(AllowlistPolicy(CONFIG), require_confirmation_for_irreversible=True)
    decision = policy.evaluate(
        url="http://localhost:8000/member/10001/open-subaccount/confirm",
        action_type=ActionType.CLICK,
        semantic_description="Confirm & Open Account",
        current_path="/member/10001/open-subaccount/confirm",
        confirmed=False,
    )
    assert not decision.allowed
    assert decision.risk_level == StepRiskLevel.IRREVERSIBLE
    assert "confirmation" in decision.reason


def test_safety_policy_allows_confirmed_irreversible_action():
    policy = SafetyPolicy(AllowlistPolicy(CONFIG), require_confirmation_for_irreversible=True)
    decision = policy.evaluate(
        url="http://localhost:8000/member/10001/open-subaccount/confirm",
        action_type=ActionType.CLICK,
        semantic_description="Confirm & Open Account",
        current_path="/member/10001/open-subaccount/confirm",
        confirmed=True,
    )
    assert decision.allowed
    assert decision.risk_level == StepRiskLevel.IRREVERSIBLE


def test_safety_policy_allowlist_violation_takes_priority_over_risk():
    # an out-of-scope URL should be blocked by the allowlist even for a perfectly safe action
    policy = SafetyPolicy(AllowlistPolicy(CONFIG))
    decision = policy.evaluate(
        url="http://evil.example.com/search", action_type=ActionType.NAVIGATE,
        semantic_description=None, current_path="/search", confirmed=False,
    )
    assert not decision.allowed
    assert "allowlist" in decision.reason
