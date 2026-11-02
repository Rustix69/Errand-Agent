"""Simulator-only crash injection. Agent-side code must never import this."""

from __future__ import annotations


class SimulatedCrash(BaseException):
    """Crash injection: the process 'dies' right after the side effect commits.

    Derives from BaseException so ordinary ``except Exception`` recovery code in
    the agent cannot swallow it, exactly like a real process kill.
    """
