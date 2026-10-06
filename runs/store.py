"""Durable run records, idempotency keys and approvals (SQLite, stdlib).

What this exists to guarantee: a caller that retries a request, whether from a timeout, a crashed
client or a duplicate click, cannot make a state-changing capability run twice. A run is created
under an idempotency key; a second request with the same (capability, key) is answered from the
first run's stored result instead of launching a browser, unless the first run never committed.

A run whose commit step was issued but whose outcome could not be confirmed (`needs_review`) blocks its
key until a person settles it with `resolve()`. That is deliberate: guessing either way is how a
refund gets paid twice or never.

The store is the system of record for who approved what, when and why. Approver identity is whatever
the caller asserts; see safety/policy.yaml.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Literal

import logging

import observability
from replay.result import ReplayResult, ReplayStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    capability_id TEXT NOT NULL,
    version TEXT,
    idempotency_key TEXT,
    target TEXT,
    requested_by TEXT NOT NULL,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL,
    committed INTEGER NOT NULL DEFAULT 0,
    commit_step TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    evidence_dir TEXT,
    result_json TEXT,
    resolution TEXT,
    pace_ms INTEGER NOT NULL DEFAULT 0,
    show_window INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS runs_key ON runs(capability_id, idempotency_key);
CREATE INDEX IF NOT EXISTS runs_cap_day ON runs(capability_id, committed, finished_at);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    tier TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decision TEXT,
    decided_by TEXT,
    decided_at TEXT,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS approvals_run ON approvals(run_id);
CREATE TABLE IF NOT EXISTS repairs (
    id TEXT PRIMARY KEY,
    capability_id TEXT NOT NULL,
    base_version TEXT NOT NULL,
    step_id TEXT NOT NULL,
    run_id TEXT,
    confident INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    proposal_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    reason TEXT,
    new_version TEXT
);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS teach_sessions (
    id TEXT PRIMARY KEY,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    version TEXT,
    target TEXT NOT NULL,
    contract_json TEXT NOT NULL,
    evidence_dir TEXT,
    stop_reason TEXT,
    reasoning TEXT,
    steps INTEGER,
    error TEXT,
    verify_json TEXT,
    lint_json TEXT,
    commit_request_json TEXT,
    commit_decision TEXT,
    commit_decided_by TEXT,
    commit_reason TEXT,
    retry_of TEXT
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_owner ON chat_messages(owner, id);
CREATE TABLE IF NOT EXISTS canary_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capability_id TEXT NOT NULL,
    version TEXT,
    at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    status TEXT,
    detail TEXT,
    run_id TEXT,
    repair_id TEXT
);
"""

# lifecycle states a run can be in besides a final ReplayStatus value
PENDING_APPROVAL = "pending_approval"
APPROVED = "approved"
REJECTED = "rejected"
RUNNING = "running"
QUEUED = "queued"  # accepted by the async API, waiting for a worker
ABANDONED = "abandoned"  # a needs_review run a person confirmed did NOT commit; its key is free again

_REUSABLE_STATUSES = {ReplayStatus.SUCCESS.value, ReplayStatus.BUSINESS_OUTCOME.value}


class RunError(Exception):
    pass


class ApprovalError(RunError):
    pass


class NotPermitted(ApprovalError):
    """The caller lacks the role or roster entry for this decision (HTTP 403), as opposed to the decision being invalid (409)."""


@dataclass
class Claim:
    kind: Literal["new", "replay", "conflict"]
    run: dict[str, Any] | None = None
    reason: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_run_id() -> str:
    return f"run_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(3)}"


