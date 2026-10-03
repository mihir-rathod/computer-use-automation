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

Known limitation: this means evidence for a real deployment would contain whatever the target
shows on screen. The planned improvement is a configurable PII-redaction pass in the evidence
logger, applied per capability.

So the scope stays exactly here: real secrets (passwords, tokens, credentials) never hit disk;
everything else stays visible.
"""
from __future__ import annotations

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
