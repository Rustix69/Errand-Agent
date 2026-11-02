from errand.contract import ErrorCode, ToolError
from errand.core import Hooks, Limits
from errand.core.budget import Budget, BudgetLimits


def _tool_order(env):
    return [c["tool"] for c in env.calls() if c["tool"] not in ("search_providers", "hold_slot")]


# ----------------------------------------------------------------------------- happy path


def test_happy_path_checks_live_state_then_books_then_verifies(env):
    h, pid, aid = env.book_flow()
    out = env.executor.execute(aid)
    assert out.kind == "confirmed" and out.booking["provider_id"] == h.provider_id
    assert out.booking["slot"] == h.slot and out.booking["price"] == h.quoted_price
    assert _tool_order(env) == ["get_booking_status", "book", "get_booking_status"]
    (att,) = env.attempts()
    assert att["status"] == "CONFIRMED" and att["verified_at"] and att["tool_calls"] == 1
    assert att["idempotency_key"] == f"book-{aid}-0" and att["seq"] == 0
    assert env.store.proposal(pid)["status"] == "executed"
    assert env.store.approval(aid)["used_at"] is not None


def test_attempt_row_exists_before_the_tool_is_called(make_env):
    seen = {}

    def hook(att):
        seen["row"] = dict(att)
        seen["book_calls"] = len(env.calls("book"))

    env = make_env(hooks=Hooks(before_tool_call=hook))
    _, _, aid = env.book_flow()
    env.executor.execute(aid)
    assert seen["row"]["status"] == "PENDING" and seen["book_calls"] == 0
    assert env.store.approval(aid)["used_at"] is not None  # consumed in the same commit as the attempt


def test_executor_takes_only_an_approval_id_and_reads_args_from_storage(env):
    h, _, aid = env.book_flow()
    env.executor.execute(aid)
    (call,) = env.calls("book")
    assert call["args"] == {"hold_id": h.hold_id, "idempotency_key": f"book-{aid}-0"}


# ----------------------------------------------------------------------------- timeouts


def test_timeout_after_commit_asks_the_source_of_truth_and_never_rebooks(env):
    h, _, aid = env.book_flow()
    env.world.sim.force("book", "timeout_after_commit")
    out = env.executor.execute(aid)
    assert out.kind == "confirmed"
    assert env.count("book") == 1  # no blind retry
    assert _tool_order(env) == ["get_booking_status", "book", "get_booking_status"]
    (att,) = env.attempts()
    assert att["status"] == "CONFIRMED" and att["verified_at"]
    assert len([b for b in env.user_bookings() if b["hold_id"] == h.hold_id]) == 1


def test_timeout_before_commit_retries_with_the_same_key(env):
    h, _, aid = env.book_flow()
    env.world.sim.force("book", "timeout_before_commit")
    out = env.executor.execute(aid)
    assert out.kind == "confirmed"
    book_calls = env.calls("book")
    assert len(book_calls) == 2
    assert {c["args"]["idempotency_key"] for c in book_calls} == {f"book-{aid}-0"}
    assert len([b for b in env.user_bookings() if b["hold_id"] == h.hold_id]) == 1
    assert env.attempts()[0]["tool_calls"] == 2


def test_retries_exhausted_reports_failure_and_books_nothing(make_env):
    env = make_env(limits=Limits(max_attempts=3))
    h, pid, aid = env.book_flow()
    env.world.sim.force("book", "timeout_before_commit", times=3)
    out = env.executor.execute(aid)
    assert out.kind == "failed" and out.reason == "retries_exhausted"
    assert env.count("book") == 3
    assert {c["args"]["idempotency_key"] for c in env.calls("book")} == {f"book-{aid}-0"}
    assert [b for b in env.user_bookings() if b["hold_id"] == h.hold_id] == []
    (att,) = env.attempts()
    assert att["status"] == "FAILED" and att["error_code"] == "retries_exhausted"
    assert env.store.proposal(pid)["status"] == "failed"


