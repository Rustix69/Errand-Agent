"""hold_expired: re-hold, rebind the SAME approval once if provider/slot/price are identical, else ask again.

Timeline used throughout (FakeClock starts 09:00): hold made 09:00 (expires 09:10); approval window
is 180 s from the approval, so approving at 09:09 leaves the approval open until 09:12.
"""

import sqlite3
from datetime import datetime

import pytest

from errand.contract import ErrorCode, ToolError
from errand.core import Hooks, resolve_pending
from errand.core.budget import Budget, BudgetLimits
from errand.core.executor import _Ctx
from errand.mocks import DEMO_USER_ID
from tests.core.helpers import SESSION, ProcessKilled


def expired_hold_open_approval(env):
    """Approved at 09:09, executed at 09:11: hold expired at 09:10, approval open until 09:12."""
    h = env.hold()
    env.clock.advance(minutes=9)
    pid = env.propose(h)
    aid = env.approve()
    env.clock.advance(seconds=120)
    return h, pid, aid


def slow_ok(env):
    """A budget that tolerates the big fake-clock jumps these tests use to expire holds."""
    return Budget(env.clock, BudgetLimits(wall_clock_s=10_000))


def seqs(env, aid):
    return [(a["seq"], a["status"], a["error_code"], bool(a["verified_at"]), a["tool_calls"]) for a in env.attempts(aid)]


# ----------------------------------------------------------------------------- same quote: no new question


def test_same_quote_rebinds_and_books_without_asking(env):
    h, pid, aid = expired_hold_open_approval(env)
    out = env.executor.execute(aid)
    assert out.kind == "confirmed" and out.new_proposal_id is None
    assert seqs(env, aid) == [(0, "FAILED", "hold_expired", True, 0), (1, "CONFIRMED", None, True, 1)]
    (audit,) = env.rebinds()
    assert audit["old_ref"] == h.hold_id and audit["new_ref"] != h.hold_id
    assert env.store.approval(aid)["rebind_count"] == 1 and env.store.approval(aid)["ref"] == audit["new_ref"]
    (call,) = env.calls("book")  # the expired hold was never sent to book
    assert call["args"] == {"hold_id": audit["new_ref"], "idempotency_key": f"book-{aid}-1"}
    assert env.count("hold_slot") == 2
    assert out.booking["price"] == h.quoted_price and out.booking["slot"] == h.slot
    assert len(env.store.all("SELECT * FROM proposals")) == 1  # nobody was asked anything


def test_same_quote_when_the_tool_itself_reports_hold_expired(make_env):
    def expire_before_first_book(att):
        if att["seq"] == 0:
            env.clock.advance(minutes=2)  # approval (until 09:12) still open at 09:11

    env = make_env(hooks=Hooks(before_tool_call=expire_before_first_book))
    h = env.hold()
    env.clock.advance(minutes=9)
    env.propose(h)
    aid = env.approve()
    out = env.executor.execute(aid, slow_ok(env))
    assert out.kind == "confirmed"
    assert seqs(env, aid) == [(0, "FAILED", "hold_expired", True, 1), (1, "CONFIRMED", None, True, 1)]
    assert [c["args"]["idempotency_key"] for c in env.calls("book")] == [f"book-{aid}-0", f"book-{aid}-1"]
    assert [c["outcome"] for c in env.calls("book")][0].startswith("error")
    assert len(env.rebinds()) == 1


# ----------------------------------------------------------------------------- price off by even one rupee: ask


