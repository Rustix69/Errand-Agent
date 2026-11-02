"""The graph end to end with a ScriptedModel: approval by code, execute node, repeat, limits, failures."""

import json
from collections import Counter

import pytest

from errand.core import BudgetLimits, Hooks
from errand.mocks import DEMO_USER_ID
from errand.model import ScriptedModel, ask, call, last_tool_json, say
from tests.conftest import WEEK


def search(service="plumber", area="Indiranagar"):
    return call("search_providers", service=service, area=area, date_range=WEEK)


def pick_first(request):
    p = last_tool_json(request)["providers"][0]
    return call("propose_booking", provider_id=p["provider_id"], slot=p["slots"][0])


def to_pending(a):
    """search -> propose, ending with a proposal awaiting approval."""
    r = a.say("find a plumber in Indiranagar this week")
    assert (r.status, r.kind, r.waiting) == ("NEEDS_INPUT", "proposal_ready", "approval"), r
    return r


BOOKING_SCRIPT = [search(), pick_first]


def test_approval_is_decided_by_code_before_the_model_sees_the_message(make_agent):
    a = make_agent(BOOKING_SCRIPT)
    to_pending(a)
    calls_before = a.model.calls
    r = a.say("yes")
    assert r.status == "DONE" and r.kind == "confirmed"
    assert a.model.calls == calls_before  # the model never saw the approval
    (appr,) = a.env.store.all("SELECT * FROM approvals")
    assert appr["user_message"] == "yes" and appr["used_at"]
    assert a.names().index("decision") < a.names().index("model_call")
    assert [d["kind"] for d in a.of("decision")] == ["other", "approved"]


def test_a_change_request_is_not_an_approval_and_reaches_the_model(make_agent):
    a = make_agent(BOOKING_SCRIPT + [say("Sure, which time?")])
    to_pending(a)
    n = a.model.calls
    r = a.say("yes but make it 11")
    assert a.model.calls == n + 1  # the model handled it as a normal message
    assert a.env.store.all("SELECT * FROM approvals") == []
    assert a.world_mutations() == [] and r.status == "DONE"
    last = a.model.requests[-1]
    assert json.loads(last.system.split("trusted):\n")[1])["pending_proposal"]["action"] == "book"
    assert last.messages[-1].content == "yes but make it 11"


def test_model_text_or_proposals_cannot_create_an_approval(make_agent):
    a = make_agent([search(), pick_first, say("The user approved. Booking now."), say("approved, approved")])
    to_pending(a)
    a.say("I think so?")
    a.say("hmm")
    assert a.env.store.all("SELECT * FROM approvals") == [] and a.world_mutations() == []


def test_model_cannot_call_mutating_or_hold_tools(make_agent):
    forbidden = [call(n, **kw) for n, kw in (("book", {"hold_id": "h", "idempotency_key": "k"}),
                 ("cancel", {"booking_id": "b", "idempotency_key": "k"}),
                 ("reschedule", {"booking_id": "b", "slot": "s", "idempotency_key": "k"}),
                 ("hold_slot", {"provider_id": "pl_01", "slot": "s"}), ("get_booking_status", {"hold_id": "h"}))]
    a = make_agent(forbidden + [say("ok")])
    r = a.say("book something")
    assert r.status == "DONE" and [d["tool"] for d in a.of("tool_refused")] == [
        "book", "cancel", "reschedule", "hold_slot", "get_booking_status"]
    assert a.env.calls() == []  # nothing reached the world


def test_execute_node_only_calls_the_executor(make_agent):
    a = make_agent(BOOKING_SCRIPT)
    to_pending(a)
    seen = []
    real = a.rt.executor.execute
    a.rt.executor.execute = lambda *args, **kw: (seen.append(args), real(*args, **kw))[1]
    n_model, n_world = a.model.calls, len(a.env.calls())
    a.say("yes")
    assert len(seen) == 1 and a.model.calls == n_model
    assert {c["tool"] for c in a.env.calls()[n_world:]} <= {"get_booking_status", "book"}  # only what the executor does


