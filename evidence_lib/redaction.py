"""Redaction floor: never persist secrets into artifacts or logs. The one real secret this
system handles today is the operator password typed during a login capability, so it is
redacted at the one call site that has the context to recognize it.

This is deliberately narrow, not a generic recursive redactor:

- The business data a capability reads or writes (balances, confirmation numbers, member names)
  is exactly what this system's evidence and dashboard exist to show. Redacting it would gut
  their debugging purpose, not just add friction to it.
- Screenshots aren't practically redactable without page-specific region configuration per
  capability -- there's no generic way to know where sensitive text renders on an arbitrary
  screen.
- For genuinely regulated data, the correct control is encrypting and access-restricting the
  evidence store itself, not blanking the system's own operational output. Redaction is the
  right tool for secrets that are never needed downstream (a password), not for the business
  data the system exists to report.

Beyond that floor, `Redactor` is an opt-in, configurable pass (patterns and field names come from
safety/policy.yaml). It scrubs the structured evidence (log.jsonl, result.json) and the run store's
copy of parameters. It does not touch screenshots, and a regex cannot catch PII it has no pattern
for, so it is a reduction in what is stored, not a guarantee.

So the floor stays: real secrets (passwords, tokens, credentials) never hit disk; everything else is
visible unless a Redactor is configured.
"""
from __future__ import annotations

import re
from typing import Any

_SENSITIVE_MARKERS = ("password", "secret", "token", "credential")


def is_sensitive_field(label: str | None) -> bool:
    if not label:
        return False
    lowered = label.lower()
    return any(marker in lowered for marker in _SENSITIVE_MARKERS)


def redact_type_params(params: dict[str, Any], semantic_description: str | None) -> dict[str, Any]:
    if is_sensitive_field(semantic_description) and "text" in params:
        return {**params, "text": "***REDACTED***"}
    return params


class Redactor:
    """Replaces values that match a pattern, and any value stored under a sensitive field name."""

    def __init__(self, patterns: dict[str, str] | None = None, field_names: list[str] | tuple[str, ...] = ()):
        self._patterns = {name: re.compile(p) for name, p in (patterns or {}).items()}
        self._fields = tuple(f.lower() for f in field_names)

    @classmethod
    def from_config(cls, config: Any) -> Redactor:
        return cls(config.patterns, config.field_names)

    def with_secrets_from(self, params: dict[str, Any]) -> Redactor:
        """Also scrub the literal values of any password-like parameter wherever they appear: a model's own
        commentary ("I typed the password ...") is free text no field name or pattern would catch."""
        for key, value in params.items():
            if is_sensitive_field(key) and isinstance(value, str) and len(value) >= 4:
                self._patterns[f"secret:{key}"] = re.compile(re.escape(value))
        return self

    def scrub_text(self, text: str) -> str:
        for name, pattern in self._patterns.items():
            text = pattern.sub(f"[REDACTED:{name.split(':')[0]}]", text)
        return text

    def scrub(self, value: Any, key: str | None = None) -> Any:
        if key is not None and any(f in key.lower() for f in self._fields):
            return "[REDACTED:field]" if value not in (None, "") else value
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, dict):
            return {k: self.scrub(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub(v) for v in value]
        return value
