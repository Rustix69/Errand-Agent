"""`show` pretty-prints one run; `save` is a plain copy of that run's JSONL. Asserts structure and numbers."""

from errand.trace import read_trace, trace_path
from errand.trace.cli import main, render
from tests.trace.helpers import booking_script, play_booking


def _run(build, **kw):
    a, tracer = build(booking_script())
    replies = play_booking(a, **kw)
    return tracer, replies


def test_show_prints_one_line_per_event_and_correct_totals(build, capsys):
    tracer, replies = _run(build)
    rid = replies[1].run_id  # the search run: 2 model calls, 1 tool call
    rows = read_trace(trace_path(tracer.dir, rid))

    assert main(["show", rid, "--dir", str(tracer.dir)]) == 0
    out = capsys.readouterr().out.splitlines()
    event_lines = [ln for ln in out[2:-2]]
    assert len(event_lines) == len(rows)
    assert [ln.split()[1] for ln in event_lines] == [r["event"] for r in rows]

    mcs = [r for r in rows if r["event"] == "model_call"]
    tin, tout = sum(r["data"]["input_tokens"] for r in mcs), sum(r["data"]["output_tokens"] for r in mcs)
    cost = sum(r["cost_usd"] for r in mcs)
    assert out[-1] == (f"totals: model_calls=2 tokens_in={tin} tokens_out={tout} cost=${cost:.6f} tool_calls=1 "
                       f"wall={rows[-1]['t']:.3f}s")


def test_show_gives_the_same_output_from_a_run_id_or_a_file_path(build, capsys):
    tracer, replies = _run(build)
    path = trace_path(tracer.dir, replies[3].run_id)
    assert main(["show", replies[3].run_id, "--dir", str(tracer.dir)]) == 0
    by_id = capsys.readouterr().out
    assert main(["show", str(path)]) == 0
    assert capsys.readouterr().out == by_id == render(read_trace(path)) + "\n"


def test_save_is_a_plain_copy_of_only_that_run(build, tmp_path, capsys):
    tracer, replies = _run(build)
    rid = replies[3].run_id
    dest = tmp_path / "artifacts" / "traces"
    assert main(["save", rid, "--dir", str(tracer.dir), "--out", str(dest)]) == 0
    saved = dest / f"{rid}.jsonl"
    assert [p.name for p in dest.iterdir()] == [saved.name]  # nothing else came along
    assert saved.read_bytes() == trace_path(tracer.dir, rid).read_bytes()

    assert main(["save", rid, "--dir", str(tracer.dir), "--out", str(saved)]) == 2  # no silent overwrite
    assert "--force" in capsys.readouterr().err
    assert main(["save", rid, "--dir", str(tracer.dir), "--out", str(saved), "--force"]) == 0


def test_unknown_run_is_a_clear_error_and_writes_nothing(build, tmp_path, capsys):
    tracer, _ = _run(build)
    out = tmp_path / "x.jsonl"
    for cmd in (["show", "run_999999"], ["save", "run_999999", "--out", str(out)]):
        assert main([*cmd, "--dir", str(tracer.dir)]) == 2
        assert "no trace for 'run_999999'" in capsys.readouterr().err
    assert not out.exists()