def test_price_one_rupee_higher_on_rehold_asks_again(env):
    h, pid, aid = expired_hold_open_approval(env)
    env.world.sim.force("hold_slot", "price_change", delta=1)
    out = env.executor.execute(aid)
    assert out.kind == "needs_reapproval" and out.reason == "terms_changed"
    assert out.diff == {"price": (h.quoted_price, h.quoted_price + 1)}
    assert env.count("book") == 0
    assert seqs(env, aid) == [(0, "FAILED", "hold_expired", True, 0)]
    assert env.store.approval(aid)["rebind_count"] == 0 and env.rebinds() == []
    child = env.store.proposal(out.new_proposal_id)
    assert child["status"] == "pending" and child["parent_proposal_id"] == pid
    assert child["price"] == h.quoted_price + 1 and child["slot"] == h.slot
    assert env.store.proposal(pid)["status"] == "invalidated"

    # The user is asked again; a fresh approval then books at the NEW price.
    d = env.guard.on_user_message(SESSION, "yes")
    assert d.kind == "approved" and d.proposal_id == out.new_proposal_id and d.approval_id != aid
    done = env.executor.execute(d.approval_id)
    assert done.kind == "confirmed" and done.booking["price"] == h.quoted_price + 1
    assert env.count("book") == 1


def test_price_one_rupee_lower_also_asks(env):
    h, _, aid = expired_hold_open_approval(env)
    env.world.sim.force("hold_slot", "price_change", delta=-1)
    out = env.executor.execute(aid)
    assert out.kind == "needs_reapproval" and out.diff == {"price": (h.quoted_price, h.quoted_price - 1)}
    assert env.count("book") == 0


def test_repeated_execute_does_not_re_ask_or_re_hold(env):
    _, _, aid = expired_hold_open_approval(env)
    env.world.sim.force("hold_slot", "price_change", delta=1)
    first = env.executor.execute(aid)
    holds = env.count("hold_slot")
    second = env.executor.execute(aid)
    assert second.kind == "needs_reapproval" and second.new_proposal_id == first.new_proposal_id
    assert env.count("hold_slot") == holds and len(env.store.all("SELECT * FROM proposals")) == 2


# ----------------------------------------------------------------------------- different slot: ask


