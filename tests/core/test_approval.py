import sqlite3

import pytest

from errand.core import classify_reply
from errand.core.approval import fingerprint
from errand.mocks import DEMO_USER_ID
from tests.core.helpers import SESSION

# ----------------------------------------------------------------------------- classifier


@pytest.mark.parametrize(
    "text,action,expected",
    [
        ("yes", "book", "approve"),
        ("Yes", "book", "approve"),
        ("YES!!", "book", "approve"),
        ("  ok. ", "book", "approve"),
        ("go ahead", "cancel", "approve"),
        ("Do it", "reschedule", "approve"),
        ("book it", "book", "approve"),
        ("cancel it", "cancel", "approve"),
        ("move it", "reschedule", "approve"),
        ("CONFIRM 7F3A", "book", "approve"),
        ("confirm 7f3a", "cancel", "approve"),
        ("cancel it", "book", "other"),  # an action phrase counts only for its own action
        ("book it", "cancel", "other"),
        ("confirm 0000", "book", "other"),  # wrong code
        ("yes but make it 11", "book", "other"),
        ("yes, also cancel my AC", "cancel", "other"),
        ("yes please", "book", "other"),
        ("sounds good", "book", "other"),
        ("ok but cheaper", "book", "other"),
        ("", "book", "other"),
        ("no", "book", "decline"),
        ("No thanks.", "book", "decline"),
        ("never mind", "cancel", "decline"),
        ("don’t", "cancel", "decline"),
    ],
)
def test_classify_reply(text, action, expected):
    assert classify_reply(text, action, "7F3A") == expected


# ----------------------------------------------------------------------------- creation path


def test_proposing_creates_no_approval(env):
    env.propose(env.hold())
    assert env.store.all("SELECT * FROM approvals") == []


def test_approval_is_created_only_from_a_user_message_and_binds_exact_terms(env):
    h = env.hold()
    pid = env.propose(h)
    d = env.guard.on_user_message(SESSION, "yes")
    assert d.kind == "approved" and d.proposal_id == pid
    appr = env.store.approval(d.approval_id)
    prop = env.store.proposal(pid)
    assert appr["user_message"] == "yes" and appr["ref"] == h.hold_id
    assert (prop["provider_id"], prop["slot"], prop["price"]) == (h.provider_id, h.slot, h.quoted_price)
    assert prop["status"] == "approved" and appr["used_at"] is None
    assert appr["fingerprint"] == prop["fingerprint"]


def test_booking_terms_use_the_holds_quote_not_the_search_price(env):
    pid_, slot, search_price = env.free_slot()
    env.world.sim.force("hold_slot", "price_change", delta=300)
    h = env.tools.hold_slot(pid_, slot)
    prop = env.store.proposal(env.propose(h))
    assert h.quoted_price == search_price + 300 and prop["price"] == h.quoted_price


@pytest.mark.parametrize("text", ["yes but make it 11", "yes, but cheaper", "what about tomorrow?", "hmm"])
def test_change_requests_are_not_approvals(env, text):
    pid = env.propose(env.hold())
    d = env.guard.on_user_message(SESSION, text)
    assert d.kind == "other"
    assert env.store.proposal(pid)["status"] == "pending"
    assert env.store.all("SELECT * FROM approvals") == []


def test_decline(env):
    pid = env.propose(env.hold())
    assert env.guard.on_user_message(SESSION, "no thanks").kind == "declined"
    assert env.store.proposal(pid)["status"] == "declined"
    assert env.guard.on_user_message(SESSION, "yes").kind == "other"  # nothing left to approve
    assert env.store.all("SELECT * FROM approvals") == []


def test_yes_with_no_proposal_is_just_a_message(env):
    assert env.guard.on_user_message(SESSION, "yes").kind == "other"
    assert env.store.all("SELECT * FROM approvals") == []


def test_new_proposal_supersedes_pending_one(env):
    h1 = env.hold(index=0)
    h2 = env.hold(service="electrician", area="Whitefield")
    p1, p2 = env.propose(h1), env.propose(h2)
    assert env.store.proposal(p1)["status"] == "superseded"
    d = env.guard.on_user_message(SESSION, "yes")
    assert d.proposal_id == p2


