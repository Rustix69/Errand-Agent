"""Composition root for a running agent (the CLI, and anything else that needs the whole thing).

It builds the stores, guard, executor, memory, context builder, poller and tracer, and wires the tracer in the
three places that matter:
  * ``Runtime.emit``                      (graph, router, poller, context events)
  * the executor's ``on_event``           (attempts, tool calls and errors of the safety core)
  * ``tracer.wrap_tools(tools)``          (a short summary of what each tool call returned)
It imports nothing from ``errand.mocks``: the caller hands in a ``ToolPort`` (see ``errand/wiring.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from errand.agent import AgentHandle, Poller, Runtime, create_agent
from errand.agent.context import DEFAULT_BUDGET, ContextBuilder
from errand.clock import Clock
from errand.config import Settings
from errand.contract.port import ToolPort
from errand.core import AgentStore, ApprovalGuard, BudgetLimits, Hooks, IdempotentExecutor, Limits
from errand.memory import MemoryStore, SqliteMemoryStore, TurnLog, seed_demo
from errand.model.base import ModelClient
from errand.trace import TraceLogger


@dataclass
class App:
    settings: Settings
    clock: Clock
    store: AgentStore
    guard: ApprovalGuard
    executor: IdempotentExecutor
    memory: MemoryStore
    turns: TurnLog
    rt: Runtime
    poller: Poller
    handle: AgentHandle
    tracer: TraceLogger
    session: str
    user_id: str
    data_dir: Path

    @property
    def agent(self) -> Any:
        return self.handle.agent

    def close(self) -> None:
        self.handle.close()
        self.store.close()


def build_app(settings: Settings, tools: ToolPort, model: ModelClient, clock: Clock, data_dir: str | Path, *,
              user_id: str, session: str = "demo", seed: bool = True, hooks: Hooks | None = None,
              budget_limits: BudgetLimits | None = None, context_budget: int = DEFAULT_BUDGET) -> App:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    tracer = TraceLogger(clock, data_dir / "traces", prices=settings.prices)
    store = AgentStore(data_dir / "agent.db", clock)
    guard = ApprovalGuard(store, clock)
    tools = tracer.wrap_tools(tools)  # tool results get a short summary in the trace
    executor = IdempotentExecutor(store, guard, tools, clock, user_id, limits=Limits(), hooks=hooks, on_event=tracer)
    memory, turns = SqliteMemoryStore(store, clock), TurnLog(store, clock)
    if seed:
        seed_demo(memory, turns, user_id)  # idempotent
    context = ContextBuilder(store, memory, turns, tracer, context_budget)
    rt = Runtime(store, guard, executor, tools, model, clock, user_id, memory, turns, context,
                 budget_limits=budget_limits or BudgetLimits(), emit=tracer)
    poller = Poller(rt)
    handle = create_agent(rt, session, data_dir / "checkpoints.db")  # startup recovery runs here
    return App(settings, clock, store, guard, executor, memory, turns, rt, poller, handle, tracer, session, user_id,
               data_dir)
