"""Capability contracts for the Larkspur Clinic target (the legacy skin). Same split as catalog.py: a human
fixes what each capability takes, returns and treats as an answer; discovery works out how to drive the UI.

Row-scoped actions (cancel, refund, claim, write-off) start from the main menu's "function entry" pages, which
take a business number (A-xxxxx, INV-xxxxx), because the patient page repeats identical "Refund" links on every
row and a locator cannot be parameterised per row.
"""
from __future__ import annotations

from artifacts_lib.schema import (
    BusinessOutcomeRule,
    CanarySpec,
    CapabilityRiskLevel,
    CapabilityTarget,
    ErrorHandling,
    JSONSchemaObject,
    Preconditions,
    RecoverableRule,
    RecoveryAction,
    SafetyMeta,
    Signal,
    SignalType,
    SurfaceType,
)

# These are imported lazily by agent/catalog.py to avoid a cycle.


def _text(value: str) -> Signal:
    return Signal(type=SignalType.TEXT_PRESENT, value=value)


def clinic_errors(*outcomes: tuple[str, str]) -> ErrorHandling:
    """Shared failure modes for the clinic, plus the business outcomes particular to one capability."""
    return ErrorHandling(
        business_outcomes=[BusinessOutcomeRule(signal=_text(text), outcome=outcome) for text, outcome in outcomes],
        recoverable=[
            RecoverableRule(signal=_text("APPLICATION ERROR"), action=RecoveryAction.RETRY, max_attempts=2, backoff_ms=300, backoff_multiplier=2.0),
            RecoverableRule(signal=_text("YOUR SESSION HAS TIMED OUT"), action=RecoveryAction.REAUTHENTICATE_AND_RESUME),
        ],
    )


NOT_FOUND = ("RECORD NOT FOUND", "not_found")
VALIDATION = ("Please correct the following", "validation_error")
DENIED = ("SUPERVISOR AUTHORIZATION REQUIRED", "permission_denied")


def _schema(properties: dict[str, dict], required: list[str]) -> JSONSchemaObject:
    return JSONSchemaObject(properties=properties, required=required)


def _str(**extra) -> dict:
    return {"type": "string", **extra}


def _out(statuses: list[str], **fields: dict) -> JSONSchemaObject:
    return _schema({"status": {"type": "string", "enum": statuses}, **fields}, ["status"])


def _spec(factory_args: dict, base_url: str):
    from agent.catalog import CapabilitySpec

    return CapabilitySpec(
        target=CapabilityTarget(app_id="clinic", surface_type=SurfaceType.LEGACY_WEB, base_url=base_url, vendor_product="larkspur-clinic-ops"),
        preconditions=factory_args.pop("preconditions", Preconditions(requires_capability="clinic.login", note="Assumes a signed-on operator session.")),
        **factory_args,
    )


def clinic_login(base_url: str):
    return _spec(dict(
        capability_id="clinic.login", version="1.0.0", name="Sign on to the clinic system",
        description="Signs on with a user ID and password.",
        goal="Sign on to the clinic system with the given username and password, then confirm the main menu is showing.",
        start_path="/legacy/login",
        input_schema=_schema({"username": _str(), "password": _str()}, ["username", "password"]),
        output_schema=_out(["signed_in", "invalid_credentials"]),
        success_checkpoint=_text("Main menu"),
        error_handling=ErrorHandling(business_outcomes=[BusinessOutcomeRule(signal=_text("Invalid user ID or password"), outcome="invalid_credentials")]),
        safety=SafetyMeta(risk_level=CapabilityRiskLevel.READ_ONLY, requires_confirmation=False),
        success_output_defaults={"status": "signed_in"}, preconditions=None,
    ), base_url)


