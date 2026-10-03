"""Fault injection. Rules are matched per request and can fire once, N times, or stay sticky until
cleared. Everything fired is logged so a test can tell "the app failed" from "the chaos rule fired"."""
from __future__ import annotations

import asyncio
import fnmatch
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request

from clinic.audit import Actor

KINDS = {"latency", "error500", "maintenance", "expire_session", "rate_limit"}
CHAOS_PREFIXES = ("/legacy", "/api")


@dataclass
class Rule:
    id: int
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    method: str | None = None
    path_glob: str | None = None
    remaining: int | None = 1
    fired: int = 0

    def matches(self, method: str, path: str) -> bool:
        if self.method and self.method.upper() != method.upper():
            return False
        return not self.path_glob or fnmatch.fnmatch(path, self.path_glob)


class Chaos:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.duplicate_guard = True
        self.rules: list[Rule] = []
        self.log: list[dict[str, Any]] = []
        self._next_id = 1
        self._hits: dict[int, list[float]] = {}

    def clear(self) -> None:
        with self._lock:
            self.rules, self.log, self._hits = [], [], {}
            self.duplicate_guard = True

    def add(self, kind: str, params: dict[str, Any] | None = None, method: str | None = None,
            path_glob: str | None = None, remaining: int | None = 1) -> Rule:
        if kind not in KINDS:
            raise ValueError(f"unknown chaos kind {kind!r}; known: {sorted(KINDS)}")
        with self._lock:
            rule = Rule(self._next_id, kind, params or {}, method, path_glob, remaining)
            self._next_id += 1
            self.rules.append(rule)
            return rule

    def remove(self, rule_id: int) -> bool:
        with self._lock:
            before = len(self.rules)
            self.rules = [r for r in self.rules if r.id != rule_id]
            return len(self.rules) != before

    def take(self, method: str, path: str) -> list[Rule]:
        fired = []
        with self._lock:
            for rule in list(self.rules):
                if not rule.matches(method, path):
                    continue
                if rule.kind == "rate_limit":
                    window = float(rule.params.get("window_seconds", 60))
                    hits = [t for t in self._hits.get(rule.id, []) if time.time() - t < window]
                    hits.append(time.time())
                    self._hits[rule.id] = hits
                    if len(hits) <= int(rule.params.get("limit", 5)):
                        continue
                rule.fired += 1
                fired.append(rule)
                if rule.remaining is not None:
                    rule.remaining -= 1
                    if rule.remaining <= 0:
                        self.rules.remove(rule)
        return fired

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"duplicate_guard": self.duplicate_guard, "rules": [asdict(r) for r in self.rules], "log": list(self.log[-100:])}


def _html_page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<html><head><title>{title}</title></head><body style=\"font-family:monospace;padding:2em\">"
        f"<h2>{title}</h2>{body}</body></html>", status_code=status)


class ChaosMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = uuid.uuid4().hex[:12]
        path = request.url.path
        if not path.startswith(CHAOS_PREFIXES):
            return await call_next(request)
        world = request.app.state.world
        fired = world.chaos.take(request.method, path)
        is_api = path.startswith("/api")
        for rule in fired:
            world.chaos.log.append({"rule": rule.id, "kind": rule.kind, "method": request.method, "path": path})
            world.audit.record(Actor.anonymous("chaos", request.state.request_id), f"chaos.{rule.kind}", "chaos",
                               str(rule.id), detail={"method": request.method, "path": path})
            if rule.kind == "latency":
                await asyncio.sleep(float(rule.params.get("ms", 1000)) / 1000)
            elif rule.kind == "expire_session":
                sid = request.cookies.get(world.auth.cookie_name)
                if sid:
                    world.auth.expire(sid)
            elif rule.kind == "error500":
                ref = f"ERR-{uuid.uuid4().hex[:8].upper()}"
                if is_api:
                    return JSONResponse({"error": "internal_error", "message": "Application error", "reference": ref}, status_code=500)
                return _html_page("APPLICATION ERROR", f"<p>An unexpected error occurred.</p><p>Reference: {ref}</p>", 500)
            elif rule.kind == "maintenance":
                if is_api:
                    return JSONResponse({"error": "maintenance", "message": "Scheduled maintenance in progress"}, status_code=503)
                return _html_page(
                    "SCHEDULED MAINTENANCE IN PROGRESS",
                    "<p>The system is briefly unavailable.</p><p><a href=\"/legacy/menu\">Continue</a></p>", 503)
            elif rule.kind == "rate_limit":
                if is_api:
                    return JSONResponse({"error": "rate_limited", "message": "Too many requests"}, status_code=429,
                                        headers={"Retry-After": "30"})
                return _html_page("TOO MANY REQUESTS", "<p>Please wait a moment and try again.</p>", 429)
        return await call_next(request)
