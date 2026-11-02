"""agent.db: the agent's own durable state (proposals, approvals, booking attempts).

Same SQLite conventions as the mock world, but a separate file and no shared code: the
agent side must not import from ``errand.mocks`` (enforced by tests/test_boundaries.py).

Invariants the schema enforces on its own, independent of application code:

* at most one *pending* proposal per session;
* one approval per proposal, ``rebind_count`` can only be 0 or 1;
* an attempt's ``seq`` is 0 or 1, unique per approval, so one approval yields at most two keys;
* at most one PENDING-or-CONFIRMED attempt per approval, so a second attempt cannot be
  opened while the first is unresolved or has succeeded.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from errand.clock import IST, Clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
  id                 TEXT PRIMARY KEY,
  session_id         TEXT NOT NULL,
  user_id            TEXT NOT NULL,
  action             TEXT NOT NULL CHECK (action IN ('book','cancel','reschedule')),
  provider_id        TEXT NOT NULL,
  slot               TEXT NOT NULL,
  price              INTEGER NOT NULL,
  hold_id            TEXT,
  booking_id         TEXT,
  new_slot           TEXT,
  terms_fp           TEXT NOT NULL,
  fingerprint        TEXT NOT NULL,
  confirm_code       TEXT NOT NULL,
  parent_proposal_id TEXT,
  status             TEXT NOT NULL CHECK (status IN
      ('pending','approved','declined','superseded','expired','invalidated','executed','failed')),
  created_at         TEXT NOT NULL,
  expires_at         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_proposal_per_session
  ON proposals(session_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS approvals (
  id                 TEXT PRIMARY KEY,
  proposal_id        TEXT NOT NULL UNIQUE REFERENCES proposals(id),
  ref                TEXT NOT NULL,            -- hold_id (book) or booking_id (cancel/reschedule)
  terms_fp           TEXT NOT NULL,
  fingerprint        TEXT NOT NULL,
  user_message       TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  expires_at         TEXT NOT NULL,
  used_at            TEXT,
  invalidated_reason TEXT,
  rebind_count       INTEGER NOT NULL DEFAULT 0 CHECK (rebind_count IN (0, 1))
);

CREATE TABLE IF NOT EXISTS approval_rebinds (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  approval_id     TEXT NOT NULL REFERENCES approvals(id),
  old_ref         TEXT NOT NULL,
  new_ref         TEXT NOT NULL,
  old_fingerprint TEXT NOT NULL,
  new_fingerprint TEXT NOT NULL,
  terms_fp        TEXT NOT NULL,
  at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS booking_attempts (
  idempotency_key TEXT PRIMARY KEY,
  approval_id     TEXT NOT NULL REFERENCES approvals(id),
  seq             INTEGER NOT NULL CHECK (seq IN (0, 1)),
  action          TEXT NOT NULL CHECK (action IN ('book','cancel','reschedule')),
  ref             TEXT NOT NULL,
  new_slot        TEXT,
  status          TEXT NOT NULL CHECK (status IN ('PENDING','CONFIRMED','FAILED','NOT_EXECUTED')),
  tool_calls      INTEGER NOT NULL DEFAULT 0,   -- mutating calls (book/cancel/reschedule) made
  error_code      TEXT,
  result_json     TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  verified_at     TEXT,
  UNIQUE (approval_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_attempt_per_approval
  ON booking_attempts(approval_id) WHERE status IN ('PENDING','CONFIRMED');

CREATE TABLE IF NOT EXISTS repeat_window (
  session_id  TEXT PRIMARY KEY,
  proposal_id TEXT NOT NULL,
  armed_at    TEXT NOT NULL
);
"""


def iso(dt: datetime) -> str:
    return dt.astimezone(IST).isoformat(timespec="seconds")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


class AgentStore:
    """Owns ``agent.db``. Re-open the same path to simulate a process restart."""

    def __init__(self, path: str | Path, clock: Clock) -> None:
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

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "AgentStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialized write transaction: commit on normal exit, roll back on ANY exit by exception
        (including BaseException, so a simulated process kill never leaves a half-written state)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ ids

    def next_id(self, c: sqlite3.Connection, prefix: str) -> str:
        key = f"ctr:{prefix}"
        row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        n = int(row[0] if row else 0) + 1
        c.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(n)),
        )
        return f"{prefix}_{n:06d}"

    # ------------------------------------------------------------------ reads (own transaction)

    def one(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def all(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def proposal(self, proposal_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))

    def approval(self, approval_id: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))

    def latest_attempt(self, approval_id: str) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM booking_attempts WHERE approval_id = ? ORDER BY seq DESC LIMIT 1", (approval_id,)
        )

    def attempts(self, approval_id: str | None = None) -> list[sqlite3.Row]:
        if approval_id is None:
            return self.all("SELECT * FROM booking_attempts ORDER BY created_at, seq, idempotency_key")
        return self.all("SELECT * FROM booking_attempts WHERE approval_id = ? ORDER BY seq", (approval_id,))

    def pending_attempts(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM booking_attempts WHERE status = 'PENDING' ORDER BY created_at, seq")

    def child_proposal(self, proposal_id: str) -> sqlite3.Row | None:
        """Latest proposal created as the re-ask for ``proposal_id`` (a needs_reapproval outcome)."""
        return self.one(
            "SELECT * FROM proposals WHERE parent_proposal_id = ? ORDER BY id DESC LIMIT 1", (proposal_id,)
        )

    # ------------------------------------------------------------------ attempt writes (take an open tx)

    @staticmethod
    def insert_attempt(
        c: sqlite3.Connection,
        *,
        key: str,
        approval_id: str,
        seq: int,
        action: str,
        ref: str,
        new_slot: str | None,
        now: datetime,
        error_code: str | None = None,
    ) -> None:
        c.execute(
            "INSERT INTO booking_attempts(idempotency_key, approval_id, seq, action, ref, new_slot, status,"
            " error_code, created_at, updated_at) VALUES (?,?,?,?,?,?,'PENDING',?,?,?)",
            (key, approval_id, seq, action, ref, new_slot, error_code, iso(now), iso(now)),
        )

    def update_attempt(
        self,
        key: str,
        *,
        status: str | None = None,
        error_code: str | None = None,
        result: dict[str, Any] | None = None,
        verified: bool = False,
        add_tool_calls: int = 0,
        clear_error: bool = False,
    ) -> None:
        now = iso(self.clock.now())
        with self.tx() as c:
            sets, args = ["updated_at = ?"], [now]
            if status is not None:
                sets.append("status = ?")
                args.append(status)
            if error_code is not None:
                sets.append("error_code = ?")
                args.append(error_code)
            if clear_error:
                sets.append("error_code = NULL")
            if result is not None:
                sets.append("result_json = ?")
                args.append(json.dumps(result, sort_keys=True, default=str))
            if verified:
                sets.append("verified_at = ?")
                args.append(now)
            if add_tool_calls:
                sets.append("tool_calls = tool_calls + ?")
                args.append(add_tool_calls)
            c.execute(f"UPDATE booking_attempts SET {', '.join(sets)} WHERE idempotency_key = ?", (*args, key))

    def set_proposal_status(self, proposal_id: str, status: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE proposals SET status = ? WHERE id = ?", (status, proposal_id))
