"""The clinic's own calendar. "Today" is fixed (and resettable) so schedules, late-cancellation
fees and available slots are deterministic across runs."""
from __future__ import annotations

from datetime import date, datetime, time


class Clock:
    def __init__(self, today: str):
        self._today = date.fromisoformat(today)

    def set_today(self, iso: str) -> None:
        self._today = date.fromisoformat(iso)

    def today(self) -> date:
        return self._today

    def now(self) -> datetime:
        return datetime.combine(self._today, time(8, 0))
