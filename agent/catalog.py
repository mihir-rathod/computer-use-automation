"""A small registry of known capability contracts -- what a human decides discovery should
produce (capability_id, typed input/output, success/error signals, target) before the agent
figures out how. See agent/recorder.py's docstring for why this split exists: a human
specifies the contract, the model figures out the implementation.

`_MOCKBANK_ERROR_HANDLING` is shared across capabilities rather than re-discovered per
capability -- MockBank's known failure modes (Phase 2) are curated once, the same way a real
system would maintain a reviewed library of known signatures for a given target app rather than
having an agent reinvent them for every new capability it learns.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from artifacts_lib.schema import (
    BusinessOutcomeRule,
    CapabilityRiskLevel,
    CapabilityTarget,
    ErrorHandling,
    JSONSchemaObject,
    Locator,
    LocatorStrategy,
    Preconditions,
    RecoverableRule,
    RecoveryAction,
    SafetyMeta,
    Signal,
    SignalType,
    SurfaceType,
    Target,
)


@dataclass
class CapabilitySpec:
    capability_id: str
    version: str
    name: str
    description: str
    goal: str
    start_path: str
    target: CapabilityTarget
    input_schema: JSONSchemaObject
    output_schema: JSONSchemaObject
    success_checkpoint: Signal
    error_handling: ErrorHandling
    safety: SafetyMeta
    preconditions: Preconditions | None = None
    success_output_defaults: dict[str, str] = field(default_factory=dict)


def _mockbank_error_handling() -> ErrorHandling:
    return ErrorHandling(
        business_outcomes=[
            BusinessOutcomeRule(signal=Signal(type=SignalType.TEXT_PRESENT, value="No member found"), outcome="not_found"),
            BusinessOutcomeRule(signal=Signal(type=SignalType.TEXT_PRESENT, value="Access denied"), outcome="permission_denied"),
        ],
        recoverable=[
            RecoverableRule(
                signal=Signal(type=SignalType.TEXT_PRESENT, value="Service temporarily unavailable"),
                action=RecoveryAction.RETRY, max_attempts=3, backoff_ms=1000,
            ),
            RecoverableRule(
                signal=Signal(type=SignalType.DIALOG_PRESENT, value="Terms Updated"),
                action=RecoveryAction.DISMISS_AND_CONTINUE,
                recovery_target=Target(
                    semantic_description="dismiss button on the Terms Updated modal",
                    locators=[Locator(strategy=LocatorStrategy.ROLE, value="button[name='Dismiss']")],
                ),
            ),
            RecoverableRule(signal=Signal(type=SignalType.REDIRECTED_TO, value="**/login"), action=RecoveryAction.REAUTHENTICATE_AND_RESUME),
        ],
    )


def _member_balance_lookup_spec(base_url: str) -> CapabilitySpec:
    return CapabilitySpec(
        capability_id="mockbank.member_balance_lookup",
        version="2.0.0",  # 2.x = LLM-discovered generation; distinct from the Phase 3 hand-written 1.x schema fixture
        name="Look up member savings and checking balance",
        description="Searches for a member by ID and reads their savings balance, checking "
                     "balance, and account status. Discovered live by an LLM -- see provenance.",
        goal=(
            "Search for the member with the given member_id and read their account details. "
            "Extract three values using extract(): the savings balance with output_name "
            "'savings_balance', the checking balance with output_name 'checking_balance', and "
            "the account status with output_name 'account_status'. The goal is complete once "
            "all three have been extracted and are visible on screen."
        ),
        start_path="/search",
        target=CapabilityTarget(app_id="mockbank", surface_type=SurfaceType.WEB, base_url=base_url, vendor_product="mockbank-core"),
        input_schema=JSONSchemaObject(properties={"member_id": {"type": "string", "pattern": "^[0-9]{4,10}$"}}, required=["member_id"]),
        output_schema=JSONSchemaObject(properties={
            "status": {"type": "string", "enum": ["found", "not_found", "permission_denied"]},
            "savings_balance": {"type": ["number", "null"]},
            "checking_balance": {"type": ["number", "null"]},
            "account_status": {"type": ["string", "null"]},
        }, required=["status"]),
        success_checkpoint=Signal(type=SignalType.TEXT_PRESENT, value="Account Summary"),
        error_handling=_mockbank_error_handling(),
        safety=SafetyMeta(risk_level=CapabilityRiskLevel.READ_ONLY, requires_confirmation=False),
        preconditions=Preconditions(requires_capability="mockbank.login", note="Assumes an authenticated operator session."),
        success_output_defaults={"status": "found"},
    )


def _meridian_signon_spec(base_url: str) -> CapabilitySpec:
    return CapabilitySpec(
        capability_id="meridian.signon",
        version="1.0.0",
        name="Sign on to MERIDIAN CORE",
        description="Authenticates an operator session against MERIDIAN CORE. Discovered "
                     "unauthenticated, unlike every other MERIDIAN capability -- see "
                     "cmd_discover's login-capability skip in cli.py.",
        goal=(
            "Sign on to the system. The form has two unlabeled text fields with no visible "
            "label association in the element list: the FIRST one (a plain text field) is the "
            "Operator ID -- type the given username into it. The SECOND one (a password-type "
            "field) is the Password -- type the given password into it. Leave the Branch "
            "dropdown at its default selected value; do not change it. Then click the button "
            "labeled 'Sign On'. The goal is complete once the MAIN MENU page is visible."
        ),
        start_path="/signon",
        target=CapabilityTarget(app_id="meridian", surface_type=SurfaceType.WEB, base_url=base_url, vendor_product="meridian-core"),
        input_schema=JSONSchemaObject(properties={"username": {"type": "string"}, "password": {"type": "string"}}, required=["username", "password"]),
        output_schema=JSONSchemaObject(properties={
            "status": {"type": "string", "enum": ["authenticated", "invalid_credentials"]},
        }, required=["status"]),
        success_checkpoint=Signal(type=SignalType.URL_MATCHES, value="**/menu"),
        error_handling=ErrorHandling(
            business_outcomes=[
                BusinessOutcomeRule(signal=Signal(type=SignalType.TEXT_PRESENT, value="Invalid operator ID or password."), outcome="invalid_credentials"),
            ],
        ),
        safety=SafetyMeta(risk_level=CapabilityRiskLevel.STATE_CHANGING, requires_confirmation=False),
        preconditions=None,
        success_output_defaults={"status": "authenticated"},
    )


_CATALOG = {
    "mockbank.member_balance_lookup": _member_balance_lookup_spec,
    "meridian.signon": _meridian_signon_spec,
}


def get_spec(capability_id: str, base_url: str) -> CapabilitySpec:
    factory = _CATALOG.get(capability_id)
    if factory is None:
        raise KeyError(f"unknown capability '{capability_id}' -- known: {sorted(_CATALOG)}")
    return factory(base_url)
