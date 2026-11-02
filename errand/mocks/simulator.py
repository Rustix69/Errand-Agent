"""Test and simulator hooks on the mock world. NOT agent tools.

The agent only ever sees ``MarketplaceTools``. Events like "the provider cancelled"
happen here, and the agent must discover them by polling ``list_bookings``.
"""

from __future__ import annotations

import json
from typing import Any

from errand.mocks import catalog
from errand.contract.errors import ErrorCode, ToolError
from errand.mocks.scenario import FORCEABLE, ConflictSlot
from errand.mocks.world import MockWorld, iso, normalize_slot


class Simulator:
    def __init__(self, world: MockWorld) -> None:
        self._w = world

    # ------------------------------------------------------------------ forced outcomes

    def force(self, tool: str, kind: str, *, times: int = 1, **params: Any) -> None:
        """Queue ``kind`` for the next ``times`` calls of ``tool`` that pass argument validation.

        Examples::

            sim.force("book", "timeout_after_commit")   # booked, then times out
            sim.force("hold_slot", "price_change", delta=300)
            sim.force("get_booking_status", "unavailable", times=3)
        """
        if tool not in FORCEABLE:
            raise ValueError(f"unknown tool {tool!r}")
        if kind not in FORCEABLE[tool]:
            raise ValueError(f"{tool} cannot be forced to {kind!r}; allowed: {sorted(FORCEABLE[tool])}")
        if kind == "price_change" and not isinstance(params.get("delta"), int):
            raise ValueError("price_change needs an integer delta=...")
        if times < 1:
            raise ValueError("times must be >= 1")
        with self._w.tx() as c:
            for _ in range(times):
                c.execute(
                    "INSERT INTO forced_outcomes(tool, kind, params, created_at) VALUES (?,?,?,?)",
                    (tool, kind, json.dumps(params), iso(self._w.clock.now())),
                )

    def pending_forced(self, tool: str | None = None) -> list[tuple[str, str]]:
        with self._w.tx() as c:
            rows = c.execute(
                "SELECT tool, kind FROM forced_outcomes WHERE consumed_at IS NULL"
                + (" AND tool = ?" if tool else "") + " ORDER BY id",
                (tool,) if tool else (),
            ).fetchall()
        return [(r["tool"], r["kind"]) for r in rows]

    def clear_forced(self) -> None:
        with self._w.tx() as c:
            c.execute("DELETE FROM forced_outcomes WHERE consumed_at IS NULL")

    # ------------------------------------------------------------------ world events

    def provider_cancels(self, booking_id: str, reason: str = "Technician unavailable") -> None:
        """The provider cancels a confirmed upcoming booking (proactive follow-up trigger)."""
        with self._w.tx() as c:
            row = c.execute("SELECT * FROM bookings WHERE booking_id = ?", (booking_id,)).fetchone()
            if row is None:
                raise ToolError(ErrorCode.BOOKING_NOT_FOUND, f"no booking {booking_id!r}")
            if self._w.booking_info(c, row).status != "confirmed":
                raise ToolError(ErrorCode.BOOKING_NOT_ACTIVE, f"booking {booking_id} is not an upcoming confirmed booking")
            c.execute(
                "UPDATE bookings SET status = 'cancelled_by_provider', cancel_reason = ?, updated_at = ? "
                "WHERE booking_id = ?",
                (reason, iso(self._w.clock.now()), booking_id),
            )

    def take_slot(self, provider_id: str, slot: str) -> str:
        """Another customer books ``slot``. Any active hold on it is released, so a later
        ``book`` on that hold fails with ``slot_taken``."""
        s = normalize_slot(slot)
        with self._w.tx() as c:
            p = self._w.provider_row(c, provider_id)
            if p is None:
                raise ToolError(ErrorCode.PROVIDER_NOT_FOUND, f"no provider {provider_id!r}")
            c.execute(
                "UPDATE holds SET status = 'released' WHERE provider_id = ? AND slot = ? AND status = 'active'",
                (provider_id, s),
            )
            booking_id = self._w.next_id(c, "bk")
            now = iso(self._w.clock.now())
            c.execute(
                "INSERT INTO bookings(booking_id, user_id, provider_id, service, slot, price, status, technician,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,'confirmed',?,?,?)",
                (booking_id, catalog.OTHER_USER_ID, provider_id, p["service"], s, p["price"],
                 p["attributes"]["technician"], now, now),
            )
        return booking_id

    def set_provider_price(self, provider_id: str, price: int) -> None:
        with self._w.tx() as c:
            cur = c.execute("UPDATE providers SET price = ? WHERE provider_id = ?", (price, provider_id))
            if cur.rowcount == 0:
                raise ToolError(ErrorCode.PROVIDER_NOT_FOUND, f"no provider {provider_id!r}")

    def add_conflict_slot(self, slot: str, provider_id: str | None = None) -> None:
        w = self._w
        cs = ConflictSlot(slot=normalize_slot(slot), provider_id=provider_id)
        w.config = w.config.model_copy(update={"reschedule_conflict_slots": [*w.config.reschedule_conflict_slots, cs]})
        w.save_config()

    # ------------------------------------------------------------------ inspection (assertions only)

    def calls(self, tool: str | None = None) -> list[dict[str, Any]]:
        with self._w.tx() as c:
            rows = c.execute(
                "SELECT * FROM tool_calls" + (" WHERE tool = ?" if tool else "") + " ORDER BY id",
                (tool,) if tool else (),
            ).fetchall()
        return [{**dict(r), "args": json.loads(r["args"])} for r in rows]

    def bookings(self, user_id: str | None = catalog.DEMO_USER_ID) -> list[dict[str, Any]]:
        """Raw booking rows (``user_id=None`` for everyone)."""
        with self._w.tx() as c:
            rows = c.execute(
                "SELECT * FROM bookings" + (" WHERE user_id = ?" if user_id else "") + " ORDER BY booking_id",
                (user_id,) if user_id else (),
            ).fetchall()
        return [dict(r) for r in rows]

    def holds(self) -> list[dict[str, Any]]:
        with self._w.tx() as c:
            return [dict(r) for r in c.execute("SELECT * FROM holds ORDER BY hold_id")]
