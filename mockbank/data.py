"""In-memory fixtures and session state for MockBank.

Everything here is intentionally fake: no real member data, no real credentials.
State lives in process memory and resets on restart -- fine for a local demo target.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

# Dummy operator credential for this mock app only -- not a real secret, never used
# against a real system. Never use real credentials in a fixture.
MOCK_USERNAME = "operator"
MOCK_PASSWORD = "bankdemo123"

ENVIRONMENTAL_CONDITIONS = {"slow", "unavailable", "terms_modal", "expire_session"}


SUB_ACCOUNT_TYPES = {"savings", "checking"}


@dataclass
class SubAccount:
    account_type: str
    balance: float
    confirmation_number: str


@dataclass
class Member:
    member_id: str
    name: str
    savings_balance: float
    checking_balance: float
    status: str
    restricted: bool = False
    sub_accounts: list[SubAccount] = field(default_factory=list)


MEMBERS: dict[str, Member] = {
    "10001": Member("10001", "Alice Johnson", 4231.55, 812.10, "Active"),
    "10002": Member("10002", "Bob Smith", 150.00, 20.00, "Active"),
    "10003": Member("10003", "Carol Lee", 98212.40, 5000.00, "Active"),
    # Reserved id used to demonstrate the permission-denied business outcome.
    "40004": Member("40004", "Restricted Member", 0.0, 0.0, "Restricted", restricted=True),
}

# session_id -> session state. "pending_sim" is a one-shot environmental condition
# armed via GET /_debug/simulate and consumed by the very next request.
SESSIONS: dict[str, dict] = {}

# Sequential within a process for readability, but salted with the process start time so
# confirmation numbers don't collide across separate server restarts -- otherwise the first
# confirmation after any restart would always be identical (e.g. a discovery run's evidence
# and a later replay run's evidence could both show the same number by coincidence).
# Correctness note: safe only for a single worker process (the documented run command doesn't
# pass --workers); a multi-worker deployment would need a shared counter, not a module global.
_process_started_at = int(time.time())
_confirmation_sequence = 0


def create_session(username: str) -> str:
    session_id = secrets.token_urlsafe(16)
    SESSIONS[session_id] = {
        "username": username,
        "pending_sim": None,
        "pending_subaccount": None,
    }
    return session_id


def get_session(session_id: str | None) -> dict | None:
    if not session_id:
        return None
    return SESSIONS.get(session_id)


def destroy_session(session_id: str | None) -> None:
    if session_id and session_id in SESSIONS:
        del SESSIONS[session_id]


def next_confirmation_number() -> str:
    global _confirmation_sequence
    _confirmation_sequence += 1
    return f"CNF-{_process_started_at}-{_confirmation_sequence:04d}"
