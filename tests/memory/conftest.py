from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from errand.clock import FakeClock
from errand.core import AgentStore
from errand.memory import SeedInfo, SqliteMemoryStore, TurnLog, seed_demo

USER = "u_demo"


@pytest.fixture
def seeded(tmp_path: Path, clock: FakeClock) -> Iterator[tuple[SqliteMemoryStore, TurnLog, SeedInfo, AgentStore]]:
    store = AgentStore(tmp_path / "agent.db", clock)
    memory, turns = SqliteMemoryStore(store, clock), TurnLog(store, clock)
    yield memory, turns, seed_demo(memory, turns, USER), store
    store.close()
