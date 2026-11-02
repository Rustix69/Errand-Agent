"""Harness for the safety-core tests: a real mock world + a real agent.db, both restartable.

Everything is asserted against DB rows and the mock's tool-call log, never reply wording.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from errand.clock import FakeClock
from errand.contract.models import Hold
from errand.core import (
    AgentStore,
    ApprovalGuard,
    ExecutionOutcome,
    Hooks,
    IdempotentExecutor,
    Limits,
)
from errand.mocks import DEMO_USER_ID, MockWorld, ScenarioConfig
from errand.wiring import tools_for
from tests.conftest import WEEK

SESSION = "s1"


class ProcessKilled(BaseException):
    """Test-only 'kill -9' at a chosen point. BaseException so no recovery code can swallow it."""


class Env:
    def __init__(
        self,
        tmp_path: Path,
        clock: FakeClock,
        *,
        config: ScenarioConfig | None = None,
        hooks: Hooks | None = None,
        limits: Limits | None = None,
        tools_wrapper: Any = None,
        approval_ttl_s: int = 180,
    ) -> None:
        self.tmp_path, self.clock = tmp_path, clock
        if config is None and not (tmp_path / "world.db").exists():
            config = ScenarioConfig(mode="forced")  # fresh world: only the failures a test forces
        # On a restart config stays None, so the stored scenario (mode, seed) is reused.
        self.world = MockWorld(tmp_path / "world.db", clock, config)
        self.store = AgentStore(tmp_path / "agent.db", clock)
        self.guard = ApprovalGuard(self.store, clock, approval_ttl_s=approval_ttl_s)
        self.tools = tools_for(self.world, DEMO_USER_ID)
        self.agent_tools = tools_wrapper(self.tools) if tools_wrapper else self.tools
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.out_of_band_keys: set[str] = set()  # world actions NOT made by the agent (declared by the test)
        self.executor = IdempotentExecutor(
            self.store, self.guard, self.agent_tools, clock, DEMO_USER_ID,
            limits=limits, hooks=hooks, on_event=lambda n, d: self.events.append((n, d)),
        )

    # ------------------------------------------------------------------ lifecycle

    def restart(self, **kw: Any) -> "Env":
        """A new process: fresh connections to the same world.db and agent.db."""
        self.close()
        return Env(self.tmp_path, self.clock, **kw)

    def close(self) -> None:
        for closer in (self.store.close, self.world.close):
            try:
                closer()
            except Exception:
                pass

    # ------------------------------------------------------------------ flow helpers

    def free_slot(self, service: str = "plumber", area: str = "Indiranagar", index: int = 0) -> tuple[str, str, int]:
        res = self.tools.search_providers(service, area, WEEK)
        for p in res.providers:
            if len(p.available_slots) > index:
                return p.provider_id, p.available_slots[index], p.price
        raise AssertionError("no free slot")

    def hold(self, **kw: Any) -> Hold:
        pid, slot, _ = self.free_slot(**kw)
        return self.tools.hold_slot(pid, slot)

    def propose(self, hold: Hold, session: str = SESSION) -> str:
        return self.guard.propose_booking(session, DEMO_USER_ID, hold)

    def approve(self, text: str = "yes", session: str = SESSION) -> str:
        d = self.guard.on_user_message(session, text)
        assert d.kind == "approved" and d.approval_id, d
        return d.approval_id

    def book_flow(self, **kw: Any) -> tuple[Hold, str, str]:
        """hold -> propose -> user says yes. Returns (hold, proposal_id, approval_id)."""
        h = self.hold(**kw)
        pid = self.propose(h)
        return h, pid, self.approve()

    def book_out_of_band(self, hold_id: str, key: str = "oob-key") -> None:
        """Someone/something other than the agent books this hold. Declared so the
        'no mutating call without approval' invariant stays strict for everything else."""
        self.out_of_band_keys.add(key)
        self.world.tools(DEMO_USER_ID).book(hold_id, key)

    def booking(self, provider_id: str, upcoming: bool = True):
        return next(
            b for b in self.tools.list_bookings(DEMO_USER_ID).bookings
            if b.provider_id == provider_id and b.is_upcoming == upcoming
        )

    # ------------------------------------------------------------------ inspection

    def calls(self, tool: str | None = None) -> list[dict[str, Any]]:
        return self.world.sim.calls(tool)

    def count(self, tool: str) -> int:
        return len(self.calls(tool))

    def attempts(self, approval_id: str | None = None) -> list[sqlite3.Row]:
        return self.store.attempts(approval_id)

    def rebinds(self) -> list[sqlite3.Row]:
        return self.store.all("SELECT * FROM approval_rebinds ORDER BY id")

    def user_bookings(self) -> list[dict[str, Any]]:
        return self.world.sim.bookings()


def kinds(out: ExecutionOutcome) -> str:
    return out.kind


def advance_to(clock: FakeClock, hh: int, mm: int, ss: int = 0) -> None:
    """Move the fake clock to a time later today (09:00 start)."""
    target = clock.now().replace(hour=hh, minute=mm, second=ss, microsecond=0)
    clock.set(target)


__all__ = ["Env", "ProcessKilled", "SESSION", "advance_to", "kinds"]
