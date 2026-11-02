from __future__ import annotations

import os
import socket
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from errand.clock import FakeClock
from errand.mocks import MarketplaceTools, MockWorld, ScenarioConfig

WEEK = {"start": "2026-11-02", "end": "2026-11-08"}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_world(tmp_path: Path, clock: FakeClock) -> Iterator[Callable[..., MockWorld]]:
    """Open a world.db under tmp_path. Calling it again with the same name simulates a restart."""
    opened: list[MockWorld] = []

    def _make(config: ScenarioConfig | None = None, name: str = "world.db", clk=None) -> MockWorld:
        w = MockWorld(tmp_path / name, clk or clock, config)
        opened.append(w)
        return w

    yield _make
    for w in opened:
        try:
            w.close()
        except Exception:
            pass


@pytest.fixture
def world(make_world) -> MockWorld:
    """Forced mode: no random failures unless a test forces them."""
    return make_world(ScenarioConfig(mode="forced"))


@pytest.fixture
def tools(world: MockWorld) -> MarketplaceTools:
    return world.tools()


def free_slot(tools: MarketplaceTools, service: str = "plumber", area: str = "Indiranagar", index: int = 0):
    """(provider_id, slot, price) of an available slot from a real search."""
    res = tools.search_providers(service, area, WEEK)
    for p in res.providers:
        if len(p.available_slots) > index:
            return p.provider_id, p.available_slots[index], p.price
    raise AssertionError("no free slot in seeded world")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Every test runs with no API key and no network: a connection attempt is an error, so nothing can silently
    reach a real model. (SQLite and the in-process mocks do not use sockets.)"""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def blocked(self, *args, **kwargs):
        raise RuntimeError("network access is blocked in tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    yield
    os.environ.pop("ANTHROPIC_API_KEY", None)  # a test that loaded a temp .env must not leak it
