"""Restarts. A new process reopens world.db and agent.db; every PENDING attempt is resolved through
the source of truth and NEVER by calling book/cancel/reschedule again."""

import pytest

from errand.contract.port import MUTATING_TOOLS
from errand.core import Hooks, Limits, Reader, resolve_pending
from errand.core.budget import Budget
from errand.mocks import DEMO_USER_ID, SimulatedCrash
from tests.core.helpers import SESSION, ProcessKilled


def mutating_calls(env):
    return [c for c in env.calls() if c["tool"] in MUTATING_TOOLS]


def recover(env):
    return resolve_pending(env.store, env.agent_tools, DEMO_USER_ID, env.clock)


def kill_before_tool(att):
    raise ProcessKilled()


# ----------------------------------------------------------------------------- crash before the tool call


def test_crash_between_attempt_row_and_tool_call_is_recovered_without_rebooking(make_env):
    env = make_env(hooks=Hooks(before_tool_call=kill_before_tool))
    h, pid, aid = env.book_flow()
    with pytest.raises(ProcessKilled):
        env.executor.execute(aid)
    (att,) = env.attempts()
    assert att["status"] == "PENDING" and env.count("book") == 0  # the attempt row existed BEFORE the call

    env2 = make_env.restart(env)
    res = recover(env2)
    assert [(r.result, r.action) for r in res] == [("not_executed", "book")]
    assert env2.attempts()[0]["status"] == "NOT_EXECUTED" and env2.attempts()[0]["verified_at"]
    assert env2.store.proposal(pid)["status"] == "failed"
    assert mutating_calls(env2) == []  # recovery never booked

    # "yes" after the restart is a repeat (window persisted in agent.db): it must NOT re-book.
    d = env2.guard.on_user_message(SESSION, "yes")
    assert d.kind == "repeat" and d.approval_id == aid
    out = env2.executor.execute(aid)
    assert out.kind == "needs_reapproval" and out.reason == "not_executed"
    assert mutating_calls(env2) == [] and [b for b in env2.user_bookings() if b["hold_id"] == h.hold_id] == []


# ----------------------------------------------------------------------------- crash after the side effect


def test_crash_after_commit_is_recovered_to_confirmed_and_yes_returns_it(make_env):
    env = make_env()
    h, pid, aid = env.book_flow()
    env.world.sim.force("book", "crash_after_commit")
    with pytest.raises(SimulatedCrash):
        env.executor.execute(aid)
    (att,) = env.attempts()
    assert att["status"] == "PENDING" and att["tool_calls"] == 1 and env.count("book") == 1

    env2 = make_env.restart(env)
    res = recover(env2)
    assert [r.result for r in res] == ["confirmed"]
    (att,) = env2.attempts()
    assert att["status"] == "CONFIRMED" and att["verified_at"] and att["error_code"] is None
    assert env2.store.proposal(pid)["status"] == "executed"
    assert env2.count("book") == 1  # still only the original call

    n = len(env2.calls())
    d = env2.guard.on_user_message(SESSION, "yes")
    assert d.kind == "repeat"
    out = env2.executor.execute(d.approval_id)
    assert out.kind == "already_done" and out.booking["slot"] == h.slot
    assert len(env2.calls()) == n  # the repeat made zero tool calls
    assert len([b for b in env2.user_bookings() if b["hold_id"] == h.hold_id]) == 1


def test_recovery_clears_a_stale_hold_expired_code_when_the_booking_did_happen(make_env):
    """Regression (BUGS #1): the executor writes error_code='hold_expired' on the still-PENDING attempt while it asks
    the source of truth. If the process dies after that write and the booking had in fact committed, startup recovery
    must mark it CONFIRMED AND clear the code (it used to keep the stale code on a successful booking)."""
    env = make_env()
    _, pid, aid = env.book_flow()
    env.world.sim.force("book", "crash_after_commit")
    with pytest.raises(SimulatedCrash):
        env.executor.execute(aid)
    (att,) = env.attempts()
    env.store.update_attempt(att["idempotency_key"], error_code="hold_expired")  # what the executor leaves if it dies here
    (att,) = env.attempts()
    assert att["status"] == "PENDING" and att["error_code"] == "hold_expired"

    env2 = make_env.restart(env)
    assert [r.result for r in recover(env2)] == ["confirmed"]
    (att,) = env2.attempts()
    assert att["status"] == "CONFIRMED" and att["verified_at"] and att["error_code"] is None
    assert env2.store.proposal(pid)["status"] == "executed" and env2.count("book") == 1


def test_crash_after_commit_resolved_by_execute_without_running_recovery_first(make_env):
    env = make_env()
    _, _, aid = env.book_flow()
    env.world.sim.force("book", "crash_after_commit")
    with pytest.raises(SimulatedCrash):
        env.executor.execute(aid)
    env2 = make_env.restart(env)
    out = env2.executor.execute(aid)  # e.g. the graph re-runs the execute node after a restart
    assert out.kind == "confirmed" and env2.count("book") == 1


