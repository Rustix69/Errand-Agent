"""Eval 1: a complete booking from "find me a plumber" to a verified confirmation, driven ONLY by the
ScriptedModel. Asserts DB state and trace events, never reply wording."""

import json

from errand.model import ask, call, last_tool_json, say
from tests.conftest import WEEK



def options_in_state(request):
    """The model reads the last search from the system-maintained state block (earlier runs' tool results
    are not replayed), like a real model would."""
    return json.loads(request.system.split("trusted):\n")[1])["last_search_options"]


def propose_first_affordable_morning(request):
    for p in options_in_state(request):
        slots = [s for s in p["slots"] if s[-5:] < "12:00"]
        if p["price"] <= 1500 and slots:
            return call("propose_booking", provider_id=p["provider_id"], slot=slots[0])
    raise AssertionError("no affordable morning option in the seeded world")


def test_find_me_a_plumber_to_confirmed(make_agent, eval_case):
    rec = eval_case(
        "Full booking: find me a plumber -> confirmed",
        "ask only for missing details; search; code holds + asks approval; 'yes' approved by code; "
        "executor books once, verified; DONE; 1 booking, attempt CONFIRMED, model not consulted on 'yes'",
        notes="ScriptedModel, forced-mode world, no failures injected", id="B1",
    )
    a = make_agent([
        ask("Which area, and when?", ["area", "date_range"], {"service": "plumber"}),
        call("search_providers", service="plumber", area="Indiranagar", date_range=WEEK),
        say("Here are the options under ₹1,500 with morning slots."),
        propose_first_affordable_morning,
    ])

    r1 = a.say("find me a plumber")
    r2 = a.say("Indiranagar, mornings this week, under 1500")
    r3 = a.say("go with the first one")
    model_calls_before_yes = a.model.calls
    r4 = a.say("yes")

    # ---- actual, from DB state and events ---------------------------------------------------------
    store, world = a.env.store, a.env.user_bookings()
    mine = [b for b in world if b["idempotency_key"]]
    att = store.attempts()
    appr = store.all("SELECT * FROM approvals")
    prop = store.all("SELECT * FROM proposals")
    rec.actual = (
        f"statuses={[r1.status, r2.status, r3.status, r4.status]}; final kind={r4.kind}; bookings={len(mine)}; "
        f"attempts={[x['status'] for x in att]} verified={[bool(x['verified_at']) for x in att]}; "
        f"approvals={len(appr)}; book calls={a.env.count('book')}; model calls on 'yes'={a.model.calls - model_calls_before_yes}"
    )

    # ---- conversation: only missing details were asked, nothing was re-asked ----------------------
    assert [d["missing"] for d in a.of("ask_user")] == [["area", "date_range"]]
    assert (r1.status, r1.waiting) == ("NEEDS_INPUT", "input")
    assert a.agent.state()["request"] == {"service": "plumber", "area": "Indiranagar", "date_range": WEEK}
    assert (r2.status, r2.kind) == ("DONE", "answer")

    # ---- proposal: created by code from the hold's quoted price, then approved by code ------------
    assert (r3.status, r3.kind, r3.waiting) == ("NEEDS_INPUT", "proposal_ready", "approval")
    (p,) = prop
    assert p["action"] == "book" and p["status"] == "executed" and p["slot"][-5:] < "12:00" and p["price"] <= 1500
    (ap,) = appr
    assert ap["user_message"] == "yes" and ap["used_at"] and ap["proposal_id"] == p["id"]
    assert [d["kind"] for d in a.of("decision")] == ["other", "other", "other", "approved"]
    assert a.model.calls == model_calls_before_yes  # the model was never consulted for the approval

    # ---- execution: exactly one booking, verified ---------------------------------------------------
    assert (r4.status, r4.kind) == ("DONE", "confirmed")
    (b,) = mine
    assert (b["provider_id"], b["slot"], b["price"]) == (p["provider_id"], p["slot"], p["price"])
    (at,) = att
    assert at["status"] == "CONFIRMED" and at["verified_at"] and at["approval_id"] == ap["id"]
    assert a.env.count("book") == 1 and [c["tool"] for c in a.world_mutations()] == ["book"]

    # ---- trace events, in order -----------------------------------------------------------------------
    names = a.names()
    order = ["run_start", "ask_user", "tool_call", "propose_requested", "proposal_created", "decision",
             "attempt_pending", "attempt_confirmed", "verified", "final"]
    pos = -1
    for n in order:
        pos = names.index(n, pos + 1)
    assert [d["status"] for d in a.of("final")] == ["NEEDS_INPUT", "DONE", "NEEDS_INPUT", "DONE"]

    # ---- bounds: per message <= 8 model turns, <= 12 tool calls ---------------------------------------
    assert all(m["turn"] <= 8 for m in a.of("model_call")) and a.model.calls == 4
    a.model.assert_consumed()
