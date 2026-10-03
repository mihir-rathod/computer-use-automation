from __future__ import annotations

import hmac
import secrets
import time
from typing import Any

from clinic.audit import Actor, AuditLog
from clinic.db import Database
from clinic.errors import NotAuthenticated, SessionExpired
from clinic.seed import hash_password
from clinic.settings import Settings


class Auth:
    cookie_name = "larkspur_session"

    def __init__(self, db: Database, audit: AuditLog, settings: Settings):
        self.db, self.audit, self.settings = db, audit, settings

    def login(self, username: str, password: str, skin: str, request_id: str) -> tuple[str, dict[str, Any]] | None:
        user = self.db.one("SELECT * FROM users WHERE username=?", ((username or "").strip(),))
        ok = user is not None and hmac.compare_digest(hash_password(password or "", user["pw_salt"]), user["pw_hash"])
        if not ok:
            self.audit.record(Actor.anonymous(skin, request_id), "auth.login", "user", (username or "-").strip() or "-", "denied")
            return None
        now = time.time()
        sid = secrets.token_urlsafe(24)
        self.db.execute("INSERT INTO sessions(id,username,status,created_at,last_seen) VALUES(?,?,'active',?,?)",
                        (sid, user["username"], now, now))
        self.audit.record(Actor(user["username"], user["role"], skin, request_id), "auth.login", "user", user["username"])
        return sid, user

    def resolve(self, session_id: str | None) -> dict[str, Any]:
        if not session_id:
            raise NotAuthenticated("Please sign in.")
        session = self.db.one("SELECT * FROM sessions WHERE id=?", (session_id,))
        if session is None:
            raise NotAuthenticated("Please sign in.")
        if session["status"] == "expired":
            raise SessionExpired("Your session has timed out.")
        now = time.time()
        if now - session["last_seen"] > self.settings.session_idle_seconds:
            self.expire(session_id)
            raise SessionExpired("Your session has timed out.")
        self.db.execute("UPDATE sessions SET last_seen=? WHERE id=?", (now, session_id))
        user = self.db.one("SELECT * FROM users WHERE username=?", (session["username"],))
        if user is None:
            raise NotAuthenticated("Please sign in.")
        return user

    def expire(self, session_id: str) -> None:
        self.db.execute("UPDATE sessions SET status='expired' WHERE id=?", (session_id,))

    def logout(self, session_id: str | None, skin: str, request_id: str) -> None:
        if not session_id:
            return
        session = self.db.one("SELECT * FROM sessions WHERE id=?", (session_id,))
        if session:
            self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            user = self.db.one("SELECT role FROM users WHERE username=?", (session["username"],))
            self.audit.record(Actor(session["username"], user["role"] if user else "-", skin, request_id), "auth.logout", "user", session["username"])
