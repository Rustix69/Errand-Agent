"""Trace logger: one JSONL file per run, deterministic, sanitized, never able to break the agent.
Asserts file contents and events, not the wording of anything."""

import json

import pytest

from errand.clock import FakeClock
from errand.model import call, say
from errand.trace import TraceLogger, read_trace, trace_path
from errand.trace.logger import sanitize
from tests.trace.helpers import USER_TEXT, booking_script, play_booking


def rows_of(tracer, run_id):
    return read_trace(trace_path(tracer.dir, run_id))


# ----------------------------------------------------------------------------- one trace per run, nothing lost


def test_one_file_per_run_and_every_event_lands_in_it(build):
    a, tracer = build(booking_script())
    replies = play_booking(a)
    run_ids = [r.run_id for r in replies]
    assert len(set(run_ids)) == 4
    files = sorted(p.stem for p in tracer.dir.glob("*.jsonl"))
    assert files == sorted(run_ids + ["_session"])  # startup recovery lives in the session trace

    total = 0
    for rid in run_ids:
        rows = rows_of(tracer, rid)
        total += len(rows)
        assert rows[0]["event"] == "run_start"
        assert [r["event"] for r in rows].count("final") == 1
        assert [r["seq"] for r in rows] == list(range(len(rows)))
        assert {r["run"] for r in rows} == {rid}
    total += len(rows_of(tracer, None))
    assert total == len(a.events) and tracer.write_errors == 0  # every emitted event is in some file


def test_ts_and_offsets_come_from_the_clock(build, clock):
    a, tracer = build([say("hi")])
    clock.advance(5)
    r = a.say("hello")
    rows = rows_of(tracer, r.run_id)
    assert rows[0]["ts"] == clock.now().isoformat(timespec="milliseconds") == "2026-11-02T09:00:05.000+05:30"
    assert all(row["t"] == 0 for row in rows)  # the clock did not move during the run


def test_same_run_twice_gives_byte_identical_traces(build):
    texts = []
    for name in ("a", "b"):
        a, tracer = build(booking_script(), name=name, clk=FakeClock())
        play_booking(a, before_yes=lambda a: a.env.world.sim.force("book", "timeout_after_commit"))
        texts.append({p.name: p.read_text() for p in sorted(tracer.dir.glob("*.jsonl"))})
    assert texts[0] == texts[1] and len(texts[0]) == 5


# ----------------------------------------------------------------------------- transitions, model calls, tools


def test_state_transitions_follow_the_graph_path(build):
    a, tracer = build(booking_script())
    replies = play_booking(a)

    def path(rid):
        return [r["data"]["node"] for r in rows_of(tracer, rid) if r["event"] == "node_exit"]

    assert path(replies[2].run_id) == ["ingest", "approval_gate", "memory_op", "capture", "understand", "hold", "respond"]
    assert path(replies[3].run_id) == ["ingest", "approval_gate", "execute", "verify", "respond"]
    done = rows_of(tracer, replies[3].run_id)
    assert [r["data"]["node"] for r in done if r["event"] == "node_enter"] == ["approval_gate", "execute", "verify", "respond"]
    assert [r["event"] for r in rows_of(tracer, replies[1].run_id) if r["event"] == "model_call"] == ["model_call"] * 2


def test_model_calls_carry_usage_cost_and_clock_latency(build, clock):
    def slow(request):
        clock.advance(2.5)  # the "model" takes 2.5 s on the fake clock
        return say("done")

    a, tracer = build([slow])
    r = a.say("hello")
    (mc,) = [x for x in rows_of(tracer, r.run_id) if x["event"] == "model_call"]
    usage = a.model.requests[0].approx_input_tokens()
    assert mc["data"]["input_tokens"] == usage and mc["data"]["model"] == "scripted"
    assert mc["latency_s"] == pytest.approx(2.5)
    price_in, price_out = tracer.prices["scripted"]
    assert mc["cost_usd"] == round((usage * price_in + mc["data"]["output_tokens"] * price_out) / 1e6, 6)


