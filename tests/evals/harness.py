"""Eval harness: a scenario = a simulated user (messages, optionally with world events fired first), a
ScriptedModel script and the expected outcome. Every scenario records `expected` up front and `actual` from
DB state and trace events, which the conftest hook turns into artifacts/eval_table.md. Never reply wording.

    run = scenario("S6", "Price changes after approval", expected="...", script=[...], user=["find a plumber"])
    run.say("yes", before=lambda a: a.env.clock.advance(minutes=11))     # a world event, then the user
    run.restart(script=[...])                                           # kill the process, reopen both DBs
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from errand.agent import Reply
from tests.agent.helpers import AgentEnv


@dataclass
class Turn:
    """One simulated-user message. `before(a)` fires a world event first (force an outcome, take a slot,
    advance the clock ...). `text` may be a function of the replies so far (a user reacting to the agent)."""

    text: str | Callable[[list[Reply]], str]
    before: Callable[[AgentEnv], None] | None = None


class Run:
    def __init__(self, make: Any, a: AgentEnv, rec: Any) -> None:
        self.make, self.a, self.rec = make, a, rec
        self.replies: list[Reply] = []
        self.extra: dict[str, Any] = {}
        self.prior_model_calls = 0

    # ------------------------------------------------------------------ playing

    def say(self, text: str | Callable[[list[Reply]], str], before: Callable[[AgentEnv], None] | None = None) -> Reply:
        if before:
            before(self.a)
        text = text(self.replies) if callable(text) else text
        try:
            reply = self.a.say(text)
        finally:
            self.record()  # `actual` is up to date even if the "process" dies inside this call
        self.replies.append(reply)
        self.record()
        return reply

    def poll(self, before: Callable[[AgentEnv], None] | None = None) -> Any:
        """The proactive poller runs with NO user message. `before(a)` fires a simulator event first."""
        if before:
            before(self.a)
        try:
            return self.a.poll()
        finally:
            self.record()

    def play(self, turns: list[str | Turn]) -> None:
        for t in turns:
            self.say(*((t.text, t.before) if isinstance(t, Turn) else (t,)))

    def restart(self, script: list[Any] = (), **agent_kw: Any) -> AgentEnv:
        """The process dies and a new one starts on the SAME world.db / agent.db / checkpoints.db: new
        connections, new ScriptedModel, startup recovery runs. The history of events carries over."""
        history = self.make.retire(self.a)
        self.prior_model_calls += self.a.model.calls
        self.a = self.make(script, **agent_kw)
        self.a.events[:0] = history
        self.record()
        return self.a

    # ------------------------------------------------------------------ recording

    def mine(self) -> list[dict[str, Any]]:
        return [b for b in self.a.env.user_bookings() if b["idempotency_key"]]

    def facts(self) -> dict[str, Any]:
        a, store = self.a, self.a.env.store
        proactive = {}
        events = store.all("SELECT status, reason FROM proactive_events ORDER BY id")
        if events:  # only scenarios that involve the poller show these columns
            proactive = {
                "proactive_events": [f"{e['status']}{'/' + e['reason'] if e['reason'] else ''}" for e in events],
                "notices": [r["kind"] for r in store.all("SELECT kind FROM outbox ORDER BY id")],
                "holds": a.env.count("hold_slot"),
            }
        return {
            **proactive,
            "replies": [f"{r.status}/{r.kind}" for r in self.replies],
            "agent_bookings": len(self.mine()),
            "world_calls book/cancel/reschedule": "/".join(str(a.env.count(t)) for t in ("book", "cancel", "reschedule")),
            "approvals": store.one("SELECT COUNT(*) FROM approvals")[0],
            "attempts": [f"{x['status']}{'+verified' if x['verified_at'] else ''}" for x in store.attempts()],
            "model_calls": self.prior_model_calls + a.model.calls,
        }

    def record(self) -> None:
        parts = {**self.facts(), **self.extra}
        self.rec.actual = "; ".join(f"{k}={v}" for k, v in parts.items())
