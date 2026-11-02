"""Scenario configuration for the mock marketplace.

Two sources of outcomes, both reproducible:

* **Seeded randomness** (``mode="seeded"``): each random decision is a hash of
  ``(seed, tool, per-tool call counter)``. The counter lives in ``world.db``, so the
  sequence survives process restarts and does not depend on in-memory RNG state.
* **Forced outcomes**: ``Simulator.force(tool, kind)`` queues an exact outcome for
  the next call of that tool that passes argument validation. The queue is stored
  in ``world.db`` too, so it survives restarts.

``mode="forced"`` switches the random failure injection off entirely (book always
succeeds, quotes never drift) so a test sees *only* the failures it forces.
Fixed contract behaviour (cancel's first-call 503, configured reschedule
conflicts) still applies in both modes.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Outcomes a test may force, per tool.
_READ_ONLY = frozenset({"success", "unavailable", "timeout"})
_SIDE_EFFECT = frozenset(
    {
        "success",
        "unavailable",  # 503 before anything happens
        "timeout_before_commit",  # times out, nothing happened
        "timeout_after_commit",  # side effect committed, THEN times out
        "crash_after_commit",  # side effect committed, then SimulatedCrash
    }
)

FORCEABLE: dict[str, frozenset[str]] = {
    "search_providers": _READ_ONLY,
    "get_provider": _READ_ONLY,
    "get_booking_status": _READ_ONLY,
    "list_bookings": _READ_ONLY,
    "hold_slot": _READ_ONLY | {"price_change", "slot_unavailable"},
    "book": _SIDE_EFFECT | {"slot_taken"},
    "cancel": _SIDE_EFFECT,
    "reschedule": _SIDE_EFFECT | {"slot_conflict"},
}

BOOK_OUTCOMES = ("success", "slot_taken", "timeout_after_commit", "timeout_before_commit")


class ConflictSlot(BaseModel):
    """A slot for which ``reschedule`` returns ``slot_conflict``.
    ``provider_id=None`` means any provider."""

    slot: str
    provider_id: str | None = None


class ScenarioConfig(BaseModel):
    seed: int = 42
    mode: Literal["seeded", "forced"] = "seeded"

    hold_minutes: int = 10

    # Contract: success 70%, slot_taken 15%, timeout 15% (half of those booked).
    book_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "success": 0.70,
            "slot_taken": 0.15,
            "timeout_after_commit": 0.075,
            "timeout_before_commit": 0.075,
        }
    )

    # Probability that hold_slot quotes a different price than search, and the deltas.
    price_change_rate: float = 0.2
    price_change_deltas: list[int] = Field(default_factory=lambda: [100, 150, 200, -100])

    # Fraction of a provider's schedule already taken by other (invisible) customers.
    busy_rate: float = 0.3

    cancel_first_call_fails: bool = True
    reschedule_conflict_slots: list[ConflictSlot] = Field(default_factory=list)
    # At seed time, add a demo conflict: AC provider's booking moved to next Saturday 10:00.
    seed_default_conflicts: bool = True

    # Simulated latency, applied through the Clock (instant with FakeClock).
    timeout_latency_s: float = 3.0
    tool_latency_s: dict[str, float] = Field(default_factory=dict)

    max_search_days: int = 14
    max_results: int = 8

    @field_validator("book_weights")
    @classmethod
    def _check_weights(cls, v: dict[str, float]) -> dict[str, float]:
        unknown = set(v) - set(BOOK_OUTCOMES)
        if unknown:
            raise ValueError(f"unknown book outcomes: {sorted(unknown)}")
        if any(w < 0 for w in v.values()) or sum(v.values()) <= 0:
            raise ValueError("book_weights must be non-negative and sum to > 0")
        return v

    @field_validator("price_change_rate", "busy_rate")
    @classmethod
    def _check_rate(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("rates must be within [0, 1]")
        return v


def roll(seed: int, *parts: object) -> float:
    """Deterministic uniform float in [0, 1) from the seed and any identifying parts."""
    digest = hashlib.sha256("|".join(map(str, (seed, *parts))).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def pick_weighted(weights: dict[str, float], r: float) -> str:
    """Map r in [0, 1) onto ``weights`` (iterated in BOOK_OUTCOMES order for stability)."""
    total = sum(weights.values())
    acc = 0.0
    last = None
    for name in BOOK_OUTCOMES:
        w = weights.get(name, 0.0)
        if w <= 0:
            continue
        last = name
        acc += w / total
        if r < acc:
            return name
    assert last is not None
    return last
