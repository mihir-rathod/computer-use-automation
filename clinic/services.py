"""The clinic's business rules. Both skins and the JSON API call this one layer, so a rule can't
differ between them and every attempt is audited in exactly one place.

State-changing work that can't be undone follows review -> confirm -> receipt. `review_*` validates
and stores a *draft* identified by a single-use token; `confirm()` executes the draft. A second
confirm with the same token is blocked (and audited) while the duplicate guard is on; the test kit
can switch the guard off to prove the platform does not rely on the target to prevent double-posts.
"""
from __future__ import annotations

import csv
import io
import json
import re
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any

from clinic.audit import Actor, AuditLog
from clinic.clock import Clock
from clinic.db import Database
from clinic.errors import AlreadyProcessed, NotFound, PermissionDenied, ValidationFailed
from clinic.format import fmt_dt, money
from clinic.seed import PROVIDERS, SLOT_STARTS
from clinic.settings import Settings

CANCEL_REASONS = {
    "patient_request": "Patient request", "provider_unavailable": "Provider unavailable",
    "weather": "Weather", "duplicate_booking": "Duplicate booking", "other": "Other (notes required)",
}
REFUND_REASONS = {
    "duplicate_payment": "Duplicate payment", "service_not_rendered": "Service not rendered",
    "billing_error": "Billing error", "insurance_overpayment": "Insurance overpayment",
    "other": "Other (notes required)",
}
WRITEOFF_REASONS = {
    "uncollectible": "Uncollectible", "hardship": "Financial hardship", "billing_error": "Billing error",
    "other": "Other (notes required)",
}
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class Clinic:
    def __init__(
        self, db: Database, clock: Clock, audit: AuditLog, settings: Settings,
        duplicate_guard: Callable[[], bool] = lambda: True,
    ):
        self.db, self.clock, self.audit, self.settings = db, clock, audit, settings
        self.duplicate_guard = duplicate_guard

    # ---- helpers ------------------------------------------------------------------------

    @contextmanager
    def _attempt(self, actor: Actor, action: str, entity_type: str, entity_id: str) -> Iterator[None]:
        try:
            yield
        except NotFound as exc:
            self.audit.record(actor, action, entity_type, entity_id, "not_found", False, {"message": exc.message})
            raise
        except PermissionDenied as exc:
            self.audit.record(actor, action, entity_type, entity_id, "denied", False, {"message": exc.message})
            raise
        except AlreadyProcessed as exc:
            self.audit.record(actor, action, entity_type, entity_id, "duplicate_blocked", False, {"message": exc.message})
            raise
        except ValidationFailed as exc:
            self.audit.record(actor, action, entity_type, entity_id, "rejected", False, {"problems": exc.problems})
            raise

    def _patient(self, patient_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM patients WHERE id=?", (patient_id,))
        if row is None:
            raise NotFound("No such patient.")
        return row

    def _appointment(self, number: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM appointments WHERE number=?", ((number or "").strip().upper(),))
        if row is None:
            raise NotFound(f"Appointment {number!r} was not found.")
        return row

    def _invoice(self, number: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM invoices WHERE number=?", ((number or "").strip().upper(),))
        if row is None:
            raise NotFound(f"Invoice {number!r} was not found.")
        return row

    def _refund_committed(self, invoice_id: int) -> int:
        return self.db.scalar(
            "SELECT COALESCE(SUM(amount_cents),0) FROM refunds WHERE invoice_id=? AND status IN ('issued','pending_approval')",
            (invoice_id,),
        )

    def _invoice_view(self, row: dict[str, Any]) -> dict[str, Any]:
        balance = row["amount_cents"] - row["paid_cents"] + row["refunded_cents"] - row["writeoff_cents"]
        refundable = max(0, row["paid_cents"] - self._refund_committed(row["id"]))
        return {**row, "balance_cents": balance, "refundable_cents": refundable}

    def _is_future(self, starts_at: str) -> bool:
        return datetime.fromisoformat(starts_at) > self.clock.now()

    def _page(self, total: int, page: int) -> tuple[int, int, int]:
        size = self.settings.page_size
        pages = max(1, -(-total // size))
        page = min(max(1, page), pages)
        return page, pages, (page - 1) * size

    # ---- patients -----------------------------------------------------------------------

    def search_patients(self, actor: Actor, mrn: str = "", last_name: str = "", dob: str = "", page: int = 1) -> dict[str, Any]:
        mrn, last_name, dob = (mrn or "").strip(), (last_name or "").strip(), (dob or "").strip()
        with self._attempt(actor, "patient.search", "patient", mrn or last_name or dob or "-"):
            problems = []
            if not (mrn or last_name or dob):
                problems.append("Enter at least one search value.")
            if dob:
                try:
                    date.fromisoformat(dob)
                except ValueError:
                    problems.append("Date of birth must be in YYYY-MM-DD format.")
            if problems:
                raise ValidationFailed(problems)
            where = (
                "(?='' OR UPPER(mrn)=UPPER(?)) AND (?='' OR UPPER(last_name) LIKE UPPER(?)||'%') AND (?='' OR dob=?)"
            )
            params = (mrn, mrn, last_name, last_name, dob, dob)
            total = self.db.scalar(f"SELECT COUNT(*) FROM patients WHERE {where}", params)
            page, pages, offset = self._page(total, page)
            items = self.db.query(
                f"SELECT * FROM patients WHERE {where} ORDER BY last_name, first_name, id LIMIT ? OFFSET ?",
                (*params, self.settings.page_size, offset),
            )
        self.audit.record(actor, "patient.search", "patient", mrn or last_name or dob, detail={"total": total})
        return {"items": items, "total": total, "page": page, "pages": pages}

    def patient_by_mrn(self, mrn: str) -> dict[str, Any] | None:
        return self.db.one("SELECT * FROM patients WHERE UPPER(mrn)=UPPER(?)", ((mrn or "").strip(),))

    def patient_detail(self, actor: Actor, patient_id: int, invoice_page: int = 1) -> dict[str, Any]:
        patient = self._patient(patient_id)
        appointments = self.db.query(
            "SELECT * FROM appointments WHERE patient_id=? ORDER BY starts_at", (patient_id,))
        for a in appointments:
            a["can_modify"] = a["status"] == "scheduled" and self._is_future(a["starts_at"])
        total = self.db.scalar("SELECT COUNT(*) FROM invoices WHERE patient_id=?", (patient_id,))
        page, pages, offset = self._page(total, invoice_page)
        invoices = [self._invoice_view(r) for r in self.db.query(
            "SELECT * FROM invoices WHERE patient_id=? ORDER BY created_on DESC, id DESC LIMIT ? OFFSET ?",
            (patient_id, self.settings.page_size, offset))]
        balance = sum(self._invoice_view(r)["balance_cents"] for r in self.db.query(
            "SELECT * FROM invoices WHERE patient_id=?", (patient_id,)))
        claims = self.db.query(
            "SELECT c.*, i.number AS invoice_number FROM claims c JOIN invoices i ON i.id=c.invoice_id"
            " WHERE c.patient_id=? ORDER BY c.id DESC", (patient_id,))
        refunds = self.db.query(
            "SELECT r.*, i.number AS invoice_number FROM refunds r JOIN invoices i ON i.id=r.invoice_id"
            " WHERE r.patient_id=? ORDER BY r.id DESC", (patient_id,))
        self.audit.record(actor, "patient.view", "patient", patient["mrn"])
        return {
            "patient": patient, "appointments": appointments, "balance_cents": balance, "claims": claims,
            "refunds": refunds, "invoices": {"items": invoices, "total": total, "page": page, "pages": pages},
        }

    def update_contact(self, actor: Actor, patient_id: int, phone: str, email: str, address: str) -> dict[str, Any]:
        patient = self._patient(patient_id)
        with self._attempt(actor, "patient.update_contact", "patient", patient["mrn"]):
            problems = []
            digits = re.sub(r"\D", "", phone or "")
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            if len(digits) != 10:
                problems.append("Phone number is not valid.")
            email = (email or "").strip()
            if not _EMAIL.fullmatch(email):
                problems.append("Email address is not valid.")
            address = (address or "").strip()
            if not address or len(address) > 120:
                problems.append("Address is required (120 characters maximum).")
            if problems:
                raise ValidationFailed(problems)
            new = {"phone": f"({digits[:3]}) {digits[3:6]}-{digits[6:]}", "email": email, "address": address}
            changes = {k: {"from": patient[k], "to": v} for k, v in new.items() if patient[k] != v}
            with self.db.transaction():
                if changes:
                    self.db.execute(
                        "UPDATE patients SET phone=?, email=?, address=? WHERE id=?",
                        (new["phone"], new["email"], new["address"], patient_id))
                self.audit.record(actor, "patient.update_contact", "patient", patient["mrn"],
                                  effect=bool(changes), detail={"changes": changes})
        return {"patient": self._patient(patient_id), "changed": sorted(changes)}

    # ---- appointments -------------------------------------------------------------------

    def _free_slots(self, appt: dict[str, Any], day_iso: str) -> list[str]:
        try:
            day = date.fromisoformat((day_iso or "").strip())
        except ValueError:
            raise ValidationFailed("Date must be in YYYY-MM-DD format.") from None
        if day.weekday() >= 5:
            raise ValidationFailed("The clinic is closed on weekends.")
        if day < self.clock.today():
            raise ValidationFailed("That date is in the past.")
        taken = {r["starts_at"] for r in self.db.query(
            "SELECT starts_at FROM appointments WHERE provider=? AND status='scheduled' AND id<>? AND substr(starts_at,1,10)=?",
            (appt["provider"], appt["id"], day.isoformat()))}
        free = []
        for slot in SLOT_STARTS:
            start = datetime.combine(day, slot)
            if start > self.clock.now() and start.isoformat(timespec="minutes") not in taken:
                free.append(slot.strftime("%H:%M"))
        return free

    def available_slots(self, number: str, day_iso: str) -> list[str]:
        return self._free_slots(self._appointment(number), day_iso)

    def reschedule(self, actor: Actor, number: str, day_iso: str, slot: str) -> dict[str, Any]:
        with self._attempt(actor, "appointment.reschedule", "appointment", (number or "").strip().upper()):
            appt = self._appointment(number)
            if appt["status"] != "scheduled" or not self._is_future(appt["starts_at"]):
                raise ValidationFailed(f"Appointment {appt['number']} can no longer be rescheduled.")
            if (slot or "").strip() not in self._free_slots(appt, day_iso):
                raise ValidationFailed(f"That time is not available for {appt['provider']}.")
            starts_at = f"{day_iso.strip()}T{slot.strip()}"
            with self.db.transaction():
                self.db.execute("UPDATE appointments SET starts_at=? WHERE id=?", (starts_at, appt["id"]))
                self.audit.record(actor, "appointment.reschedule", "appointment", appt["number"], effect=True,
                                  detail={"from": appt["starts_at"], "to": starts_at})
        return {"appointment": self._appointment(number)}

    def appointment_info(self, number: str) -> dict[str, Any]:
        appt = self._appointment(number)
        patient = self._patient(appt["patient_id"])
        return {**appt, "patient": patient,
                "can_modify": appt["status"] == "scheduled" and self._is_future(appt["starts_at"])}

    def invoice_info(self, number: str) -> dict[str, Any]:
        invoice = self._invoice_view(self._invoice(number))
        return {**invoice, "patient": self._patient(invoice["patient_id"])}

    # ---- drafts: review -> confirm -> receipt -------------------------------------------

    def _new_draft(self, actor: Actor, kind: str, entity_type: str, subject: str,
                   payload: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
        token = secrets.token_hex(12)
        review = {"kind": kind, "subject": subject, **review}
        self.db.execute(
            "INSERT INTO drafts(token,kind,username,subject,payload,review,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (token, kind, actor.username, subject, json.dumps(payload), json.dumps(review), "open", time.time()))
        self.audit.record(actor, f"{kind}.review", entity_type, subject, detail={"requires_approval": review.get("requires_approval", False)})
        return {"token": token, "review": review}

    def _require_reason(self, code: str, notes: str, allowed: dict[str, str]) -> list[str]:
        problems = []
        if code not in allowed:
            problems.append("Select a reason.")
        elif code == "other" and not (notes or "").strip():
            problems.append("Notes are required when the reason is Other.")
        return problems

    def review_cancel(self, actor: Actor, number: str, reason: str, notes: str = "") -> dict[str, Any]:
        with self._attempt(actor, "cancel.review", "appointment", (number or "").strip().upper()):
            appt = self._appointment(number)
            patient = self._patient(appt["patient_id"])
            problems = self._require_reason(reason, notes, CANCEL_REASONS)
            if appt["status"] != "scheduled":
                problems.append(f"Appointment {appt['number']} is already {appt['status']}.")
            elif not self._is_future(appt["starts_at"]):
                problems.append("Appointment has already started or passed.")
            if problems:
                raise ValidationFailed(problems)
            hours = (datetime.fromisoformat(appt["starts_at"]) - self.clock.now()).total_seconds() / 3600
            fee = self.settings.late_cancel_fee_cents if hours < self.settings.late_cancel_window_hours else 0
            lines = [
                {"label": "Patient", "value": f"{patient['last_name']}, {patient['first_name']} ({patient['mrn']})"},
                {"label": "Appointment", "value": f"{appt['number']} - {fmt_dt(appt['starts_at'])}"},
                {"label": "Provider", "value": appt["provider"]},
                {"label": "Reason", "value": CANCEL_REASONS[reason]},
                {"label": "Late cancellation fee", "value": money(fee)},
            ]
            warnings = [f"Cancelling within {self.settings.late_cancel_window_hours} hours adds a {money(fee)} fee."] if fee else []
            return self._new_draft(
                actor, "cancel", "appointment", appt["number"],
                {"appt_id": appt["id"], "reason": reason, "notes": notes, "fee_cents": fee},
                {"title": "Confirm appointment cancellation", "lines": lines, "warnings": warnings,
                 "irreversible": True})

    def review_claim(self, actor: Actor, number: str, amount_cents: int) -> dict[str, Any]:
        with self._attempt(actor, "claim.review", "invoice", (number or "").strip().upper()):
            invoice = self._invoice_view(self._invoice(number))
            patient = self._patient(invoice["patient_id"])
            problems = []
            if not patient["insurer"]:
                problems.append("Patient has no insurance on file - claims cannot be submitted.")
            if invoice["claimed"]:
                problems.append(f"Invoice {invoice['number']} already has a submitted claim.")
            if invoice["balance_cents"] <= 0:
                problems.append("Invoice has no outstanding balance.")
            elif not 0 < amount_cents <= invoice["balance_cents"]:
                problems.append(f"Claim amount must be between $0.01 and {money(invoice['balance_cents'])}.")
            if problems:
                raise ValidationFailed(problems)
            lines = [
                {"label": "Patient", "value": f"{patient['last_name']}, {patient['first_name']} ({patient['mrn']})"},
                {"label": "Invoice", "value": f"{invoice['number']} - {invoice['description']}"},
                {"label": "Payer", "value": patient["insurer"]},
                {"label": "Claim amount", "value": money(amount_cents)},
            ]
            return self._new_draft(
                actor, "claim", "invoice", invoice["number"],
                {"invoice_id": invoice["id"], "amount_cents": amount_cents},
                {"title": "Confirm insurance claim submission", "lines": lines, "warnings": [], "irreversible": True})

    def review_refund(self, actor: Actor, number: str, amount_cents: int, reason: str, notes: str = "") -> dict[str, Any]:
        with self._attempt(actor, "refund.review", "invoice", (number or "").strip().upper()):
            invoice = self._invoice_view(self._invoice(number))
            patient = self._patient(invoice["patient_id"])
            problems = self._require_reason(reason, notes, REFUND_REASONS)
            if invoice["refundable_cents"] <= 0:
                problems.append("Nothing on this invoice can be refunded.")
            elif not 0 < amount_cents <= invoice["refundable_cents"]:
                problems.append(f"Refund amount must be between $0.01 and {money(invoice['refundable_cents'])}.")
            if problems:
                raise ValidationFailed(problems)
            needs_approval = amount_cents > self.settings.refund_cap_cents and actor.role != "supervisor"
            lines = [
                {"label": "Patient", "value": f"{patient['last_name']}, {patient['first_name']} ({patient['mrn']})"},
                {"label": "Invoice", "value": f"{invoice['number']} - {invoice['description']}"},
                {"label": "Refund amount", "value": money(amount_cents)},
                {"label": "Reason", "value": REFUND_REASONS[reason]},
            ]
            warnings = [
                f"Refunds above {money(self.settings.refund_cap_cents)} need supervisor approval. "
                "Confirming will send this for approval; no money moves yet."
            ] if needs_approval else []
            return self._new_draft(
                actor, "refund", "invoice", invoice["number"],
                {"invoice_id": invoice["id"], "amount_cents": amount_cents, "reason": reason, "notes": notes},
                {"title": "Confirm refund", "lines": lines, "warnings": warnings, "irreversible": True,
                 "requires_approval": needs_approval})

    def review_writeoff(self, actor: Actor, number: str, amount_cents: int, reason: str, notes: str = "") -> dict[str, Any]:
        with self._attempt(actor, "writeoff.review", "invoice", (number or "").strip().upper()):
            invoice = self._invoice_view(self._invoice(number))
            patient = self._patient(invoice["patient_id"])
            problems = self._require_reason(reason, notes, WRITEOFF_REASONS)
            if invoice["balance_cents"] <= 0:
                problems.append("Invoice has no outstanding balance to write off.")
            elif not 0 < amount_cents <= invoice["balance_cents"]:
                problems.append(f"Write-off amount must be between $0.01 and {money(invoice['balance_cents'])}.")
            if problems:
                raise ValidationFailed(problems)
            lines = [
                {"label": "Patient", "value": f"{patient['last_name']}, {patient['first_name']} ({patient['mrn']})"},
                {"label": "Invoice", "value": f"{invoice['number']} - {invoice['description']}"},
                {"label": "Write-off amount", "value": money(amount_cents)},
                {"label": "Reason", "value": WRITEOFF_REASONS[reason]},
            ]
            return self._new_draft(
                actor, "writeoff", "invoice", invoice["number"],
                {"invoice_id": invoice["id"], "amount_cents": amount_cents, "reason": reason, "notes": notes},
                {"title": "Confirm balance write-off", "lines": lines,
                 "warnings": ["Write-offs require supervisor authorization."], "irreversible": True})

    def discard(self, actor: Actor, token: str) -> None:
        draft = self.db.one("SELECT * FROM drafts WHERE token=?", (token,))
        if draft is None or draft["username"] != actor.username:
            raise NotFound("Unknown transaction.")
        if draft["status"] == "open":
            self.db.execute("UPDATE drafts SET status='discarded' WHERE token=?", (token,))
            self.audit.record(actor, f"{draft['kind']}.discard", "draft", draft["subject"])

    def confirm(self, actor: Actor, token: str) -> dict[str, Any]:
        with self.db.lock:
            draft = self.db.one("SELECT * FROM drafts WHERE token=?", ((token or "").strip(),))
            if draft is None:
                self.audit.record(actor, "transaction.confirm", "draft", "-", "not_found", False, {})
                raise NotFound("This transaction is unknown or has expired.")
            kind = draft["kind"]
            with self._attempt(actor, f"{kind}.confirm", "draft", draft["subject"]):
                if draft["username"] != actor.username:
                    raise PermissionDenied("This transaction belongs to another operator.")
                if draft["status"] == "discarded":
                    raise ValidationFailed("This transaction was cancelled.")
                if draft["status"] == "consumed" and self.duplicate_guard():
                    raise AlreadyProcessed("This transaction was already processed.", json.loads(draft["result"]))
                if draft["status"] == "open" and time.time() - draft["created_at"] > self.settings.draft_ttl_seconds:
                    self.db.execute("UPDATE drafts SET status='expired' WHERE token=?", (token,))
                    raise ValidationFailed("This transaction has expired. Please start again.")
                if draft["status"] == "expired":
                    raise ValidationFailed("This transaction has expired. Please start again.")
                payload = json.loads(draft["payload"])
                with self.db.transaction():
                    result = getattr(self, f"_exec_{kind}")(actor, payload)
                    self.db.execute("UPDATE drafts SET status='consumed', result=? WHERE token=?", (json.dumps(result), token))
                return result

    # ---- executors ----------------------------------------------------------------------

    def _exec_cancel(self, actor: Actor, p: dict[str, Any]) -> dict[str, Any]:
        appt = self.db.one("SELECT * FROM appointments WHERE id=?", (p["appt_id"],))
        if appt["status"] != "scheduled":
            raise ValidationFailed(f"Appointment {appt['number']} is already {appt['status']}.")
        self.db.execute("UPDATE appointments SET status='cancelled', cancel_reason=? WHERE id=?", (p["reason"], appt["id"]))
        fee_invoice = None
        if p["fee_cents"]:
            fee_invoice = self.db.next_number("INV", 30001)
            self.db.execute(
                "INSERT INTO invoices(number,patient_id,appointment_id,description,amount_cents,paid_cents,created_on)"
                " VALUES(?,?,?,?,?,0,?)",
                (fee_invoice, appt["patient_id"], appt["id"], f"Late cancellation fee ({appt['number']})",
                 p["fee_cents"], self.clock.today().isoformat()))
        receipt = self.db.next_number("CXL", 1, 6)
        self.audit.record(actor, "appointment.cancel", "appointment", appt["number"], effect=True,
                          detail={"receipt": receipt, "fee_cents": p["fee_cents"], "fee_invoice": fee_invoice,
                                  "reason": p["reason"]})
        lines = [{"label": "Appointment", "value": appt["number"]}, {"label": "Status", "value": "Cancelled"}]
        if fee_invoice:
            lines.append({"label": "Fee invoice", "value": f"{fee_invoice} ({money(p['fee_cents'])})"})
        return {"kind": "cancel", "status": "completed", "receipt_number": receipt,
                "message": "APPOINTMENT CANCELLED", "lines": lines}

    def _exec_claim(self, actor: Actor, p: dict[str, Any]) -> dict[str, Any]:
        invoice = self.db.one("SELECT * FROM invoices WHERE id=?", (p["invoice_id"],))
        if invoice["claimed"]:
            raise ValidationFailed(f"Invoice {invoice['number']} already has a submitted claim.")
        patient = self._patient(invoice["patient_id"])
        number = self.db.next_number("CLM", 1, 6)
        self.db.execute(
            "INSERT INTO claims(number,invoice_id,patient_id,payer,amount_cents,status,submitted_by,submitted_on)"
            " VALUES(?,?,?,?,?,'submitted',?,?)",
            (number, invoice["id"], patient["id"], patient["insurer"], p["amount_cents"], actor.username,
             self.clock.today().isoformat()))
        self.db.execute("UPDATE invoices SET claimed=1 WHERE id=?", (invoice["id"],))
        self.audit.record(actor, "claim.submit", "invoice", invoice["number"], effect=True,
                          detail={"claim": number, "amount_cents": p["amount_cents"], "payer": patient["insurer"]})
        return {"kind": "claim", "status": "completed", "receipt_number": number, "message": "CLAIM SUBMITTED",
                "lines": [{"label": "Invoice", "value": invoice["number"]}, {"label": "Payer", "value": patient["insurer"]},
                          {"label": "Amount", "value": money(p["amount_cents"])}]}

    def _exec_refund(self, actor: Actor, p: dict[str, Any]) -> dict[str, Any]:
        invoice = self.db.one("SELECT * FROM invoices WHERE id=?", (p["invoice_id"],))
        view = self._invoice_view(invoice)
        if p["amount_cents"] > view["refundable_cents"]:
            raise ValidationFailed(f"Refund exceeds the refundable amount ({money(view['refundable_cents'])}).")
        needs_approval = p["amount_cents"] > self.settings.refund_cap_cents and actor.role != "supervisor"
        number = self.db.next_number("RFD", 1, 6)
        base = (number, invoice["id"], invoice["patient_id"], p["amount_cents"], p["reason"], p.get("notes") or "")
        if needs_approval:
            refund_id = self.db.execute(
                "INSERT INTO refunds(number,invoice_id,patient_id,amount_cents,reason_code,notes,status,requested_by,created_on)"
                " VALUES(?,?,?,?,?,?,'pending_approval',?,?)",
                (*base, actor.username, self.clock.today().isoformat()))
            approval = self.db.next_number("APR", 1, 6)
            self.db.execute(
                "INSERT INTO approvals(number,refund_id,requested_by,status,created_on) VALUES(?,?,?,'pending',?)",
                (approval, refund_id, actor.username, self.clock.today().isoformat()))
            self.audit.record(actor, "refund.request_approval", "invoice", invoice["number"], effect=True,
                              detail={"refund": number, "approval": approval, "amount_cents": p["amount_cents"]})
            return {"kind": "refund", "status": "pending_approval", "receipt_number": approval,
                    "message": "REFUND SENT FOR SUPERVISOR APPROVAL",
                    "lines": [{"label": "Refund", "value": number}, {"label": "Amount", "value": money(p["amount_cents"])},
                              {"label": "Approval", "value": f"{approval} (pending)"}]}
        self.db.execute(
            "INSERT INTO refunds(number,invoice_id,patient_id,amount_cents,reason_code,notes,status,requested_by,approved_by,created_on)"
            " VALUES(?,?,?,?,?,?,'issued',?,?,?)",
            (*base, actor.username, actor.username if p["amount_cents"] > self.settings.refund_cap_cents else None,
             self.clock.today().isoformat()))
        self.db.execute("UPDATE invoices SET refunded_cents = refunded_cents + ? WHERE id=?", (p["amount_cents"], invoice["id"]))
        self.audit.record(actor, "refund.issue", "invoice", invoice["number"], effect=True,
                          detail={"refund": number, "amount_cents": p["amount_cents"], "reason": p["reason"]})
        return {"kind": "refund", "status": "completed", "receipt_number": number, "message": "REFUND ISSUED",
                "lines": [{"label": "Invoice", "value": invoice["number"]}, {"label": "Amount", "value": money(p["amount_cents"])}]}

    def _exec_writeoff(self, actor: Actor, p: dict[str, Any]) -> dict[str, Any]:
        if actor.role != "supervisor":
            raise PermissionDenied("SUPERVISOR AUTHORIZATION REQUIRED")
        invoice = self._invoice_view(self.db.one("SELECT * FROM invoices WHERE id=?", (p["invoice_id"],)))
        if p["amount_cents"] > invoice["balance_cents"]:
            raise ValidationFailed(f"Write-off exceeds the outstanding balance ({money(invoice['balance_cents'])}).")
        number = self.db.next_number("WOF", 1, 6)
        self.db.execute(
            "INSERT INTO writeoffs(number,invoice_id,amount_cents,reason,notes,applied_by,created_on) VALUES(?,?,?,?,?,?,?)",
            (number, invoice["id"], p["amount_cents"], p["reason"], p.get("notes") or "", actor.username,
             self.clock.today().isoformat()))
        self.db.execute("UPDATE invoices SET writeoff_cents = writeoff_cents + ? WHERE id=?", (p["amount_cents"], invoice["id"]))
        self.audit.record(actor, "writeoff.apply", "invoice", invoice["number"], effect=True,
                          detail={"writeoff": number, "amount_cents": p["amount_cents"], "reason": p["reason"]})
        return {"kind": "writeoff", "status": "completed", "receipt_number": number, "message": "BALANCE WRITTEN OFF",
                "lines": [{"label": "Invoice", "value": invoice["number"]}, {"label": "Amount", "value": money(p["amount_cents"])}]}

    # ---- approvals ----------------------------------------------------------------------

    def list_approvals(self, actor: Actor, status: str = "pending") -> list[dict[str, Any]]:
        if actor.role != "supervisor":
            self.audit.record(actor, "approval.list", "approval", "-", "denied", False, {})
            raise PermissionDenied("SUPERVISOR AUTHORIZATION REQUIRED")
        rows = self.db.query(
            "SELECT a.*, r.number AS refund_number, r.amount_cents, r.reason_code, r.notes, i.number AS invoice_number,"
            " p.mrn, p.first_name, p.last_name FROM approvals a JOIN refunds r ON r.id=a.refund_id"
            " JOIN invoices i ON i.id=r.invoice_id JOIN patients p ON p.id=r.patient_id"
            " WHERE (?='all' OR a.status=?) ORDER BY a.id", (status, status))
        self.audit.record(actor, "approval.list", "approval", "-", detail={"count": len(rows)})
        return rows

    def decide_approval(self, actor: Actor, number: str, decision: str, note: str = "") -> dict[str, Any]:
        number = (number or "").strip().upper()
        with self._attempt(actor, f"approval.{decision}", "approval", number):
            if decision not in ("approve", "deny"):
                raise ValidationFailed("Decision must be approve or deny.")
            if actor.role != "supervisor":
                raise PermissionDenied("SUPERVISOR AUTHORIZATION REQUIRED")
            with self.db.transaction():
                approval = self.db.one("SELECT * FROM approvals WHERE number=?", (number,))
                if approval is None:
                    raise NotFound(f"Approval {number!r} was not found.")
                if approval["status"] != "pending":
                    raise ValidationFailed(f"Approval {number} was already {approval['status']}.")
                refund = self.db.one("SELECT * FROM refunds WHERE id=?", (approval["refund_id"],))
                invoice = self.db.one("SELECT * FROM invoices WHERE id=?", (refund["invoice_id"],))
                now = self.clock.today().isoformat()
                if decision == "approve":
                    others = self._refund_committed(invoice["id"]) - refund["amount_cents"]
                    if refund["amount_cents"] > invoice["paid_cents"] - others:
                        raise ValidationFailed("The invoice no longer has enough paid balance to refund.")
                    self.db.execute("UPDATE refunds SET status='issued', approved_by=? WHERE id=?", (actor.username, refund["id"]))
                    self.db.execute("UPDATE invoices SET refunded_cents = refunded_cents + ? WHERE id=?",
                                    (refund["amount_cents"], invoice["id"]))
                    new_status = "approved"
                else:
                    self.db.execute("UPDATE refunds SET status='denied' WHERE id=?", (refund["id"],))
                    new_status = "denied"
                self.db.execute("UPDATE approvals SET status=?, decided_by=?, decision_note=?, decided_on=? WHERE id=?",
                                (new_status, actor.username, note or "", now, approval["id"]))
                self.audit.record(actor, f"refund.{decision}", "approval", number, effect=True,
                                  detail={"refund": refund["number"], "amount_cents": refund["amount_cents"], "note": note})
        return {"approval": number, "status": new_status, "refund": refund["number"]}

    # ---- schedule -----------------------------------------------------------------------

    def schedule(self, actor: Actor, day_iso: str, provider: str = "") -> list[dict[str, Any]]:
        try:
            day = date.fromisoformat((day_iso or "").strip())
        except ValueError:
            raise ValidationFailed("Date must be in YYYY-MM-DD format.") from None
        rows = self.db.query(
            "SELECT a.*, p.mrn, p.first_name, p.last_name FROM appointments a JOIN patients p ON p.id=a.patient_id"
            " WHERE substr(a.starts_at,1,10)=? AND (?='' OR a.provider=?) ORDER BY a.starts_at, a.provider",
            (day.isoformat(), provider or "", provider or ""))
        self.audit.record(actor, "schedule.view", "schedule", day.isoformat(), detail={"count": len(rows)})
        return rows

    def schedule_csv(self, actor: Actor, day_iso: str, provider: str = "") -> str:
        rows = self.schedule(actor, day_iso, provider)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["appointment", "time", "provider", "mrn", "patient", "reason", "status"])
        for r in rows:
            writer.writerow([r["number"], r["starts_at"][11:16], r["provider"], r["mrn"],
                             f"{r['last_name']}, {r['first_name']}", r["reason"], r["status"]])
        self.audit.record(actor, "schedule.export", "schedule", day_iso, detail={"count": len(rows)})
        return out.getvalue()

    def providers(self) -> list[str]:
        return list(PROVIDERS)

    def stats(self) -> dict[str, int]:
        tables = ["patients", "appointments", "invoices", "claims", "refunds", "approvals", "writeoffs"]
        counts = {t: self.db.scalar(f"SELECT COUNT(*) FROM {t}") for t in tables}
        counts["appointments_cancelled"] = self.db.scalar("SELECT COUNT(*) FROM appointments WHERE status='cancelled'")
        counts["refunds_issued"] = self.db.scalar("SELECT COUNT(*) FROM refunds WHERE status='issued'")
        counts["refunds_pending"] = self.db.scalar("SELECT COUNT(*) FROM refunds WHERE status='pending_approval'")
        return counts

