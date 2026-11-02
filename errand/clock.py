"""Injectable time source.

Everything time-dependent (hold expiry, slot "past vs upcoming", wall-clock limits,
approval expiry, simulated tool latency) goes through a ``Clock`` so tests can
fast-forward deterministically instead of sleeping.

All datetimes are timezone-aware and expressed in IST (the marketplace is local).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# Monday 2 Nov 2026, 09:00 IST. Fixed so seeded data and slots are reproducible.
DEFAULT_START = datetime(2026, 11, 2, 9, 0, tzinfo=IST)


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current wall-clock time (aware, IST)."""

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; only differences are meaningful."""

    def sleep(self, seconds: float) -> None:
        """Block (real clock) or advance time (fake clock)."""


class SystemClock:
    """Real time. Used by the CLI and the demo."""

    def now(self) -> datetime:
        return datetime.now(IST)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """Deterministic clock for tests. Time only moves when told to.

    ``sleep`` advances both wall-clock and monotonic time instantly, so simulated
    latency (e.g. a tool timing out after 3 s) counts against wall-clock budgets
    without slowing the test suite down.
    """

    def __init__(self, start: datetime = DEFAULT_START) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=IST)
        self._now = start.astimezone(IST)
        self._mono = 0.0
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def monotonic(self) -> float:
        with self._lock:
            return self._mono

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.advance(seconds)

    def advance(self, seconds: float | timedelta = 0, **kwargs: float) -> datetime:
        """Move time forward, e.g. ``advance(30)`` or ``advance(minutes=11)``."""
        delta = seconds if isinstance(seconds, timedelta) else timedelta(seconds=seconds)
        delta += timedelta(**kwargs)
        if delta < timedelta(0):
            raise ValueError("FakeClock cannot go backwards")
        with self._lock:
            self._now += delta
            self._mono += delta.total_seconds()
            return self._now

    def set(self, when: datetime) -> datetime:
        """Jump forward to an absolute time."""
        if when.tzinfo is None:
            when = when.replace(tzinfo=IST)
        return self.advance(when - self.now())
