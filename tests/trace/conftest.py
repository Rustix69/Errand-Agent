from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from errand.clock import FakeClock
from errand.model import ScriptedModel
from errand.trace import TraceLogger
from tests.agent.helpers import AgentEnv, check_run_invariants
from tests.core.helpers import Env
from tests.core.invariants import assert_invariants


@pytest.fixture
def build(tmp_path: Path, clock: FakeClock) -> Iterator[Callable[..., tuple[AgentEnv, TraceLogger]]]:
    """A traced agent: world + agent.db + graph + a TraceLogger writing to <name>/traces."""
    made: list[AgentEnv] = []

    def _build(steps, name: str = "w", clk: FakeClock | None = None) -> tuple[AgentEnv, TraceLogger]:
        base = tmp_path / name
        base.mkdir()
        tracer = TraceLogger(clk or clock, base / "traces")
        env = Env(base, clk or clock, trace=tracer, tools_wrapper=tracer.wrap_tools)
        a = AgentEnv(env, ScriptedModel(list(steps)), trace=tracer)
        made.append(a)
        return a, tracer

    yield _build
    for a in made:
        check_run_invariants(a)
        assert_invariants(a.env)
    for a in made:
        a.close()
        a.env.close()
