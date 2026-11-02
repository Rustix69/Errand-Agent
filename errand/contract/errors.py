"""Errors raised by marketplace tools (the shared tool contract).

Business outcomes (``slot_taken``, ``hold_expired``, ``slot_conflict`` ...) and
transport failures (``timeout``, ``unavailable``/503) are all ``ToolError`` with a
stable ``code``; callers branch on the code, never on the message.
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    INVALID_ARGUMENT = "invalid_argument"
    UNKNOWN_SERVICE = "unknown_service"
    UNKNOWN_AREA = "unknown_area"
    PROVIDER_NOT_FOUND = "provider_not_found"
    SLOT_UNAVAILABLE = "slot_unavailable"
    HOLD_NOT_FOUND = "hold_not_found"
    HOLD_NOT_ACTIVE = "hold_not_active"
    HOLD_EXPIRED = "hold_expired"
    SLOT_TAKEN = "slot_taken"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    BOOKING_NOT_FOUND = "booking_not_found"
    BOOKING_NOT_ACTIVE = "booking_not_active"
    SLOT_CONFLICT = "slot_conflict"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"


class ToolError(Exception):
    """A tool call failed. ``retryable`` says whether the same call may succeed later."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class ToolTimeout(ToolError):
    """The call did not return in time. The side effect MAY have happened;
    only ``get_booking_status`` can tell."""

    def __init__(self, tool: str, after_s: float) -> None:
        super().__init__(
            ErrorCode.TIMEOUT,
            f"{tool} timed out after {after_s:g}s; outcome unknown",
            retryable=True,
            details={"tool": tool, "after_s": after_s},
        )


class ServiceUnavailable(ToolError):
    """Transient HTTP 503. Nothing happened; retrying is safe."""

    def __init__(self, tool: str) -> None:
        super().__init__(
            ErrorCode.UNAVAILABLE,
            f"{tool}: 503 Service Unavailable (transient)",
            retryable=True,
            details={"tool": tool, "http_status": 503},
        )
