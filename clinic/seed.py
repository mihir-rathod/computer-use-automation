"""Deterministic seed data. Everything here is synthetic (reserved example domains, fictional
555 phone numbers). The same seed always produces the same patients, schedule and invoices, and
`seed()` returns a map of named fixtures so tests can find a patient with a specific property
(self-pay, 28 invoices, an appointment inside the late-cancellation window) without hardcoding ids."""
from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, time, timedelta
from typing import Any

from clinic.clock import Clock
from clinic.db import Database

PROVIDERS = ["Dr. A. Okafor", "Dr. M. Lindqvist", "N.P. S. Ramirez", "Dr. J. Whitfield"]
REASONS = ["Annual physical", "Follow-up visit", "Lab review", "Vaccination", "Acute visit", "Physical therapy intake"]
INSURERS = ["Cascade Mutual", "Northern Shield Health", "BlueFern Plan"]
SLOT_STARTS = [time(9 + i // 2, 30 * (i % 2)) for i in range(16)]

USERS = {
    "frontdesk": ("Front Desk", "frontdesk", "desk-demo-123"),
    "frontdesk2": ("Front Desk (second)", "frontdesk", "desk2-demo-123"),
    "supervisor": ("Billing Supervisor", "supervisor", "super-demo-123"),
}

_FIRST = ["Avery", "Jordan", "Maya", "Theo", "Priya", "Sam", "Lena", "Marcus", "Noor", "Elias", "Sofia", "Kenji",
          "Amara", "Oscar", "Ingrid", "Dev", "Camila", "Tobias", "Yara", "Felix", "Hana", "Isaac", "Mira", "Rafael",
          "Zoe", "Anders", "Leila", "Bruno", "Talia", "Gus"]
_LAST = ["Brennan", "Pell", "Okonkwo-Reyes", "Vance", "Natarajan", "Ito-Calder", "Hartmann", "Delacroix", "Suleiman",
         "Marsh", "Lindgren", "Tanaka", "Adeyemi", "Fitzgerald", "Holm", "Kapoor", "Moreau", "Brandt", "Haddad",
         "Whitaker", "Sato", "Novak", "Castellano", "Eriksen", "Barzani", "Quill", "Rowe", "Ferreira", "Stroud", "Yates"]
_STREETS = ["Alder", "Birch", "Cedar", "Dunmore", "Elm", "Fairview", "Garnet", "Hollis", "Ivy", "Juniper"]
_CITIES = ["Larkspur Falls", "Millbrook", "Oakridge", "Pinehurst"]
_AMOUNTS = [8500, 12400, 15600, 18000, 22500, 31000, 42000]


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 20_000).hex()


def _weekday(day: date) -> date:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


class _Seeder:
    def __init__(self, db: Database, clock: Clock):
        self.db = db
        self.today = clock.today()
        self.rng = random.Random(20260302)
        self.busy: set[tuple[str, str]] = set()

    def patient(self, mrn: str, first: str, last: str, insurer: str | None) -> int:
        n = int(mrn[-6:])
        slug = f"{first}.{last}".lower().replace(" ", "")
        return self.db.execute(
            "INSERT INTO patients(mrn,first_name,last_name,dob,phone,email,address,insurer,insurance_member_id)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                mrn, first, last, date(1940 + (n * 7) % 75, 1 + n % 12, 1 + (n * 3) % 28).isoformat(),
                f"({self.rng.choice([206, 425, 253])}) 555-{100 + n % 90:04d}", f"{slug}@example.test",
                f"{100 + (n * 13) % 800} {self.rng.choice(_STREETS)} St, {self.rng.choice(_CITIES)}, WA 98{n % 90 + 10:03d}",
                insurer, f"{(insurer or 'XX')[:2].upper()}-{880000 + n * 17}" if insurer else None,
            ),
        )

    def appointment(self, patient_id: int, day_offset: int, slot: time, provider: str | None = None,
                    status: str = "scheduled", reason: str | None = None) -> tuple[int, str]:
        day = _weekday(self.today + timedelta(days=day_offset))
        first = SLOT_STARTS.index(slot)
        ordered_slots = SLOT_STARTS[first:] + SLOT_STARTS[:first]
        candidates = [provider] if provider else PROVIDERS
        for candidate_slot in ordered_slots:
            start = datetime.combine(day, candidate_slot).isoformat(timespec="minutes")
            who = next((c for c in candidates if (c, start) not in self.busy), None)
            if who:
                break
        else:
            raise RuntimeError("seed schedule exhausted")
        self.busy.add((who, start))
        number = self.db.next_number("A", 20001)
        appt_id = self.db.execute(
            "INSERT INTO appointments(number,patient_id,provider,starts_at,duration_min,reason,status)"
            " VALUES(?,?,?,?,?,?,?)",
            (number, patient_id, who, start, 30, reason or self.rng.choice(REASONS), status),
        )
        return appt_id, number

    def invoice(self, patient_id: int, description: str, amount: int, paid: int,
                appt_id: int | None = None, day_offset: int = -10) -> str:
        number = self.db.next_number("INV", 30001)
        self.db.execute(
            "INSERT INTO invoices(number,patient_id,appointment_id,description,amount_cents,paid_cents,created_on)"
            " VALUES(?,?,?,?,?,?,?)",
            (number, patient_id, appt_id, description, amount, paid, (self.today + timedelta(days=day_offset)).isoformat()),
        )
        return number

    def past_visit(self, patient_id: int, day_offset: int, paid: str = "random") -> str:
        slot = self.rng.choice(SLOT_STARTS)
        appt_id, _ = self.appointment(patient_id, day_offset, slot, status="completed")
        reason = self.db.scalar("SELECT reason FROM appointments WHERE id=?", (appt_id,))
        amount = self.rng.choice(_AMOUNTS)
        if paid == "random":
            paid_cents = self.rng.choice([amount, amount, amount // 2, 0])
        else:
            paid_cents = amount if paid == "full" else 0
        return self.invoice(patient_id, f"Office visit - {reason}", amount, paid_cents, appt_id, day_offset)


def seed(db: Database, clock: Clock) -> dict[str, Any]:
    db.reset()
    s = _Seeder(db, clock)

    for username, (display, role, password) in USERS.items():
        salt = hashlib.sha256(username.encode()).hexdigest()[:16]
        db.execute(
            "INSERT INTO users(username,display_name,role,pw_salt,pw_hash) VALUES(?,?,?,?,?)",
            (username, display, role, salt, hash_password(password, salt)),
        )

    avery = s.patient("LK-100001", "Avery", "Brennan", "Cascade Mutual")
    paid_visit = s.past_visit(avery, -14, paid="full")
    paid_visit_cents = db.scalar("SELECT paid_cents FROM invoices WHERE number=?", (paid_visit,))
    _, avery_appt = s.appointment(avery, 2, time(10, 0), PROVIDERS[0], reason="Follow-up visit")
    _, avery_appt2 = s.appointment(avery, 9, time(14, 30), PROVIDERS[1], reason="Lab review")
    claimable = s.invoice(avery, "Lab panel - comprehensive", 12400, 0, day_offset=-3)

    jordan = s.patient("LK-100002", "Jordan", "Pell", None)
    self_pay_invoice = s.invoice(jordan, "Office visit - acute", 9500, 0, day_offset=-5)
    s.appointment(jordan, 4, time(11, 0), PROVIDERS[2], reason="Acute visit")

    maya = s.patient("LK-100003", "Maya", "Okonkwo-Reyes", "BlueFern Plan")
    for i in range(28):
        s.invoice(maya, f"Visit charge {i + 1:02d}", _AMOUNTS[i % len(_AMOUNTS)], _AMOUNTS[i % len(_AMOUNTS)] if i % 3 else 0,
                  day_offset=-60 + i)
    s.appointment(maya, 3, time(13, 0), PROVIDERS[3])

    theo = s.patient("LK-100004", "Theo", "Vance", "Northern Shield Health")
    big_invoice = s.invoice(theo, "Procedure - minor surgery", 120_000, 90_000, day_offset=-20)
    s.appointment(theo, 6, time(9, 30), PROVIDERS[0], reason="Follow-up visit")

    priya = s.patient("LK-100005", "Priya", "Natarajan", "Cascade Mutual")
    _, late_appt = s.appointment(priya, 0, time(15, 0), PROVIDERS[2], reason="Annual physical")

    s.patient("LK-100006", "Sam", "Ito-Calder", "BlueFern Plan")

    for i in range(7, 43):
        mrn = f"LK-{100000 + i}"
        insurer = s.rng.choice([*INSURERS, *INSURERS, None])
        pid = s.patient(mrn, s.rng.choice(_FIRST), s.rng.choice(_LAST), insurer)
        for _ in range(s.rng.randint(0, 3)):
            s.appointment(pid, s.rng.randint(1, 14), s.rng.choice(SLOT_STARTS))
        for _ in range(s.rng.randint(0, 3)):
            s.past_visit(pid, -s.rng.randint(2, 60))

    return {
        "standard": {
            "mrn": "LK-100001", "patient_id": avery, "upcoming_appointment": avery_appt,
            "upcoming_appointment_2": avery_appt2, "refundable_invoice": paid_visit,
            "claimable_invoice": claimable, "paid_cents": paid_visit_cents,
        },
        "self_pay": {"mrn": "LK-100002", "invoice": self_pay_invoice},
        "many_invoices": {"mrn": "LK-100003", "invoice_count": 28},
        "big_refund": {"mrn": "LK-100004", "invoice": big_invoice, "paid_cents": 90_000},
        "late_cancel": {"mrn": "LK-100005", "appointment": late_appt},
        "empty": {"mrn": "LK-100006"},
        "users": {name: {"role": role} for name, (_, role, _) in USERS.items()},
    }
