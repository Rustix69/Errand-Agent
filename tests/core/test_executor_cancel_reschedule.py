import pytest

from errand.mocks import DEMO_USER_ID
from errand.mocks.catalog import next_saturday
from tests.core.helpers import SESSION


def cancel_flow(env, provider="cl_04"):
    b = env.booking(provider)
    pid = env.guard.propose_cancel(SESSION, DEMO_USER_ID, b)
    return b, pid, env.approve()


def reschedule_flow(env, new_slot, provider="ac_01"):
    b = env.booking(provider)
    pid = env.guard.propose_reschedule(SESSION, DEMO_USER_ID, b, new_slot)
    return b, pid, env.approve()


def saturday_slots(env):
    sat = next_saturday(env.clock.now().date()).isoformat()
    res = env.tools.search_providers("ac_servicing", "HSR Layout", {"start": sat, "end": sat})
    free = next(p for p in res.providers if p.provider_id == "ac_01").available_slots
    return sat, free


def status_of(env, booking_id):
    return next(b for b in env.tools.list_bookings(DEMO_USER_ID).bookings if b.booking_id == booking_id)


# ----------------------------------------------------------------------------- cancel


def test_cancel_503_then_same_key_retry_succeeds(env):
    b, pid, aid = cancel_flow(env)
    setup_calls = len(env.calls())
    out = env.executor.execute(aid)
    assert out.kind == "confirmed" and out.booking["status"] == "cancelled"
    cancels = env.calls("cancel")
    assert [c["outcome"].split(":")[0] for c in cancels] == ["error", "ok"]  # first call 503, retry ok
    assert {c["args"]["idempotency_key"] for c in cancels} == {f"cancel-{aid}-0"}
    assert [c["tool"] for c in env.calls()[setup_calls:]] == [
        "list_bookings", "cancel", "list_bookings", "cancel", "list_bookings",
    ]  # pre-check, 503, source-of-truth check, retry, verify
    assert status_of(env, b.booking_id).status == "cancelled"
    (att,) = env.attempts()
    assert att["status"] == "CONFIRMED" and att["verified_at"] and att["tool_calls"] == 2
    assert env.store.proposal(pid)["status"] == "executed"


def test_cancel_retries_exhausted_leaves_the_booking_and_says_so(env):
    b, pid, aid = cancel_flow(env)
    env.world.sim.force("cancel", "unavailable", times=3)
    out = env.executor.execute(aid)
    assert out.kind == "failed" and out.reason == "retries_exhausted"
    assert env.count("cancel") == 3 and status_of(env, b.booking_id).status == "confirmed"
    assert env.attempts()[0]["status"] == "FAILED" and env.store.proposal(pid)["status"] == "failed"


def test_cancel_timeout_after_commit_is_verified_not_retried(env):
    b, _, aid = cancel_flow(env)
    env.world.sim.force("cancel", "timeout_after_commit")
    out = env.executor.execute(aid)
    assert out.kind == "confirmed" and env.count("cancel") == 1
    assert status_of(env, b.booking_id).status == "cancelled"


def test_provider_cancelled_first_means_no_cancel_call(env):
    b, _, aid = cancel_flow(env)
    env.world.sim.provider_cancels(b.booking_id)
    out = env.executor.execute(aid)
    assert out.kind == "approval_invalid" and out.reason == "booking_not_active"
    assert env.count("cancel") == 0 and env.attempts() == []


def test_cancel_of_a_missing_booking(env):
    b = env.booking("cl_04")
    env.guard.propose_cancel(SESSION, DEMO_USER_ID, b.model_copy(update={"booking_id": "bk_999999"}))
    out = env.executor.execute(env.approve())
    assert out.kind == "approval_invalid" and out.reason == "booking_not_found" and env.count("cancel") == 0


def test_repeated_cancel_confirmation_returns_the_first_result(env):
    _, _, aid = cancel_flow(env)
    first = env.executor.execute(aid)
    n = len(env.calls())
    second = env.executor.execute(aid)
    assert second.kind == "already_done" and second.booking == first.booking and len(env.calls()) == n


# ----------------------------------------------------------------------------- reschedule


