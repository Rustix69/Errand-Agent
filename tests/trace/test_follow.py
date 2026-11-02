"""`follow`: the live trace view for a second pane. Asserts on the lines it returns, not on timing."""

import json

from errand.clock import FakeClock
from errand.trace import TraceLogger
from errand.trace.cli import Follower, follow


def emit_run(tracer, run, *extra):
    tracer("run_start", {"run_id": run, "session_id": "s1", "chars": 3})
    for name, data in extra:
        tracer(name, {"run_id": run, **data})


def test_follow_shows_only_new_lines_and_starts_a_section_for_each_new_run(tmp_path):
    clock, d = FakeClock(), tmp_path / "traces"
    t = TraceLogger(clock, d)
    emit_run(t, "run_000001", ("node_enter", {"node": "understand"}))
    f = Follower(d, cols=100)
    first = f.poll()
    assert first[0].startswith("── run_000001") and [ln.split()[1] for ln in first[1:]] == ["run_start", "node_enter"]
    assert f.poll() == []  # nothing new
    t("node_exit", {"run_id": "run_000001", "node": "understand"})
    assert [ln.split()[1] for ln in f.poll()] == ["node_exit"]  # no repeated header for the same run
    clock.advance(1)
    emit_run(t, "run_000002", ("model_call", {"turn": 1, "model": "scripted", "input_tokens": 10, "output_tokens": 2,
                                              "tool_calls": [], "usage_source": "api"}))
    out = f.poll()
    assert out[0].startswith("── run_000002") and [ln.split()[1] for ln in out[1:]] == ["run_start", "model_call"]
    assert "~$" in out[-1] and " api " in out[-1]  # the estimate label and the real-usage marker


def test_follow_waits_for_a_half_written_line_and_clips_to_the_pane_width(tmp_path):
    d = tmp_path / "traces"
    d.mkdir()
    path = d / "run_000001.jsonl"
    row = {"run": "run_000001", "seq": 0, "ts": "x", "t": 0.0, "event": "tool_call",
           "data": {"tool": "search_providers", "args": {"service": "plumber", "area": "Indiranagar" * 5}}}
    path.write_text(json.dumps(row)[:40])  # half a line
    f = Follower(d, cols=60, replay="all")
    assert f.poll() == []
    path.write_text(json.dumps(row) + "\n")
    lines = f.poll()
    assert len(lines) == 2 and all(len(ln) <= 62 for ln in lines[1:])  # fits a 60-column pane (header line excluded)


def test_replay_modes_and_colour(tmp_path):
    clock, d = FakeClock(), tmp_path / "traces"
    t = TraceLogger(clock, d)
    emit_run(t, "run_000001")
    clock.advance(1)
    emit_run(t, "run_000002", ("tool_error", {"tool": "book", "code": "timeout", "retryable": True}))
    assert Follower(d, replay="none").poll() == []  # only what happens from now on
    latest = Follower(d, replay="latest").poll()
    assert latest[0].startswith("── run_000002") and not any("run_000001" in ln for ln in latest)
    everything = Follower(d, replay="all").poll()
    assert [ln for ln in everything if ln.startswith("──")][0].startswith("── run_000001")
    coloured = Follower(d, replay="all", color=True).poll()
    assert any("\033[31m" in ln and "tool_error" in ln for ln in coloured)  # errors are red


def test_the_follow_loop_prints_what_the_follower_returns(tmp_path):
    t = TraceLogger(FakeClock(), tmp_path / "traces")
    emit_run(t, "run_000001")
    seen: list[str] = []
    follow(tmp_path / "traces", seen.append, poll_s=0, stop=lambda: len(seen) >= 2)
    assert seen[0].startswith("── run_000001") and seen[1].split()[1] == "run_start"