def test_unknown_model_has_null_cost(build, clock):
    a, tracer = build([say("hi")])
    tracer.prices = {}
    r = a.say("hello")
    (mc,) = [x for x in rows_of(tracer, r.run_id) if x["event"] == "model_call"]
    assert mc["cost_usd"] is None


def test_tool_names_args_and_latency_for_a_timeout_after_success(build):
    a, tracer = build(booking_script())
    replies = play_booking(a, before_yes=lambda a: a.env.world.sim.force("book", "timeout_after_commit"))
    assert replies[3].kind == "confirmed" and a.env.count("book") == 1

    search = [r for r in rows_of(tracer, replies[1].run_id) if r["event"] == "tool_call"]
    assert search[0]["data"] == {"tool": "search_providers", "args": {
        "service": "plumber", "area": "Indiranagar", "date_range": {"start": "2026-11-02", "end": "2026-11-08"}}}

    rows = rows_of(tracer, replies[3].run_id)
    ev = [r["event"] for r in rows]
    i_book = next(i for i, r in enumerate(rows) if r["event"] == "tool_call" and r["data"]["tool"] == "book")
    assert rows[i_book]["data"]["args"]["idempotency_key"].startswith("book-")
    err = rows[i_book + 1]
    assert (err["event"], err["data"]["tool"], err["data"]["code"]) == ("tool_error", "book", "timeout")
    assert err["latency_s"] == pytest.approx(3.0)  # the mock's simulated timeout, on the fake clock
    truth = rows[i_book + 2], rows[i_book + 3]
    assert [(r["event"], r["data"]["tool"]) for r in truth] == [("tool_call", "get_booking_status"), ("tool_result", "get_booking_status")]
    # the order that makes the claim honest: timeout -> source of truth -> confirmed -> verified -> final
    order = [ev.index(e) for e in ("attempt_confirmed", "verified", "final")]
    assert i_book < order[0] < order[1] < order[2]
    (verified,) = [r for r in rows if r["event"] == "verified"]
    assert verified["data"]["kind"] == "confirmed" and verified["data"]["booking_id"]
    assert ev.count("tool_call") == 3  # status (live check), book, status (truth): no second book


def test_tool_result_rows_say_what_came_back_in_a_timeout_after_success(build):
    a, tracer = build(booking_script())
    replies = play_booking(a, before_yes=lambda a: a.env.world.sim.force("book", "timeout_after_commit"))
    rows = rows_of(tracer, replies[3].run_id)
    (verified,) = [r for r in rows if r["event"] == "verified"]
    results = [r for r in rows if r["event"] == "tool_result"]
    live, truth = results  # the pre-check (nothing booked yet), then the answer after the timeout
    assert live["data"]["summary"]["status"] == "not_booked" and "booking_id" not in live["data"]["summary"]
    assert truth["data"]["tool"] == "get_booking_status"
    assert truth["data"]["summary"] == {"status": "confirmed", "booking_id": verified["data"]["booking_id"],
                                        "hold_status": "booked"}
    assert not [r for r in rows if r["event"] == "tool_error" and "summary" in r["data"]]  # the timeout has a code, no summary
    for r in results:  # short and scalar: ids, status, counts, never free text
        assert all(isinstance(v, (bool, int, float)) or len(v) <= 40 for v in r["data"]["summary"].values())


def test_resumed_run_shows_execute_entered_twice(build):
    from errand.mocks import SimulatedCrash

    a, tracer = build(booking_script())
    play_booking(a, texts=USER_TEXT[:3])
    a.env.world.sim.force("book", "crash_after_commit")
    with pytest.raises(SimulatedCrash):
        a.say("yes")
    r = a.agent.resume()
    assert r.kind == "confirmed" and a.env.count("book") == 1
    rows = rows_of(tracer, r.run_id)
    nodes = [(x["event"], x["data"]["node"]) for x in rows if x["event"].startswith("node_") and x["data"]["node"] == "execute"]
    assert nodes == [("node_enter", "execute"), ("node_enter", "execute"), ("node_exit", "execute")]