def test_cancel_crash_after_commit_is_recovered_from_list_bookings(make_env):
    env = make_env()
    b = env.booking("cl_04")
    env.guard.propose_cancel(SESSION, DEMO_USER_ID, b)
    aid = env.approve()
    env.world.sim.force("cancel", "crash_after_commit")
    with pytest.raises(SimulatedCrash):
        env.executor.execute(aid)
    env2 = make_env.restart(env)
    assert [r.result for r in recover(env2)] == ["confirmed"]
    assert env2.count("cancel") == 1
    assert next(x for x in env2.tools.list_bookings(DEMO_USER_ID).bookings if x.booking_id == b.booking_id).status == "cancelled"


# ----------------------------------------------------------------------------- the source of truth is down


def test_recovery_leaves_attempts_pending_while_the_truth_is_unreachable(make_env):
    env = make_env(hooks=Hooks(before_tool_call=kill_before_tool))
    _, _, aid = env.book_flow()
    with pytest.raises(ProcessKilled):
        env.executor.execute(aid)
    env2 = make_env.restart(env)
    env2.world.sim.force("get_booking_status", "unavailable", times=3)
    res = recover(env2)
    assert [r.result for r in res] == ["unknown"] and "unavailable" in res[0].detail
    assert env2.attempts()[0]["status"] == "PENDING" and mutating_calls(env2) == []
    assert [r.result for r in recover(env2)] == ["not_executed"]  # next startup, truth is back


def test_recovery_is_idempotent(make_env):
    env = make_env()
    _, _, aid = env.book_flow()
    env.world.sim.force("book", "crash_after_commit")
    with pytest.raises(SimulatedCrash):
        env.executor.execute(aid)
    env2 = make_env.restart(env)
    assert len(recover(env2)) == 1 and recover(env2) == []


def test_recovery_handles_several_pending_attempts_independently(make_env):
    killed = set()
    env = make_env(hooks=Hooks(before_tool_call=lambda att: att["approval_id"] in killed and kill_before_tool(att)))

    h1 = env.hold(service="plumber", area="Indiranagar")
    env.propose(h1, session="a")
    a1 = env.approve(session="a")
    env.world.sim.force("book", "crash_after_commit")
    with pytest.raises(SimulatedCrash):
        env.executor.execute(a1)

    h2 = env.hold(service="electrician", area="Whitefield")
    env.propose(h2, session="b")
    a2 = env.approve(session="b")
    killed.add(a2)
    with pytest.raises(ProcessKilled):
        env.executor.execute(a2)

    env2 = make_env.restart(env)
    res = {r.key: r.result for r in recover(env2)}
    assert res == {f"book-{a1}-0": "confirmed", f"book-{a2}-0": "not_executed"}
    assert len(mutating_calls(env2)) == 1  # just the original, committed-then-crashed book


def test_pending_hold_expired_attempt_becomes_failed_verified_and_execute_resumes_it(make_env):
    env = make_env(
        hooks=Hooks(in_consume_tx=lambda c: env.world.sim.force("get_booking_status", "unavailable", times=3))
    )
    h = env.hold()
    env.clock.advance(minutes=9)
    env.propose(h)
    aid = env.approve()
    env.clock.advance(seconds=120)
    assert env.executor.execute(aid).kind == "unknown"  # left PENDING/hold_expired

    env2 = make_env.restart(env)
    assert [r.result for r in recover(env2)] == ["hold_expired"]
    a0 = env2.attempts(aid)[0]
    assert a0["status"] == "FAILED" and a0["error_code"] == "hold_expired" and a0["verified_at"]
    assert mutating_calls(env2) == []  # recovery did not book or re-hold
    assert env2.count("hold_slot") == 1
    assert env2.executor.execute(aid).kind == "confirmed"  # execute (not recovery) finishes through seq 1


# ----------------------------------------------------------------------------- structural guarantees


@pytest.mark.parametrize("tool", sorted(MUTATING_TOOLS))
def test_reader_cannot_call_a_mutating_tool(env, tool):
    r = Reader(env.agent_tools, env.clock, Limits(), Budget(env.clock))
    with pytest.raises(PermissionError):
        r(tool, idempotency_key="k")
    assert mutating_calls(env) == []


def test_reader_retries_only_retryable_errors_and_charges_the_budget(env):
    budget = Budget(env.clock)
    r = Reader(env.agent_tools, env.clock, Limits(max_query_tries=3), budget)
    env.world.sim.force("list_bookings", "unavailable", times=2)
    r("list_bookings", user_id=DEMO_USER_ID)
    assert budget.tool_calls == 3 and env.count("list_bookings") == 3
