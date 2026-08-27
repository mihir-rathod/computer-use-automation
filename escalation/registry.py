"""In-memory session registry so the operator console (a separate thread/server) can look up
a SessionManager by id. Single-process, matching SessionManager's own scope justification.
"""
from __future__ import annotations

from escalation.session_manager import SessionManager

_SESSIONS: dict[str, SessionManager] = {}


def register_session(session: SessionManager) -> None:
    _SESSIONS[session.session_id] = session


def get_session(session_id: str) -> SessionManager | None:
    return _SESSIONS.get(session_id)


def list_sessions() -> list[SessionManager]:
    """For the operator console's own index page (escalation/operator_console.py) -- lets a
    human land on whichever run needs them without already knowing its run id, which matters
    once the chatbot (not just the CLI, which prints the id) is the thing starting runs."""
    return list(_SESSIONS.values())


def unregister_session(session_id: str) -> None:
    _SESSIONS.pop(session_id, None)
