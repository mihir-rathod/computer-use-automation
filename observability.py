"""Structured logging: one JSON object per line, every line tagged with the run it belongs to.

    {"ts": "...", "level": "INFO", "event": "run.finished", "run_id": "run_...", "principal": "alex",
     "capability_id": "clinic.issue_refund", "status": "success", "duration_s": 6.2, "committed": true}

The run id travels in a context variable, so a line logged deep inside the executor, the browser pool or an approval carries it
without every function being handed it. Fields are an allowlist of facts (ids, statuses, durations, error codes); parameter
*values* are never logged, only their names, because values can be personal data. Configure with LOG_FORMAT=json|text and
LOG_LEVEL (default INFO for the API server, WARNING for the CLI so command output stays clean)."""
from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("cua_run_id", default=None)
_principal: contextvars.ContextVar[str | None] = contextvars.ContextVar("cua_principal", default=None)
LOGGER = logging.getLogger("cua")
_configured = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        body: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname, "event": record.getMessage(),
        }
        if (rid := _run_id.get()) is not None:
            body["run_id"] = rid
        if (who := _principal.get()) is not None:
            body["principal"] = who
        body.update(getattr(record, "fields", {}))
        if record.exc_info:
            body["exception"] = self.formatException(record.exc_info).splitlines()[-1]
        return json.dumps(body, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        parts = [f"{datetime.fromtimestamp(record.created).strftime('%H:%M:%S')} {record.levelname:<7} {record.getMessage()}"]
        if (rid := _run_id.get()) is not None:
            parts.append(f"run={rid}")
        parts += [f"{k}={v}" for k, v in getattr(record, "fields", {}).items()]
        return " ".join(parts)


class _CurrentStderr(logging.StreamHandler):
    """Writes to whatever sys.stderr is *now*, so a stream that something later replaces and closes (a test runner's capture) cannot leave
    the handler writing to a dead file."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def stream(self) -> Any:  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, _: Any) -> None:
        pass


def configure(default_level: str = "INFO", stream: Any = None) -> None:
    """Idempotent. Safe to call from the API server's startup and from the CLI."""
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(stream) if stream is not None else _CurrentStderr()
    handler.setFormatter(JsonFormatter() if os.environ.get("LOG_FORMAT", "json") == "json" else TextFormatter())
    LOGGER.handlers = [handler]
    LOGGER.setLevel(os.environ.get("LOG_LEVEL", default_level).upper())
    LOGGER.propagate = False
    _configured = True


def log(event: str, level: int = logging.INFO, **fields: Any) -> None:
    LOGGER.log(level, event, extra={"fields": fields})


@contextmanager
def bind(run_id: str | None = None, principal: str | None = None) -> Iterator[None]:
    tokens = []
    if run_id is not None:
        tokens.append((_run_id, _run_id.set(run_id)))
    if principal is not None:
        tokens.append((_principal, _principal.set(principal)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


@contextmanager
def timed() -> Iterator[dict[str, float]]:
    box: dict[str, float] = {}
    start = time.monotonic()
    try:
        yield box
    finally:
        box["seconds"] = round(time.monotonic() - start, 3)
