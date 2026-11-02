import sqlite3
from datetime import timedelta

import pytest

from errand.clock import FakeClock
from errand.mocks import ErrorCode, MockWorld, ScenarioConfig, ToolError, ToolTimeout
from tests.conftest import WEEK, free_slot

# ----------------------------------------------------------------------------- hold


def test_hold_lasts_10_minutes_and_hides_slot(tools, clock):
    pid, slot, price = free_slot(tools)
    h = tools.hold_slot(pid, slot)
    assert h.expires_at == clock.now() + timedelta(minutes=10)
    assert h.quoted_price == h.search_price == price and not h.price_changed
    still_listed = [p for p in tools.search_providers("plumber", "Indiranagar", WEEK).providers
                    if p.provider_id == pid and slot in p.available_slots]
    assert not still_listed


def test_rehold_same_slot_returns_same_hold(tools):
    pid, slot, _ = free_slot(tools)
    assert tools.hold_slot(pid, slot).hold_id == tools.hold_slot(pid, slot).hold_id


def test_expired_hold_frees_slot(tools, clock):
    pid, slot, _ = free_slot(tools)
    first = tools.hold_slot(pid, slot)
    clock.advance(minutes=10, seconds=1)
    second = tools.hold_slot(pid, slot)
    assert second.hold_id != first.hold_id
    assert tools.get_booking_status(hold_id=first.hold_id).hold.status == "expired"


def test_forced_price_change_on_hold(world, tools):
    pid, slot, price = free_slot(tools)
    world.sim.force("hold_slot", "price_change", delta=300)
    h = tools.hold_slot(pid, slot)
    assert h.quoted_price == price + 300 and h.price_changed


def test_seeded_price_changes_happen_at_configured_rate(make_world):
    w = make_world(ScenarioConfig(seed=7, price_change_rate=0.5))
    t = w.tools()
    res = t.search_providers("plumber", "Indiranagar", WEEK)
    holds = [t.hold_slot(p.provider_id, s) for p in res.providers for s in p.available_slots[:10]]
    changed = sum(h.price_changed for h in holds)
    assert 0.25 * len(holds) < changed < 0.75 * len(holds)


def test_hold_unavailable_slot(tools):
    with pytest.raises(ToolError) as e:
        tools.hold_slot("pl_01", "2026-11-03T03:00")  # outside working hours
    assert e.value.code == ErrorCode.SLOT_UNAVAILABLE
    with pytest.raises(ToolError) as e:
        tools.hold_slot("pl_01", "2026-11-01T10:00")  # past
    assert e.value.code == ErrorCode.INVALID_ARGUMENT
    with pytest.raises(ToolError) as e:
        tools.hold_slot("pl_01", "2026-11-03T10:30")
    assert e.value.code == ErrorCode.INVALID_ARGUMENT


# ----------------------------------------------------------------------------- book


def _hold(tools):
    pid, slot, _ = free_slot(tools)
    return tools.hold_slot(pid, slot)


def test_book_success_and_status(world, tools):
    h = _hold(tools)
    r = tools.book(h.hold_id, "key-1")
    assert r.status == "confirmed" and r.price == h.quoted_price and not r.replayed
    st = tools.get_booking_status(idempotency_key="key-1")
    assert st.status == "confirmed" and st.booking.booking_id == r.booking_id
    assert tools.get_booking_status(hold_id=h.hold_id).hold.status == "booked"


def test_book_same_key_is_idempotent(world, tools):
    h = _hold(tools)
    a = tools.book(h.hold_id, "key-1")
    b = tools.book(h.hold_id, "key-1")
    assert b.booking_id == a.booking_id and b.replayed
    assert len([x for x in world.sim.bookings() if x["idempotency_key"] == "key-1"]) == 1


def test_book_used_hold_with_new_key_is_rejected(world, tools):
    h = _hold(tools)
    tools.book(h.hold_id, "key-1")
    with pytest.raises(ToolError) as e:
        tools.book(h.hold_id, "key-2")
    assert e.value.code == ErrorCode.HOLD_NOT_ACTIVE
    assert sum(1 for x in world.sim.bookings() if x["hold_id"] == h.hold_id) == 1


def test_key_reuse_for_different_hold_conflicts(world, tools):
    h1 = _hold(tools)
    tools.book(h1.hold_id, "key-1")
    pid, slot, _ = free_slot(tools, "electrician", "Whitefield")
    h2 = tools.hold_slot(pid, slot)
    with pytest.raises(ToolError) as e:
        tools.book(h2.hold_id, "key-1")
    assert e.value.code == ErrorCode.IDEMPOTENCY_CONFLICT


def test_book_after_expiry(world, tools, clock):
    h = _hold(tools)
    clock.advance(minutes=10, seconds=1)
    with pytest.raises(ToolError) as e:
        tools.book(h.hold_id, "key-1")
    assert e.value.code == ErrorCode.HOLD_EXPIRED
    assert tools.get_booking_status(idempotency_key="key-1").status == "not_booked"


