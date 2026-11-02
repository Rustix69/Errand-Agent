"""Regression (BUGS #3): a proposal is honoured only when the model has SEEN the results of every read it asked for, and
a finished errand does not leak its request and search options into the next one. Both found by running the real model."""

import json

import pytest

from errand.agent.tools import READ_TOOLS
from errand.model import call, calls, say
from tests.agent.helpers import memory_items
from tests.conftest import WEEK
from tests.evals.scripts import pick, search, state, tool_json

FIND = "find a plumber in Indiranagar this week"
BLIND = ("propose_booking", {"provider_id": "pl_02", "slot": "2026-11-03T10:00"})  # a slot the model never looked up
SEARCH = ("search_providers", {"service": "plumber", "area": "Indiranagar", "date_range": WEEK})


def tool_messages(request):
    return {m.name: json.loads(m.content) for m in request.messages if m.role == "tool"}


# ----------------------------------------------------------------------------- one turn with a propose AND a read


@pytest.mark.parametrize("order", ["propose_then_search", "search_then_propose"])
def test_a_proposal_in_the_same_turn_as_a_read_is_ignored_and_the_model_decides_on_the_next_turn(make_agent, order):
    pair = (BLIND, SEARCH) if order == "propose_then_search" else (SEARCH, BLIND)
    a = make_agent([calls(*pair), pick()])
    r = a.say(FIND)

    # the blind proposal created nothing and was logged, with its reason
    assert [(d["tool"], d["reason"]) for d in a.of("tool_call_ignored")] == [("propose_booking", "read_results_unseen")]
    assert len(a.of("propose_requested")) == 1  # only the proposal made AFTER the results were seen
    held = a.env.calls("hold_slot")
    assert len(held) == 1 and (held[0]["args"]["provider_id"], held[0]["args"]["slot"]) != ("pl_02", "2026-11-03T10:00")
    row = a.env.store.one("SELECT * FROM proposals WHERE status = 'pending'")
    assert (r.status, r.kind) == ("NEEDS_INPUT", "proposal_ready") and row["provider_id"] == held[0]["args"]["provider_id"]
    assert any(o["provider_id"] == row["provider_id"] and row["slot"] in o["slots"] for o in a.agent.state()["options"])

    # the model's NEXT request carried the search results and the ignored-proposal result
    second = tool_messages(a.model.requests[1])
    assert second["propose_booking"]["error"] == "proposal_ignored" and second["search_providers"]["providers"]
    assert a.model.calls == 2


def test_the_same_rule_holds_for_list_bookings_and_a_cancellation(make_agent):
    def propose_cancel(request):
        b = next(b for b in tool_json(request, "list_bookings")["bookings"] if b["service"] == "home_cleaning" and b["is_upcoming"])
        return call("propose_cancel", booking_id=b["booking_id"])

    a = make_agent([calls(("list_bookings", {}), ("propose_cancel", {"booking_id": "bk_000006"})), propose_cancel])
    r = a.say("cancel my cleaning")
    assert [(d["tool"], d["reason"]) for d in a.of("tool_call_ignored")] == [("propose_cancel", "read_results_unseen")]
    assert (r.kind, a.env.count("cancel")) == ("proposal_ready", 0)
    assert a.env.store.one("SELECT action FROM proposals WHERE status = 'pending'")["action"] == "cancel"


def test_a_model_that_keeps_proposing_blind_is_stopped_by_the_turn_limit_and_never_gets_a_proposal(make_agent):
    a = make_agent([calls(("list_bookings", {}), BLIND)] * 9, context_budget=20_000)
    r = a.say("book something")
    assert (r.status, r.reason) == ("LIMIT_REACHED", "model_turns")
    assert len(a.of("tool_call_ignored")) == 8 and a.of("propose_requested") == []
    assert a.env.store.one("SELECT COUNT(*) FROM proposals")[0] == 0 and a.env.count("hold_slot") == 0


def test_a_proposal_with_no_reads_in_its_turn_is_still_honoured(make_agent):
    a = make_agent([search(), pick()])  # one call per turn: the normal flow
    assert a.say(FIND).kind == "proposal_ready" and a.of("tool_call_ignored") == []
    assert set(READ_TOOLS) == {"search_providers", "get_provider", "list_bookings"}  # what counts as a read


# ----------------------------------------------------------------------------- state after a finished errand


def test_a_confirmed_booking_clears_the_request_and_options_so_the_next_errand_starts_fresh(make_agent):
    seen = {}

    def electrician(request):
        seen["state"], seen["memories"] = state(request), {m["key"] for m in memory_items(request.system)}
        return say("Which area?")

    a = make_agent([search(), pick(), electrician], seed=True)
    a.say(FIND)
    open_state = a.agent.state()
    assert open_state["request"]["service"] == "plumber" and open_state["options"]  # kept while the errand is open
    assert a.say("yes").kind == "confirmed"

    after = a.agent.state()
    assert after["request"] == {} and after["options"] == [] and after["pending"] is None
    (cleared,) = a.of("search_state_cleared")
    assert cleared["request_keys"] == ["area", "date_range", "service"] and cleared["options"] == len(open_state["options"])

    a.say("Find me an electrician")
    assert seen["state"]["request_so_far"] == {} and seen["state"]["last_search_options"] == []  # no plumber leftovers
    assert "electrician_preferred" in seen["memories"] and not {k for k in seen["memories"] if k.startswith("plumber")}


def test_a_declined_proposal_keeps_its_options_because_the_user_may_pick_another(make_agent):
    a = make_agent([search(), pick()])
    a.say(FIND)
    before = a.agent.state()
    assert a.say("no").kind == "declined"
    after = a.agent.state()
    assert after["request"] == before["request"] and after["options"] == before["options"] and after["options"]
    assert a.of("search_state_cleared") == []