def test_reschedule_success_is_verified_against_list_bookings(env):
    _, free = saturday_slots(env)
    target = next(s for s in free if not s.endswith("T10:00"))
    b, _, aid = reschedule_flow(env, target)
    out = env.executor.execute(aid)
    assert out.kind == "confirmed" and out.booking["slot"] == target
    (call,) = env.calls("reschedule")
    assert call["args"] == {"booking_id": b.booking_id, "slot": target, "idempotency_key": f"reschedule-{aid}-0"}
    assert status_of(env, b.booking_id).slot == target


def test_reschedule_slot_conflict_goes_back_to_options_then_fresh_approval_works(env):
    sat, free = saturday_slots(env)
    b, pid, aid = reschedule_flow(env, f"{sat}T10:00")  # seeded conflict slot
    out = env.executor.execute(aid)
    assert out.kind == "slot_conflict" and out.reason == "slot_conflict"
    assert env.count("reschedule") == 1  # no retry on a conflict
    assert status_of(env, b.booking_id).slot == b.slot
    assert env.attempts()[0]["status"] == "FAILED" and env.store.proposal(pid)["status"] == "failed"
    assert env.executor.execute(aid).kind == "slot_conflict" and env.count("reschedule") == 1

    # Back to options: a new slot needs a NEW proposal and a NEW approval.
    other = next(s for s in free if not s.endswith("T10:00"))
    env.guard.propose_reschedule(SESSION, DEMO_USER_ID, b, other)
    aid2 = env.approve()
    assert aid2 != aid and env.executor.execute(aid2).kind == "confirmed"
    assert status_of(env, b.booking_id).slot == other


def test_forced_slot_conflict(env):
    _, free = saturday_slots(env)
    _, _, aid = reschedule_flow(env, next(s for s in free if not s.endswith("T10:00")))
    env.world.sim.force("reschedule", "slot_conflict")
    assert env.executor.execute(aid).kind == "slot_conflict"


def test_reschedule_timeout_after_commit_is_verified_not_retried(env):
    _, free = saturday_slots(env)
    target = next(s for s in free if not s.endswith("T10:00"))
    b, _, aid = reschedule_flow(env, target)
    env.world.sim.force("reschedule", "timeout_after_commit")
    out = env.executor.execute(aid)
    assert out.kind == "confirmed" and env.count("reschedule") == 1
    assert status_of(env, b.booking_id).slot == target


def test_reschedule_503_retries_with_the_same_key(env):
    _, free = saturday_slots(env)
    _, _, aid = reschedule_flow(env, next(s for s in free if not s.endswith("T10:00")))
    env.world.sim.force("reschedule", "unavailable")
    assert env.executor.execute(aid).kind == "confirmed"
    assert {c["args"]["idempotency_key"] for c in env.calls("reschedule")} == {f"reschedule-{aid}-0"}
    assert env.count("reschedule") == 2


def test_booking_moved_after_approval_invalidates_it(env):
    sat, free = saturday_slots(env)
    targets = [s for s in free if not s.endswith("T10:00")]
    b, _, aid = reschedule_flow(env, targets[0])
    env.out_of_band_keys.add("oob-move")
    env.world.tools(DEMO_USER_ID).reschedule(b.booking_id, targets[1], "oob-move")
    out = env.executor.execute(aid)
    assert out.kind == "approval_invalid" and out.reason == "fingerprint_mismatch"
    assert out.diff["slot"] == (b.slot, targets[1])
    assert env.count("reschedule") == 1  # only the out-of-band one


def test_unexpected_tool_error_on_reschedule_is_a_clear_failure(env):
    b = env.booking("ac_01")
    env.guard.propose_reschedule(SESSION, DEMO_USER_ID, b, b.slot)  # moving to the same slot: the tool refuses
    out = env.executor.execute(env.approve())
    assert out.kind == "failed" and out.reason == "invalid_argument"
    assert env.attempts()[0]["status"] == "FAILED"


@pytest.mark.parametrize("approve_text", ["yes", "go ahead", "move it"])
def test_reschedule_approval_phrases(env, approve_text):
    _, free = saturday_slots(env)
    b = env.booking("ac_01")
    env.guard.propose_reschedule(SESSION, DEMO_USER_ID, b, next(s for s in free if not s.endswith("T10:00")))
    assert env.guard.on_user_message(SESSION, approve_text).kind == "approved"
