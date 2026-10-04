"""Helpers shared by tests that drive the clinic target through the platform."""
from __future__ import annotations

import httpx


def effects(base: str, action: str = "refund.issue") -> list[dict]:
    """Audit rows where state really changed: the ground truth for "this posted exactly once"."""
    return httpx.get(f"{base}/_test/audit", params={"action": action, "effects_only": True}, timeout=5).json()["items"]


def chaos(base: str, kind: str, **kw) -> None:
    httpx.post(f"{base}/_test/chaos", json={"kind": kind, **kw}, timeout=5).raise_for_status()


def reset(base: str, duplicate_guard: bool = False) -> dict:
    """Back to the seed. The target's own duplicate guard is off by default: the platform must not depend on it."""
    fixtures = httpx.post(f"{base}/_test/reset", timeout=5).json()["fixtures"]
    httpx.post(f"{base}/_test/chaos/duplicate-guard", json={"enabled": duplicate_guard}, timeout=5)
    return fixtures


def sign_in(page, base: str, user: str = "frontdesk", password: str = "desk-demo-123") -> None:
    page.goto(f"{base}/legacy/login")
    page.locator('input[name="username"]').fill(user)
    page.locator('input[name="password"]').fill(password)
    page.get_by_role("button", name="Sign In").click()
    page.get_by_text("Main menu").first.wait_for()