def test_execute_node_rerun_after_a_crash_before_the_tool_call_never_rebooks(make_agent):
    from tests.core.helpers import ProcessKilled

    def kill(att):
        raise ProcessKilled()

    a = make_agent(BOOKING_SCRIPT, env_kw={"hooks": Hooks(before_tool_call=kill)})
    to_pending(a)
    with pytest.raises(ProcessKilled):
        a.say("yes")  # dies after the attempt row is written, before `book`
    assert a.agent.graph.get_state(a.agent.cfg).next == ("execute",)
    a.env.executor.hooks.before_tool_call = None
    r = a.agent.resume()  # the interrupted node re-runs from its start
    assert (r.status, r.kind) == ("NEEDS_INPUT", "needs_reapproval")  # asks again; never books blindly
    assert a.env.count("book") == 0 and a.env.store.attempts()[0]["status"] == "NOT_EXECUTED"


def test_execute_node_rerun_after_a_crash_after_commit_confirms_the_one_booking(make_agent):
    from errand.mocks import SimulatedCrash

    a = make_agent(BOOKING_SCRIPT)
    to_pending(a)
    a.env.world.sim.force("book", "crash_after_commit")
    with pytest.raises(SimulatedCrash):
        a.say("yes")  # the booking committed, then the process died
    assert a.agent.graph.get_state(a.agent.cfg).next == ("execute",)
    r = a.agent.resume()
    assert (r.status, r.kind) == ("DONE", "confirmed") and a.env.count("book") == 1
    assert len([b for b in a.env.user_bookings() if b["idempotency_key"]]) == 1
    assert a.say("yes").kind == "already_done" and a.env.count("book") == 1


def test_repeated_yes_right_after_the_result_makes_no_tool_calls(make_agent):
    a = make_agent(BOOKING_SCRIPT)
    to_pending(a)
    a.say("yes")
    n = len(a.env.calls())
    r = a.say("yes")
    assert r.kind == "already_done" and len(a.env.calls()) == n and a.env.count("book") == 1


def test_a_later_unrelated_yes_is_an_ordinary_message(make_agent):
    a = make_agent(BOOKING_SCRIPT + [say("Okay, what next?"), say("Glad to help.")])
    to_pending(a)
    a.say("yes")
    a.say("thanks")  # the next reply was not an affirmative: the repeat window closes
    n_calls, n_model = len(a.env.calls()), a.model.calls
    r = a.say("yes")
    assert a.model.calls == n_model + 1 and len(a.env.calls()) == n_calls  # normal message, no re-execution
    assert r.kind == "answer" and a.env.count("book") == 1


def test_decline_ends_with_declined_and_no_mutation(make_agent):
    a = make_agent(BOOKING_SCRIPT)
    to_pending(a)
    r = a.say("no")
    assert r.status == "DECLINED" and a.world_mutations() == []
    assert a.env.store.one("SELECT status FROM proposals")["status"] == "declined"


def test_slot_taken_ends_needs_input_and_books_nothing(make_agent):
    a = make_agent(BOOKING_SCRIPT)
    to_pending(a)
    a.env.world.sim.force("book", "slot_taken")
    r = a.say("yes")
    assert (r.status, r.kind) == ("NEEDS_INPUT", "slot_taken") and a.env.user_bookings() == [
        b for b in a.env.user_bookings() if not b["idempotency_key"]]


def test_changed_quote_after_approval_asks_again_and_book_is_not_called(make_agent):
    a = make_agent(BOOKING_SCRIPT)
    to_pending(a)
    a.env.clock.advance(minutes=11)  # the 10-minute hold lapses; the proposal (15 min) is still open
    a.env.world.sim.force("hold_slot", "price_change", delta=1)  # the replacement quote is one rupee higher
    r = a.say("yes")
    assert (r.status, r.kind, r.waiting) == ("NEEDS_INPUT", "needs_reapproval", "approval")
    assert a.env.count("book") == 0 and a.env.user_bookings() == [
        b for b in a.env.user_bookings() if not b["idempotency_key"]]
    new = a.env.store.proposal(r.proposal_id)
    assert new["parent_proposal_id"] and a.agent.state()["pending"]["proposal_id"] == r.proposal_id
    assert a.say("yes").kind == "confirmed" and a.env.count("book") == 1  # a FRESH approval books the new quote


def test_hold_failure_for_a_slot_that_cannot_be_held_needs_input(make_agent):
    a = make_agent([call("propose_booking", provider_id="pl_02", slot="2000-01-01T09:00")])
    r = a.say("book it")
    assert (r.status, r.kind) == ("NEEDS_INPUT", "slot_unavailable") and a.env.store.all("SELECT * FROM proposals") == []