class ShiftSlot:
    """A marketplace that answers re-hold requests with a hold on a DIFFERENT slot."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def hold_slot(self, provider_id, slot):
        dt = datetime.fromisoformat(slot)
        return self._inner.hold_slot(provider_id, dt.replace(hour=dt.hour + 1).strftime("%Y-%m-%dT%H:%M"))


def _slot_with_free_next_hour(env):
    res = env.tools.search_providers("plumber", "Indiranagar", {"start": "2026-11-03", "end": "2026-11-06"})
    for p in res.providers:
        for s in p.available_slots:
            nxt = s[:11] + f"{int(s[11:13]) + 1:02d}:00"
            if nxt in p.available_slots:
                return p.provider_id, s
    raise AssertionError("no consecutive free slots")


def test_different_slot_on_rehold_asks_again(make_env):
    env = make_env(tools_wrapper=ShiftSlot)
    pid_, slot = _slot_with_free_next_hour(env)
    h = env.tools.hold_slot(pid_, slot)
    env.clock.advance(minutes=9)
    env.propose(h)
    aid = env.approve()
    env.clock.advance(seconds=120)
    out = env.executor.execute(aid)
    assert out.kind == "needs_reapproval" and out.reason == "terms_changed" and "slot" in out.diff
    assert out.diff["slot"][0] == slot and out.diff["slot"][1] != slot
    assert env.count("book") == 0 and env.rebinds() == []
    assert env.store.proposal(out.new_proposal_id)["slot"] == out.diff["slot"][1]


def test_slot_taken_before_execution_never_reaches_book(env):
    h, _, aid = expired_hold_open_approval(env)
    env.world.sim.take_slot(h.provider_id, h.slot)  # hold row is released by the world
    out = env.executor.execute(aid)
    assert out.kind == "slot_taken" and out.reason == "hold_released"
    assert env.count("book") == 0 and env.attempts() == []


def test_slot_gone_on_rehold_goes_back_to_options(make_env):
    # Another customer takes the slot AFTER the pre-check, right before our re-hold.
    env = make_env(hooks=Hooks(before_rebind=lambda att: env.world.sim.take_slot(h.provider_id, h.slot)))
    h, pid, aid = expired_hold_open_approval(env)
    out = env.executor.execute(aid)
    assert out.kind == "slot_taken" and out.reason == "slot_gone_on_rehold"
    assert env.count("book") == 0 and env.rebinds() == []
    assert env.store.proposal(pid)["status"] == "failed"
    assert seqs(env, aid) == [(0, "FAILED", "hold_expired", True, 0)]


def test_approval_window_closing_mid_flow_asks_again_even_if_the_quote_is_identical(make_env):
    # Approval has its OWN window: it can close while we are resolving, and then no rebind may happen.
    env = make_env(hooks=Hooks(before_rebind=lambda att: env.clock.advance(seconds=90)))  # 09:11 -> 09:12:30
    h, pid, aid = expired_hold_open_approval(env)
    out = env.executor.execute(aid, slow_ok(env))
    assert out.kind == "needs_reapproval" and out.reason == "approval_window_closed"
    assert env.count("book") == 0 and env.rebinds() == []
    child = env.store.proposal(out.new_proposal_id)
    assert child["status"] == "pending" and child["price"] == h.quoted_price and child["slot"] == h.slot


# ----------------------------------------------------------------------------- one rebind only


def test_second_expiry_after_a_rebind_asks_again_and_never_opens_seq_2(make_env):
    def expire_seq1(att):
        if att["seq"] == 1:
            env.clock.advance(minutes=11)

    env = make_env(hooks=Hooks(before_tool_call=expire_seq1))
    h, pid, aid = expired_hold_open_approval(env)
    out = env.executor.execute(aid, slow_ok(env))
    assert out.kind == "needs_reapproval" and out.reason == "rebind_limit" and out.new_proposal_id
    assert seqs(env, aid) == [(0, "FAILED", "hold_expired", True, 0), (1, "FAILED", "hold_expired", True, 1)]
    assert env.count("book") == 1 and len(env.rebinds()) == 1
    again = env.executor.execute(aid)
    assert again.kind == "needs_reapproval" and again.new_proposal_id == out.new_proposal_id
    assert len(env.attempts(aid)) == 2


# ----------------------------------------------------------------------------- verification gate


def test_timeout_after_commit_then_hold_expires_during_backoff_is_confirmed_without_seq1(make_env):
    env = make_env(
        hooks=Hooks(before_tool_call=lambda att: env.world.sim.force("get_booking_status", "unavailable"))
    )
    h = env.hold()  # expires 09:10:00
    env.clock.advance(minutes=9, seconds=57)  # 3 s of hold left
    env.propose(h)
    aid = env.approve()
    env.world.sim.force("book", "timeout_after_commit")  # commit, then 3 s of latency -> hold expires
    out = env.executor.execute(aid)
    assert out.kind == "confirmed"
    assert env.clock.now() > h.expires_at  # the hold really did expire while we were resolving
    assert env.count("hold_slot") == 1  # no re-hold
    assert env.count("book") == 1
    assert seqs(env, aid) == [(0, "CONFIRMED", None, True, 1)]
    assert env.rebinds() == [] and env.store.approval(aid)["rebind_count"] == 0
    assert len([b for b in env.user_bookings() if b["idempotency_key"]]) == 1


class LyingBook:
    """book commits for real but the caller is told 'hold_expired': the agent must not trust that."""

    def __init__(self, inner, holder):
        self._inner, self._holder = inner, holder

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def book(self, hold_id, idempotency_key):
        self._holder["world"].sim.force("book", "timeout_after_commit")
        try:
            self._inner.book(hold_id, idempotency_key)
        except ToolError:
            pass
        raise ToolError(ErrorCode.HOLD_EXPIRED, "hold expired (this claim is false)")


def test_hold_expired_claim_is_verified_not_trusted(make_env):
    holder = {}
    env = make_env(tools_wrapper=lambda inner: LyingBook(inner, holder))
    holder["world"] = env.world
    h, _, aid = env.book_flow()
    out = env.executor.execute(aid)
    assert out.kind == "confirmed"
    assert env.count("hold_slot") == 1 and env.rebinds() == []
    assert seqs(env, aid) == [(0, "CONFIRMED", None, True, 1)]
    assert len([b for b in env.user_bookings() if b["hold_id"] == h.hold_id]) == 1


def test_hold_expired_with_status_down_stays_pending_and_does_not_rehold(make_env):
    env = make_env(
        hooks=Hooks(in_consume_tx=lambda c: env.world.sim.force("get_booking_status", "unavailable", times=3))
    )
    h, _, aid = expired_hold_open_approval(env)
    out = env.executor.execute(aid)
    assert out.kind == "unknown" and out.reason == "hold_expired_unverified"
    assert seqs(env, aid) == [(0, "PENDING", "hold_expired", False, 0)]
    assert env.count("hold_slot") == 1 and env.count("book") == 0
    # Truth reachable again: the same approval resumes and finishes through seq 1.
    done = env.executor.execute(aid)
    assert done.kind == "confirmed"
    assert seqs(env, aid) == [(0, "FAILED", "hold_expired", True, 0), (1, "CONFIRMED", None, True, 1)]


def test_seq1_cannot_be_opened_while_seq0_is_unverified_schema_backstop(make_env):
    env = make_env(hooks=Hooks(before_tool_call=lambda att: (_ for _ in ()).throw(ProcessKilled())))
    _, _, aid = env.book_flow()
    with pytest.raises(ProcessKilled):
        env.executor.execute(aid)
    assert seqs(env, aid) == [(0, "PENDING", None, False, 0)]
    for seq, key in ((1, "k1"), (2, "k2")):
        with pytest.raises(sqlite3.IntegrityError):
            with env.store.tx() as c:
                env.store.insert_attempt(
                    c, key=key, approval_id=aid, seq=seq, action="book", ref="h", new_slot=None, now=env.clock.now()
                )


# ----------------------------------------------------------------------------- crashes around the rebind


def test_process_dies_after_seq0_is_failed_and_before_seq1_resume_finishes_it(make_env):
    def die(att):
        raise ProcessKilled()

    env = make_env(hooks=Hooks(before_rebind=die))
    h, _, aid = expired_hold_open_approval(env)
    with pytest.raises(ProcessKilled):
        env.executor.execute(aid)
    assert seqs(env, aid) == [(0, "FAILED", "hold_expired", True, 0)]  # nothing stuck in PENDING
    assert env.count("book") == 0

    env2 = make_env.restart(env)
    assert resolve_pending(env2.store, env2.agent_tools, DEMO_USER_ID, env2.clock) == []
    out = env2.executor.execute(aid)  # NOT stuck on "FAILED returns the stored outcome"
    assert out.kind == "confirmed"
    assert seqs(env2, aid) == [(0, "FAILED", "hold_expired", True, 0), (1, "CONFIRMED", None, True, 1)]
    assert len(env2.rebinds()) == 1 and env2.count("book") == 1


def test_resume_after_the_approval_window_closed_asks_again(make_env):
    env = make_env(hooks=Hooks(before_rebind=lambda att: (_ for _ in ()).throw(ProcessKilled())))
    _, _, aid = expired_hold_open_approval(env)
    with pytest.raises(ProcessKilled):
        env.executor.execute(aid)
    env.clock.advance(seconds=90)  # 09:12:30, past the approval window
    env2 = make_env.restart(env)
    out = env2.executor.execute(aid)
    assert out.kind == "needs_reapproval" and out.reason == "approval_window_closed" and out.new_proposal_id
    assert env2.count("book") == 0 and env2.rebinds() == []


def test_consume_and_attempt_are_one_transaction(make_env):
    env = make_env(hooks=Hooks(in_consume_tx=lambda c: (_ for _ in ()).throw(ProcessKilled())))
    _, _, aid = expired_hold_open_approval(env)  # pre-check path (seq 0 is written as PENDING/hold_expired)
    with pytest.raises(ProcessKilled):
        env.executor.execute(aid)
    assert env.store.approval(aid)["used_at"] is None and env.attempts() == []  # all-or-nothing
    env2 = make_env.restart(env)
    assert env2.executor.execute(aid).kind == "confirmed"  # approval was never consumed, still valid


def test_pre_check_path_writes_consume_and_pending_seq0_together(make_env):
    seen = {}
    env = make_env(
        hooks=Hooks(in_consume_tx=lambda c: seen.update(
            rows=c.execute("SELECT count(*) FROM booking_attempts").fetchone()[0]))
    )
    _, _, aid = expired_hold_open_approval(env)
    env.executor.execute(aid)
    assert seen["rows"] == 0  # attempt not yet inserted when the hook runs, but committed with the consume
    first = env.attempts(aid)[0]
    assert first["tool_calls"] == 0 and first["seq"] == 0


def test_crash_between_seq1_insert_and_its_book_never_opens_a_seq2(make_env):
    def die_on_seq1(att):
        if att["seq"] == 1:
            raise ProcessKilled()

    env = make_env(hooks=Hooks(before_tool_call=die_on_seq1))
    _, _, aid = expired_hold_open_approval(env)
    with pytest.raises(ProcessKilled):
        env.executor.execute(aid)
    assert [s[:2] for s in seqs(env, aid)] == [(0, "FAILED"), (1, "PENDING")]
    env2 = make_env.restart(env)
    res = resolve_pending(env2.store, env2.agent_tools, DEMO_USER_ID, env2.clock)
    assert [r.result for r in res] == ["not_executed"]
    out = env2.executor.execute(aid)
    assert out.kind == "needs_reapproval" and out.reason == "not_executed"
    assert len(env2.attempts(aid)) == 2 and env2.count("book") == 0


def test_running_seq1_twice_is_refused(make_env):
    env = make_env(hooks=Hooks(before_tool_call=lambda att: att["seq"] == 1 and (_ for _ in ()).throw(ProcessKilled())))
    _, _, aid = expired_hold_open_approval(env)
    with pytest.raises(ProcessKilled):
        env.executor.execute(aid)
    appr = env.store.approval(aid)
    prop = env.store.proposal(appr["proposal_id"])
    from errand.core.approval import terms_of
    from errand.core.budget import Budget
    from errand.core.truth import Limits, Reader

    budget = Budget(env.clock)
    ctx = _Ctx(aid, prop, terms_of(prop), budget, Reader(env.agent_tools, env.clock, Limits(), budget))
    att0 = env.attempts(aid)[0]
    out = env.executor._rebind_flow(ctx, att0)  # a second attempt to open seq 1
    assert out.kind == "needs_reapproval" and out.reason == "rebind_limit"
    assert [s[:2] for s in seqs(env, aid)] == [(0, "FAILED"), (1, "PENDING")]
    assert env.count("book") == 0 and len(env.rebinds()) == 1
    with pytest.raises(sqlite3.IntegrityError):  # and the schema refuses it independently
        with env.store.tx() as c:
            env.store.insert_attempt(
                c, key="dup", approval_id=aid, seq=1, action="book", ref="h", new_slot=None, now=env.clock.now()
            )


def test_executing_again_after_the_rebind_booking_returns_it_without_tool_calls(env):
    _, _, aid = expired_hold_open_approval(env)
    first = env.executor.execute(aid)
    n, books = len(env.calls()), env.count("book")
    again = env.executor.execute(aid)
    assert again.kind == "already_done" and again.booking == first.booking
    assert len(env.calls()) == n and env.count("book") == books == 1 and len(env.attempts(aid)) == 2
