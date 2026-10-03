from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from clinic.audit import Actor, AuditLog
from clinic.clock import Clock
from clinic.db import Database
from clinic.seed import seed
from clinic.services import Clinic
from clinic.settings import Settings


class World:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.db = Database()
        self.clock = Clock(self.settings.virtual_today)
        self.audit = AuditLog(self.db)
        self.guard = True
        self.clinic = Clinic(self.db, self.clock, self.audit, self.settings, duplicate_guard=lambda: self.guard)
        self.fixtures = seed(self.db, self.clock)

    def actor(self, role: str = "frontdesk", name: str | None = None) -> Actor:
        return Actor(name or ("supervisor" if role == "supervisor" else "frontdesk"), role, "test", "req-1")

    def effects(self, **filters):
        return self.audit.list(effects_only=True, **filters)


@pytest.fixture
def world() -> World:
    return World()



def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def clinic_url():
    """A real clinic server on a free port, for browser tests."""
    from clinic.app import create_app

    app = create_app(Settings())
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            httpx.get(f"{url}/healthz", timeout=0.5)
            break
        except httpx.ConnectError:
            time.sleep(0.1)
    else:
        raise RuntimeError("clinic server did not start")
    yield url
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def clinic(clinic_url):
    """Reset the clinic to its seed before each browser test; returns the base url."""
    httpx.post(f"{clinic_url}/_test/reset", timeout=5)
    return clinic_url


def audit(url: str, **params) -> list[dict]:
    return httpx.get(f"{url}/_test/audit", params=params, timeout=5).json()["items"]