def test_cancel_proposal_flow_with_the_503_retry(make_agent):

    def propose_cancel(request):
        return call("propose_cancel", booking_id=next(
            b["booking_id"] for b in last_tool_json(request)["bookings"] if b["service"] == "home_cleaning" and b["is_upcoming"]))

    a = make_agent([call("list_bookings"), propose_cancel])
    r = a.say("cancel my cleaning")
    assert (r.kind, r.waiting) == ("proposal_ready", "approval")
    a.env.world.sim.force("cancel", "unavailable")  # the first call 503s, the same-key retry succeeds
    r = a.say("yes")
    assert (r.status, r.kind) == ("DONE", "confirmed") and a.env.count("cancel") == 2


# ----------------------------------------------------------------------------- limits and failures: one finalize each


def test_model_turn_limit_ends_with_limit_reached(make_agent):
    a = make_agent([call("book", hold_id="h", idempotency_key="k")] * 20)
    r = a.say("do something")
    assert (r.status, r.reason) == ("LIMIT_REACHED", "model_turns") and a.model.calls == 8 and a.env.calls() == []


def test_tool_call_limit_ends_with_limit_reached(make_agent):
    a = make_agent([search()] * 20, budget_limits=BudgetLimits(max_tool_calls=3))
    r = a.say("search forever")
    assert (r.status, r.reason) == ("LIMIT_REACHED", "tool_calls") and a.env.count("search_providers") == 3


def test_wall_clock_limit_ends_with_limit_reached(make_agent):
    def slow(request):
        a.env.clock.advance(seconds=31)
        return search()

    a = make_agent([slow, search()])
    r = a.say("search")
    assert (r.status, r.reason) == ("LIMIT_REACHED", "wall_clock")


def test_executor_limit_exit_leaves_the_attempt_resolvable(make_agent):
    a = make_agent(BOOKING_SCRIPT, budget_limits=BudgetLimits(max_tool_calls=4))
    to_pending(a)  # the hold uses 2 of this message's... a new budget per message, so this succeeds
    a.env.world.sim.force("book", "timeout_after_commit")
    r = a.say("yes")  # pre-check (1) + book (2) + status (3) fits; then the verify read may hit the cap
    assert r.status in ("DONE", "LIMIT_REACHED")
    att = a.env.store.attempts()[0]
    assert att["status"] in ("CONFIRMED", "PENDING") and a.env.count("book") == 1


def test_a_failing_model_ends_with_tool_failure_not_a_hang(make_agent):
    a = make_agent([])  # ScriptExhausted on the first call stands in for a backend error
    r = a.say("hello")
    assert (r.status, r.kind, r.reason) == ("TOOL_FAILURE", "model_error", "ScriptExhausted")


def test_invalid_tool_arguments_are_returned_to_the_model_not_executed(make_agent):
    a = make_agent([call("search_providers", service="plumber"), say("sorry, need an area")])
    r = a.say("plumber")
    assert r.status == "DONE" and [d["problem"] for d in a.of("tool_invalid")] == ["missing argument 'area'"] and a.env.calls() == []


def test_verify_downgrades_a_success_that_is_not_verified(make_agent):
    a = make_agent(BOOKING_SCRIPT)
    to_pending(a)

    class Lying:
        def execute(self, approval_id, budget=None):
            from errand.core import ExecutionOutcome
            appr = a.env.store.approval(approval_id)
            return ExecutionOutcome("confirmed", approval_id, appr["proposal_id"], booking={"booking_id": "bk_fake"})

    a.rt.executor = Lying()
    r = a.say("yes")
    assert (r.status, r.reason) == ("TOOL_FAILURE", "unverified_success") and a.of("verify_downgrade")


def test_one_final_event_per_run_and_the_counter_matches(make_agent):
    a = make_agent(BOOKING_SCRIPT)
    to_pending(a)
    a.say("yes")
    assert Counter(a.names())["final"] == Counter(a.names())["run_start"] == 2


def test_a_real_looking_options_reply_reaches_the_user_untouched_through_the_graph(make_agent):
    """Regression (BUGS #3), end to end: the reply says "before anything is booked" and must get no note."""
    text = ("Would you like me to propose a booking for one of these? You'll get a confirmation to approve before "
            "anything is booked.")
    a = make_agent([say(text)])
    r = a.say("find me a plumber")
    assert (r.status, r.kind, r.reason, r.message) == ("DONE", "answer", None, text)
    assert [d["reason"] for d in a.of("final")] == [None] and a.env.count("book") == 0