def test_new_proposal_invalidates_an_unused_approval(env):
    h1 = env.hold(index=0)
    p1 = env.propose(h1)
    a1 = env.approve()
    env.propose(env.hold(service="electrician", area="Whitefield"))
    assert env.store.approval(a1)["invalidated_reason"] == "superseded"
    out = env.executor.execute(a1)
    assert out.kind == "approval_invalid" and out.reason == "superseded"
    assert env.count("book") == 0 and env.store.proposal(p1)["status"] == "superseded"


def test_one_pending_proposal_per_session_is_enforced_by_the_schema(env):
    env.propose(env.hold())
    with pytest.raises(sqlite3.IntegrityError):
        with env.store.tx() as c:
            c.execute(
                "INSERT INTO proposals(id, session_id, user_id, action, provider_id, slot, price, terms_fp,"
                " fingerprint, confirm_code, status, created_at, expires_at)"
                " VALUES ('x', ?, 'u', 'book', 'p', 's', 1, 't', 'f', 'c', 'pending', 'n', 'n')",
                (SESSION,),
            )


def test_proposal_expires_before_approval(env):
    pid = env.propose(env.hold())
    env.clock.advance(minutes=16)
    assert env.guard.on_user_message(SESSION, "yes").kind == "expired"
    assert env.store.proposal(pid)["status"] == "expired"
    assert env.store.all("SELECT * FROM approvals") == []


def test_approval_expiry_blocks_the_tool(env):
    _, pid, aid = env.book_flow()
    env.clock.advance(seconds=181)
    out = env.executor.execute(aid)
    assert out.kind == "approval_invalid" and out.reason == "expired"
    assert env.count("book") == 0 and env.attempts() == []


def test_consume_is_single_use(env):
    _, _, aid = env.book_flow()
    with env.store.tx() as c:
        assert env.guard.consume(c, aid) is None
    with env.store.tx() as c:
        assert env.guard.consume(c, aid) == "used"


def test_fingerprint_binds_terms_and_reference(env):
    h = env.hold()
    prop = env.store.proposal(env.propose(h))
    from errand.core.approval import terms_of

    t = terms_of(prop)
    assert fingerprint(t, h.hold_id) == prop["fingerprint"]
    assert fingerprint(t, "hold_other") != prop["fingerprint"]
    assert fingerprint(t.__class__(**{**t.as_dict(), "price": t.price + 1}), h.hold_id) != prop["fingerprint"]


# ----------------------------------------------------------------------------- rebind rules (unit)


def _consumed(env):
    h, pid, aid = env.book_flow()
    with env.store.tx() as c:
        assert env.guard.consume(c, aid) is None
    return h, aid


def _rebind(env, aid, hold):
    with env.store.tx() as c:
        return env.guard.rebind(c, aid, hold)


def test_rebind_allowed_for_identical_terms_and_audited(env):
    h, aid = _consumed(env)
    new = h.model_copy(update={"hold_id": "hold_new"})
    assert _rebind(env, aid, new) == (None, None)
    row = env.store.approval(aid)
    assert row["rebind_count"] == 1 and row["ref"] == "hold_new"
    (audit,) = env.rebinds()
    assert (audit["old_ref"], audit["new_ref"]) == (h.hold_id, "hold_new")
    assert audit["old_fingerprint"] != audit["new_fingerprint"] and audit["terms_fp"] == row["terms_fp"]


def test_rebind_refused_when_price_is_one_rupee_off(env):
    h, aid = _consumed(env)
    reason, diff = _rebind(env, aid, h.model_copy(update={"hold_id": "n", "quoted_price": h.quoted_price + 1}))
    assert reason == "terms_changed" and diff == {"price": (h.quoted_price, h.quoted_price + 1)}
    assert env.store.approval(aid)["rebind_count"] == 0 and env.rebinds() == []
    reason, _ = _rebind(env, aid, h.model_copy(update={"hold_id": "n", "quoted_price": h.quoted_price - 1}))
    assert reason == "terms_changed"