# ----------------------------------------------------------------------------- what may and may not be in a trace


def test_untrusted_review_text_is_only_ever_inside_the_wrapper(build):
    a, tracer = build([call("get_provider", provider_id="pl_01"), say("ok")])
    r = a.say("tell me about pl_01")
    (out,) = [x for x in rows_of(tracer, r.run_id) if x["event"] == "tool_output"]
    content = out["data"]["content"]
    at = content.index("IMPORTANT SYSTEM NOTICE")
    assert content.rfind("<untrusted_data", 0, at) != -1
    assert content.rfind("<untrusted_data", 0, at) > content.rfind("</untrusted_data>", 0, at)
    assert any(x["event"] == "untrusted_scan" for x in rows_of(tracer, r.run_id))  # the detection is traced too
    (res,) = [x for x in rows_of(tracer, r.run_id) if x["event"] == "tool_result"]
    assert set(res["data"]["summary"]) == {"provider_id", "reviews"}  # counts and ids only, no review text


def test_raw_user_text_never_reaches_a_trace(build):
    marker = "zq-unique-9137"
    a, tracer = build(booking_script())
    play_booking(a, texts=[f"{marker} find me a plumber"])
    blob = "".join(p.read_text() for p in tracer.dir.glob("*.jsonl"))
    assert marker not in blob and '"chars"' in blob


def test_sanitize_caps_strings_redacts_secrets_and_bounds_size():
    out = sanitize({"a": "x" * 50, "api_key": "sk-123", "Authorization": "Bearer t", "nested": {"token": "t", "ok": 1},
                    "many": list(range(100))}, cap=10)
    assert out["a"] == "x" * 10 + "…[+40 chars]"
    assert out["api_key"] == out["Authorization"] == out["nested"]["token"] == "[redacted]"
    assert out["nested"]["ok"] == 1 and len(out["many"]) == 31 and out["many"][-1] == "…[+70 items]"


def test_long_values_are_capped_in_the_file(tmp_path, clock):
    t = TraceLogger(clock, tmp_path / "t", cap=50)
    t("tool_output", {"run_id": "run_9", "tool": "x", "content": "y" * 500})
    (row,) = read_trace(trace_path(t.dir, "run_9"))
    assert len(row["data"]["content"]) < 80 and "[+450 chars]" in row["data"]["content"]


# ----------------------------------------------------------------------------- tracing never changes behaviour


def test_a_broken_trace_destination_does_not_stop_the_agent(build):
    a, tracer = build(booking_script())
    import shutil

    shutil.rmtree(tracer.dir)
    tracer.dir.write_text("not a directory")  # every write will now fail
    replies = play_booking(a)
    assert [r.status for r in replies] == ["NEEDS_INPUT", "DONE", "NEEDS_INPUT", "DONE"]
    assert replies[3].kind == "confirmed" and a.env.count("book") == 1
    assert tracer.write_errors > 0


def test_model_call_cost_is_labelled_an_estimate_and_usage_records_what_the_api_reported(build):
    from errand.model import ModelTurn, Usage
    from errand.trace.cli import render

    api_turn = ModelTurn(text="ok", usage=Usage(100, 20, cache_read_tokens=60, cache_write_tokens=5, source="api"),
                         request_id="req_demo")
    a, tracer = build([api_turn, say("synthetic")])
    r1 = a.say("hello")
    r2 = a.say("again")
    (real,) = [x for x in rows_of(tracer, r1.run_id) if x["event"] == "model_call"]
    assert real["cost_usd"] == round((100 * 3.0 + 20 * 15.0) / 1e6, 6) and real["cost_estimated"] is True
    assert real["data"]["usage_source"] == "api" and real["data"]["request_id"] == "req_demo"
    assert (real["data"]["cache_read_tokens"], real["data"]["cache_write_tokens"]) == (60, 5)  # recorded, not priced
    (synth,) = [x for x in rows_of(tracer, r2.run_id) if x["event"] == "model_call"]
    assert synth["data"]["usage_source"] == "synthetic" and "cache_read_tokens" not in synth["data"]
    text = render(rows_of(tracer, r1.run_id))
    assert "~$" in text and " est " in text and " api " in text and "(estimate)" in text


