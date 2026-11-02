"""The mock marketplace's state: a SQLite ``world.db`` plus shared helpers.

The agent never touches this object. It receives ``world.tools(user_id)`` (the 8
contract tools) and nothing else. Tests and the simulator use ``world.sim``.

Durability rules that matter for the agent's recovery logic:

* Every side effect commits in its own transaction **before** the tool returns or
  raises. A ``timeout_after_commit`` outcome therefore leaves a durable booking
  behind that only ``get_booking_status`` (or ``list_bookings``) reveals.
* Counters behind seeded randomness and the forced-outcome queue are stored in the
  DB, so a fresh ``MockWorld`` on the same file continues the same sequence.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from errand.clock import IST, Clock
from errand.mocks import catalog
from errand.mocks.errors import ErrorCode, ToolError
from errand.mocks.models import BookingInfo, HoldInfo
from errand.mocks.scenario import ConflictSlot, ScenarioConfig, roll

if TYPE_CHECKING:
    from errand.mocks.simulator import Simulator
    from errand.mocks.tools import MarketplaceTools

SCHEMA = """
CREATE TABLE IF NOT EXISTS world_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS providers (
  provider_id  TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  service      TEXT NOT NULL,
  areas        TEXT NOT NULL,
  price        INTEGER NOT NULL,
  rating       REAL NOT NULL,
  review_count INTEGER NOT NULL,
  start_hour   INTEGER NOT NULL,
  end_hour     INTEGER NOT NULL,
  work_days    TEXT NOT NULL,
  attributes   TEXT NOT NULL,
  about        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  provider_id TEXT NOT NULL REFERENCES providers(provider_id),
  author      TEXT NOT NULL,
  rating      INTEGER NOT NULL,
  text        TEXT NOT NULL,
  review_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holds (
  hold_id      TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL,
  provider_id  TEXT NOT NULL REFERENCES providers(provider_id),
  slot         TEXT NOT NULL,
  search_price INTEGER NOT NULL,
  quoted_price INTEGER NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('active','expired','booked','released')),
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS holds_one_active_per_slot
  ON holds(provider_id, slot) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS bookings (
  booking_id      TEXT PRIMARY KEY,
  user_id         TEXT NOT NULL,
  provider_id     TEXT NOT NULL REFERENCES providers(provider_id),
  service         TEXT NOT NULL,
  slot            TEXT NOT NULL,
  price           INTEGER NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('confirmed','cancelled','cancelled_by_provider')),
  hold_id         TEXT UNIQUE,
  idempotency_key TEXT UNIQUE,
  technician      TEXT,
  cancel_reason   TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS bookings_one_confirmed_per_slot
  ON bookings(provider_id, slot) WHERE status = 'confirmed';
CREATE TABLE IF NOT EXISTS idempotency (
  tool         TEXT NOT NULL,
  key          TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  response     TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (tool, key)
);
CREATE TABLE IF NOT EXISTS forced_outcomes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  tool        TEXT NOT NULL,
  kind        TEXT NOT NULL,
  params      TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  consumed_at TEXT
);
CREATE TABLE IF NOT EXISTS tool_calls (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  tool       TEXT NOT NULL,
  args       TEXT NOT NULL,
  outcome    TEXT NOT NULL,
  error_code TEXT
);
"""

SLOT_FMT = "%Y-%m-%dT%H:%M"


# --------------------------------------------------------------------------- helpers


def iso(dt: datetime) -> str:
    """Canonical timestamp: IST, second precision. Lexicographically sortable."""
    return dt.astimezone(IST).isoformat(timespec="seconds")


def fmt_slot(dt: datetime) -> str:
    return dt.astimezone(IST).strftime(SLOT_FMT)


def parse_slot(value: Any, field: str = "slot") -> datetime:
    """Parse a 1-hour slot like '2026-11-03T09:00' (local IST; an explicit offset is converted)."""
    if not isinstance(value, str) or not value.strip():
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{field} must be a 'YYYY-MM-DDTHH:00' string")
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{field} {value!r} is not ISO 'YYYY-MM-DDTHH:00'")
    dt = dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)
    if dt.minute or dt.second or dt.microsecond:
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{field} {value!r}: slots start on the hour")
    return dt


def normalize_slot(value: Any, field: str = "slot") -> str:
    return fmt_slot(parse_slot(value, field))


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(ErrorCode.INVALID_ARGUMENT, f"{field} must be a non-empty string")
    return value.strip()


# --------------------------------------------------------------------------- world


class MockWorld:
    """Owns ``world.db``. Open the same path again to simulate a process restart."""

    def __init__(
        self,
        path: str | Path,
        clock: Clock,
        config: ScenarioConfig | None = None,
    ) -> None:
        self.path = str(path)
        self.clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

        with self.tx() as c:
            stored = self._meta_get(c, "config")
            if stored is None:
                self.config = config if config is not None else ScenarioConfig()
                self._seed(c)
            else:
                # An explicit config overrides the stored one (seed data is never re-created).
                self.config = config if config is not None else ScenarioConfig.model_validate_json(stored)
            self._meta_set(c, "config", self.config.model_dump_json())

    # ----------------------------------------------------------------- lifecycle

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "MockWorld":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def tools(self, user_id: str = catalog.DEMO_USER_ID) -> "MarketplaceTools":
        """The 8 contract tools, bound to an authenticated user. The only agent-facing surface."""
        from errand.mocks.tools import MarketplaceTools

        return MarketplaceTools(self, user_id)

    @property
    def sim(self) -> "Simulator":
        """Test/simulator hooks. Never handed to the agent."""
        from errand.mocks.simulator import Simulator

        return Simulator(self)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialized write transaction. Commits on normal exit, rolls back on any exception."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ----------------------------------------------------------------- seeding

    def _seed(self, c: sqlite3.Connection) -> None:
        now = self.clock.now()
        today = now.date()
        providers = catalog.build_providers()
        for p in providers:
            c.execute(
                "INSERT INTO providers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    p["provider_id"], p["name"], p["service"], json.dumps(p["areas"]), p["price"],
                    p["rating"], p["review_count"], p["start_hour"], p["end_hour"],
                    json.dumps(p["work_days"]), json.dumps(p["attributes"]), p["about"],
                ),
            )
            for r in catalog.build_reviews(p, today):
                c.execute(
                    "INSERT INTO reviews(provider_id, author, rating, text, review_date) VALUES (?,?,?,?,?)",
                    (p["provider_id"], r["author"], r["rating"], r["text"], r["review_date"]),
                )
        by_id = {p["provider_id"]: p for p in providers}
        for b in catalog.demo_bookings(now):
            p = by_id[b["provider_id"]]
            created = parse_slot(b["slot"]) - timedelta(days=3)
            c.execute(
                "INSERT INTO bookings(booking_id, user_id, provider_id, service, slot, price, status,"
                " technician, cancel_reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.next_id(c, "bk"), catalog.DEMO_USER_ID, p["provider_id"], p["service"],
                    b["slot"], p["price"], b["status"], p["attributes"]["technician"],
                    b.get("cancel_reason"), iso(min(created, now)), iso(min(created, now)),
                ),
            )
        # Seed-relative demo conflicts are stored apart from the config, so reopening the
        # world with a different ScenarioConfig cannot silently drop them.
        self._meta_set(c, "seed_conflicts", json.dumps(catalog.default_conflicts(today)))
        self._meta_set(c, "seeded_at", iso(now))

    # ----------------------------------------------------------------- meta / counters

    @staticmethod
    def _meta_get(c: sqlite3.Connection, key: str) -> str | None:
        row = c.execute("SELECT value FROM world_meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    @staticmethod
    def _meta_set(c: sqlite3.Connection, key: str, value: str) -> None:
        c.execute(
            "INSERT INTO world_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def counter(self, c: sqlite3.Connection, name: str) -> int:
        """Increment and return a persistent counter (1-based)."""
        key = f"ctr:{name}"
        n = int(self._meta_get(c, key) or 0) + 1
        self._meta_set(c, key, str(n))
        return n

    def peek_counter(self, c: sqlite3.Connection, name: str) -> int:
        return int(self._meta_get(c, f"ctr:{name}") or 0)

    def next_id(self, c: sqlite3.Connection, prefix: str) -> str:
        return f"{prefix}_{self.counter(c, 'id:' + prefix):06d}"

    def roll(self, *parts: object) -> float:
        return roll(self.config.seed, *parts)

    def conflict_slots(self, c: sqlite3.Connection) -> list[ConflictSlot]:
        """Slots where ``reschedule`` returns ``slot_conflict``: configured + (optionally) seeded demo ones."""
        slots = list(self.config.reschedule_conflict_slots)
        if self.config.seed_default_conflicts:
            slots += [ConflictSlot(**d) for d in json.loads(self._meta_get(c, "seed_conflicts") or "[]")]
        return slots

    def save_config(self) -> None:
        with self.tx() as c:
            self._meta_set(c, "config", self.config.model_dump_json())

    # ----------------------------------------------------------------- forced outcomes

    def take_forced(self, c: sqlite3.Connection, tool: str) -> tuple[str, dict[str, Any]] | None:
        row = c.execute(
            "SELECT id, kind, params FROM forced_outcomes WHERE tool = ? AND consumed_at IS NULL "
            "ORDER BY id LIMIT 1",
            (tool,),
        ).fetchone()
        if row is None:
            return None
        c.execute("UPDATE forced_outcomes SET consumed_at = ? WHERE id = ?", (iso(self.clock.now()), row["id"]))
        return row["kind"], json.loads(row["params"])

    def take_forced_now(self, tool: str) -> tuple[str, dict[str, Any]] | None:
        with self.tx() as c:
            return self.take_forced(c, tool)

    # ----------------------------------------------------------------- idempotency

    def idem_get(self, c: sqlite3.Connection, tool: str, key: str) -> sqlite3.Row | None:
        return c.execute(
            "SELECT request_hash, response FROM idempotency WHERE tool = ? AND key = ?", (tool, key)
        ).fetchone()

    def idem_put(self, c: sqlite3.Connection, tool: str, key: str, request_hash: str, response: str) -> None:
        c.execute(
            "INSERT INTO idempotency(tool, key, request_hash, response, created_at) VALUES (?,?,?,?,?)",
            (tool, key, request_hash, response, iso(self.clock.now())),
        )

    # ----------------------------------------------------------------- call log (sim-only)

    def log_call(self, tool: str, args: dict[str, Any], outcome: str, error_code: str | None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO tool_calls(ts, tool, args, outcome, error_code) VALUES (?,?,?,?,?)",
                (iso(self.clock.now()), tool, json.dumps(args, default=str, sort_keys=True), outcome, error_code),
            )

    # ----------------------------------------------------------------- providers & slots

    @staticmethod
    def provider_row(c: sqlite3.Connection, provider_id: str) -> dict[str, Any] | None:
        row = c.execute("SELECT * FROM providers WHERE provider_id = ?", (provider_id,)).fetchone()
        if row is None:
            return None
        p = dict(row)
        p["areas"] = json.loads(p["areas"])
        p["work_days"] = json.loads(p["work_days"])
        p["attributes"] = json.loads(p["attributes"])
        return p

    def in_schedule(self, provider: dict[str, Any], dt: datetime) -> bool:
        return dt.weekday() in provider["work_days"] and provider["start_hour"] <= dt.hour < provider["end_hour"]

    def busy_with_others(self, provider_id: str, slot: str) -> bool:
        """Baseline demand from customers the user never sees. Pure function of the seed."""
        return self.roll("busy", provider_id, slot) < self.config.busy_rate

    def slot_is_free(
        self,
        c: sqlite3.Connection,
        provider: dict[str, Any],
        slot: str,
        *,
        ignore_booking_id: str | None = None,
    ) -> bool:
        now = self.clock.now()
        dt = parse_slot(slot)
        if dt <= now or not self.in_schedule(provider, dt):
            return False
        if self.busy_with_others(provider["provider_id"], slot):
            return False
        booked = c.execute(
            "SELECT booking_id FROM bookings WHERE provider_id = ? AND slot = ? AND status = 'confirmed'",
            (provider["provider_id"], slot),
        ).fetchone()
        if booked is not None and booked["booking_id"] != ignore_booking_id:
            return False
        held = c.execute(
            "SELECT expires_at FROM holds WHERE provider_id = ? AND slot = ? AND status = 'active'",
            (provider["provider_id"], slot),
        ).fetchone()
        return not (held is not None and datetime.fromisoformat(held["expires_at"]) > now)

    def free_slots(self, c: sqlite3.Connection, provider: dict[str, Any], start: date, end: date) -> list[str]:
        slots = []
        d = start
        while d <= end:
            for h in range(provider["start_hour"], provider["end_hour"]):
                s = f"{d.isoformat()}T{h:02d}:00"
                if self.slot_is_free(c, provider, s):
                    slots.append(s)
            d += timedelta(days=1)
        return slots

    def expire_holds(self, c: sqlite3.Connection) -> None:
        c.execute(
            "UPDATE holds SET status = 'expired' WHERE status = 'active' AND expires_at <= ?",
            (iso(self.clock.now()),),
        )

    # ----------------------------------------------------------------- views

    def hold_status(self, row: sqlite3.Row | dict[str, Any]) -> str:
        if row["status"] == "active" and datetime.fromisoformat(row["expires_at"]) <= self.clock.now():
            return "expired"
        return row["status"]

    def hold_info(self, row: sqlite3.Row) -> HoldInfo:
        return HoldInfo(
            hold_id=row["hold_id"],
            provider_id=row["provider_id"],
            slot=row["slot"],
            quoted_price=row["quoted_price"],
            status=self.hold_status(row),
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )

    def booking_info(self, c: sqlite3.Connection, row: sqlite3.Row) -> BookingInfo:
        upcoming = parse_slot(row["slot"]) > self.clock.now()
        status = row["status"]
        if status == "confirmed" and not upcoming:
            status = "completed"
        name = c.execute("SELECT name FROM providers WHERE provider_id = ?", (row["provider_id"],)).fetchone()
        return BookingInfo(
            booking_id=row["booking_id"],
            provider_id=row["provider_id"],
            provider_name=name["name"] if name else row["provider_id"],
            service=row["service"],
            slot=row["slot"],
            price=row["price"],
            status=status,
            technician=row["technician"],
            is_upcoming=upcoming,
            cancel_reason=row["cancel_reason"],
        )