class RunStore:
    def __init__(self, path: Path | str = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        columns = {r[1] for r in self._conn.execute("PRAGMA table_info(runs)")}
        if "target" not in columns:  # a database created before runs recorded which target identity they ran as
            self._conn.execute("ALTER TABLE runs ADD COLUMN target TEXT")
        for column in ("pace_ms", "show_window"):  # watch settings arrived later
            if column not in columns:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _row(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    # ---- runs and idempotency --------------------------------------------------------------

    def begin(
        self, capability_id: str, version: str | None, params: dict[str, Any], requested_by: str,
        idempotency_key: str | None = None, status: str = RUNNING, evidence_dir: str | None = None,
        target: str | None = None, pace_ms: int = 0, show_window: bool = False,
    ) -> Claim:
        """Atomically either creates a run or says why it must not. With no key there is nothing to
        deduplicate on, so the run is always new."""
        with self._tx() as db:
            if idempotency_key:
                prior = db.execute(
                    "SELECT * FROM runs WHERE capability_id=? AND idempotency_key=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (capability_id, idempotency_key)).fetchone()
                if prior is not None:
                    prior = dict(prior)
                    verdict = self._judge_prior(prior)
                    if verdict.kind != "new":
                        if json.loads(prior["params_json"]) != json.loads(json.dumps(params, default=str)):
                            # Answering a *different* request with the first one's stored result would be wrong, and
                            # for a commit it would silently drop the second request.
                            return Claim("conflict", prior, f"idempotency key already used by run {prior['id']} with different parameters; use a new key")
                        return verdict
            run_id = new_run_id()
            db.execute(
                "INSERT INTO runs(id,capability_id,version,idempotency_key,requested_by,params_json,status,created_at,started_at,evidence_dir,target,pace_ms,show_window)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, capability_id, version, idempotency_key, requested_by, json.dumps(params, default=str), status, _now(),
                 _now() if status == RUNNING else None, evidence_dir, target, int(pace_ms), int(show_window)))
            return Claim("new", dict(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()))

    @staticmethod
    def _judge_prior(prior: dict[str, Any]) -> Claim:
        status, committed = prior["status"], bool(prior["committed"])
        resolution = prior["resolution"]
        if resolution == "committed" or committed or status in _REUSABLE_STATUSES:
            return Claim("replay", prior, "an earlier run with this idempotency key already completed")
        if status in (RUNNING, QUEUED, PENDING_APPROVAL, APPROVED):
            return Claim("conflict", prior, f"run {prior['id']} with this idempotency key is still {status}")
        if status == ReplayStatus.NEEDS_REVIEW.value and resolution is None:
            return Claim("conflict", prior, f"run {prior['id']} may have committed and has not been settled; resolve it before retrying")
        return Claim("new", prior)  # failed before committing, dry run, rejected, or abandoned: safe to try again

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self._row("SELECT * FROM runs WHERE id=?", (run_id,))

    def list_runs(self, capability_id: str | None = None, status: str | None = None, limit: int = 50, requested_by: str | None = None,
                  q: str | None = None, offset: int = 0) -> list[dict[str, Any]]:
        where, args = [], []
        if requested_by:
            where.append("requested_by=?")
            args.append(requested_by)
        if q:
            where.append("(capability_id LIKE ? OR id LIKE ? OR requested_by LIKE ?)")
            args += [f"%{q}%"] * 3
        if capability_id:
            where.append("capability_id=?")
            args.append(capability_id)
        if status:
            where.append("status=?")
            args.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self._rows(f"SELECT * FROM runs {clause} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?", (*args, limit, offset))

    def active_window_runs(self) -> int:
        return int(self._rows("SELECT COUNT(*) AS n FROM runs WHERE show_window=1 AND status IN (?, ?)", (QUEUED, RUNNING))[0]["n"])

    def unresolved_reviews(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM runs WHERE status=? AND resolution IS NULL ORDER BY created_at DESC", (ReplayStatus.NEEDS_REVIEW.value,))

    def mark_running(self, run_id: str, evidence_dir: str | None = None) -> None:
        with self._tx() as db:
            db.execute("UPDATE runs SET status=?, started_at=?, evidence_dir=COALESCE(?, evidence_dir) WHERE id=?",
                       (RUNNING, _now(), evidence_dir, run_id))

    def claim_approved(self, run_id: str, to_status: str = RUNNING) -> bool:
        """Atomically moves an approved run to running. Exactly one caller wins, so two concurrent resumes of the same
        approval cannot both execute it (which, for a commit step, would be a double post)."""
        with self._tx() as db:
            cur = db.execute("UPDATE runs SET status=?, started_at=? WHERE id=? AND status=?", (to_status, _now(), run_id, APPROVED))
            return cur.rowcount == 1

    def finish(self, run_id: str, result: ReplayResult, evidence_dir: str | None = None) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE runs SET status=?, committed=?, commit_step=?, error_code=?, finished_at=?, evidence_dir=COALESCE(?, evidence_dir), result_json=? WHERE id=?",
                (result.status.value, int(result.committed), result.commit_step, result.error.code if result.error else None,
                 _now(), evidence_dir, result.model_dump_json(), run_id))

    def stored_result(self, run: dict[str, Any]) -> ReplayResult | None:
        return ReplayResult.model_validate_json(run["result_json"]) if run.get("result_json") else None

    def committed_today(self, capability_id: str) -> int:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        row = self._row("SELECT COUNT(*) AS n FROM runs WHERE capability_id=? AND committed=1 AND substr(finished_at,1,10)=?",
                        (capability_id, today))
        return int(row["n"]) if row else 0

    def resolve(self, run_id: str, outcome: Literal["committed", "not_committed"], by: str, reason: str) -> dict[str, Any]:
        """A person settles a needs_review run against the target's own records."""
        if not by or not reason.strip():
            raise RunError("resolving a needs_review run requires who and why")
        with self._tx() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise RunError(f"unknown run {run_id}")
            if run["status"] != ReplayStatus.NEEDS_REVIEW.value or run["resolution"] is not None:
                raise RunError(f"run {run_id} is not an unresolved needs_review run (status={run['status']})")
            db.execute("UPDATE runs SET resolution=?, status=? WHERE id=?",
                       (outcome, ReplayStatus.NEEDS_REVIEW.value if outcome == "committed" else ABANDONED, run_id))
            db.execute("INSERT INTO approvals(run_id,tier,requested_by,requested_at,decision,decided_by,decided_at,reason) VALUES(?,?,?,?,?,?,?,?)",
                       (run_id, "resolution", by, _now(), outcome, by, _now(), reason))
        return self.get(run_id)  # type: ignore[return-value]

    # ---- teaching sessions --------------------------------------------------------------------------------

    def teach_create(self, created_by: str, capability_id: str, target: str, contract_json: str, retry_of: str | None = None) -> dict[str, Any]:
        sid = "teach_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "_" + secrets.token_hex(3)
        with self._tx() as db:
            db.execute("INSERT INTO teach_sessions(id, created_by, created_at, status, capability_id, target, contract_json, retry_of) VALUES(?,?,?,?,?,?,?,?)",
                       (sid, created_by, _now(), "queued", capability_id, target, contract_json, retry_of))
        return self.teach_get(sid)  # type: ignore[return-value]

    def teach_get(self, sid: str) -> dict[str, Any] | None:
        return self._row("SELECT * FROM teach_sessions WHERE id=?", (sid,))

    def teach_list(self, created_by: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if created_by:
            return self._rows("SELECT * FROM teach_sessions WHERE created_by=? ORDER BY created_at DESC, rowid DESC LIMIT ?", (created_by, limit))
        return self._rows("SELECT * FROM teach_sessions ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))

    def teach_update(self, sid: str, **fields: Any) -> None:
        allowed = {"status", "version", "evidence_dir", "stop_reason", "reasoning", "steps", "error", "verify_json", "lint_json", "finished_at",
                   "commit_request_json", "commit_decision", "commit_decided_by", "commit_reason"}
        bad = set(fields) - allowed
        if bad:
            raise RunError(f"cannot set {sorted(bad)}")
        with self._tx() as db:
            db.execute(f"UPDATE teach_sessions SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), sid))

    def teach_active(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM teach_sessions WHERE status IN ('queued','running','awaiting_commit','verifying')")

    def teach_decide_commit(self, sid: str, decision: str, by: str, reason: str) -> dict[str, Any]:
        """Records a supervisor's answer to a teaching session's request to take its one irreversible step. Atomic: only the first answer counts."""
        if not reason.strip():
            raise ApprovalError("a decision needs a reason")
        with self._tx() as db:
            row = db.execute("SELECT * FROM teach_sessions WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise ApprovalError(f"unknown teaching session {sid}")
            if row["status"] != "awaiting_commit" or row["commit_decision"] is not None:
                raise ApprovalError(f"session {sid} is not waiting for a commit decision (status={row['status']})")
            db.execute("UPDATE teach_sessions SET commit_decision=?, commit_decided_by=?, commit_reason=? WHERE id=?", (decision, by, reason, sid))
        return self.teach_get(sid)  # type: ignore[return-value]

    # ---- chat ---------------------------------------------------------------------------------------------

    def chat_add(self, owner: str, role: str, text: str, run_id: str | None = None) -> dict[str, Any]:
        with self._tx() as db:
            cur = db.execute("INSERT INTO chat_messages(owner, role, text, run_id, created_at) VALUES(?,?,?,?,?)", (owner, role, text, run_id, _now()))
            return dict(db.execute("SELECT * FROM chat_messages WHERE id=?", (cur.lastrowid,)).fetchone())

    def chat_list(self, owner: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM chat_messages WHERE owner=? ORDER BY id DESC LIMIT ?", (owner, limit))
        return list(reversed(rows))

    def chat_clear(self, owner: str) -> int:
        with self._tx() as db:
            return db.execute("DELETE FROM chat_messages WHERE owner=?", (owner,)).rowcount

    # ---- API keys ---------------------------------------------------------------------------

    ROLES = ("viewer", "operator", "supervisor", "admin")

    @staticmethod
    def _hash_key(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def create_key(self, name: str, role: str) -> str:
        """Returns the key ONCE; only its hash is stored, so a lost key is replaced, never recovered. The key's name is the
        identity recorded on every run, approval and decision made with it."""
        if role not in self.ROLES:
            raise RunError(f"role must be one of {', '.join(self.ROLES)}")
        if not name.strip():
            raise RunError("a key needs a name")
        key = "cua_" + secrets.token_urlsafe(24)
        with self._tx() as db:
            db.execute("INSERT INTO api_keys(name, role, key_hash, created_at) VALUES(?,?,?,?)", (name.strip(), role, self._hash_key(key), _now()))
        return key

    def authenticate(self, key: str) -> dict[str, Any] | None:
        row = self._row("SELECT * FROM api_keys WHERE key_hash=? AND revoked_at IS NULL", (self._hash_key(key),))
        if row is not None:
            with self._tx() as db:
                db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (_now(), row["id"]))
        return row

    def list_keys(self) -> list[dict[str, Any]]:
        return self._rows("SELECT id, name, role, created_at, last_used_at, revoked_at FROM api_keys ORDER BY id")

    def revoke_key(self, name: str) -> int:
        with self._tx() as db:
            return db.execute("UPDATE api_keys SET revoked_at=? WHERE name=? AND revoked_at IS NULL", (_now(), name)).rowcount

    def recover_interrupted(self, has_commit_step: Any) -> list[str]:
        """Run on server start. A run still marked running or queued belonged to a process that is gone. If it could have
        issued an irreversible step it is needs_review (unknown), otherwise a plain failure; either way it stops blocking."""
        recovered = []
        for run in self._rows("SELECT id, capability_id FROM runs WHERE status IN (?, ?)", (RUNNING, QUEUED)):
            ambiguous = bool(has_commit_step(run["capability_id"]))
            with self._tx() as db:
                db.execute("UPDATE runs SET status=?, error_code='interrupted', finished_at=? WHERE id=?",
                           (ReplayStatus.NEEDS_REVIEW.value if ambiguous else ReplayStatus.HARD_FAILURE.value, _now(), run["id"]))
            recovered.append(run["id"])
            observability.log("run.interrupted", logging.WARNING, run_id=run["id"], capability_id=run["capability_id"],
                              became="needs_review" if ambiguous else "hard_failure")
        return recovered

    # ---- repair proposals and canaries -------------------------------------------------------

    def save_repair(self, proposal: Any) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO repairs(id,capability_id,base_version,step_id,run_id,confident,proposal_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (proposal.id, proposal.capability_id, proposal.base_version, proposal.step_id, proposal.run_id,
                 int(proposal.confident), proposal.model_dump_json(), _now()))

    def get_repair(self, proposal_id: str) -> dict[str, Any] | None:
        return self._row("SELECT * FROM repairs WHERE id=?", (proposal_id,))

    def list_repairs(self, status: str | None = None, capability_id: str | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if capability_id:
            where.append("capability_id=?")
            args.append(capability_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self._rows(f"SELECT * FROM repairs {clause} ORDER BY created_at DESC, rowid DESC", tuple(args))

    def decide_repair(self, proposal_id: str, status: Literal["approved", "rejected"], by: str, reason: str, new_version: str | None = None) -> None:
        with self._tx() as db:
            db.execute("UPDATE repairs SET status=?, decided_by=?, decided_at=?, reason=?, new_version=? WHERE id=?",
                       (status, by, _now(), reason, new_version, proposal_id))

    def record_canary(self, capability_id: str, version: str | None, ok: bool, status: str | None, detail: str, run_id: str | None, repair_id: str | None) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO canary_runs(capability_id,version,at,ok,status,detail,run_id,repair_id) VALUES(?,?,?,?,?,?,?,?)",
                       (capability_id, version, _now(), int(ok), status, detail, run_id, repair_id))

    def canary_history(self, capability_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if capability_id:
            return self._rows("SELECT * FROM canary_runs WHERE capability_id=? ORDER BY id DESC LIMIT ?", (capability_id, limit))
        return self._rows("SELECT * FROM canary_runs ORDER BY id DESC LIMIT ?", (limit,))

    # ---- approvals ---------------------------------------------------------------------------

    def request_approval(self, run_id: str, tier: str, requested_by: str) -> int:
        with self._tx() as db:
            cur = db.execute("INSERT INTO approvals(run_id,tier,requested_by,requested_at) VALUES(?,?,?,?)",
                             (run_id, tier, requested_by, _now()))
            return int(cur.lastrowid)

    def approvals_for(self, run_id: str) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM approvals WHERE run_id=? ORDER BY id", (run_id,))

    def pending_approvals(self) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT a.*, r.capability_id, r.params_json FROM approvals a JOIN runs r ON r.id=a.run_id "
            "WHERE a.decision IS NULL AND r.status=? ORDER BY a.id", (PENDING_APPROVAL,))

    def decide(self, run_id: str, decision: Literal["approved", "rejected"], by: str, reason: str) -> dict[str, Any]:
        """Records the decision. Whether `by` is *allowed* to decide is the caller's check (it needs the
        policy roster); this refuses a self-approval and a decision with no reason."""
        if not reason.strip():
            raise ApprovalError("a decision needs a reason")
        with self._tx() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise ApprovalError(f"unknown run {run_id}")
            if run["status"] != PENDING_APPROVAL:
                raise ApprovalError(f"run {run_id} is not waiting for approval (status={run['status']})")
            approval = db.execute("SELECT * FROM approvals WHERE run_id=? AND decision IS NULL ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
            if approval is None:
                raise ApprovalError(f"run {run_id} has no open approval request")
            if by == approval["requested_by"]:
                raise ApprovalError("the person who requested a run cannot approve it")
            db.execute("UPDATE approvals SET decision=?, decided_by=?, decided_at=?, reason=? WHERE id=?",
                       (decision, by, _now(), reason, approval["id"]))
            db.execute("UPDATE runs SET status=? WHERE id=?", (APPROVED if decision == "approved" else REJECTED, run_id))
        return self.get(run_id)  # type: ignore[return-value]

    def close(self) -> None:
        self._conn.close()
