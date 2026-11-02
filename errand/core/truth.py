"""Source-of-truth queries, shared by the executor and startup recovery.

``Reader`` can only call READ tools: it refuses ``book``/``cancel``/``reschedule`` by
construction, so code that resolves ambiguous outcomes (including startup recovery)
cannot re-book even by mistake.

``query_truth`` answers one question for an attempt: did it happen?
  done      -> the world shows the intended end state (verified)
  not_done  -> the world shows the pre-action state
  diverged  -> anything else; never treated as success
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from errand.clock import Clock
from errand.contract.errors import ToolError
from errand.contract.models import BookingInfo
from errand.contract.port import MUTATING_TOOLS, TOOL_NAMES, ToolPort
from errand.core.approval import Terms, terms_of
from errand.core.budget import Budget
from errand.core.store import AgentStore

Emit = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class Limits:
    max_attempts: int = 3  # mutating tries per attempt (always the SAME idempotency key)
    max_query_tries: int = 3  # tries for one read / source-of-truth query
    backoff_s: tuple[float, ...] = (1.0, 2.0)

    def backoff(self, i: int) -> float:
        return self.backoff_s[min(i, len(self.backoff_s) - 1)] if self.backoff_s else 0.0


class Reader:
    """Budgeted, retrying access to the READ tools only."""

    def __init__(
        self,
        tools: ToolPort,
        clock: Clock,
        limits: Limits,
        budget: Budget | None = None,
        emit: Emit | None = None,
    ) -> None:
        self.tools, self.clock, self.limits, self.budget = tools, clock, limits, budget
        self.emit = emit or (lambda name, data: None)

    def __call__(self, name: str, **kwargs: Any) -> Any:
        if name in MUTATING_TOOLS or name not in TOOL_NAMES:
            raise PermissionError(f"Reader may only call read tools, not {name!r}")
        last: ToolError | None = None
        for i in range(self.limits.max_query_tries):
            if self.budget is not None:
                self.budget.charge_tool()
            self.emit("tool_call", {"tool": name, "args": kwargs})
            try:
                result = getattr(self.tools, name)(**kwargs)
            except ToolError as e:
                self.emit("tool_error", {"tool": name, "code": e.code, "retryable": e.retryable})
                if not e.retryable:
                    raise
                last = e
                if i < self.limits.max_query_tries - 1:
                    self.clock.sleep(self.limits.backoff(i))
                continue
            self.emit("tool_result", {"tool": name})
            return result
        assert last is not None
        raise last


@dataclass(frozen=True)
class Truth:
    state: Literal["done", "not_done", "diverged"]
    booking: BookingInfo | None = None
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def attempt_terms(store: AgentStore, attempt: sqlite3.Row) -> Terms:
    appr = store.approval(attempt["approval_id"])
    assert appr is not None
    prop = store.proposal(appr["proposal_id"])
    assert prop is not None
    return terms_of(prop)


def query_truth(read: Reader, user_id: str, attempt: sqlite3.Row, terms: Terms) -> Truth:
    """May raise ToolError (source of truth unavailable). The caller must then treat the
    outcome as UNKNOWN and must not retry the mutation."""
    action = attempt["action"]
    if action == "book":
        st = read("get_booking_status", idempotency_key=attempt["idempotency_key"])
        if st.status == "not_booked":
            return Truth("not_done")
        b = st.booking
        if st.status in ("confirmed", "completed") and b is not None:
            if (b.provider_id, b.slot, b.price) == (terms.provider_id, terms.slot, terms.price):
                return Truth("done", b)
            return Truth("diverged", b, "booking_terms_mismatch")
        return Truth("diverged", b, f"unexpected_status:{st.status}")

    lb = read("list_bookings", user_id=user_id)
    b = next((x for x in lb.bookings if x.booking_id == attempt["ref"]), None)
    if b is None:
        return Truth("diverged", None, "booking_missing")
    if action == "cancel":
        if b.status == "cancelled":
            return Truth("done", b)
        if b.status == "confirmed":
            return Truth("not_done", b)
        return Truth("diverged", b, f"unexpected_status:{b.status}")
    # reschedule
    if b.status != "confirmed":
        return Truth("diverged", b, f"unexpected_status:{b.status}")
    if b.slot == attempt["new_slot"]:
        return Truth("done", b)
    if b.slot == terms.slot:
        return Truth("not_done", b)
    return Truth("diverged", b, "slot_elsewhere")
