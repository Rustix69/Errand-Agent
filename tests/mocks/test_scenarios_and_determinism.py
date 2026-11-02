from collections import Counter

import pytest

from errand.clock import FakeClock
from errand.mocks import MockWorld, ScenarioConfig, SimulatedCrash, ToolError, ToolTimeout
from errand.mocks.scenario import BOOK_OUTCOMES, pick_weighted, roll
from tests.conftest import WEEK


def _all_slots(tools, service="plumber", area="Indiranagar"):
    res = tools.search_providers(service, area, WEEK)
    return [(p.provider_id, s) for p in res.providers for s in p.available_slots]


def _book_outcomes(world, n):
    """Run n hold+book cycles and record each outcome code."""
    t = world.tools()
    outcomes = []
    for i, (pid, slot) in enumerate(_all_slots(t)[:n]):
        h = t.hold_slot(pid, slot)
        try:
            t.book(h.hold_id, f"k-{i}")
            outcomes.append("success")
        except ToolTimeout:
            outcomes.append("timeout:" + t.get_booking_status(idempotency_key=f"k-{i}").status)
        except ToolError as e:
            outcomes.append(e.code)
    return outcomes


# ----------------------------------------------------------------------------- seeded randomness


def test_book_outcome_distribution_matches_contract():
    weights = ScenarioConfig().book_weights
    counts = Counter(pick_weighted(weights, roll(42, "book", n)) for n in range(1, 20001))
    share = {k: counts[k] / 20000 for k in BOOK_OUTCOMES}
    assert share["success"] == pytest.approx(0.70, abs=0.015)
    assert share["slot_taken"] == pytest.approx(0.15, abs=0.015)
    timeouts = share["timeout_after_commit"] + share["timeout_before_commit"]
    assert timeouts == pytest.approx(0.15, abs=0.015)
    assert share["timeout_after_commit"] / timeouts == pytest.approx(0.5, abs=0.05)


def test_same_seed_same_world(make_world):
    a = make_world(ScenarioConfig(seed=123), name="a.db")
    b = make_world(ScenarioConfig(seed=123), name="b.db")
    assert a.tools().search_providers("plumber", "Jayanagar", WEEK) == b.tools().search_providers(
        "plumber", "Jayanagar", WEEK
    )
    assert _book_outcomes(a, 25) == _book_outcomes(b, 25)


def test_different_seed_different_outcomes(make_world):
    a = make_world(ScenarioConfig(seed=1), name="a.db")
    b = make_world(ScenarioConfig(seed=2), name="b.db")
    assert _book_outcomes(a, 30) != _book_outcomes(b, 30)


def test_seeded_mode_produces_every_book_outcome(make_world):
    outcomes = Counter(_book_outcomes(make_world(ScenarioConfig(seed=42)), 60))
    assert {"success", "slot_taken", "timeout:confirmed", "timeout:not_booked"} <= set(outcomes)


def test_seeded_sequence_continues_across_restart(make_world, tmp_path):
    """The RNG position lives in world.db, so a restart does not replay or skip outcomes."""
    ref = _book_outcomes(make_world(ScenarioConfig(seed=9), name="ref.db"), 20)
    first = make_world(ScenarioConfig(seed=9), name="restart.db")
    t = first.tools()
    slots = _all_slots(t)[:20]
    got = []
    for i, (pid, slot) in enumerate(slots):
        if i == 10:  # "restart" halfway through
            first.close()
            first = make_world(name="restart.db")
            t = first.tools()
        h = t.hold_slot(pid, slot)
        try:
            t.book(h.hold_id, f"k-{i}")
            got.append("success")
        except ToolTimeout:
            got.append("timeout:" + t.get_booking_status(idempotency_key=f"k-{i}").status)
        except ToolError as e:
            got.append(e.code)
    assert got == ref


def test_forced_mode_has_no_random_failures(world):
    assert set(_book_outcomes(world, 30)) == {"success"}


# ----------------------------------------------------------------------------- forced outcomes


def test_forced_outcomes_queue_in_order_then_fall_back(world, tools):
    world.sim.force("book", "slot_taken")
    world.sim.force("book", "unavailable", times=2)
    assert world.sim.pending_forced("book") == [("book", "slot_taken"), ("book", "unavailable"), ("book", "unavailable")]
    assert _book_outcomes(world, 4) == ["slot_taken", "unavailable", "unavailable", "success"]
    assert world.sim.pending_forced() == []


def test_forced_outcome_not_consumed_by_invalid_call(world, tools):
    world.sim.force("book", "timeout_after_commit")
    with pytest.raises(ToolError):
        tools.book("hold_404", "k")
    assert world.sim.pending_forced("book") == [("book", "timeout_after_commit")]


def test_forced_outcomes_survive_restart(make_world):
    w = make_world(ScenarioConfig(mode="forced"))
    w.sim.force("get_provider", "unavailable")
    w.close()
    w2 = make_world()
    assert w2.config.mode == "forced"  # config persisted too
    with pytest.raises(ToolError) as e:
        w2.tools().get_provider("pl_01")
    assert e.value.code == "unavailable"


def test_read_only_tool_timeouts(world, tools, clock):
    world.sim.force("search_providers", "timeout")
    t0 = clock.monotonic()
    with pytest.raises(ToolTimeout):
        tools.search_providers("plumber", "Indiranagar", WEEK)
    assert clock.monotonic() - t0 == pytest.approx(world.config.timeout_latency_s)
    assert tools.search_providers("plumber", "Indiranagar", WEEK).providers


@pytest.mark.parametrize("tool,kind", [("book", "explode"), ("search_providers", "slot_taken"), ("nope", "success")])
def test_invalid_force_rejected(world, tool, kind):
    with pytest.raises(ValueError):
        world.sim.force(tool, kind)


def test_crash_after_commit_then_restart(make_world):
    w = make_world(ScenarioConfig(mode="forced"))
    t = w.tools()
    pid, slot = _all_slots(t)[0]
    h = t.hold_slot(pid, slot)
    w.sim.force("book", "crash_after_commit")
    with pytest.raises(SimulatedCrash):
        t.book(h.hold_id, "k-crash")
    w.close()
    w2 = make_world()
    st = w2.tools().get_booking_status(idempotency_key="k-crash")
    assert st.status == "confirmed" and st.booking.slot == slot


def test_simulated_crash_is_not_an_exception():
    assert not issubclass(SimulatedCrash, Exception)


def test_tool_latency_uses_clock(make_world, clock):
    w = make_world(ScenarioConfig(mode="forced", tool_latency_s={"search_providers": 1.5}))
    t0 = clock.monotonic()
    w.tools().search_providers("plumber", "Indiranagar", WEEK)
    assert clock.monotonic() - t0 == pytest.approx(1.5)


def test_call_log_records_every_call_for_assertions(world, tools):
    tools.get_provider("pl_01")
    with pytest.raises(ToolError):
        tools.get_provider("pl_99")
    calls = world.sim.calls("get_provider")
    assert [(c["args"]["provider_id"], c["outcome"], c["error_code"]) for c in calls] == [
        ("pl_01", "ok", None),
        ("pl_99", "error", "provider_not_found"),
    ]


def test_world_seeded_relative_to_clock(tmp_path):
    from datetime import datetime

    from errand.clock import IST

    clk = FakeClock(datetime(2027, 3, 10, 8, 0, tzinfo=IST))
    with MockWorld(tmp_path / "w.db", clk) as w:
        upcoming = [b for b in w.tools().list_bookings("u_demo").bookings if b.is_upcoming]
        assert {b.service for b in upcoming} == {"ac_servicing", "home_cleaning"}
