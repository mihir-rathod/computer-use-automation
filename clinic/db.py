from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

SCHEMA = """
CREATE TABLE users(
  username TEXT PRIMARY KEY, display_name TEXT NOT NULL, role TEXT NOT NULL,
  pw_salt TEXT NOT NULL, pw_hash TEXT NOT NULL);
CREATE TABLE sessions(
  id TEXT PRIMARY KEY, username TEXT NOT NULL, status TEXT NOT NULL,
  created_at REAL NOT NULL, last_seen REAL NOT NULL);
CREATE TABLE patients(
  id INTEGER PRIMARY KEY, mrn TEXT UNIQUE NOT NULL, first_name TEXT NOT NULL, last_name TEXT NOT NULL,
  dob TEXT NOT NULL, phone TEXT NOT NULL, email TEXT NOT NULL, address TEXT NOT NULL,
  insurer TEXT, insurance_member_id TEXT);
CREATE TABLE appointments(
  id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, patient_id INTEGER NOT NULL REFERENCES patients(id),
  provider TEXT NOT NULL, starts_at TEXT NOT NULL, duration_min INTEGER NOT NULL, reason TEXT NOT NULL,
  status TEXT NOT NULL, cancel_reason TEXT);
CREATE TABLE invoices(
  id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, patient_id INTEGER NOT NULL REFERENCES patients(id),
  appointment_id INTEGER REFERENCES appointments(id), description TEXT NOT NULL,
  amount_cents INTEGER NOT NULL, paid_cents INTEGER NOT NULL DEFAULT 0,
  refunded_cents INTEGER NOT NULL DEFAULT 0, writeoff_cents INTEGER NOT NULL DEFAULT 0,
  claimed INTEGER NOT NULL DEFAULT 0, created_on TEXT NOT NULL);
CREATE TABLE claims(
  id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  patient_id INTEGER NOT NULL, payer TEXT NOT NULL, amount_cents INTEGER NOT NULL, status TEXT NOT NULL,
  submitted_by TEXT NOT NULL, submitted_on TEXT NOT NULL);
CREATE TABLE refunds(
  id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  patient_id INTEGER NOT NULL, amount_cents INTEGER NOT NULL, reason_code TEXT NOT NULL, notes TEXT,
  status TEXT NOT NULL, requested_by TEXT NOT NULL, approved_by TEXT, created_on TEXT NOT NULL);
CREATE TABLE approvals(
  id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, refund_id INTEGER NOT NULL REFERENCES refunds(id),
  requested_by TEXT NOT NULL, status TEXT NOT NULL, decided_by TEXT, decision_note TEXT,
  created_on TEXT NOT NULL, decided_on TEXT);
CREATE TABLE writeoffs(
  id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  amount_cents INTEGER NOT NULL, reason TEXT NOT NULL, notes TEXT, applied_by TEXT NOT NULL, created_on TEXT NOT NULL);
CREATE TABLE drafts(
  token TEXT PRIMARY KEY, kind TEXT NOT NULL, username TEXT NOT NULL, subject TEXT NOT NULL,
  payload TEXT NOT NULL, review TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, result TEXT);
CREATE TABLE counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, role TEXT NOT NULL,
  skin TEXT NOT NULL, request_id TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL, outcome TEXT NOT NULL, effect INTEGER NOT NULL, detail TEXT NOT NULL);
"""

_TABLES = (
    "audit", "counters", "drafts", "writeoffs", "approvals", "refunds", "claims",
    "invoices", "appointments", "patients", "sessions", "users",
)


class Database:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        self._depth = 0

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        with self.lock:
            row = self._conn.execute(sql, params).fetchone()
            return row[0] if row else None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.lock:
            return self._conn.execute(sql, params).lastrowid or 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.lock:
            if self._depth:
                yield
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
            finally:
                self._depth = 0

    def next_number(self, prefix: str, start: int, width: int = 5) -> str:
        with self.lock:
            row = self._conn.execute("SELECT value FROM counters WHERE name=?", (prefix,)).fetchone()
            value = (row[0] + 1) if row else start
            self._conn.execute(
                "INSERT INTO counters(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (prefix, value),
            )
            return f"{prefix}-{value:0{width}d}"

    def reset(self) -> None:
        with self.lock:
            for table in _TABLES:
                self._conn.execute(f"DROP TABLE IF EXISTS {table}")
            self._conn.executescript(SCHEMA)
