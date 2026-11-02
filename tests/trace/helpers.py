"""Shared pieces for trace tests: the 4-message booking script and a runner."""

from __future__ import annotations

import json

from errand.model import ask, call, say
from tests.conftest import WEEK

USER_TEXT = ["find me a plumber", "Indiranagar, mornings this week, under 1500", "go with the first one", "yes"]


def _first_affordable_morning(request):
    options = json.loads(request.system.split("trusted):\n")[1])["last_search_options"]
    for p in options:
        slots = [s for s in p["slots"] if s[-5:] < "12:00"]
        if p["price"] <= 1500 and slots:
            return call("propose_booking", provider_id=p["provider_id"], slot=slots[0])
    raise AssertionError("no affordable morning option")


def booking_script():
    return [
        ask("Which area, and when?", ["area", "date_range"], {"service": "plumber"}),
        call("search_providers", service="plumber", area="Indiranagar", date_range=WEEK),
        say("Here are the options."),
        _first_affordable_morning,
    ]


def play_booking(a, texts=USER_TEXT, before_yes=None):
    replies = []
    for text in texts:
        if text == "yes" and before_yes:
            before_yes(a)
        replies.append(a.say(text))
    return replies
