from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = ":memory:"
    session_idle_seconds: int = 600
    test_token: str | None = None
    virtual_today: str = "2026-03-02"
    refund_cap_cents: int = 20_000
    late_cancel_fee_cents: int = 2_500
    late_cancel_window_hours: int = 24
    draft_ttl_seconds: int = 1_800
    page_size: int = 10

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        return cls(
            db_path=env("CLINIC_DB_PATH", ":memory:"),
            session_idle_seconds=int(env("CLINIC_SESSION_IDLE_SECONDS", "600")),
            test_token=env("CLINIC_TEST_TOKEN") or None,
            virtual_today=env("CLINIC_VIRTUAL_TODAY", "2026-03-02"),
        )
