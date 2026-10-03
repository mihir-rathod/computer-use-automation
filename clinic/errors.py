from __future__ import annotations

from typing import Any


class ClinicError(Exception):
    code = "error"

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.problems = problems or []


class NotFound(ClinicError):
    code = "not_found"


class ValidationFailed(ClinicError):
    code = "validation_error"

    def __init__(self, problems: list[str] | str):
        items = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(items), items)


class PermissionDenied(ClinicError):
    code = "permission_denied"


class NotAuthenticated(ClinicError):
    code = "not_authenticated"


class SessionExpired(ClinicError):
    code = "session_expired"


class AlreadyProcessed(ClinicError):
    code = "already_processed"

    def __init__(self, message: str, result: dict[str, Any]):
        super().__init__(message)
        self.result = result
