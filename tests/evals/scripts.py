"""Small building blocks for scenario scripts (what the scripted model does)."""

from __future__ import annotations

import json
from typing import Any

from errand.model import ModelRequest, call
from tests.agent.helpers import memory_items
from tests.conftest import WEEK


def search(service: str = "plumber", area: str = "Indiranagar", date_range: dict[str, str] = WEEK):
    return call("search_providers", service=service, area=area, date_range=date_range)


def tool_json(request: ModelRequest, name: str) -> Any:
    """The JSON of the most recent result of tool `name` in this run."""
    for m in reversed(request.messages):
        if m.role == "tool" and m.name == name:
            return json.loads(m.content)
    raise LookupError(f"no {name} result in this run")


def state(request: ModelRequest) -> dict[str, Any]:
    return json.loads(request.system.split("trusted):\n")[1])


def remembered(request: ModelRequest, key: str) -> str | None:
    return next((m["value"] for m in memory_items(request.system) if m["key"] == key), None)


def morning(slot: str) -> bool:
    return slot[-5:] < "12:00"


def pick(*, morning_only: bool = False, max_price: int | None = None, provider: str | None = None,
         avoid: tuple[str, str] | None = None, min_time: str | None = None, from_state: bool = False):
    """A script step that proposes the first search option matching the filters (read from the last search
    result, or from the trusted state block when the search happened in an earlier message)."""
    def step(request: ModelRequest):
        options = state(request)["last_search_options"] if from_state else tool_json(request, "search_providers")["providers"]
        for p in options:
            if provider and p["provider_id"] != provider:
                continue
            if max_price is not None and p["price"] > max_price:
                continue
            for s in p["slots"]:
                if morning_only and not morning(s):
                    continue
                if min_time and s[-5:] < min_time:
                    continue
                if avoid and (p["provider_id"], s) == tuple(avoid):
                    continue
                return call("propose_booking", provider_id=p["provider_id"], slot=s)
        raise AssertionError("scenario found no option matching the user's constraints")
    return step
