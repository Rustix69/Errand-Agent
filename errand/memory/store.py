"""Persistent user memory, the turn log and the per-turn context log, all in ``agent.db``.

THE RULE: forgotten facts and unconfirmed inferred facts never reach the model. It is enforced in SQL,
by the view ``usable_memories``, and every retrieval reads that view and nothing else. Python ranking
(``rank``) only ever receives rows the view already returned, so it cannot un-filter anything.

* ``explicit``: the user said it. Usable at once.
* ``inferred``: the agent guessed it. Usable only after the user confirms it (``confirm``).
* ``forgotten``: tombstone kept for audit; never retrieved. ``superseded_by``: an old value replaced by a
  correction; kept for inspection, never retrieved.

Memory is only ever written by deterministic code (commands, capture, seeds), never by the model.
Tables are created through ``AgentStore``'s public ``tx()`` so ``store.py`` in the core is untouched.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Protocol

from errand.clock import Clock
from errand.core.store import AgentStore, iso

DDL = (
    """CREATE TABLE IF NOT EXISTS memories (
         id            TEXT PRIMARY KEY,
         user_id       TEXT NOT NULL,
         key           TEXT NOT NULL,
         value         TEXT NOT NULL,
         source        TEXT NOT NULL CHECK (source IN ('explicit','inferred')),
         confirmed     INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0,1)),
         forgotten     INTEGER NOT NULL DEFAULT 0 CHECK (forgotten IN (0,1)),
         superseded_by TEXT,
         tags          TEXT NOT NULL DEFAULT '[]',
         created_at    TEXT NOT NULL,
         updated_at    TEXT NOT NULL
       )""",
    # The hard filter. Nothing that is forgotten, replaced, or an unconfirmed guess is ever selectable here.
    """CREATE VIEW IF NOT EXISTS usable_memories AS
         SELECT * FROM memories
         WHERE forgotten = 0 AND superseded_by IS NULL AND (source = 'explicit' OR confirmed = 1)""",
    """CREATE TABLE IF NOT EXISTS turns (
         session_id TEXT NOT NULL,
         seq        INTEGER NOT NULL,
         user_id    TEXT NOT NULL,
         role       TEXT NOT NULL CHECK (role IN ('user','assistant')),
         content    TEXT NOT NULL,
         at         TEXT NOT NULL,
         PRIMARY KEY (session_id, seq)
       )""",
    """CREATE TABLE IF NOT EXISTS context_log (
         id                 INTEGER PRIMARY KEY AUTOINCREMENT,
         run_id             TEXT,
         session_id         TEXT NOT NULL,
         turn               INTEGER NOT NULL,
         at                 TEXT NOT NULL,
         budget             INTEGER NOT NULL,
         tokens             INTEGER NOT NULL,
         memory_ids         TEXT NOT NULL,
         dropped_memory_ids TEXT NOT NULL,
         turns_included     INTEGER NOT NULL,
         summary_included   INTEGER NOT NULL
       )""",
)


def ensure_schema(store: AgentStore) -> None:
    with store.tx() as c:
        for stmt in DDL:
            c.execute(stmt)


@dataclass(frozen=True)
class Memory:
    id: str
    user_id: str
    key: str
    value: str
    source: str
    confirmed: bool
    tags: tuple[str, ...]
    updated_at: str

    @staticmethod
    def of(row: sqlite3.Row) -> "Memory":
        return Memory(row["id"], row["user_id"], row["key"], row["value"], row["source"], bool(row["confirmed"]),
                      tuple(json.loads(row["tags"])), row["updated_at"])


@dataclass
class Inspection:
    current: list[Memory] = field(default_factory=list)  # usable now
    awaiting: list[Memory] = field(default_factory=list)  # inferred, not yet confirmed (NOT usable)
    forgotten_count: int = 0


@dataclass
class OpResult:
    ok: bool
    kind: str  # saved | corrected | forgotten | confirmed | rejected | not_found | ambiguous | unchanged
    memory: Memory | None = None
    ids: list[str] = field(default_factory=list)
    candidates: list[Memory] = field(default_factory=list)


class MemoryStore(Protocol):
    """Production would swap in a vector-backed store (e.g. pgvector) behind this."""

    def relevant(self, user_id: str, words: set[str], limit: int = 12, service: str | None = None) -> list[Memory]: ...
    def inspect(self, user_id: str) -> Inspection: ...
    def add(self, user_id: str, key: str, value: str, **kw: Any) -> OpResult: ...
    def correct(self, user_id: str, target: str, value: str) -> OpResult: ...
    def forget(self, user_id: str, target: str) -> OpResult: ...
    def confirm(self, user_id: str, memory_id: str) -> OpResult: ...
    def reject(self, user_id: str, memory_id: str) -> OpResult: ...


# --------------------------------------------------------------------------------------- ranking (Python)

STOPWORDS = frozenset(
    "a an and any are as at be but by can could do does for from get give had has have her him his how i if in "
    "into is it its just let like make me my need needs not now of on one or our out please should so some that "
    "the their them then there these they this to too us want was we were what when where which who will with "
    "would you your find book booking looking help week today tomorrow same last time next also about only under over after before than fine very much more less each every visits".split()
)


def words_of(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3 and w not in STOPWORDS}


def same_word(a: str, b: str) -> bool:
    """Cheap stem-ish match: equal, one a prefix of the other (>= 4 chars), or the same first 5 letters."""
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return (len(short) >= 4 and long.startswith(short)) or (len(short) >= 5 and a[:5] == b[:5])


# Which tags mean "this memory is about one kind of service". Rows carrying such a tag are matched by tags only.
SERVICE_TAGS: dict[str, frozenset[str]] = {
    "plumber": frozenset({"plumber", "plumbing"}),
    "electrician": frozenset({"electrician", "electrical", "wiring"}),
    "ac_servicing": frozenset({"ac", "ac_servicing", "servicing"}),
    "carpenter": frozenset({"carpenter", "carpentry"}),
    "home_cleaning": frozenset({"cleaning", "cleaner", "home_cleaning"}),
}
_ALL_SERVICE_TAGS = frozenset().union(*SERVICE_TAGS.values())


def service_of(text: str | None) -> str | None:
    """The service a request is about ("plumber", "ac servicing", "AC_servicing" ...), or None if unknown."""
    tokens = re.findall(r"[a-z]+", (text or "").lower())
    return next((k for k, tags in SERVICE_TAGS.items() if any(same_word(t, g) for t in tokens for g in tags)), None)


def score(m: Memory, words: set[str], service: str | None = None) -> int:
    """0 = not relevant. A memory tagged for a service is matched by TAGS only (a stray shared word such as
    "kitchen" must not pull in a carpenter note), and when the request's service is known a memory tagged for a
    DIFFERENT service is dropped outright. Memories without a service tag also get keyword overlap."""
    svc_tags = {t.lower() for t in m.tags} & _ALL_SERVICE_TAGS
    if svc_tags and service and not svc_tags & SERVICE_TAGS[service]:
        return 0
    s = 3 if "profile" in m.tags else 0
    s += 2 * sum(1 for t in m.tags if t != "profile" and any(same_word(t.lower(), w) for w in words))
    if svc_tags:
        s += 2 if service else 0  # about the service the user is asking for
    else:
        s += sum(1 for rw in words_of(f"{m.key.replace('_', ' ')} {m.value}") if any(same_word(rw, w) for w in words))
    return s


def rank(rows: list[Memory], words: set[str], service: str | None = None) -> list[Memory]:
    """Order ALREADY-FILTERED rows by relevance (profile 3, tag hit 2 each, keyword overlap 1 each, see ``score``).
    Score 0 means not relevant and is dropped. Newest wins ties. Never called with unfiltered rows."""
    scored = [(score(m, words, service), m) for m in rows]
    scored = [(s, m) for s, m in scored if s > 0]
    scored.sort(key=lambda sm: (sm[1].updated_at, sm[1].id), reverse=True)  # newest first ...
    scored.sort(key=lambda sm: -sm[0])  # ... then by score (stable, so newest wins ties)
    return [m for _, m in scored]


# --------------------------------------------------------------------------------------- the store


class SqliteMemoryStore:
    def __init__(self, store: AgentStore, clock: Clock) -> None:
        self.store, self.clock = store, clock
        ensure_schema(store)

    # ------------------------------------------------------------------ retrieval (SQL filter, then rank)

    def usable(self, user_id: str) -> list[Memory]:
        """The ONLY retrieval query. It reads the filtering view, so forbidden rows are never selected."""
        rows = self.store.all("SELECT * FROM usable_memories WHERE user_id = ? ORDER BY id", (user_id,))
        return [Memory.of(r) for r in rows]

    def relevant(self, user_id: str, words: set[str], limit: int = 12, service: str | None = None) -> list[Memory]:
        return rank(self.usable(user_id), words, service_of(service))[:limit]

    # ------------------------------------------------------------------ inspect / write

    @staticmethod
    def _current(c: sqlite3.Connection, user_id: str) -> list[sqlite3.Row]:
        return c.execute("SELECT * FROM memories WHERE user_id = ? AND forgotten = 0 AND superseded_by IS NULL "
                         "ORDER BY id", (user_id,)).fetchall()

    def inspect(self, user_id: str) -> Inspection:
        rows = self.store.all(
            "SELECT * FROM memories WHERE user_id = ? AND forgotten = 0 AND superseded_by IS NULL ORDER BY id", (user_id,)
        )
        n = self.store.one("SELECT COUNT(*) FROM memories WHERE user_id = ? AND forgotten = 1", (user_id,))[0]
        out = Inspection(forgotten_count=n)
        for r in rows:
            m = Memory.of(r)
            (out.current if (m.source == "explicit" or m.confirmed) else out.awaiting).append(m)
        return out

    def _insert(self, c: sqlite3.Connection, user_id: str, key: str, value: str, source: str, confirmed: bool,
                tags: tuple[str, ...], forgotten: bool = False, at: str | None = None) -> str:
        mid = self.store.next_id(c, "m")
        now = at or iso(self.clock.now())
        c.execute(
            "INSERT INTO memories(id, user_id, key, value, source, confirmed, forgotten, tags, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mid, user_id, key, value, source, int(confirmed), int(forgotten), json.dumps(list(tags)), now, now),
        )
        return mid

    def _supersede(self, c: sqlite3.Connection, user_id: str, key: str, by: str) -> list[str]:
        old = [r["id"] for r in c.execute(
            "SELECT id FROM memories WHERE user_id = ? AND key = ? AND id != ? AND forgotten = 0 "
            "AND superseded_by IS NULL", (user_id, key, by)).fetchall()]
        for oid in old:
            c.execute("UPDATE memories SET superseded_by = ?, updated_at = ? WHERE id = ?",
                      (by, iso(self.clock.now()), oid))
        return old

    def _get(self, memory_id: str) -> Memory | None:
        r = self.store.one("SELECT * FROM memories WHERE id = ?", (memory_id,))
        return Memory.of(r) if r else None

    def add(self, user_id: str, key: str, value: str, *, source: str = "explicit", confirmed: bool | None = None,
            tags: tuple[str, ...] = (), forgotten: bool = False, at: str | None = None) -> OpResult:
        """Explicit statements are usable at once and REPLACE any current row with the same key (a correction).
        Inferred rows start unconfirmed unless the caller says otherwise (seeds)."""
        confirmed = (source == "explicit") if confirmed is None else confirmed
        with self.store.tx() as c:
            same = [r for r in self._current(c, user_id) if r["key"] == key and r["value"] == value
                    and r["source"] == "explicit"]
            if source == "explicit" and not forgotten and same:
                return OpResult(True, "unchanged", Memory.of(same[0]), [same[0]["id"]])
            mid = self._insert(c, user_id, key, value, source, confirmed, tuple(tags), forgotten, at)
            old = self._supersede(c, user_id, key, mid) if (source == "explicit" and not forgotten) else []
        return OpResult(True, "corrected" if old else "saved", self._get(mid), [mid, *old])

    def _resolve(self, user_id: str, target: str) -> list[Memory]:
        """A target is a memory id or a key. Only current (not forgotten / superseded) rows can be targeted."""
        rows = self.store.all(
            "SELECT * FROM memories WHERE user_id = ? AND forgotten = 0 AND superseded_by IS NULL "
            "AND (id = ? OR key = ?) ORDER BY id", (user_id, target, target))
        return [Memory.of(r) for r in rows]

    def correct(self, user_id: str, target: str, value: str) -> OpResult:
        found = self._resolve(user_id, target)
        if not found:
            return OpResult(False, "not_found")
        usable = [m for m in found if m.source == "explicit" or m.confirmed]
        if len(usable) > 1 and target not in {m.id for m in found}:
            return OpResult(False, "ambiguous", candidates=usable)
        old = (usable or found)[0]
        with self.store.tx() as c:
            mid = self._insert(c, user_id, old.key, value, "explicit", True, old.tags)
            self._supersede(c, user_id, old.key, mid)  # every current row of that key, the targeted one included
        return OpResult(True, "corrected", self._get(mid), [mid, old.id])

    def forget(self, user_id: str, target: str) -> OpResult:
        found = self._resolve(user_id, target)
        if not found:
            return OpResult(False, "not_found")
        with self.store.tx() as c:
            for m in found:
                c.execute("UPDATE memories SET forgotten = 1, updated_at = ? WHERE id = ?",
                          (iso(self.clock.now()), m.id))
        return OpResult(True, "forgotten", found[0], [m.id for m in found], found)

    def confirm(self, user_id: str, memory_id: str) -> OpResult:
        m = next((x for x in self._resolve(user_id, memory_id) if x.id == memory_id), None)
        if m is None or m.source != "inferred":
            return OpResult(False, "not_found")
        with self.store.tx() as c:
            c.execute("UPDATE memories SET confirmed = 1, updated_at = ? WHERE id = ?",
                      (iso(self.clock.now()), m.id))
            old = self._supersede(c, user_id, m.key, m.id)  # the user just chose this value over the current one
        return OpResult(True, "confirmed", self._get(m.id), [m.id, *old])

    def reject(self, user_id: str, memory_id: str) -> OpResult:
        m = next((x for x in self._resolve(user_id, memory_id) if x.id == memory_id), None)
        if m is None or m.source != "inferred":
            return OpResult(False, "not_found")
        with self.store.tx() as c:
            c.execute("UPDATE memories SET forgotten = 1, updated_at = ? WHERE id = ?",
                      (iso(self.clock.now()), m.id))
        return OpResult(True, "rejected", m, [m.id])

    def forbidden_values(self, user_id: str) -> set[str]:
        """Values that must not appear in ANY text sent to the model (forgotten, unconfirmed guesses, replaced),
        minus values that are currently usable. Used to redact raw conversation turns."""
        bad = self.store.all(
            "SELECT value FROM memories WHERE user_id = ? AND (forgotten = 1 OR superseded_by IS NOT NULL "
            "OR (source = 'inferred' AND confirmed = 0))", (user_id,))
        ok = {m.value.lower() for m in self.usable(user_id)}
        return {r["value"].lower() for r in bad if len(r["value"]) >= 4 and r["value"].lower() not in ok}


# --------------------------------------------------------------------------------------- turn log


@dataclass(frozen=True)
class Turn:
    session_id: str
    seq: int
    role: str
    content: str
    at: str


class TurnLog:
    """Durable conversation text (completed exchanges only). The context builder reads it; the graph writes it."""

    def __init__(self, store: AgentStore, clock: Clock) -> None:
        self.store, self.clock = store, clock
        ensure_schema(store)

    def append(self, session_id: str, user_id: str, role: str, content: str, at: str | None = None) -> None:
        with self.store.tx() as c:
            n = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM turns WHERE session_id = ?", (session_id,)).fetchone()[0]
            c.execute("INSERT INTO turns(session_id, seq, user_id, role, content, at) VALUES (?,?,?,?,?,?)",
                      (session_id, n, user_id, role, content, at or iso(self.clock.now())))

    def append_exchange(self, session_id: str, user_id: str, user_text: str, assistant_text: str) -> None:
        self.append(session_id, user_id, "user", user_text)
        self.append(session_id, user_id, "assistant", assistant_text)

    def session(self, session_id: str) -> list[Turn]:
        rows = self.store.all("SELECT * FROM turns WHERE session_id = ? ORDER BY seq", (session_id,))
        return [Turn(r["session_id"], r["seq"], r["role"], r["content"], r["at"]) for r in rows]

    def others(self, user_id: str, session_id: str) -> list[Turn]:
        rows = self.store.all("SELECT * FROM turns WHERE user_id = ? AND session_id != ? ORDER BY at, session_id, seq",
                              (user_id, session_id))
        return [Turn(r["session_id"], r["seq"], r["role"], r["content"], r["at"]) for r in rows]
