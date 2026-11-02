"""The MemoryStore interface is real: a store that implements ONLY the Protocol's methods (nothing SQLite-specific)
drives the graph's memory commands, the context builder and the proactive poller. This is what "production would
swap in pgvector behind MemoryStore" rests on."""

from __future__ import annotations

from dataclasses import replace

from errand.agent.context import ContextBuilder
from errand.memory import Inspection, Memory, MemoryStore, OpResult, rank, service_of
from errand.model import ScriptedModel, say
from tests.agent.helpers import AgentEnv, context_rows, memory_items
from tests.core.helpers import Env

PROTOCOL_METHODS = {"usable", "forbidden_values", "relevant", "inspect", "add", "correct", "forget", "confirm", "reject"}


class ListMemoryStore:
    """A deliberately plain in-memory implementation. Only Protocol methods; no `.store`, no SQL."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._n = 0

    def _put(self, user_id, key, value, source, confirmed, tags, forgotten=False) -> Memory:
        self._n += 1
        m = Memory(f"fm_{self._n}", user_id, key, value, source, confirmed, tuple(tags), "2026-11-02T09:00:00+05:30")
        self.rows.append({"m": m, "forgotten": forgotten, "superseded": False})
        return m

    def _live(self, user_id):  # the filter: forgotten / superseded / unconfirmed-inferred are never usable
        return [r for r in self.rows if r["m"].user_id == user_id and not r["forgotten"] and not r["superseded"]]

    def usable(self, user_id):
        return [r["m"] for r in self._live(user_id) if r["m"].source == "explicit" or r["m"].confirmed]

    def forbidden_values(self, user_id):
        ok = {m.value.lower() for m in self.usable(user_id)}
        bad = [r["m"].value.lower() for r in self.rows if r["m"].user_id == user_id and (
            r["forgotten"] or r["superseded"] or (r["m"].source == "inferred" and not r["m"].confirmed))]
        return {v for v in bad if len(v) >= 4 and v not in ok}

    def relevant(self, user_id, words, limit=12, service=None):
        return rank(self.usable(user_id), words, service_of(service))[:limit]

    def inspect(self, user_id):
        ins = Inspection(forgotten_count=sum(1 for r in self.rows if r["forgotten"]))
        for r in self._live(user_id):
            (ins.current if r["m"].source == "explicit" or r["m"].confirmed else ins.awaiting).append(r["m"])
        return ins

    def add(self, user_id, key, value, *, source="explicit", confirmed=None, tags=(), forgotten=False, at=None):
        for r in self._live(user_id):
            if source == "explicit" and r["m"].key == key:
                r["superseded"] = True
        m = self._put(user_id, key, value, source, (source == "explicit") if confirmed is None else confirmed, tags, forgotten)
        return OpResult(True, "saved", m, [m.id])

    def _find(self, user_id, target):
        return [r for r in self._live(user_id) if target in (r["m"].id, r["m"].key)]

    def correct(self, user_id, target, value):
        found = self._find(user_id, target)
        if not found:
            return OpResult(False, "not_found")
        old = found[0]["m"]
        res = self.add(user_id, old.key, value, tags=old.tags)
        return replace(res, kind="corrected")

    def forget(self, user_id, target):
        found = self._find(user_id, target)
        for r in found:
            r["forgotten"] = True
        return OpResult(bool(found), "forgotten" if found else "not_found", found[0]["m"] if found else None,
                        [r["m"].id for r in found])

    def confirm(self, user_id, memory_id):
        for r in self._live(user_id):
            if r["m"].id == memory_id and r["m"].source == "inferred":
                r["m"] = replace(r["m"], confirmed=True)
                return OpResult(True, "confirmed", r["m"], [memory_id])
        return OpResult(False, "not_found")

    def reject(self, user_id, memory_id):
        for r in self._live(user_id):
            if r["m"].id == memory_id and r["m"].source == "inferred":
                r["forgotten"] = True
                return OpResult(True, "rejected", r["m"], [memory_id])
        return OpResult(False, "not_found")


def seeded_fake() -> ListMemoryStore:
    s = ListMemoryStore()
    s.add("u_demo", "home_area", "HSR Layout", tags=("profile", "area"))
    s.add("u_demo", "earliest_time", "10:00", tags=("profile", "time"))
    s.add("u_demo", "plumber_budget", "Up to ₹1,500 for plumbing", tags=("plumber", "plumbing"))
    s.add("u_demo", "gate_code", "4821 ZQFAKE", tags=("profile",), forgotten=True)  # forgotten: must never be sent
    s.add("u_demo", "slot_guess", "Prefers evening ZQGUESS", source="inferred", confirmed=False, tags=("profile", "time"))
    return s


def test_the_protocol_names_every_method_the_agent_calls():
    assert PROTOCOL_METHODS <= set(vars(MemoryStore))
    fake = ListMemoryStore()
    assert all(callable(getattr(fake, name)) for name in PROTOCOL_METHODS)


def test_the_graph_context_builder_and_poller_run_on_a_store_that_only_implements_the_protocol(tmp_path, clock):
    env = Env(tmp_path, clock)
    a = AgentEnv(env, ScriptedModel([say("Here are plumbers."), say("ok")]))
    fake = seeded_fake()
    a.rt.memory = fake
    a.context = a.rt.context = ContextBuilder(env.store, fake, a.turns, a.rt.emit)

    # 1) the context builder: relevant memories in, forgotten / unconfirmed never, redaction through the interface
    a.say("find me a plumber for my kitchen tap")
    section = memory_items(a.model.requests[0].system)
    assert {m["key"] for m in section} == {"home_area", "earliest_time", "plumber_budget"}
    assert "ZQFAKE" not in a.model.requests[0].system and "ZQGUESS" not in a.model.requests[0].system
    assert all(set(row["memory_ids"]) <= {m.id for m in fake.usable("u_demo")} for row in context_rows(a))

    # 2) the graph's memory commands (handled by code, no model call)
    n = a.model.calls
    assert a.say("forget my area").kind == "memory_forgotten" and a.model.calls == n
    assert "home_area" not in {m.key for m in fake.usable("u_demo")}
    assert a.say("I'm in Jadavpur").kind in ("answer", "model_error")  # `capture` saved it via add(); the model reply is incidental
    assert [m.value for m in fake.usable("u_demo") if m.key == "home_area"] == ["Jadavpur"]

    # 3) the poller reads the saved area and earliest time through the same interface
    assert fake.correct("u_demo", "home_area", "HSR Layout").kind == "corrected"  # (Jadavpur has no cleaner in the price cap)
    a.poll()
    bid = next(b["booking_id"] for b in a.env.user_bookings() if b["provider_id"] == "cl_04")
    a.env.world.sim.provider_cancels(bid, "Technician unavailable")
    assert a.poll().decisions == [(1, "replacement")]
    (search,) = [d for d in a.of("tool_call") if d["tool"] == "search_providers" and d["args"]["service"] == "home_cleaning"]
    assert search["args"]["area"] == "HSR Layout"  # the value the fake holds, read through usable()
    env.close()