def test_book_unknown_hold(tools):
    with pytest.raises(ToolError) as e:
        tools.book("hold_999999", "key-1")
    assert e.value.code == ErrorCode.HOLD_NOT_FOUND


def test_slot_taken(world, tools):
    h = _hold(tools)
    world.sim.force("book", "slot_taken")
    with pytest.raises(ToolError) as e:
        tools.book(h.hold_id, "key-1")
    assert e.value.code == ErrorCode.SLOT_TAKEN and not e.value.retryable
    assert tools.get_booking_status(hold_id=h.hold_id).status == "not_booked"
    with pytest.raises(ToolError) as e:
        tools.hold_slot(h.provider_id, h.slot)
    assert e.value.code == ErrorCode.SLOT_UNAVAILABLE


def test_slot_taken_by_another_customer_after_hold(world, tools):
    h = _hold(tools)
    world.sim.take_slot(h.provider_id, h.slot)
    with pytest.raises(ToolError) as e:
        tools.book(h.hold_id, "key-1")
    assert e.value.code == ErrorCode.SLOT_TAKEN


# ----------------------------------------------------------------------------- timeouts


class ProbeClock(FakeClock):
    """Observes world.db through an independent connection whenever the mock 'waits'.
    The only wait in a timed-out book is between commit and raise."""

    def __init__(self, db_path, key):
        super().__init__()
        self.db_path, self.key, self.seen = str(db_path), key, []

    def sleep(self, seconds):
        if seconds > 0:
            with sqlite3.connect(self.db_path) as other:
                n = other.execute("SELECT count(*) FROM bookings WHERE idempotency_key = ?", (self.key,)).fetchone()[0]
            self.seen.append(n)
        super().sleep(seconds)


def test_timeout_after_commit_persists_booking_before_raising(tmp_path):
    clock = ProbeClock(tmp_path / "world.db", "key-1")
    w = MockWorld(tmp_path / "world.db", clock, ScenarioConfig(mode="forced"))
    t = w.tools()
    h = _hold(t)
    w.sim.force("book", "timeout_after_commit")
    with pytest.raises(ToolTimeout) as e:
        t.book(h.hold_id, "key-1")
    assert e.value.code == ErrorCode.TIMEOUT and e.value.retryable
    # While the caller was still waiting, another connection already saw the committed booking.
    assert clock.seen == [1]
    w.close()


def test_timeout_after_commit_truth_only_via_status(world, tools, clock):
    h = _hold(tools)
    world.sim.force("book", "timeout_after_commit")
    t0 = clock.monotonic()
    with pytest.raises(ToolTimeout):
        tools.book(h.hold_id, "key-1")
    assert clock.monotonic() - t0 == pytest.approx(world.config.timeout_latency_s)
    st = tools.get_booking_status(idempotency_key="key-1")
    assert st.status == "confirmed" and st.booking.slot == h.slot
    assert tools.get_booking_status(hold_id=h.hold_id).status == "confirmed"
    # A blind retry with the same key does not double-book: it replays the stored result.
    again = tools.book(h.hold_id, "key-1")
    assert again.replayed and again.booking_id == st.booking.booking_id
    assert sum(1 for b in world.sim.bookings() if b["idempotency_key"] == "key-1") == 1


def test_timeout_after_commit_survives_restart(make_world):
    w = make_world(ScenarioConfig(mode="forced"))
    t = w.tools()
    h = _hold(t)
    w.sim.force("book", "timeout_after_commit")
    with pytest.raises(ToolTimeout):
        t.book(h.hold_id, "key-1")
    w.close()
    w2 = make_world()  # same file, fresh process
    assert w2.tools().get_booking_status(idempotency_key="key-1").status == "confirmed"


def test_timeout_before_commit_then_retry_same_key(world, tools):
    h = _hold(tools)
    world.sim.force("book", "timeout_before_commit")
    with pytest.raises(ToolTimeout):
        tools.book(h.hold_id, "key-1")
    st = tools.get_booking_status(hold_id=h.hold_id)
    assert st.status == "not_booked" and st.hold.status == "active"
    r = tools.book(h.hold_id, "key-1")
    assert r.status == "confirmed" and not r.replayed
    assert sum(1 for b in world.sim.bookings() if b["idempotency_key"] == "key-1") == 1


def test_book_503_has_no_side_effect(world, tools):
    h = _hold(tools)
    world.sim.force("book", "unavailable")
    with pytest.raises(ToolError) as e:
        tools.book(h.hold_id, "key-1")
    assert e.value.code == ErrorCode.UNAVAILABLE
    assert tools.get_booking_status(hold_id=h.hold_id).status == "not_booked"


def test_get_booking_status_needs_exactly_one_arg(tools):
    for kwargs in ({}, {"hold_id": "h", "idempotency_key": "k"}):
        with pytest.raises(ToolError) as e:
            tools.get_booking_status(**kwargs)
        assert e.value.code == ErrorCode.INVALID_ARGUMENT
