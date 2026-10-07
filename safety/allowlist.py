"""Enforces an explicit, configurable allowlist (permitted domains/routes, allowed action
types) -- the agent must not act outside it. Loaded from JSON at startup; checked inside Surface.act() itself (see
surface/web.py) -- the one chokepoint both discovery and replay pass through, so neither can
accidentally bypass a rule the other respects. A violation always raises; there is no
allowed-by-default / silent-skip path.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

DEFAULT_ALLOWLIST_PATH = Path(__file__).resolve().parent / "allowlist_clinic.json"


class AllowlistViolation(Exception):
    pass


class AllowlistConfig(BaseModel):
    allowed_base_urls: list[str] = Field(default_factory=list)
    allowed_route_patterns: list[str] = Field(
        default_factory=list,
        description="Glob patterns matched against the URL *path* only (see the same "
                     "path-only reasoning in surface/web.py's check_signal).",
    )
    allowed_action_types: list[str] = Field(default_factory=list)

    @classmethod
    def from_json(cls, path: Path = DEFAULT_ALLOWLIST_PATH) -> AllowlistConfig:
        return cls.model_validate_json(path.read_text())


class AllowlistPolicy:
    def __init__(self, config: AllowlistConfig):
        self.config = config

    def check(self, url: str, action_type: str) -> None:
        if action_type not in self.config.allowed_action_types:
            raise AllowlistViolation(f"action type '{action_type}' is not in the allowlist")
        if not any(url.startswith(base) for base in self.config.allowed_base_urls):
            raise AllowlistViolation(f"URL '{url}' is not under any allowed base URL")
        path = urlsplit(url).path
        if not any(fnmatch.fnmatchcase(path, pattern) for pattern in self.config.allowed_route_patterns):
            raise AllowlistViolation(f"path '{path}' does not match any allowed route pattern")
