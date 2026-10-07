"""Looks at an app's main page, signed on as the target's own account, and lists what an operator could click there. The drafting model is shown this so the plan it
writes uses screens that exist instead of ones it imagines."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import runtime
from artifacts_lib.schema import ActionType
from safety.config import PolicyConfig
from surface.base import Action
from surface.pool import get_pool
from surface.web import WebSurface

_SKIP = {"sign out", "log out", "logout", "skip to content"}


def peek_home(profile: dict[str, Any], policy: PolicyConfig, artifacts_dir: Path, limit: int = 40) -> list[str]:
    """Names of the links and buttons on the main page. An empty list when it cannot be looked at: drafting carries on without it."""
    runtime.wait_until_up(profile["base_url"])
    safety = runtime.build_safety_policy(profile["base_url"], profile["allowlist"], policy)

    def job(page: Any) -> list[str]:
        page.set_default_timeout(runtime.DEFAULT_ACTION_TIMEOUT_MS)
        page.goto(f"{profile['base_url']}{profile['login_path']}")
        surface = WebSurface(page, base_url=profile["base_url"], safety_policy=safety)
        _, login = runtime.try_login(surface, profile["username"], profile["password"], profile["login_capability"], artifacts_dir)
        if login.status.value != "success":
            return []
        surface.act(Action(kind=ActionType.NAVIGATE, params={"url": profile.get("home_path", "/")}, actor="system"))
        names: list[str] = []
        for e in surface.perceive(actor="system").elements:
            name = (e.name or "").strip()
            if e.role in ("link", "button", "tab", "menuitem") and name and name.lower() not in _SKIP and name not in names:
                names.append(name)
        return names[:limit]

    try:
        return get_pool().run(job)
    except Exception:  # noqa: BLE001 -- a draft without the menu is still a draft
        return []