def test_503_on_book_is_resolved_the_same_way_as_a_timeout(env):
    _, _, aid = env.book_flow()
    env.world.sim.force("book", "unavailable")
    assert env.executor.execute(aid).kind == "confirmed"
    assert env.count("book") == 2


def test_status_tool_down_after_timeout_leaves_attempt_pending_and_does_not_retry(make_env):
    env = make_env(
        hooks=Hooks(before_tool_call=lambda att: env.world.sim.force("get_booking_status", "unavailable", times=3))
    )
    h, _, aid = env.book_flow()
    env.world.sim.force("book", "timeout_after_commit")
    out = env.executor.execute(aid)
    assert out.kind == "unknown" and out.reason == "source_of_truth_unavailable"
    assert env.count("book") == 1  # the mutation is NOT retried while the truth is unknown
    (att,) = env.attempts()
    assert att["status"] == "PENDING" and att["verified_at"] is None
    # Once the truth is reachable the same approval resolves to the booking that did happen.
    again = env.executor.execute(aid)
    assert again.kind == "confirmed" and env.count("book") == 1
    assert env.attempts()[0]["status"] == "CONFIRMED"


def test_success_is_never_claimed_if_verification_is_unavailable(make_env):
    env = make_env(
        hooks=Hooks(before_tool_call=lambda att: env.world.sim.force("get_booking_status", "unavailable", times=3))
    )
    _, _, aid = env.book_flow()
    out = env.executor.execute(aid)  # book itself succeeds, but we cannot verify it
    assert out.kind == "unknown" and out.reason == "verification_unavailable"
    assert env.attempts()[0]["status"] == "PENDING"
    assert env.executor.execute(aid).kind == "confirmed" and env.count("book") == 1


# ----------------------------------------------------------------------------- business outcomes


def test_slot_taken_goes_back_to_options(env):
    h, pid, aid = env.book_flow()
    env.world.sim.force("book", "slot_taken")
    out = env.executor.execute(aid)
    assert out.kind == "slot_taken" and out.reason == ErrorCode.SLOT_TAKEN
    assert env.count("book") == 1  # no retry
    (att,) = env.attempts()
    assert att["status"] == "FAILED" and att["error_code"] == "slot_taken"
    assert [b for b in env.user_bookings() if b["hold_id"] == h.hold_id] == []
    assert env.store.proposal(pid)["status"] == "failed"
    assert env.executor.execute(aid).kind == "slot_taken" and env.count("book") == 1  # stored outcome


def test_slot_taken_by_another_customer_before_execution_never_calls_book(env):
    h, _, aid = env.book_flow()
    env.world.sim.take_slot(h.provider_id, h.slot)
    out = env.executor.execute(aid)
    assert out.kind == "slot_taken" and env.count("book") == 0 and env.attempts() == []


def test_unexpected_tool_error_is_a_clear_failure(env):
    h, _, aid = env.book_flow()

    class Boom:
        def __getattr__(self, name):
            return getattr(env.agent_tools, name)

        def book(self, hold_id, idempotency_key):
            raise ToolError("internal_error", "boom", retryable=False)

    env.executor.tools = Boom()
    out = env.executor.execute(aid)
    assert out.kind == "failed" and out.reason == "internal_error"
    assert env.attempts()[0]["status"] == "FAILED"


# ----------------------------------------------------------------------------- approval vs live state


def test_stale_price_invalidates_the_approval_and_skips_the_tool(env):
    h = env.hold()
    stale = h.model_copy(update={"quoted_price": h.quoted_price - 300})  # proposal shown at an older, lower quote
    env.propose(stale)
    aid = env.approve()
    out = env.executor.execute(aid)
    assert out.kind == "approval_invalid" and out.reason == "fingerprint_mismatch"
    assert out.diff["price"] == (h.quoted_price - 300, h.quoted_price)
    assert env.count("book") == 0 and env.attempts() == []
    assert env.store.approval(aid)["invalidated_reason"] == "fingerprint_mismatch"
    assert env.executor.execute(aid).kind == "approval_invalid"  # and it stays dead


