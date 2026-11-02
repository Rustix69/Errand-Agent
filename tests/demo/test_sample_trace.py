"""The committed trace deliverable (artifacts/traces/timeout-after-success/) is exactly what the code produces now."""

from pathlib import Path

from demo.sample_trace import DEFAULT_OUT, RUN_FILES, record
from errand.trace import read_trace

COMMITTED = Path(__file__).resolve().parents[2] / DEFAULT_OUT


def test_the_committed_trace_is_byte_identical_to_a_fresh_recording(tmp_path):
    fresh = record(tmp_path)
    assert [p.name for p in fresh] == list(RUN_FILES)
    for p in fresh:
        committed = COMMITTED / p.name
        assert committed.exists(), f"missing {committed}: run `python -m demo.sample_trace`"
        assert committed.read_bytes() == p.read_bytes(), f"{committed} is stale: run `python -m demo.sample_trace`"


def test_the_trace_shows_a_timeout_after_success_verified_through_the_source_of_truth():
    first, second = (read_trace(COMMITTED / n) for n in RUN_FILES)
    # run 1: the model's turns carry tokens and an ESTIMATED cost, labelled synthetic because the model is scripted
    calls = [r for r in first if r["event"] == "model_call"]
    assert len(calls) == 2 and all(r["data"]["usage_source"] == "synthetic" and r["cost_estimated"] for r in calls)
    # run 2: approved by code (no model call), book times out AFTER committing, status check proves it, then confirmed
    assert not [r for r in second if r["event"] == "model_call"]
    ev = [(r["event"], r["data"].get("tool"), r["data"].get("code")) for r in second]
    i_book = ev.index(("tool_call", "book", None))
    assert ev[i_book + 1] == ("tool_error", "book", "timeout")
    assert second[i_book + 1]["latency_s"] == 3.0
    assert ev[i_book + 2] == ("tool_call", "get_booking_status", None)
    result = second[i_book + 3]
    assert result["event"] == "tool_result" and result["data"]["summary"]["status"] == "confirmed"
    names = [e[0] for e in ev]
    assert names.index("attempt_confirmed") > i_book + 3 and names.index("verified") > names.index("attempt_confirmed")
    assert second[-1]["event"] == "node_exit" and [r["data"]["status"] for r in second if r["event"] == "final"] == ["DONE"]
    assert sum(1 for r in first + second if r["event"] == "tool_call" and r["data"]["tool"] == "book") == 1
