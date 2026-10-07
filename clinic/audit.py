"""The ground-truth audit log. Every attempt to read or change something is recorded by the
domain layer itself, whichever skin or API caller triggered it, so a test can assert on what the
server actually did rather than what a UI happened to show. `effect=1` marks rows where state
really changed; everything else (reads, reviews, denials, rejections, blocked duplicates) is
recorded with effect=0."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from clinic.db import Database


@dataclass(frozen=True)
class Actor:
    username: str
    role: str
    skin: str
    request_id: str

    @classmethod
    def anonymous(cls, skin: str, request_id: str) -> Actor:
        return cls("-", "-", skin, request_id)


class AuditLog:
    def __init__(self, db: Database):
        self.db = db

    def record(
        self, actor: Actor, action: str, entity_type: str, entity_id: str,
        outcome: str = "ok", effect: bool = False, detail: dict[str, Any] | None = None,
    ) -> int:
        return self.db.execute(
            "INSERT INTO audit(ts,actor,role,skin,request_id,action,entity_type,entity_id,outcome,effect,detail)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                datetime.now(UTC).isoformat(timespec="milliseconds"), actor.username, actor.role, actor.skin,
                actor.request_id, action, entity_type, str(entity_id), outcome, 1 if effect else 0,
                json.dumps(detail or {}, sort_keys=True),
            ),
        )

    def list(
        self, since_id: int = 0, actor: str | None = None, action: str | None = None,
        entity_id: str | None = None, outcome: str | None = None, effects_only: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses, params = ["id > ?"], [since_id]
        for column, value in (("actor", actor), ("action", action), ("entity_id", entity_id), ("outcome", outcome)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if effects_only:
            clauses.append("effect = 1")
        rows = self.db.query(
            f"SELECT * FROM audit WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?", (*params, limit)
        )
        for row in rows:
            row["detail"] = json.loads(row["detail"])
            row["effect"] = bool(row["effect"])
        return rows
