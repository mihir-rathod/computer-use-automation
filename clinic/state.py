from __future__ import annotations

from typing import Any

from clinic.audit import AuditLog
from clinic.auth import Auth
from clinic.chaos import Chaos
from clinic.clock import Clock
from clinic.db import Database
from clinic.seed import seed
from clinic.services import Clinic
from clinic.settings import Settings
from clinic.ui import UI


class World:
    """Everything one running clinic app owns. Held on `app.state`, never in module globals, so
    tests and parallel instances don't share state."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.clock = Clock(settings.virtual_today)
        self.audit = AuditLog(self.db)
        self.chaos = Chaos()
        self.ui = UI()
        self.clinic = Clinic(self.db, self.clock, self.audit, settings, duplicate_guard=lambda: self.chaos.duplicate_guard)
        self.auth = Auth(self.db, self.audit, settings)
        self.fixtures: dict[str, Any] = {}
        self.reset()

    def reset(self, today: str | None = None) -> dict[str, Any]:
        if today:
            self.clock.set_today(today)
        else:
            self.clock.set_today(self.settings.virtual_today)
        self.chaos.clear()
        self.ui = UI()
        self.fixtures = seed(self.db, self.clock)
        return self.fixtures