def _poll(t, run, *, found=None, replaced=0, note=None):
    """The events a poll emits, in the order the real code emits them (the wrapper notes a summary between the
    tool_call and the tool_result)."""
    t("poll_start", {"run_id": run, "session_id": "s1"})
    t("tool_call", {"tool": "list_bookings", "args": {"user_id": "u_demo"}})
    if note:
        t.note_result("list_bookings", note)
    t("tool_result", {"tool": "list_bookings"})
    if found:
        t(found, {"run_id": run, "event_id": 1})
    t("poll_end", {"run_id": run, "handled": 1 if found == "poll_detected" else 0, "replaced": replaced})


def test_a_poll_that_found_nothing_writes_no_file_and_leaves_no_state_behind(tmp_path, clock):
    t = TraceLogger(clock, tmp_path / "t")
    _poll(t, "poll_000001", note={"n": 6})
    assert not (t.dir / "poll_000001.jsonl").exists() and t._held == {} and t._current is None
    # its summary was consumed by its own (dropped) tool_result, so it cannot leak into a later result
    t("tool_call", {"run_id": "run_5", "tool": "list_bookings", "args": {}})
    t("tool_result", {"run_id": "run_5", "tool": "list_bookings"})
    (row,) = [r for r in read_trace(trace_path(t.dir, "run_5")) if r["event"] == "tool_result"]
    assert "summary" not in row["data"]


def test_a_poll_that_found_something_is_written_whole_with_contiguous_seq_and_its_summary(tmp_path, clock):
    t = TraceLogger(clock, tmp_path / "t")
    _poll(t, "poll_000002", found="poll_detected", note={"n": 6})
    rows = read_trace(trace_path(t.dir, "poll_000002"))
    assert [r["event"] for r in rows] == ["poll_start", "tool_call", "tool_result", "poll_detected", "poll_end"]
    assert [r["seq"] for r in rows] == list(range(5)) and rows[2]["data"]["summary"] == {"n": 6}
    assert rows[1]["t"] == 0 and all(r["run"] == "poll_000002" for r in rows)


@pytest.mark.parametrize("flush_event", ["poll_resumed", "poll_error"])
def test_resumed_and_failed_polls_are_written_too(tmp_path, clock, flush_event):
    t = TraceLogger(clock, tmp_path / "t")
    _poll(t, "poll_000003", found=flush_event)
    assert [r["event"] for r in read_trace(trace_path(t.dir, "poll_000003"))][-2:] == [flush_event, "poll_end"]


def test_a_poll_that_only_tidied_a_replaced_event_is_written_and_a_killed_one_keeps_its_rows(tmp_path, clock):
    t = TraceLogger(clock, tmp_path / "t")
    _poll(t, "poll_000004", replaced=1)
    assert [r["event"] for r in read_trace(trace_path(t.dir, "poll_000004"))] == ["poll_start", "tool_call", "tool_result", "poll_end"]
    t("poll_start", {"run_id": "poll_000005", "session_id": "s1"})  # killed right after detecting: no poll_end ever
    t("poll_detected", {"run_id": "poll_000005", "event_id": 1})
    t("poll_proposal", {"event_id": 1})
    assert [r["event"] for r in read_trace(trace_path(t.dir, "poll_000005"))] == ["poll_start", "poll_detected", "poll_proposal"]


def test_user_runs_are_never_deferred(tmp_path, clock):
    t = TraceLogger(clock, tmp_path / "t")
    t("run_start", {"run_id": "run_7", "session_id": "s1", "chars": 3})
    assert [r["event"] for r in read_trace(trace_path(t.dir, "run_7"))] == ["run_start"]  # written at once