def patient_lookup(base_url: str):
    return _spec(dict(
        capability_id="clinic.patient_lookup", version="1.0.0", name="Look up a patient by MRN",
        description="Finds a patient by medical record number and returns their demographics and balance due.",
        goal=("Search for the patient with the given MRN, open their record with the Select link, then extract the patient's name, "
              "date of birth, phone, email and balance due."),
        start_path="/legacy/patients",
        input_schema=_schema({"mrn": _str(pattern=r"^LK-[0-9]{6}$")}, ["mrn"]),
        output_schema=_out(["found", "not_found"], patient_name={"type": ["string", "null"]}, date_of_birth={"type": ["string", "null"]},
                           phone={"type": ["string", "null"]}, email={"type": ["string", "null"]}, balance_due={"type": ["string", "null"]}),
        success_checkpoint=_text("Patient record"),
        error_handling=clinic_errors(("No patients matched your search", "not_found")),
        safety=SafetyMeta(risk_level=CapabilityRiskLevel.READ_ONLY, requires_confirmation=False),
        success_output_defaults={"status": "found"},
        canary=CanarySpec(params={"mrn": "LK-100001"}, expect={"status": "found", "patient_name": "Brennan, Avery"}),
    ), base_url)


def update_patient_contact(base_url: str):
    return _spec(dict(
        capability_id="clinic.update_patient_contact", version="1.0.0", name="Update a patient's contact details",
        description="Replaces a patient's phone, email and address.",
        goal=("Search for the patient with the given MRN, open their record with the Select link, follow Update contact, replace the phone, "
              "email and address with the given values and press Save changes. Finish when CONTACT INFORMATION UPDATED shows."),
        start_path="/legacy/patients",
        input_schema=_schema({"mrn": _str(pattern=r"^LK-[0-9]{6}$"), "phone": _str(), "email": _str(), "address": _str()}, ["mrn", "phone", "email", "address"]),
        output_schema=_out(["updated", "not_found", "validation_error"]),
        success_checkpoint=_text("CONTACT INFORMATION UPDATED"),
        error_handling=clinic_errors(("No patients matched your search", "not_found"), VALIDATION),
        safety=SafetyMeta(risk_level=CapabilityRiskLevel.STATE_CHANGING, requires_confirmation=False),
        success_output_defaults={"status": "updated"},
    ), base_url)


def reschedule_appointment(base_url: str):
    return _spec(dict(
        capability_id="clinic.reschedule_appointment", version="1.0.0", name="Reschedule an appointment",
        description="Moves a scheduled appointment to a new date and time.",
        goal=("From the Reschedule function page enter the appointment number and press Continue, enter the new date and press Continue, "
              "choose the new time from the list and press Save changes. Finish when APPOINTMENT RESCHEDULED shows."),
        start_path="/legacy/fn/reschedule",
        input_schema=_schema({"appointment": _str(pattern=r"^A-[0-9]{5}$"), "date": _str(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), "time": _str(pattern=r"^[0-9]{2}:[0-9]{2}$")},
                             ["appointment", "date", "time"]),
        output_schema=_out(["rescheduled", "not_found", "validation_error"]),
        success_checkpoint=_text("APPOINTMENT RESCHEDULED"),
        error_handling=clinic_errors(NOT_FOUND, VALIDATION),
        safety=SafetyMeta(risk_level=CapabilityRiskLevel.STATE_CHANGING, requires_confirmation=False),
        success_output_defaults={"status": "rescheduled"},
    ), base_url)


def cancel_appointment(base_url: str):
    return _spec(dict(
        capability_id="clinic.cancel_appointment", version="1.0.0", name="Cancel an appointment",
        description="Cancels a scheduled appointment and returns the receipt number.",
        goal=("From the Cancel appointment function page enter the appointment number and press Continue, choose the cancellation reason and press "
              "Continue to reach the review page, then press Confirm. On the receipt, extract the receipt number."),
        start_path="/legacy/fn/cancel",
        input_schema=_schema({"appointment": _str(pattern=r"^A-[0-9]{5}$"), "reason": _str(enum=["patient_request", "provider_unavailable", "weather", "duplicate_booking"])},
                             ["appointment", "reason"]),
        output_schema=_out(["cancelled", "not_found", "validation_error"], receipt_number={"type": ["string", "null"]}),
        success_checkpoint=_text("APPOINTMENT CANCELLED"),
        error_handling=clinic_errors(NOT_FOUND, VALIDATION),
        safety=SafetyMeta(risk_level=CapabilityRiskLevel.STATE_CHANGING, requires_confirmation=True),
        success_output_defaults={"status": "cancelled"},
    ), base_url)


