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


def _meridian_error_handling() -> ErrorHandling:
    """MERIDIAN's six named runtime conditions (recon against the live site, not guessed):
    three business outcomes with clean, distinct text; a session timeout that renders inline at
    the *current* URL rather than redirecting (so it's a text signal, not REDIRECTED_TO, unlike
    MockBank's); a maintenance interstitial whose own "Continue" link goes to /menu rather than
    back to the page that triggered it, so RETRY against the original action is the right
    recovery here, not dismiss_and_continue (which assumes landing back on the intended page);
    and a hard application error that offers no continue/retry at all, deliberately left with no
    recoverable rule so it falls through to hard failure/escalation, matching the real UI.

    Two different pairs map to the same outcome, not one signal each -- MERIDIAN renders the
    *injected* condition differently from the equivalent *natural* one in both cases found so
    far. "TRANSACTION REJECTED" is the injected/generic 400 rendering, but a real business-rule
    rejection (found live -- attempting to transfer from a share on HOLD) renders as "The
    transaction could not be validated:" plus a bulleted reason. Likewise "RECORD NOT FOUND" is
    the injected rendering (a direct GET to a member url with ?inject=notfound), but a natural
    zero-result search (found live -- searching a member number that doesn't exist) renders as
    "No member records matched your search." on the search page itself. Worth expecting this
    pattern to repeat for the other two capabilities' natural error text, not assuming the
    injected copy is the only real-world rendering.
    """
    return ErrorHandling(
        business_outcomes=[
            BusinessOutcomeRule(signal=Signal(type=SignalType.TEXT_PRESENT, value="RECORD NOT FOUND"), outcome="not_found"),
            BusinessOutcomeRule(signal=Signal(type=SignalType.TEXT_PRESENT, value="No member records matched your search."), outcome="not_found"),
            BusinessOutcomeRule(signal=Signal(type=SignalType.TEXT_PRESENT, value="SUPERVISOR OVERRIDE REQUIRED"), outcome="permission_denied"),
            BusinessOutcomeRule(signal=Signal(type=SignalType.TEXT_PRESENT, value="TRANSACTION REJECTED"), outcome="validation_error"),
            BusinessOutcomeRule(signal=Signal(type=SignalType.TEXT_PRESENT, value="could not be validated"), outcome="validation_error"),
        ],
        recoverable=[
            RecoverableRule(signal=Signal(type=SignalType.TEXT_PRESENT, value="YOUR SESSION HAS TIMED OUT"), action=RecoveryAction.REAUTHENTICATE_AND_RESUME),
            RecoverableRule(
                signal=Signal(type=SignalType.TEXT_PRESENT, value="SCHEDULED MAINTENANCE IN PROGRESS"),
                action=RecoveryAction.RETRY, max_attempts=3, backoff_ms=1000,
            ),
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


def _meridian_balance_inquiry_spec(base_url: str) -> CapabilitySpec:
    return CapabilitySpec(
        capability_id="meridian.balance_inquiry",
        version="1.0.0",
        name="Look up a member and read their balances",
        description="Covers both 'Member inquiry / selection' and 'Member record / balance' "
                     "from the brief in one flow, mirroring how mockbank.member_balance_lookup "
                     "bundles search+read rather than splitting them into two capabilities.",
        goal=(
            "Search for the member with the given member_id (leave Search by: set to its "
            "default 'Member Number'), then select them from the results to open their member "
            "record. That page has a Name field near the top and a SHARES / BALANCES table "
            "below it, listing every share the member holds -- some members have many rows. "
            "Extract the member's full name with output_name 'member_name'. In the table, find "
            "the FIRST data row (the one directly under the header row) and extract its Share "
            "ID with output_name 'first_share_id' and its Balance with output_name "
            "'first_share_balance'. The goal is complete once all three have been extracted."
        ),
        start_path="/members",
        target=CapabilityTarget(app_id="meridian", surface_type=SurfaceType.WEB, base_url=base_url, vendor_product="meridian-core"),
        input_schema=JSONSchemaObject(properties={"member_id": {"type": "string"}}, required=["member_id"]),
        output_schema=JSONSchemaObject(properties={
            "status": {"type": "string", "enum": ["found", "not_found"]},
            "member_name": {"type": ["string", "null"]},
            "first_share_id": {"type": ["string", "null"]},
            "first_share_balance": {"type": ["number", "null"]},
        }, required=["status"]),
        success_checkpoint=Signal(type=SignalType.TEXT_PRESENT, value="SHARES / BALANCES"),
        error_handling=_meridian_error_handling(),
        safety=SafetyMeta(risk_level=CapabilityRiskLevel.READ_ONLY, requires_confirmation=False),
        preconditions=Preconditions(requires_capability="meridian.signon", note="Assumes an authenticated operator session."),
        success_output_defaults={"status": "found"},
    )


def _meridian_funds_transfer_spec(base_url: str) -> CapabilitySpec:
    return CapabilitySpec(
        capability_id="meridian.funds_transfer",
        version="1.0.0",
        name="Transfer funds between a member's shares",
        description="Moves money from one share to another for a given member, via MERIDIAN's "
                     "entry -> review -> post confirmation flow. The final post step is "
                     "irreversible and gated on human confirmation.",
        goal=(
            "Search for the member with the given member_id (leave Search by: set to its "
            "default 'Member Number'), then select them from the results -- this should land "
            "directly on the Funds Transfer form for that member. Set the From Share dropdown "
            "to the option whose value matches the given from_share, and the To Share dropdown "
            "to the option whose value matches the given to_share. Type the given amount into "
            "the Amount field and the given memo into the Memo field. Click Continue to reach "
            "the review screen. Check the review screen shows the same from/to/amount, then "
            "click whichever button actually finalizes/posts the transfer (do not click Cancel "
            "or go back). The goal is complete once a confirmation of the posted transfer is "
            "visible."
        ),
        start_path="/members?next=transfer",
        target=CapabilityTarget(app_id="meridian", surface_type=SurfaceType.WEB, base_url=base_url, vendor_product="meridian-core"),
        input_schema=JSONSchemaObject(properties={
            "member_id": {"type": "string"},
            "from_share": {"type": "string", "description": "Exact share id, e.g. '100987-S0001'."},
            "to_share": {"type": "string", "description": "Exact share id, e.g. '100987-S0070'."},
            "amount": {"type": "number"},
            "memo": {"type": "string"},
        }, required=["member_id", "from_share", "to_share", "amount"]),
        output_schema=JSONSchemaObject(properties={
            "status": {"type": "string", "enum": ["posted", "not_found", "permission_denied", "validation_error"]},
            "confirmation_number": {"type": ["string", "null"]},
        }, required=["status"]),
        # Provisional -- the real post-confirmation text hasn't been observed yet (recon
        # couldn't safely submit a real transfer). Corrected immediately after the first
        # discovery run's own screenshots show the actual page; discovery itself doesn't
        # consult success_checkpoint, only replay does, so this doesn't block the spike.
        success_checkpoint=Signal(type=SignalType.TEXT_PRESENT, value="Transfer"),
        error_handling=_meridian_error_handling(),
        safety=SafetyMeta(risk_level=CapabilityRiskLevel.STATE_CHANGING, requires_confirmation=True),
        preconditions=Preconditions(requires_capability="meridian.signon", note="Assumes an authenticated operator session."),
        success_output_defaults={"status": "posted"},
    )


_CATALOG = {
    "mockbank.member_balance_lookup": _member_balance_lookup_spec,
    "meridian.signon": _meridian_signon_spec,
    "meridian.balance_inquiry": _meridian_balance_inquiry_spec,
    "meridian.funds_transfer": _meridian_funds_transfer_spec,
}


def get_spec(capability_id: str, base_url: str) -> CapabilitySpec:
    factory = _CATALOG.get(capability_id)
    if factory is None:
        raise KeyError(f"unknown capability '{capability_id}' -- known: {sorted(_CATALOG)}")
    return factory(base_url)
