"""The 8 marketplace tools with FIXED contracts. This is the only surface the agent gets.

Outcome resolution for every call:
1. Argument validation (bad input raises immediately; nothing is consumed).
2. Idempotent replay for side-effect tools (same key + same request -> stored result).
3. State validation (hold exists / not expired, booking active ...).
4. Outcome: the next forced outcome for this tool if one is queued, otherwise the
   seeded decision (or the deterministic default in ``mode="forced"``).
5. Side effects COMMIT, and only then is any post-commit timeout/crash raised.
"""

from __future__ import annotations

import inspect
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any, TypeVar

from pydantic import ValidationError

from errand.mocks import catalog
from errand.contract.port import TOOL_NAMES
from errand.contract.errors import ErrorCode, ServiceUnavailable, ToolError, ToolTimeout
from errand.mocks.crash import SimulatedCrash
from errand.contract.models import (
    BookingList,
    BookingStatusResult,
    BookResult,
    CancelResult,
    DateRange,
    Hold,
    ProviderDetails,
    ProviderSummary,
    Review,
    RescheduleResult,
    SearchResult,
)
from errand.mocks.scenario import pick_weighted
from errand.mocks.world import (
    MockWorld,
    iso,
    normalize_slot,
    parse_slot,
    require_str,
    stable_hash,
)

T = TypeVar("T")

_COMMITTING = frozenset({"success", "timeout_after_commit", "crash_after_commit"})


def _coerce_range(value: Any) -> DateRange:
    try:
        if isinstance(value, DateRange):
            return value
        if isinstance(value, dict):
            return DateRange.model_validate(value)
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return DateRange(start=value[0], end=value[1])
        if isinstance(value, (str, date)):
            return DateRange(start=value, end=value)
    except ValidationError as e:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"invalid date_range: {e.errors()[0]['msg']}")
    raise ToolError(
        ErrorCode.INVALID_ARGUMENT,
        "date_range must be {'start': 'YYYY-MM-DD', 'end': 'YYYY-MM-DD'} or a [start, end] pair",
    )


