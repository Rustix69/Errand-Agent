"""The agent-facing tool surface: exactly the 8 contract tools, nothing else.

``ToolPort`` is the structural type the agent side codes against.
``ContractTools`` wraps any implementation and exposes ONLY these 8 methods, so even
if the underlying object (a mock, today) has extra attributes, the agent side cannot
reach them.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from errand.contract.models import (
    BookingList,
    BookingStatusResult,
    BookResult,
    CancelResult,
    Hold,
    ProviderDetails,
    RescheduleResult,
    SearchResult,
)

TOOL_NAMES: tuple[str, ...] = (
    "search_providers",
    "get_provider",
    "hold_slot",
    "book",
    "get_booking_status",
    "cancel",
    "reschedule",
    "list_bookings",
)

# Tools with side effects. The LLM is never given these (CLAUDE.md core safety rule).
MUTATING_TOOLS: frozenset[str] = frozenset({"book", "cancel", "reschedule"})


@runtime_checkable
class ToolPort(Protocol):
    def search_providers(self, service: str, area: str, date_range: Any) -> SearchResult: ...
    def get_provider(self, provider_id: str) -> ProviderDetails: ...
    def hold_slot(self, provider_id: str, slot: str) -> Hold: ...
    def book(self, hold_id: str, idempotency_key: str) -> BookResult: ...
    def get_booking_status(
        self, hold_id: str | None = None, idempotency_key: str | None = None
    ) -> BookingStatusResult: ...
    def cancel(self, booking_id: str, idempotency_key: str) -> CancelResult: ...
    def reschedule(self, booking_id: str, slot: str, idempotency_key: str) -> RescheduleResult: ...
    def list_bookings(self, user_id: str) -> BookingList: ...


class ContractTools:
    """Narrowing wrapper: delegates the 8 contract methods and nothing else."""

    __slots__ = ("_inner",)

    def __init__(self, inner: ToolPort) -> None:
        object.__setattr__(self, "_inner", inner)

    def search_providers(self, service: str, area: str, date_range: Any) -> SearchResult:
        return self._inner.search_providers(service, area, date_range)

    def get_provider(self, provider_id: str) -> ProviderDetails:
        return self._inner.get_provider(provider_id)

    def hold_slot(self, provider_id: str, slot: str) -> Hold:
        return self._inner.hold_slot(provider_id, slot)

    def book(self, hold_id: str, idempotency_key: str) -> BookResult:
        return self._inner.book(hold_id, idempotency_key)

    def get_booking_status(
        self, hold_id: str | None = None, idempotency_key: str | None = None
    ) -> BookingStatusResult:
        return self._inner.get_booking_status(hold_id=hold_id, idempotency_key=idempotency_key)

    def cancel(self, booking_id: str, idempotency_key: str) -> CancelResult:
        return self._inner.cancel(booking_id, idempotency_key)

    def reschedule(self, booking_id: str, slot: str, idempotency_key: str) -> RescheduleResult:
        return self._inner.reschedule(booking_id, slot, idempotency_key)

    def list_bookings(self, user_id: str) -> BookingList:
        return self._inner.list_bookings(user_id)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ContractTools is read-only")
