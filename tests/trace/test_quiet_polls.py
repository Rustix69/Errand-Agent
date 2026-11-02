"""A poll that handles nothing leaves no trace (it runs every few seconds); a poll that finds something is traced in
full, in order. Asserts events and files, never timing."""

import pytest

from errand.agent import PollHooks, Poller
from errand.trace import read_trace
from tests.core.helpers import ProcessKilled


def saved_preferences(a):
    a.memory.add("u_demo", "home_area", "HSR Layout", tags=("profile", "area"))
    a.memory.add("u_demo", "earliest_time", "10:00", tags=("profile", "time"))


def provider_cancels(a, provider="cl_04"):
    bid = next(b["booking_id"] for b in a.env.user_bookings() if b["provider_id"] == provider)
    a.env.world.sim.provider_cancels(bid, "Technician unavailable")


def poll_files(tracer):
    return sorted(tracer.dir.glob("poll_*.jsonl"))


def names(path):
    return [(r["event"], r["data"].get("tool")) for r in read_trace(path)]


def test_polls_that_find_nothing_leave_no_trace(build):
    a, tracer = build([])
    calls_before = len(a.env.calls("list_bookings"))
    for _ in range(5):
        a.poll()
    assert len(a.env.calls("list_bookings")) == calls_before + 5  # the poller really ran every time
    assert poll_files(tracer) == []  # ... and left no trace file
    all_rows = [r for p in tracer.dir.glob("*.jsonl") for r in read_trace(p)]
    assert not [r for r in all_rows if r["event"].startswith("poll_")]


def test_a_poll_that_detects_a_cancellation_is_traced_in_full_and_in_order(build):
    a, tracer = build([])
    saved_preferences(a)
    a.poll()  # quiet
    provider_cancels(a)
    a.poll()
    (path,) = poll_files(tracer)
    seen = names(path)
    assert seen[0] == ("poll_start", None) and seen[-1] == ("poll_end", None)
    # the events held while the poll was still quiet come out FIRST, then the detection and what it led to
    order = [seen.index(x) for x in (("tool_call", "list_bookings"), ("tool_result", "list_bookings"), ("poll_detected", None),
                                     ("poll_proposal", None), ("poll_decision", None))]
    assert order == sorted(order)
    rows = read_trace(path)
    assert [r["seq"] for r in rows] == list(range(len(rows)))
    result = next(r for r in rows if r["event"] == "tool_result")
    assert result["data"]["summary"]["n"] == 6  # the summary of THIS poll's call is attached
    a.poll()  # nothing new: no second file
    assert len(poll_files(tracer)) == 1


def test_a_poll_that_fails_is_traced(build):
    a, tracer = build([])
    a.env.world.sim.force("list_bookings", "unavailable", times=3)
    a.poll()
    (path,) = poll_files(tracer)
    events = [e for e, _ in names(path)]
    assert "poll_start" in events and "tool_error" in events and "poll_error" in events


def test_a_poll_killed_mid_way_keeps_what_it_had_traced(build):
    a, tracer = build([])
    saved_preferences(a)
    a.poll()
    provider_cancels(a)

    def kill(event):
        raise ProcessKilled()

    a.poller = Poller(a.rt, PollHooks(after_detected=kill))
    with pytest.raises(ProcessKilled):
        a.poll()
    (path,) = poll_files(tracer)
    events = [e for e, _ in names(path)]
    assert events[:1] == ["poll_start"] and "poll_detected" in events and "poll_end" not in events


def test_a_poll_that_only_tidies_a_replaced_event_is_traced(build):
    a, tracer = build([])
    saved_preferences(a)
    a.poll()
    provider_cancels(a)
    a.poll()
    assert a.say("yes").kind == "confirmed"  # the replacement is booked through the normal path
    a.poll()  # nothing new is detected, but the stored event moves to 'replaced'
    first, second = poll_files(tracer)
    end = [r for r in read_trace(second) if r["event"] == "poll_end"][0]
    assert end["data"]["handled"] == 0 and end["data"]["replaced"] == 1
    assert a.env.store.one("SELECT status FROM proactive_events")["status"] == "replaced"