def test_changed_slot_invalidates_the_approval(env):
    pid, slot, _ = env.free_slot()
    h = env.tools.hold_slot(pid, slot)
    env.propose(h.model_copy(update={"slot": "2026-11-05T17:00"}))
    out = env.executor.execute(env.approve())
    assert out.kind == "approval_invalid" and "slot" in out.diff and env.count("book") == 0


def test_hold_swapped_for_another_hold_invalidates(env):
    h1 = env.hold(index=0)
    env.propose(h1.model_copy(update={"hold_id": "hold_0000zz"}))
    out = env.executor.execute(env.approve())
    assert out.kind == "approval_invalid" and out.reason == "hold_not_found" and env.count("book") == 0


def test_hold_already_used_for_a_booking_is_never_booked_again(env):
    h, _, aid = env.book_flow()
    env.book_out_of_band(h.hold_id)  # the world books this hold behind the agent's back
    out = env.executor.execute(aid)
    assert out.kind == "approval_invalid" and out.reason == "hold_already_booked"
    assert env.count("book") == 1  # only the out-of-band one; the executor added none
    assert env.attempts() == [] and env.store.approval(aid)["used_at"] is None


def test_approval_expired_between_approval_and_execution(env):
    _, _, aid = env.book_flow()
    env.clock.advance(seconds=200)
    assert env.executor.execute(aid).reason == "expired" and env.count("book") == 0


def test_executing_an_unknown_approval(env):
    out = env.executor.execute("apr_999999")
    assert out.kind == "approval_invalid" and out.reason == "unknown_approval" and env.calls() == []


def test_reexecuting_returns_the_stored_result_with_zero_tool_calls(env):
    _, _, aid = env.book_flow()
    first = env.executor.execute(aid)
    n = len(env.calls())
    second = env.executor.execute(aid)
    assert second.kind == "already_done" and second.booking == first.booking
    assert len(env.calls()) == n and second.tool_calls == 0


# ----------------------------------------------------------------------------- bounded execution


def test_tool_call_budget_leaves_the_attempt_pending_then_resolves_without_rebooking(env):
    _, _, aid = env.book_flow()
    out = env.executor.execute(aid, Budget(env.clock, BudgetLimits(max_tool_calls=2)))
    assert out.kind == "limit_reached" and out.reason == "tool_calls"
    assert env.attempts()[0]["status"] == "PENDING" and env.count("book") == 1
    again = env.executor.execute(aid)
    assert again.kind == "confirmed" and env.count("book") == 1


def test_wall_clock_budget_stops_before_the_next_call(env):
    _, _, aid = env.book_flow()
    env.world.sim.force("book", "timeout_after_commit")  # 3s simulated latency
    out = env.executor.execute(aid, Budget(env.clock, BudgetLimits(wall_clock_s=2)))
    assert out.kind == "limit_reached" and out.reason == "wall_clock"
    assert env.count("book") == 1 and env.attempts()[0]["status"] == "PENDING"
    assert env.executor.execute(aid).kind == "confirmed" and env.count("book") == 1


# ----------------------------------------------------------------------------- unrelated proposals stay independent


def test_two_bookings_in_one_session_use_separate_approvals_and_keys(env):
    _, _, a1 = env.book_flow()
    assert env.executor.execute(a1).kind == "confirmed"
    h2 = env.hold(service="electrician", area="Whitefield")
    env.propose(h2)
    a2 = env.approve()
    assert a2 != a1 and env.executor.execute(a2).kind == "confirmed"
    keys = {c["args"]["idempotency_key"] for c in env.calls("book")}
    assert keys == {f"book-{a1}-0", f"book-{a2}-0"}
    assert len([b for b in env.user_bookings() if b["idempotency_key"]]) == 2
