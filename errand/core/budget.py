"""Bounded execution: per-user-message caps on model turns, tool calls and wall-clock time.

Charged BEFORE each call and never in the middle of one, so an exhausted budget can leave
an attempt PENDING (to be resolved later) but never half-way through a tool call.
"""

from __future__ import annotations

from dataclasses import dataclass

from errand.clock import Clock


class BudgetExhausted(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # "tool_calls" | "model_turns" | "wall_clock"


@dataclass(frozen=True)
class BudgetLimits:
    max_model_turns: int = 8
    max_tool_calls: int = 12
    wall_clock_s: float = 30.0


class Budget:
    def __init__(self, clock: Clock, limits: BudgetLimits | None = None) -> None:
        self.clock = clock
        self.limits = limits or BudgetLimits()
        self.started = clock.monotonic()
        self.tool_calls = 0
        self.model_turns = 0

    def elapsed(self) -> float:
        return self.clock.monotonic() - self.started

    def _check_time(self) -> None:
        if self.elapsed() >= self.limits.wall_clock_s:
            raise BudgetExhausted("wall_clock")

    def charge_tool(self) -> None:
        self._check_time()
        if self.tool_calls >= self.limits.max_tool_calls:
            raise BudgetExhausted("tool_calls")
        self.tool_calls += 1

    def charge_model_turn(self) -> None:
        self._check_time()
        if self.model_turns >= self.limits.max_model_turns:
            raise BudgetExhausted("model_turns")
        self.model_turns += 1
