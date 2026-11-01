"""Fixed, credential-free errors for local unified CAS authentication."""

from __future__ import annotations

from typing import Any


class AutoAuthError(Exception):
    """An error safe to cross the CLI/service boundary.

    Messages are fixed strings. Details may contain status codes, actions and
    non-secret counters only; never a password, username, cookie, ticket,
    app-token, execution value or raw school response.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        action: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        self.code = code
        self.action = action
        self.details = details or {}
        super().__init__(message)

    def payload(self, *, source_system: str | None = None) -> dict:
        error: dict[str, Any] = {"message": str(self)}
        if self.action:
            error["action"] = self.action
        error.update(self.details)
        payload = {"status": self.code, "error": error}
        if source_system:
            payload["source_system"] = source_system
        return payload

    def __repr__(self) -> str:  # keep tracebacks free of secrets too
        return f"AutoAuthError({self.code!r})"
