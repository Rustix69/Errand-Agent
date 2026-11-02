from datetime import timedelta

import pytest

from errand.mocks import ErrorCode, ScenarioConfig, ServiceUnavailable, ToolError, ToolTimeout
from errand.mocks.catalog import DEMO_USER_AREA, ac_booking_day, next_saturday


def _booking(tools, provider_id):
    return next(b for b in tools.list_bookings("u_demo").bookings if b.provider_id == provider_id and b.is_upcoming)


# ----------------------------------------------------------------------------- list_bookings


def test_demo_user_history_supports_example_requests(tools, clock):
    bookings = tools.list_bookings("u_demo").bookings
    past_electricians = [b for b in bookings if b.service == "electrician" and not b.is_upcoming]
    assert len(past_electricians) == 2
    assert max(past_electricians, key=lambda b: b.slot).provider_id == "el_01"  # "same electrician as last time"
    ac = [b for b in bookings if b.service == "ac_servicing" and b.is_upcoming]
    assert len(ac) == 1 and ac[0].status == "confirmed"
    assert ac[0].slot.startswith(ac_booking_day(clock.now().date()).isoformat())
    assert {b.status for b in bookings} >= {"completed", "cancelled", "confirmed"}


def test_demo_history_providers_all_serve_home_area(tools):
    for b in tools.list_bookings("u_demo").bookings:
        assert DEMO_USER_AREA in tools.get_provider(b.provider_id).areas, b.provider_id


def test_list_bookings_is_scoped_to_user(tools):
    with pytest.raises(ToolError) as e:
        tools.list_bookings("u_other")
    assert e.value.code == ErrorCode.PERMISSION_DENIED


def test_upcoming_becomes_completed_as_time_passes(tools, clock):
    ac = _booking(tools, "ac_01")
    clock.advance(days=8)
    after = next(b for b in tools.list_bookings("u_demo").bookings if b.booking_id == ac.booking_id)
    assert after.status == "completed" and not after.is_upcoming


# ----------------------------------------------------------------------------- cancel


def test_cancel_first_call_503_then_retry_succeeds(world, tools):
    b = _booking(tools, "cl_04")
    with pytest.raises(ServiceUnavailable) as e:
        tools.cancel(b.booking_id, "c-1")
    assert e.value.retryable and e.value.details["http_status"] == 503
    assert _booking(tools, "cl_04").status == "confirmed"  # nothing happened
    r = tools.cancel(b.booking_id, "c-1")
    assert r.status == "cancelled"
    after = next(x for x in tools.list_bookings("u_demo").bookings if x.booking_id == b.booking_id)
    assert after.status == "cancelled"


def test_cancel_replay_and_double_cancel(world, tools):
    b = _booking(tools, "cl_04")
    with pytest.raises(ServiceUnavailable):
        tools.cancel(b.booking_id, "c-1")
    first = tools.cancel(b.booking_id, "c-1")
    again = tools.cancel(b.booking_id, "c-1")
    assert again.replayed and again.cancelled_at == first.cancelled_at
    with pytest.raises(ToolError) as e:
        tools.cancel(b.booking_id, "c-2")
    assert e.value.code == ErrorCode.BOOKING_NOT_ACTIVE


def test_cancel_first_call_failure_is_configurable(make_world):
    w = make_world(ScenarioConfig(cancel_first_call_fails=False))
    t = w.tools()
    assert t.cancel(_booking(t, "cl_04").booking_id, "c-1").status == "cancelled"


def test_cancel_past_or_foreign_booking(tools):
    past = next(b for b in tools.list_bookings("u_demo").bookings if b.status == "completed")
    with pytest.raises(ToolError) as e:
        tools.cancel(past.booking_id, "c-1")
    assert e.value.code == ErrorCode.BOOKING_NOT_ACTIVE
    with pytest.raises(ToolError) as e:
        tools.cancel("bk_999999", "c-2")
    assert e.value.code == ErrorCode.BOOKING_NOT_FOUND


def test_cancel_timeout_after_commit(world, tools):
    b = _booking(tools, "cl_04")
    world.sim.force("cancel", "timeout_after_commit")
    with pytest.raises(ToolTimeout):
        tools.cancel(b.booking_id, "c-1")
    after = next(x for x in tools.list_bookings("u_demo").bookings if x.booking_id == b.booking_id)
    assert after.status == "cancelled"


