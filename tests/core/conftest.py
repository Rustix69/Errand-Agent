from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from errand.clock import FakeClock
from tests.core.helpers import Env
from tests.core.invariants import assert_invariants


@pytest.fixture
def make_env(tmp_path: Path, clock: FakeClock) -> Iterator[Callable[..., Env]]:
    """Factory so a test can pass hooks/limits/config. Invariants are checked on EVERY env at teardown."""
    envs: list[Env] = []

    def _make(**kw: Any) -> Env:
        env = Env(tmp_path, clock, **kw)
        envs.append(env)
        return env

    def _restart(old: Env, **kw: Any) -> Env:
        """New process: close the old connections, reopen the same world.db / agent.db."""
        old.close()
        return _make(**kw)

    _make.restart = _restart  # type: ignore[attr-defined]
    yield _make
    for env in envs[-1:]:  # the latest env sees the full world + agent history
        assert_invariants(env)
    for env in envs:
        env.close()


@pytest.fixture
def env(make_env: Callable[..., Env]) -> Env:
    return make_env()
