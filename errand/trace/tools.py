"""A thin wrapper around the tool port, applied where the agent is wired (not in the core).

The core emits ``tool_result`` with only the tool name. This wrapper hands the logger a short summary of
each successful result, which the logger attaches to that tool's next ``tool_result`` row. The summary is
built from a whitelist of scalar fields (ids, status, counts, price, flags): never free text such as
review text, ``about``, names or ``cancel_reason``. A failure while summarizing never affects the call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from errand.contract.port import TOOL_NAMES

_SCALARS = ("booking_id", "hold_id", "provider_id", "status", "quoted_price", "price_changed", "replayed")
_COUNTS = {"providers": "n", "bookings": "n", "reviews": "reviews"}
_MAX_STR = 40


def summarize(result: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in _SCALARS:
        v = getattr(result, k, None)
        if isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= _MAX_STR):
            out[k] = v
    for attr, key in _COUNTS.items():
        if isinstance(getattr(result, attr, None), list):
            out[key] = len(getattr(result, attr))
    booking, hold = getattr(result, "booking", None), getattr(result, "hold", None)
    if booking is not None and "booking_id" not in out:  # get_booking_status: the booking it found
        out["booking_id"] = booking.booking_id
    if hold is not None:
        out["hold_status"] = hold.status
    return out


class SummarizingTools:
    """Forwards exactly the 8 contract tools and nothing else; reports a summary after each success."""

    def __init__(self, inner: Any, note: Callable[[str, dict[str, Any]], None]) -> None:
        self._inner, self._note = inner, note

    def __getattr__(self, name: str) -> Any:
        if name not in TOOL_NAMES:
            raise AttributeError(name)
        fn = getattr(self._inner, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            result = fn(*args, **kwargs)
            try:
                self._note(name, summarize(result))
            except Exception:
                pass
            return result

        return call
