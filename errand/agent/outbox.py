"""Notices the agent wants the user to see, written by the proactive poller (CODE-written text only).

Kept free of heavy imports so the context builder can read recent notices without a cycle. An outbox row is
created exactly once per event (``event_id`` is UNIQUE); delivery is at-least-once: undelivered rows are shown
again after a restart until the caller marks them delivered.
"""

from __future__ import annotations

from errand.core.store import AgentStore

DDL = (
    """CREATE TABLE IF NOT EXISTS booking_snapshots (
         user_id    TEXT NOT NULL,
         booking_id TEXT NOT NULL,
         status     TEXT NOT NULL,
         slot       TEXT NOT NULL,
         provider_id TEXT NOT NULL,
         service    TEXT NOT NULL,
         seen_at    TEXT NOT NULL,
         PRIMARY KEY (user_id, booking_id)
       )""",
    """CREATE TABLE IF NOT EXISTS proactive_events (
         id           INTEGER PRIMARY KEY AUTOINCREMENT,
         user_id      TEXT NOT NULL,
         booking_id   TEXT NOT NULL,
         kind         TEXT NOT NULL,
         status       TEXT NOT NULL CHECK (status IN ('detected','notified','replaced','ignored')),
         reason       TEXT,
         booking_json TEXT NOT NULL,
         hold_id      TEXT,
         proposal_id  TEXT,
         detected_at  TEXT NOT NULL,
         notified_at  TEXT,
         UNIQUE (user_id, booking_id, kind)
       )""",
    """CREATE TABLE IF NOT EXISTS outbox (
         id           INTEGER PRIMARY KEY AUTOINCREMENT,
         session_id   TEXT NOT NULL,
         event_id     INTEGER NOT NULL UNIQUE REFERENCES proactive_events(id),
         kind         TEXT NOT NULL,
         message      TEXT NOT NULL,
         created_at   TEXT NOT NULL,
         delivered_at TEXT
       )""",
)


def ensure_schema(store: AgentStore) -> None:
    with store.tx() as c:
        for stmt in DDL:
            c.execute(stmt)


def recent_notices(store: AgentStore, session_id: str, n: int = 3) -> list[str]:
    """The last few notices for this session (delivered or not), oldest first. For the model's trusted state."""
    ensure_schema(store)
    rows = store.all("SELECT message FROM outbox WHERE session_id = ? ORDER BY id DESC LIMIT ?", (session_id, n))
    return [r["message"] for r in reversed(rows)]


def undelivered(store: AgentStore, session_id: str) -> list:
    ensure_schema(store)
    return store.all("SELECT * FROM outbox WHERE session_id = ? AND delivered_at IS NULL ORDER BY id", (session_id,))


def mark_delivered(store: AgentStore, ids: list[int]) -> None:
    with store.tx() as c:
        for i in ids:
            c.execute("UPDATE outbox SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
                      (store.clock.now().isoformat(timespec="seconds"), i))