class MarketplaceTools:
    TOOL_NAMES = TOOL_NAMES

    def __init__(self, world: MockWorld, user_id: str) -> None:
        self._w = world
        self.user_id = user_id
        self._note = "ok"

    # ------------------------------------------------------------------ dispatch

    def call(self, name: str, args: dict[str, Any]) -> Any:
        """Name-based dispatch restricted to the 8 contract tools."""
        if name not in self.TOOL_NAMES:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"unknown tool {name!r}")
        fn = getattr(self, name)
        try:
            inspect.signature(fn).bind(**args)
        except TypeError as e:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"bad arguments for {name}: {e}")
        return fn(**args)

    def _call(self, tool: str, args: dict[str, Any], fn: Callable[[], T]) -> T:
        w = self._w
        w.clock.sleep(w.config.tool_latency_s.get(tool, 0.0))
        self._note = "ok"
        outcome, code = "ok", None
        try:
            return fn()
        except ToolError as e:
            outcome, code = "error", e.code
            raise
        except SimulatedCrash:
            outcome, code = "crash", "crash"
            raise
        finally:
            if self._note != "ok":
                outcome = f"{outcome}:{self._note}"
            w.log_call(tool, args, outcome, code)

    def _raise_pending(self, tool: str, pending: ToolError | None, crash: bool) -> None:
        """Called AFTER the transaction committed."""
        if crash:
            raise SimulatedCrash(f"{tool}: process crashed after commit")
        if pending is None:
            return
        if isinstance(pending, ToolTimeout):
            self._w.clock.sleep(self._w.config.timeout_latency_s)
        raise pending

    def _forced_read(self, tool: str) -> None:
        forced = self._w.take_forced_now(tool)
        if forced is None:
            return
        kind, _ = forced
        self._note = kind
        if kind == "unavailable":
            raise ServiceUnavailable(tool)
        if kind == "timeout":
            self._w.clock.sleep(self._w.config.timeout_latency_s)
            raise ToolTimeout(tool, self._w.config.timeout_latency_s)

    def _transport_failure(self, tool: str, kind: str) -> ToolError | None:
        if kind == "unavailable":
            return ServiceUnavailable(tool)
        if kind in ("timeout_before_commit", "timeout_after_commit", "timeout"):
            return ToolTimeout(tool, self._w.config.timeout_latency_s)
        return None

    # ------------------------------------------------------------------ read-only tools

    def search_providers(self, service: str, area: str, date_range: Any) -> SearchResult:
        args = {"service": service, "area": area, "date_range": date_range}
        return self._call("search_providers", args, lambda: self._search(service, area, date_range))

    def _search(self, service: Any, area: Any, date_range: Any) -> SearchResult:
        w = self._w
        svc = catalog.normalize_service(require_str(service, "service"))
        if svc is None:
            raise ToolError(
                ErrorCode.UNKNOWN_SERVICE, f"unknown service {service!r}",
                details={"valid_services": list(catalog.SERVICES)},
            )
        ar = catalog.normalize_area(require_str(area, "area"))
        if ar is None:
            raise ToolError(
                ErrorCode.UNKNOWN_AREA, f"unknown area {area!r}", details={"valid_areas": list(catalog.AREAS)}
            )
        dr = _coerce_range(date_range)
        today = w.clock.now().date()
        if dr.end < today:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "date_range is entirely in the past")
        start = max(dr.start, today)
        if (dr.end - start).days + 1 > w.config.max_search_days:
            raise ToolError(
                ErrorCode.INVALID_ARGUMENT, f"date_range may span at most {w.config.max_search_days} days"
            )
        self._forced_read("search_providers")
        with w.tx() as c:
            ids = [r[0] for r in c.execute("SELECT provider_id FROM providers WHERE service = ?", (svc,))]
            provs = [p for p in (w.provider_row(c, i) for i in ids) if p and ar in p["areas"]]
            provs.sort(key=lambda p: (-p["rating"], p["provider_id"]))
            summaries = [
                ProviderSummary(
                    provider_id=p["provider_id"],
                    name=p["name"],
                    service=p["service"],
                    areas=p["areas"],
                    price=p["price"],
                    rating=p["rating"],
                    review_count=p["review_count"],
                    available_slots=w.free_slots(c, p, start, dr.end),
                    attributes=p["attributes"],
                )
                for p in provs[: w.config.max_results]
            ]
        return SearchResult(service=svc, area=ar, date_range=DateRange(start=start, end=dr.end), providers=summaries)

    def get_provider(self, provider_id: str) -> ProviderDetails:
        return self._call("get_provider", {"provider_id": provider_id}, lambda: self._get_provider(provider_id))

    def _get_provider(self, provider_id: Any) -> ProviderDetails:
        w = self._w
        pid = require_str(provider_id, "provider_id")
        self._forced_read("get_provider")
        with w.tx() as c:
            p = w.provider_row(c, pid)
            if p is None:
                raise ToolError(ErrorCode.PROVIDER_NOT_FOUND, f"no provider {pid!r}")
            reviews = [
                Review(author=r["author"], rating=r["rating"], date=r["review_date"], text=r["text"])
                for r in c.execute("SELECT * FROM reviews WHERE provider_id = ? ORDER BY id", (pid,))
            ]
        return ProviderDetails(
            provider_id=p["provider_id"],
            name=p["name"],
            service=p["service"],
            areas=p["areas"],
            price=p["price"],
            rating=p["rating"],
            review_count=p["review_count"],
            working_hours=f"{p['start_hour']:02d}:00-{p['end_hour']:02d}:00",
            working_days=[catalog.WEEKDAYS[d] for d in p["work_days"]],
            attributes=p["attributes"],
            about=p["about"],
            reviews=reviews,
        )

    def get_booking_status(self, hold_id: str | None = None, idempotency_key: str | None = None) -> BookingStatusResult:
        args = {"hold_id": hold_id, "idempotency_key": idempotency_key}
        return self._call("get_booking_status", args, lambda: self._status(hold_id, idempotency_key))

    def _status(self, hold_id: Any, idempotency_key: Any) -> BookingStatusResult:
        w = self._w
        if (hold_id is None) == (idempotency_key is None):
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "pass exactly one of hold_id or idempotency_key")
        self._forced_read("get_booking_status")
        with w.tx() as c:
            if idempotency_key is not None:
                key = require_str(idempotency_key, "idempotency_key")
                b = c.execute(
                    "SELECT * FROM bookings WHERE idempotency_key = ? AND user_id = ?", (key, self.user_id)
                ).fetchone()
                h = None
                if b is not None and b["hold_id"]:
                    h = c.execute("SELECT * FROM holds WHERE hold_id = ?", (b["hold_id"],)).fetchone()
            else:
                hid = require_str(hold_id, "hold_id")
                h = c.execute("SELECT * FROM holds WHERE hold_id = ? AND user_id = ?", (hid, self.user_id)).fetchone()
                if h is None:
                    raise ToolError(ErrorCode.HOLD_NOT_FOUND, f"no hold {hid!r}")
                b = c.execute("SELECT * FROM bookings WHERE hold_id = ?", (hid,)).fetchone()
            info = w.booking_info(c, b) if b is not None else None
            hold = w.hold_info(h) if h is not None else None
        return BookingStatusResult(status=info.status if info else "not_booked", booking=info, hold=hold)

    def list_bookings(self, user_id: str) -> BookingList:
        return self._call("list_bookings", {"user_id": user_id}, lambda: self._list(user_id))

    def _list(self, user_id: Any) -> BookingList:
        w = self._w
        uid = require_str(user_id, "user_id")
        if uid != self.user_id:
            raise ToolError(ErrorCode.PERMISSION_DENIED, "can only list the authenticated user's bookings")
        self._forced_read("list_bookings")
        with w.tx() as c:
            rows = c.execute("SELECT * FROM bookings WHERE user_id = ? ORDER BY slot, booking_id", (uid,)).fetchall()
            infos = [w.booking_info(c, r) for r in rows]
        return BookingList(user_id=uid, as_of=w.clock.now(), bookings=infos)

    # ------------------------------------------------------------------ hold

    def hold_slot(self, provider_id: str, slot: str) -> Hold:
        args = {"provider_id": provider_id, "slot": slot}
        return self._call("hold_slot", args, lambda: self._hold(provider_id, slot))

    def _hold(self, provider_id: Any, slot: Any) -> Hold:
        w, cfg = self._w, self._w.config
        pid = require_str(provider_id, "provider_id")
        s = normalize_slot(slot)
        now = w.clock.now()
        if parse_slot(s) <= now:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, f"slot {s} is in the past")
        pending: ToolError | None = None
        result: Hold | None = None
        with w.tx() as c:
            p = w.provider_row(c, pid)
            if p is None:
                raise ToolError(ErrorCode.PROVIDER_NOT_FOUND, f"no provider {pid!r}")
            w.expire_holds(c)
            mine = c.execute(
                "SELECT * FROM holds WHERE provider_id = ? AND slot = ? AND status = 'active' AND user_id = ?",
                (pid, s, self.user_id),
            ).fetchone()
            if mine is not None:
                # Re-holding a slot this user already holds returns the same hold (no new quote).
                self._note = "existing_hold"
                return self._hold_model(mine)
            if not w.slot_is_free(c, p, s):
                raise ToolError(ErrorCode.SLOT_UNAVAILABLE, f"{pid} is not available at {s}")

            delta = 0
            forced = w.take_forced(c, "hold_slot")
            if forced is not None:
                kind, params = forced
                self._note = kind
                pending = self._transport_failure("hold_slot", kind)
                if kind == "slot_unavailable":
                    pending = ToolError(ErrorCode.SLOT_UNAVAILABLE, f"{pid} is not available at {s}")
                elif kind == "price_change":
                    delta = int(params["delta"])
            elif cfg.mode == "seeded":
                n = w.counter(c, "hold_slot")
                if cfg.price_change_deltas and w.roll("hold_price", n) < cfg.price_change_rate:
                    deltas = cfg.price_change_deltas
                    delta = deltas[int(w.roll("hold_delta", n) * len(deltas))]
                    self._note = "price_change"

            if pending is None:
                hold_id = w.next_id(c, "hold")
                quoted = max(100, p["price"] + delta)
                expires = now + timedelta(minutes=cfg.hold_minutes)
                c.execute(
                    "INSERT INTO holds(hold_id, user_id, provider_id, slot, search_price, quoted_price, status,"
                    " created_at, expires_at) VALUES (?,?,?,?,?,?,'active',?,?)",
                    (hold_id, self.user_id, pid, s, p["price"], quoted, iso(now), iso(expires)),
                )
                result = Hold(
                    hold_id=hold_id, provider_id=pid, slot=s, search_price=p["price"],
                    quoted_price=quoted, price_changed=quoted != p["price"], expires_at=expires,
                )
        self._raise_pending("hold_slot", pending, crash=False)
        assert result is not None
        return result

    @staticmethod
    def _hold_model(row: sqlite3.Row) -> Hold:
        return Hold(
            hold_id=row["hold_id"], provider_id=row["provider_id"], slot=row["slot"],
            search_price=row["search_price"], quoted_price=row["quoted_price"],
            price_changed=row["quoted_price"] != row["search_price"],
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )

    # ------------------------------------------------------------------ book

    def book(self, hold_id: str, idempotency_key: str) -> BookResult:
        args = {"hold_id": hold_id, "idempotency_key": idempotency_key}
        return self._call("book", args, lambda: self._book(hold_id, idempotency_key))

    def _book(self, hold_id: Any, idempotency_key: Any) -> BookResult:
        w, cfg = self._w, self._w.config
        hid = require_str(hold_id, "hold_id")
        key = require_str(idempotency_key, "idempotency_key")
        req_hash = stable_hash({"hold_id": hid})
        pending: ToolError | None = None
        crash = False
        result: BookResult | None = None

        with w.tx() as c:
            prior = w.idem_get(c, "book", key)
            if prior is not None:
                if prior["request_hash"] != req_hash:
                    raise ToolError(
                        ErrorCode.IDEMPOTENCY_CONFLICT, "idempotency_key was already used for a different hold"
                    )
                self._note = "replayed"
                return BookResult.model_validate_json(prior["response"]).model_copy(update={"replayed": True})

            h = c.execute("SELECT * FROM holds WHERE hold_id = ? AND user_id = ?", (hid, self.user_id)).fetchone()
            if h is None:
                raise ToolError(ErrorCode.HOLD_NOT_FOUND, f"no hold {hid!r}")
            status = w.hold_status(h)
            if status == "booked":
                raise ToolError(ErrorCode.HOLD_NOT_ACTIVE, f"hold {hid} was already used for a booking")
            if status == "released":
                taken = c.execute(
                    "SELECT 1 FROM bookings WHERE provider_id = ? AND slot = ? AND status = 'confirmed'",
                    (h["provider_id"], h["slot"]),
                ).fetchone()
                if taken:
                    raise ToolError(ErrorCode.SLOT_TAKEN, f"{h['slot']} was taken by another customer")
                raise ToolError(ErrorCode.HOLD_NOT_ACTIVE, f"hold {hid} was released")
            if status == "expired":
                raise ToolError(
                    ErrorCode.HOLD_EXPIRED, f"hold {hid} expired at {h['expires_at']}",
                    details={"expires_at": h["expires_at"]},
                )

            forced = w.take_forced(c, "book")
            if forced is not None:
                kind = forced[0]
            else:
                n = w.counter(c, "book")
                kind = "success" if cfg.mode == "forced" else pick_weighted(cfg.book_weights, w.roll("book", n))
            self._note = kind

            if kind == "slot_taken":
                # Another customer got the slot first: the hold is lost and the slot is gone.
                c.execute("UPDATE holds SET status = 'released' WHERE hold_id = ?", (hid,))
                self._insert_booking(c, catalog.OTHER_USER_ID, h["provider_id"], h["slot"], h["quoted_price"])
                pending = ToolError(ErrorCode.SLOT_TAKEN, f"{h['slot']} was taken by another customer")
            elif kind in _COMMITTING:
                booking_id = self._insert_booking(
                    c, self.user_id, h["provider_id"], h["slot"], h["quoted_price"], hold_id=hid, key=key
                )
                c.execute("UPDATE holds SET status = 'booked' WHERE hold_id = ?", (hid,))
                result = BookResult(
                    booking_id=booking_id, hold_id=hid, provider_id=h["provider_id"], slot=h["slot"],
                    price=h["quoted_price"], status="confirmed",
                )
                w.idem_put(c, "book", key, req_hash, result.model_dump_json())
                pending = self._transport_failure("book", kind)
                crash = kind == "crash_after_commit"
            else:
                pending = self._transport_failure("book", kind)
        # The booking (if any) is durable from here on. Only now may the call fail.
        self._raise_pending("book", pending, crash)
        assert result is not None
        return result

    def _insert_booking(
        self, c: sqlite3.Connection, user_id: str, provider_id: str, slot: str, price: int,
        *, hold_id: str | None = None, key: str | None = None,
    ) -> str:
        w = self._w
        p = w.provider_row(c, provider_id)
        assert p is not None
        booking_id = w.next_id(c, "bk")
        now = iso(w.clock.now())
        c.execute(
            "INSERT INTO bookings(booking_id, user_id, provider_id, service, slot, price, status, hold_id,"
            " idempotency_key, technician, created_at, updated_at) VALUES (?,?,?,?,?,?,'confirmed',?,?,?,?,?)",
            (booking_id, user_id, provider_id, p["service"], slot, price, hold_id, key,
             p["attributes"]["technician"], now, now),
        )
        return booking_id

    # ------------------------------------------------------------------ cancel

    def cancel(self, booking_id: str, idempotency_key: str) -> CancelResult:
        args = {"booking_id": booking_id, "idempotency_key": idempotency_key}
        return self._call("cancel", args, lambda: self._cancel(booking_id, idempotency_key))

    def _cancel(self, booking_id: Any, idempotency_key: Any) -> CancelResult:
        w, cfg = self._w, self._w.config
        bid = require_str(booking_id, "booking_id")
        key = require_str(idempotency_key, "idempotency_key")
        req_hash = stable_hash({"booking_id": bid})
        pending: ToolError | None = None
        crash = False
        result: CancelResult | None = None

        with w.tx() as c:
            prior = w.idem_get(c, "cancel", key)
            if prior is not None:
                if prior["request_hash"] != req_hash:
                    raise ToolError(ErrorCode.IDEMPOTENCY_CONFLICT, "idempotency_key was already used for another booking")
                self._note = "replayed"
                return CancelResult.model_validate_json(prior["response"]).model_copy(update={"replayed": True})
            b = self._own_booking(c, bid)
            info = w.booking_info(c, b)
            if info.status != "confirmed":
                raise ToolError(
                    ErrorCode.BOOKING_NOT_ACTIVE, f"booking {bid} is {info.status}", details={"status": info.status}
                )
            attempt = w.counter(c, f"cancel_calls:{bid}")
            forced = w.take_forced(c, "cancel")
            if forced is not None:
                kind = forced[0]
            else:
                kind = "unavailable" if (cfg.cancel_first_call_fails and attempt == 1) else "success"
            self._note = kind

            if kind in _COMMITTING:
                now = w.clock.now()
                c.execute(
                    "UPDATE bookings SET status = 'cancelled', cancel_reason = 'Cancelled by user', updated_at = ?"
                    " WHERE booking_id = ?",
                    (iso(now), bid),
                )
                result = CancelResult(booking_id=bid, status="cancelled", cancelled_at=now)
                w.idem_put(c, "cancel", key, req_hash, result.model_dump_json())
                crash = kind == "crash_after_commit"
            pending = self._transport_failure("cancel", kind)
        self._raise_pending("cancel", pending, crash)
        assert result is not None
        return result

    def _own_booking(self, c: sqlite3.Connection, bid: str) -> sqlite3.Row:
        b = c.execute("SELECT * FROM bookings WHERE booking_id = ? AND user_id = ?", (bid, self.user_id)).fetchone()
        if b is None:
            raise ToolError(ErrorCode.BOOKING_NOT_FOUND, f"no booking {bid!r}")
        return b

    # ------------------------------------------------------------------ reschedule

    def reschedule(self, booking_id: str, slot: str, idempotency_key: str) -> RescheduleResult:
        args = {"booking_id": booking_id, "slot": slot, "idempotency_key": idempotency_key}
        return self._call("reschedule", args, lambda: self._reschedule(booking_id, slot, idempotency_key))

    def _is_configured_conflict(self, c: sqlite3.Connection, provider_id: str, slot: str) -> bool:
        return any(
            cs.slot == slot and cs.provider_id in (None, provider_id) for cs in self._w.conflict_slots(c)
        )

    def _reschedule(self, booking_id: Any, slot: Any, idempotency_key: Any) -> RescheduleResult:
        w = self._w
        bid = require_str(booking_id, "booking_id")
        new_slot = normalize_slot(slot)
        key = require_str(idempotency_key, "idempotency_key")
        req_hash = stable_hash({"booking_id": bid, "slot": new_slot})
        pending: ToolError | None = None
        crash = False
        result: RescheduleResult | None = None

        with w.tx() as c:
            prior = w.idem_get(c, "reschedule", key)
            if prior is not None:
                if prior["request_hash"] != req_hash:
                    raise ToolError(ErrorCode.IDEMPOTENCY_CONFLICT, "idempotency_key was already used for another request")
                self._note = "replayed"
                return RescheduleResult.model_validate_json(prior["response"]).model_copy(update={"replayed": True})
            b = self._own_booking(c, bid)
            info = w.booking_info(c, b)
            if info.status != "confirmed":
                raise ToolError(
                    ErrorCode.BOOKING_NOT_ACTIVE, f"booking {bid} is {info.status}", details={"status": info.status}
                )
            if new_slot == b["slot"]:
                raise ToolError(ErrorCode.INVALID_ARGUMENT, f"booking {bid} is already at {new_slot}")
            if parse_slot(new_slot) <= w.clock.now():
                raise ToolError(ErrorCode.INVALID_ARGUMENT, f"slot {new_slot} is in the past")

            forced = w.take_forced(c, "reschedule")
            kind = forced[0] if forced is not None else "success"
            if kind in _COMMITTING:
                p = w.provider_row(c, b["provider_id"])
                assert p is not None
                if self._is_configured_conflict(c, p["provider_id"], new_slot) or not w.slot_is_free(
                    c, p, new_slot, ignore_booking_id=bid
                ):
                    kind = "slot_conflict"
            self._note = kind

            if kind == "slot_conflict":
                pending = ToolError(
                    ErrorCode.SLOT_CONFLICT, f"{new_slot} conflicts with another job for {b['provider_id']}",
                    details={"booking_id": bid, "slot": new_slot, "provider_id": b["provider_id"]},
                )
            elif kind in _COMMITTING:
                c.execute(
                    "UPDATE bookings SET slot = ?, updated_at = ? WHERE booking_id = ?",
                    (new_slot, iso(w.clock.now()), bid),
                )
                result = RescheduleResult(
                    booking_id=bid, provider_id=b["provider_id"], old_slot=b["slot"], new_slot=new_slot,
                    status="confirmed",
                )
                w.idem_put(c, "reschedule", key, req_hash, result.model_dump_json())
                pending = self._transport_failure("reschedule", kind)
                crash = kind == "crash_after_commit"
            else:
                pending = self._transport_failure("reschedule", kind)
        self._raise_pending("reschedule", pending, crash)
        assert result is not None
        return result