def test_rebind_refused_for_a_different_slot_or_provider(env):
    h, aid = _consumed(env)
    reason, diff = _rebind(env, aid, h.model_copy(update={"hold_id": "n", "slot": "2026-11-05T17:00"}))
    assert reason == "terms_changed" and "slot" in diff
    reason, diff = _rebind(env, aid, h.model_copy(update={"hold_id": "n", "provider_id": "pl_99"}))
    assert reason == "terms_changed" and "provider_id" in diff


def test_rebind_only_once_per_approval(env):
    h, aid = _consumed(env)
    assert _rebind(env, aid, h.model_copy(update={"hold_id": "h2"}))[0] is None
    assert _rebind(env, aid, h.model_copy(update={"hold_id": "h3"}))[0] == "rebind_limit"
    assert len(env.rebinds()) == 1 and env.store.approval(aid)["ref"] == "h2"


def test_rebind_refused_after_the_approval_window(env):
    h, aid = _consumed(env)
    env.clock.advance(seconds=181)
    assert _rebind(env, aid, h.model_copy(update={"hold_id": "n"}))[0] == "approval_window_closed"


def test_rebind_requires_a_consumed_approval(env):
    h, _, aid = env.book_flow()  # approved, NOT consumed
    assert _rebind(env, aid, h.model_copy(update={"hold_id": "n"}))[0] == "rebind_limit"
    assert env.rebinds() == []


def test_rebind_is_for_bookings_only(env):
    b = env.booking("cl_04")
    env.guard.propose_cancel(SESSION, DEMO_USER_ID, b)
    aid = env.approve()
    with env.store.tx() as c:
        env.guard.consume(c, aid)
    h = env.hold()
    assert _rebind(env, aid, h)[0] == "not_rebindable"


# ----------------------------------------------------------------------------- repeat window scope


def _approved_and_done(env):
    h, pid, aid = env.book_flow()
    out = env.executor.execute(aid)
    assert out.kind == "confirmed"
    env.guard.mark_result_reported(SESSION, pid)
    return pid, aid


def test_yes_right_after_the_result_is_a_repeat_with_no_tool_calls(env):
    pid, aid = _approved_and_done(env)
    n = len(env.calls())
    d = env.guard.on_user_message(SESSION, "yes")
    assert (d.kind, d.proposal_id, d.approval_id) == ("repeat", pid, aid)
    out = env.executor.execute(d.approval_id)
    assert out.kind == "already_done" and len(env.calls()) == n and env.count("book") == 1


def test_yes_later_is_an_ordinary_message_not_a_repeat(env):
    _approved_and_done(env)
    assert env.guard.on_user_message(SESSION, "thanks!").kind == "other"
    assert env.guard.on_user_message(SESSION, "yes").kind == "other"  # window already spent
    assert len(env.store.all("SELECT * FROM approvals")) == 1


def test_new_proposal_disarms_repeat_and_yes_approves_only_the_new_one(env):
    old_pid, old_aid = _approved_and_done(env)
    h2 = env.hold(service="electrician", area="Whitefield")
    new_pid = env.propose(h2)
    d = env.guard.on_user_message(SESSION, "yes")
    assert d.kind == "approved" and d.proposal_id == new_pid and d.approval_id != old_aid


def test_double_yes_before_the_result_is_reported_executes_once(env):
    h = env.hold()
    pid = env.propose(h)
    d1 = env.guard.on_user_message(SESSION, "yes")
    d2 = env.guard.on_user_message(SESSION, "yes")
    assert d1.kind == "approved" and (d2.kind, d2.approval_id) == ("repeat", d1.approval_id)
    first = env.executor.execute(d1.approval_id)
    second = env.executor.execute(d2.approval_id)
    assert (first.kind, second.kind) == ("confirmed", "already_done")
    assert env.count("book") == 1
    assert sum(1 for b in env.user_bookings() if b["idempotency_key"]) == 1
    assert first.booking["booking_id"] == second.booking["booking_id"]


def test_repeat_window_covers_only_the_very_next_reply(env):
    pid, _ = _approved_and_done(env)
    assert env.guard.on_user_message(SESSION, "what time is it?").kind == "other"
    assert env.guard.on_user_message(SESSION, "yes").kind == "other"
    assert env.store.one("SELECT * FROM repeat_window WHERE session_id = ?", (SESSION,)) is None

