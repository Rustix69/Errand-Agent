"""Runtime wiring and the ``Agent`` façade (one chat session = one LangGraph thread)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from errand.agent.graph import build_graph
from errand.clock import Clock
from errand.contract.port import ToolPort
from errand.core import (
    AgentStore, ApprovalGuard, Budget, BudgetLimits, IdempotentExecutor, Limits, resolve_pending,
)
from errand.model.base import ModelClient

Emit = Callable[[str, dict[str, Any]], None]


@dataclass
class Runtime:
    store: AgentStore
    guard: ApprovalGuard
    executor: IdempotentExecutor
    tools: ToolPort
    model: ModelClient
    clock: Clock
    user_id: str
    limits: Limits = field(default_factory=Limits)
    budget_limits: BudgetLimits = field(default_factory=BudgetLimits)
    emit: Emit = lambda name, data: None
    _budgets: dict[str, Budget] = field(default_factory=dict)

    def new_run(self) -> str:
        with self.store.tx() as c:
            run_id = self.store.next_id(c, "run")
        self._budgets[run_id] = Budget(self.clock, self.budget_limits)
        return run_id

    def budget(self, run_id: str) -> Budget:
        """Per-user-message budget. After a process restart it is rebuilt: the resumed run gets a new one."""
        if run_id not in self._budgets:
            self._budgets[run_id] = Budget(self.clock, self.budget_limits)
            self.emit("budget_recreated", {"run_id": run_id})
        return self._budgets[run_id]


@dataclass(frozen=True)
class Reply:
    status: str
    kind: str
    message: str
    waiting: str | None
    proposal_id: str | None
    reason: str | None
    run_id: str


class UnfinishedRun(RuntimeError):
    """The thread has a run that stopped mid-graph (crash). Call ``Agent.resume()`` first."""


class Agent:
    def __init__(self, rt: Runtime, session_id: str, checkpointer: SqliteSaver) -> None:
        self.rt, self.session_id = rt, session_id
        self.graph = build_graph(rt).compile(checkpointer=checkpointer)
        self.cfg = {"configurable": {"thread_id": session_id}}

    def _snapshot(self) -> Any:
        return self.graph.get_state(self.cfg)

    def _reply(self) -> Reply:
        v = self._snapshot().values
        f = v["final"]
        return Reply(f["status"], f["kind"], f["message"], f["waiting"], f["proposal_id"], f["reason"], v["run_id"])

    def send(self, text: str) -> Reply:
        snap = self._snapshot()
        if any(t.interrupts for t in snap.tasks):  # paused waiting for the user: resume that node
            self.graph.invoke(Command(resume=text), self.cfg)
        elif snap.next:
            raise UnfinishedRun(f"run stopped before {snap.next}; call resume()")
        else:
            self.graph.invoke(
                {"session_id": self.session_id, "user_id": self.rt.user_id, "user_message": text}, self.cfg
            )
        return self._reply()

    def resume(self) -> Reply | None:
        """Continue a run that stopped mid-graph. The interrupted node re-runs from its start."""
        snap = self._snapshot()
        if not snap.next or any(t.interrupts for t in snap.tasks):
            return None
        self.graph.invoke(None, self.cfg)
        return self._reply()

    def state(self) -> dict[str, Any]:
        return dict(self._snapshot().values)


class AgentHandle:
    """Owns the checkpoint connection (its own sqlite file, separate from agent.db)."""

    def __init__(self, agent: Agent, conn: sqlite3.Connection) -> None:
        self.agent, self._conn = agent, conn

    def close(self) -> None:
        self._conn.close()


def create_agent(rt: Runtime, session_id: str, checkpoint_path: str | Path) -> AgentHandle:
    """Startup: resolve every PENDING attempt through the source of truth (never by re-booking), then
    open the LangGraph checkpointer in its own sqlite file."""
    resolutions = resolve_pending(rt.store, rt.tools, rt.user_id, rt.clock, on_event=rt.emit)
    rt.emit("startup_recovery", {"resolved": [(r.key, r.result) for r in resolutions]})
    conn = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return AgentHandle(Agent(rt, session_id, SqliteSaver(conn)), conn)
