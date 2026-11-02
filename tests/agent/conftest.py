from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from errand.clock import FakeClock
from errand.model import ScriptedModel
from tests.agent.helpers import AgentEnv, check_run_invariants
from tests.core.helpers import Env
from tests.core.invariants import assert_invariants


@pytest.fixture
def make_agent(tmp_path: Path, clock: FakeClock) -> Iterator[Callable[..., AgentEnv]]:
    made: list[AgentEnv] = []

    def _make(steps=(), *, env_kw: dict | None = None, **kw) -> AgentEnv:
        env = Env(tmp_path, clock, **(env_kw or {}))
        a = AgentEnv(env, ScriptedModel(list(steps)), **kw)
        made.append(a)
        return a

    yield _make
    for a in made:
        check_run_invariants(a)
        assert_invariants(a.env)
    for a in made:
        a.close()
        a.env.close()


@pytest.fixture
def env(tmp_path: Path, clock: FakeClock) -> Iterator[Env]:
    """A bare world + agent.db for router-level tests (no graph)."""
    e = Env(tmp_path, clock)
    yield e
    assert_invariants(e)
    e.close()