# ----------------------------------------------------------------------------- reschedule


def test_reschedule_configured_conflict(world, tools, clock):
    ac = _booking(tools, "ac_01")
    sat10 = f"{next_saturday(clock.now().date()).isoformat()}T10:00"
    with pytest.raises(ToolError) as e:
        tools.reschedule(ac.booking_id, sat10, "r-1")
    assert e.value.code == ErrorCode.SLOT_CONFLICT
    assert _booking(tools, "ac_01").slot == ac.slot  # unchanged


def test_seeded_conflict_survives_reopen_with_new_config(make_world, clock):
    make_world(ScenarioConfig()).close()
    w = make_world(ScenarioConfig(mode="forced"))  # explicit config on an existing world.db
    t = w.tools()
    sat10 = f"{next_saturday(clock.now().date()).isoformat()}T10:00"
    with pytest.raises(ToolError) as e:
        t.reschedule(_booking(t, "ac_01").booking_id, sat10, "r-1")
    assert e.value.code == ErrorCode.SLOT_CONFLICT


def test_reschedule_to_free_saturday_slot(world, tools, clock):
    ac = _booking(tools, "ac_01")
    sat = next_saturday(clock.now().date()).isoformat()
    res = tools.search_providers("ac_servicing", "HSR Layout", {"start": sat, "end": sat})
    free = next(p for p in res.providers if p.provider_id == "ac_01").available_slots
    target = next(s for s in free if s != f"{sat}T10:00")
    r = tools.reschedule(ac.booking_id, target, "r-1")
    assert (r.old_slot, r.new_slot) == (ac.slot, target)
    assert _booking(tools, "ac_01").slot == target
    again = tools.reschedule(ac.booking_id, target, "r-1")
    assert again.replayed and again.old_slot == ac.slot


def test_reschedule_into_occupied_slot_conflicts(world, tools, clock):
    ac = _booking(tools, "ac_01")
    sat = next_saturday(clock.now().date()).isoformat()
    res = tools.search_providers("ac_servicing", "HSR Layout", {"start": sat, "end": sat})
    target = next(s for s in next(p for p in res.providers if p.provider_id == "ac_01").available_slots
                  if s != f"{sat}T10:00")
    world.sim.take_slot("ac_01", target)
    with pytest.raises(ToolError) as e:
        tools.reschedule(ac.booking_id, target, "r-1")
    assert e.value.code == ErrorCode.SLOT_CONFLICT


def test_reschedule_conflict_configurable(world, tools, clock):
    ac = _booking(tools, "ac_01")
    d = (clock.now() + timedelta(days=1)).date().isoformat()
    world.sim.add_conflict_slot(f"{d}T12:00")  # any provider
    with pytest.raises(ToolError) as e:
        tools.reschedule(ac.booking_id, f"{d}T12:00", "r-1")
    assert e.value.code == ErrorCode.SLOT_CONFLICT


def test_forced_reschedule_conflict(world, tools, clock):
    ac = _booking(tools, "ac_01")
    world.sim.force("reschedule", "slot_conflict")
    with pytest.raises(ToolError) as e:
        tools.reschedule(ac.booking_id, f"{(clock.now() + timedelta(days=1)).date()}T11:00", "r-1")
    assert e.value.code == ErrorCode.SLOT_CONFLICT


# ----------------------------------------------------------------------------- provider cancels (sim hook)


def test_provider_cancellation_visible_only_via_list(world, tools):
    b = _booking(tools, "cl_04")
    world.sim.provider_cancels(b.booking_id, "Team unavailable")
    after = next(x for x in tools.list_bookings("u_demo").bookings if x.booking_id == b.booking_id)
    assert after.status == "cancelled_by_provider" and after.cancel_reason == "Team unavailable"
    # The freed slot becomes holdable again (unless baseline demand covers it).
    with pytest.raises(ToolError) as e:
        tools.cancel(b.booking_id, "c-1")
    assert e.value.code == ErrorCode.BOOKING_NOT_ACTIVE