def issue_refund(base_url: str):
    return _spec(dict(
        capability_id="clinic.issue_refund", version="1.0.0", name="Issue a refund on an invoice",
        description="Refunds part or all of a paid invoice. Above the clinic's refund cap the refund is queued for supervisor approval instead.",
        goal=("From the Issue refund function page enter the invoice number and press Continue, enter the amount, choose the reason and press Continue "
              "to reach the review page, then press Confirm. On the receipt, extract the receipt number."),
        start_path="/legacy/fn/refund",
        input_schema=_schema({"invoice": _str(pattern=r"^INV-[0-9]{5}$"), "amount": _str(pattern=r"^[0-9]+(\.[0-9]{2})?$"),
                              "reason": _str(enum=["duplicate_payment", "service_not_rendered", "billing_error", "insurance_overpayment"])},
                             ["invoice", "amount", "reason"]),
        output_schema=_out(["issued", "pending_supervisor_approval", "not_found", "validation_error"], receipt_number={"type": ["string", "null"]}),
        success_checkpoint=_text("Receipt number"),
        error_handling=clinic_errors(("REFUND SENT FOR SUPERVISOR APPROVAL", "pending_supervisor_approval"), NOT_FOUND, VALIDATION),
        safety=SafetyMeta(risk_level=CapabilityRiskLevel.STATE_CHANGING, requires_confirmation=True),
        success_output_defaults={"status": "issued"},
    ), base_url)


def submit_claim(base_url: str):
    return _spec(dict(
        capability_id="clinic.submit_claim", version="1.0.0", name="Submit an insurance claim",
        description="Submits an insurance claim for an invoice's outstanding balance.",
        goal=("From the Submit claim function page enter the invoice number and press Continue, then type the given claim amount into the Amount "
              "field (it arrives prefilled; replace it) and press Continue to reach the review page, then press Confirm. On the receipt, extract the receipt number."),
        start_path="/legacy/fn/claim",
        input_schema=_schema({"invoice": _str(pattern=r"^INV-[0-9]{5}$"), "amount": _str(pattern=r"^[0-9]+(\.[0-9]{2})?$")}, ["invoice", "amount"]),
        output_schema=_out(["submitted", "not_found", "validation_error"], receipt_number={"type": ["string", "null"]}),
        success_checkpoint=_text("CLAIM SUBMITTED"),
        error_handling=clinic_errors(NOT_FOUND, VALIDATION),
        safety=SafetyMeta(risk_level=CapabilityRiskLevel.STATE_CHANGING, requires_confirmation=True),
        success_output_defaults={"status": "submitted"},
    ), base_url)


def write_off_balance(base_url: str):
    return _spec(dict(
        capability_id="clinic.write_off_balance", version="1.0.0", name="Write off an invoice balance",
        description="Writes off part or all of an invoice's outstanding balance. Needs a supervisor sign-on.",
        goal=("From the Write off function page enter the invoice number and press Continue, enter the amount, choose the reason and press Continue "
              "to reach the review page, then press Confirm. On the receipt, extract the receipt number."),
        start_path="/legacy/fn/writeoff",
        input_schema=_schema({"invoice": _str(pattern=r"^INV-[0-9]{5}$"), "amount": _str(pattern=r"^[0-9]+(\.[0-9]{2})?$"),
                              "reason": _str(enum=["uncollectible", "hardship", "billing_error"])}, ["invoice", "amount", "reason"]),
        output_schema=_out(["written_off", "not_found", "validation_error", "permission_denied"], receipt_number={"type": ["string", "null"]}),
        success_checkpoint=_text("BALANCE WRITTEN OFF"),
        error_handling=clinic_errors(DENIED, NOT_FOUND, VALIDATION),
        safety=SafetyMeta(risk_level=CapabilityRiskLevel.STATE_CHANGING, requires_confirmation=True),
        success_output_defaults={"status": "written_off"},
    ), base_url)


CLINIC_CATALOG = {
    "clinic.login": clinic_login,
    "clinic.patient_lookup": patient_lookup,
    "clinic.update_patient_contact": update_patient_contact,
    "clinic.reschedule_appointment": reschedule_appointment,
    "clinic.cancel_appointment": cancel_appointment,
    "clinic.issue_refund": issue_refund,
    "clinic.submit_claim": submit_claim,
    "clinic.write_off_balance": write_off_balance,
}
