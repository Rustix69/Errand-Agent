"""Harness for graph tests: a real mock world + agent.db (tests.core Env), a ScriptedModel, a real
LangGraph with a SqliteSaver in its own file. Asserts DB rows and events, never reply wording."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from errand.agent import AgentHandle, Runtime, create_agent
from errand.agent.context import DEFAULT_BUDGET, ContextBuilder
from errand.agent.tools import MODEL_TOOL_NAMES
from errand.core import BudgetLimits
from errand.memory import SqliteMemoryStore, TurnLog, seed_demo
from errand.model import ScriptedModel
from tests.core.helpers import SESSION, Env

EXPECTED_TOOLS = ("search_providers", "get_provider", "list_bookings", "propose_booking", "propose_cancel",
                  "propose_reschedule", "ask_user")


class AgentEnv:
    def __init__(self, env: Env, model: ScriptedModel, budget_limits: BudgetLimits | None = None,
                 session: str = SESSION, checkpoint: Path | None = None, trace: Any = None,
                 context_budget: int = DEFAULT_BUDGET, seed: bool = False) -> None:
        self.env, self.model = env, model
        self.events = env.events  # the executor already appends here, so ordering is global

        def emit(name: str, data: dict[str, Any]) -> None:
            self.events.append((name, data))
            if trace:
                trace(name, data)  # executor events reach the tracer through Env(trace=...)

        self.memory = SqliteMemoryStore(env.store, env.clock)
        self.turns = TurnLog(env.store, env.clock)
        self.seed = seed_demo(self.memory, self.turns, "u_demo") if seed else None
        self.context = ContextBuilder(env.store, self.memory, self.turns, emit, context_budget)
        self.rt = Runtime(
            env.store, env.guard, env.executor, env.agent_tools, model, env.clock, "u_demo",
            self.memory, self.turns, self.context, budget_limits=budget_limits or BudgetLimits(), emit=emit,
        )
        self.handle: AgentHandle = create_agent(self.rt, session, checkpoint or env.tmp_path / "checkpoints.db")
        self.agent = self.handle.agent

    def say(self, text: str):
        return self.agent.send(text)

    def names(self) -> list[str]:
        return [n for n, _ in self.events]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [d for n, d in self.events if n == name]

    def world_mutations(self) -> list[dict[str, Any]]:
        return [c for c in self.env.calls() if c["tool"] in ("book", "cancel", "reschedule")]

    def close(self) -> None:
        self.handle.close()


def check_run_invariants(a: AgentEnv) -> None:
    """Every agent test: one `final` per `run_start`; the model always had the exact tool list."""
    assert len(a.of("final")) == len(a.of("run_start")), (len(a.of("final")), len(a.of("run_start")))
    for req in a.model.requests:
        assert req.tool_names == EXPECTED_TOOLS == MODEL_TOOL_NAMES, req.tool_names


def memory_items(system: str) -> list[dict[str, Any]]:
    """The saved-memories section of a model request's system prompt (what the model was told it knows)."""
    import json

    if "Saved memories (" not in system:
        return []
    return json.loads(system.split("Saved memories (")[1].split("\n")[1])


def request_text(req) -> str:
    return req.system + "\n" + "\n".join(m.content for m in req.messages)


def context_rows(a: AgentEnv) -> list[dict[str, Any]]:
    import json

    rows = a.env.store.all("SELECT * FROM context_log ORDER BY id")
    return [{**dict(r), "memory_ids": json.loads(r["memory_ids"]), "dropped_memory_ids": json.loads(r["dropped_memory_ids"])}
            for r in rows]
