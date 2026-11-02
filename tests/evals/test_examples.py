"""The three example requests from the assignment, end to end, on the seeded demo user."""

import json

from errand.model import call, say
from tests.evals.scripts import pick, remembered, search, state, tool_json
from tests.conftest import WEEK

SATURDAY = {"start": "2026-11-07", "end": "2026-11-07"}  # the fake clock starts Monday 2 Nov 2026


def area_from_memory(service: str):
    def step(request):
        return search(service, remembered(request, "home_area"))
    return step


def test_e1_find_a_plumber_this_week_mornings_under_1500(scenario):
    run = scenario(
        "E1", "Example 1: plumber for my kitchen tap this week, mornings only, under ₹1,500",
        "area comes from memory (not asked); morning slot, price <= 1500; 'yes' approved by code; 1 verified booking",
        [area_from_memory("plumber"), pick(morning_only=True, max_price=1500)],
        ["Find a plumber for my kitchen tap this week, mornings only, under ₹1,500", "yes"], seed=True,
        notes="seeded user: home area HSR Layout is remembered",
    )
    a = run.a
    assert a.of("ask_user") == []  # the area was not asked: it is a saved preference
    (srch,) = [d for d in a.of("tool_call") if d["tool"] == "search_providers"]
    assert srch["args"]["area"] == "HSR Layout"
    (booking,) = run.mine()
    assert booking["slot"][-5:] < "12:00" and booking["price"] <= 1500 and booking["service"] == "plumber"
    assert [(r.status, r.kind) for r in run.replies] == [("NEEDS_INPUT", "proposal_ready"), ("DONE", "confirmed")]
    assert a.model.calls == 2  # the model was not consulted for the approval


def test_e2_move_my_ac_servicing_to_saturday(scenario):
    def propose_move(request):
        ac = next(b for b in tool_json(request, "list_bookings")["bookings"]
                  if b["service"] == "ac_servicing" and b["is_upcoming"] and b["status"] == "confirmed")
        slots = next(p for p in tool_json(request, "search_providers")["providers"]
                     if p["provider_id"] == ac["provider_id"])["slots"]
        return call("propose_reschedule", booking_id=ac["booking_id"], slot=next(s for s in slots if s[-5:] != "10:00"))

    run = scenario(
        "E2", "Example 2: move my AC servicing to Saturday",
        "finds the upcoming AC booking; proposes a free Saturday slot; 'yes' reschedules once; booking is on Saturday",
        [call("list_bookings"), search("ac_servicing", "HSR Layout", SATURDAY), propose_move],
        ["Move my AC servicing to Saturday", "yes"], seed=True,
    )
    assert [(r.status, r.kind) for r in run.replies] == [("NEEDS_INPUT", "proposal_ready"), ("DONE", "confirmed")]
    assert run.a.env.count("reschedule") == 1
    ac = next(b for b in run.a.env.user_bookings() if b["provider_id"] == "ac_01" and b["status"] == "confirmed")
    assert ac["slot"].startswith("2026-11-07") and ac["slot"][-5:] != "10:00"
    (att,) = run.a.env.store.attempts()
    assert att["action"] == "reschedule" and att["status"] == "CONFIRMED" and att["verified_at"]


def test_e3_same_electrician_as_last_time_not_before_10(scenario):
    def propose_same(request):
        past = [b for b in tool_json(request, "list_bookings")["bookings"]
                if b["service"] == "electrician" and not b["is_upcoming"] and b["status"] == "completed"]
        last = max(past, key=lambda b: b["slot"])
        # the saved memory must agree with the marketplace's own history (artifacts/bugs-other.md #7)
        assert last["provider_id"] in remembered(request, "electrician_preferred")
        return pick(provider=last["provider_id"], min_time="10:00", from_state=True)(request)

    run = scenario(
        "E3", "Example 3: book the same electrician as last time, but not before 10 a.m.",
        "history says el_01; proposal is el_01 at/after 10:00; the 10:00 statement matches the saved preference "
        "(no new memory); 'yes' books once",
        [call("list_bookings"), lambda r: search("electrician", remembered(r, "home_area")), propose_same],
        ["Book the same electrician as last time, but not before 10 a.m.", "yes"], seed=True,
    )
    (booking,) = run.mine()
    assert booking["provider_id"] == "el_01" and booking["slot"][-5:] >= "10:00"
    assert run.a.of("memory_captured") == []  # "not before 10" is what we already knew
    assert [m.value for m in run.a.memory.usable("u_demo") if m.key == "earliest_time"] == ["10:00"]
    assert run.replies[-1].kind == "confirmed"
