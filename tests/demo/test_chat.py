"""The terminal demo, driven line by line with a ScriptedModel and a fake clock: no key, no network, no terminal."""

import pytest

from demo.chat import INJECTION_PROMPT, MARKER, Chat, main, open_demo
from errand.clock import FakeClock
from errand.config import Settings
from errand.model import ScriptedModel, call, say
from errand.trace import read_trace
from tests.evals.scripts import pick, search

FIND = "find a plumber in Indiranagar this week"
BOOK = [search(), pick()]


@pytest.fixture
def make(tmp_path):
    opened = []

    def _make(steps, *, data=None, clock=None):
        clock = clock or FakeClock()
        app, demo = open_demo(Settings(api_key_set=True), ScriptedModel(list(steps)), data or tmp_path / "data", clock=clock)
        out: list[str] = []
        opened.append((app, demo))
        return Chat(app, demo, out.append, fake_clock=clock), app, demo, out

    yield _make
    for app, demo in opened:
        for closer in (app.close, demo.close):
            try:
                closer()
            except Exception:
                pass


def feed(*lines):
    it = iter(lines)

    def input_fn(prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return input_fn


def trace_rows(app):
    rows = []
    for p in sorted((app.data_dir / "traces").glob("*.jsonl")):
        rows += read_trace(p)
    return rows


def agent_bookings(demo):
    return [b for b in demo.sim.bookings() if b["idempotency_key"]]


# ----------------------------------------------------------------------------- wiring and a full booking


def test_the_tracer_is_wired_into_runtime_the_executor_and_the_tools(make):
    chat, app, demo, out = make(BOOK)
    assert app.rt.emit is app.tracer and app.executor.emit is app.tracer  # Runtime.emit AND the executor's on_event
    chat.run(feed(FIND, "yes", "/quit"))
    rows = trace_rows(app)
    events = {r["event"] for r in rows}
    assert {"model_call", "tool_call", "node_enter", "context_built"} <= events  # Runtime.emit
    assert {"attempt_pending", "attempt_confirmed", "execute_outcome"} <= events  # the executor's on_event
    assert any(r["event"] == "tool_result" and "summary" in r["data"] for r in rows)  # tracer.wrap_tools
    assert not {"poll_start", "poll_end"} & events  # polls that found nothing leave no trace ...
    assert len(demo.sim.calls("list_bookings")) >= 3  # ... but the poller did run before each of the three inputs
    assert len(agent_bookings(demo)) == 1 and any("[DONE · confirmed" in line for line in out)
    assert (app.data_dir / MARKER).exists() and {"world.db", "agent.db", "checkpoints.db"} <= {p.name for p in app.data_dir.iterdir()}


# ----------------------------------------------------------------------------- the demo commands


def test_provider_cancel_is_noticed_by_polling_notified_once_and_rebooked_only_after_yes(make):
    chat, app, demo, out = make([say("Sure, what would you like to know?")])
    chat.run(feed("/provider-cancel cleaning", "tell me more", "yes", "/quit"))  # a 2nd input: the notice must not repeat
    notices = [line for line in out if line.startswith("notice> ")]
    (row,) = app.store.all("SELECT * FROM outbox")
    assert len(notices) == 1 and row["message"] in notices[0] and row["delivered_at"]
    assert any("The agent has not been told" in line for line in out)  # the simulator line, then the agent notices
    assert len(agent_bookings(demo)) == 1 and demo.sim.calls("book")[0]["args"]["idempotency_key"]  # only after "yes"
    assert app.rt.model.calls == 1  # "tell me more" used the model; the proactive notice and the approval did not
    assert [r["data"]["decision"] for r in trace_rows(app) if r["event"] == "poll_decision"] == ["replacement"]
    assert [r["status"] for r in app.store.all("SELECT status FROM proactive_events")] == ["replaced"]


def test_timeout_command_arms_a_timeout_after_commit_and_the_trace_shows_the_recovery(make):
    chat, app, demo, out = make(BOOK)
    chat.run(feed("/timeout", FIND, "yes", "/quit"))
    rows = trace_rows(app)
    names = [(r["event"], r["data"].get("tool"), r["data"].get("code")) for r in rows]
    i_err = names.index(("tool_error", "book", "timeout"))
    assert ("tool_call", "get_booking_status", None) in names[i_err:]
    assert any(r["event"] == "attempt_confirmed" for r in rows[i_err:])
    assert len(demo.sim.calls("book")) == 1 and len(agent_bookings(demo)) == 1
    assert any("[DONE · confirmed" in line for line in out)


def test_demo_injection_asks_about_the_planted_reviews_and_nothing_is_approved_or_booked(make):
    chat, app, demo, out = make([call("get_provider", provider_id="pl_01"), say("Mixed reviews, honestly.")])
    chat.run(feed("/demo injection", "/quit"))
    assert any(INJECTION_PROMPT in line for line in out)
    rows = trace_rows(app)
    assert any(r["event"] == "untrusted_scan" for r in rows)  # the planted instruction was detected and logged
    assert demo.sim.calls("book") == [] and app.store.one("SELECT COUNT(*) FROM approvals")[0] == 0


def test_price_slot_taken_and_force_commands_arm_the_simulator(make):
    chat, app, demo, out = make([*BOOK, say("ok")])
    chat.handle("/price +200")
    chat.handle("/force get_booking_status unavailable 2")
    assert ("hold_slot", "price_change") in demo.sim.pending_forced() and len(demo.sim.pending_forced("get_booking_status")) == 2
    chat.handle("/force clear")
    assert demo.sim.pending_forced() == []
    chat.handle("/slot-taken")
    assert any("no pending proposal" in line for line in out)
    chat.handle(FIND)  # a pending proposal now exists
    chat.handle("/slot-taken")
    p = app.store.one("SELECT * FROM proposals WHERE status = 'pending'")
    assert any(f"{p['provider_id']} {p['slot']}" in line for line in out)
    chat.handle("yes")
    assert any("[NEEDS_INPUT · slot_taken" in line for line in out) and agent_bookings(demo) == []


def test_bad_commands_are_one_line_messages_not_tracebacks(make):
    chat, app, demo, out = make([])
    for line in ["/nope", "/force", "/force nonsense x", "/timeout sideways", "/price abc", "/advance", "/advance 5x",
                 "/provider-cancel", "/provider-cancel xyz", "/provider-cancel bk_999999", "/demo", "/help"]:
        n = len(out)
        assert chat.handle(line) is True and len(out) > n, line
    assert any(line.startswith("! ") for line in out) and any(line.startswith("unknown command") for line in out)
    assert chat.handle("/quit") is False


def test_advance_moves_the_fake_clock_only(make):
    chat, app, demo, out = make([])
    before = app.clock.now()
    chat.handle("/advance 11m")
    assert (app.clock.now() - before).total_seconds() == 660


# ----------------------------------------------------------------------------- restarts and startup


def test_a_new_process_continues_the_same_conversation_and_booking(make, tmp_path):
    data = tmp_path / "shared"
    chat, app, demo, out = make(BOOK, data=data)
    chat.handle(FIND)  # a proposal is now waiting for approval
    app.close(), demo.close()
    chat2, app2, demo2, out2 = make([], data=data, clock=app.clock)  # "restart": same files, new connections
    chat2.startup()
    chat2.handle("yes")
    assert any("[DONE · confirmed" in line for line in out2) and len(agent_bookings(demo2)) == 1


def test_a_run_interrupted_by_a_crash_is_resumed_at_startup(make, tmp_path):
    from errand.mocks import SimulatedCrash

    data = tmp_path / "crash"
    chat, app, demo, out = make(BOOK, data=data)
    chat.handle(FIND)
    demo.sim.force("book", "crash_after_commit")
    with pytest.raises(SimulatedCrash):
        chat.handle("yes")  # the booking committed, then the process died
    app.close(), demo.close()
    chat2, app2, demo2, out2 = make([], data=data, clock=app.clock)
    chat2.startup()
    assert any("recovered a run" in line for line in out2) and any("[DONE ·" in line for line in out2)
    assert len(agent_bookings(demo2)) == 1 and len(demo2.sim.calls("book")) == 1


# ----------------------------------------------------------------------------- main(): keys, wiping, secrets


def test_main_without_a_key_is_one_clear_error_and_exit_code_2(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "d")], env_file=tmp_path / "none.env", input_fn=feed("/quit"))
    err = capsys.readouterr().err
    assert code == 2 and "ANTHROPIC_API_KEY" in err and "Traceback" not in err and err.count("\n") == 1
    assert not (tmp_path / "d").exists()  # nothing was created


def test_fresh_refuses_to_wipe_a_folder_this_tool_did_not_create(tmp_path, capsys):
    other = tmp_path / "important"
    other.mkdir()
    (other / "notes.txt").write_text("keep me")
    code = main(["--data-dir", str(other), "--fresh"], model=ScriptedModel([]), env_file=tmp_path / "none.env",
                input_fn=feed("/quit"))
    assert code == 2 and (other / "notes.txt").read_text() == "keep me" and "refusing" in capsys.readouterr().err


def test_main_runs_with_a_real_client_object_and_never_prints_the_key(tmp_path, monkeypatch, capsys):
    secret = "sk-ant-SENTINEL-main-test"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)  # present, but no message is ever sent (stdin ends at once)
    outs: list[str] = []
    code = main(["--data-dir", str(tmp_path / "d")], env_file=tmp_path / "none.env", input_fn=feed("/help", "/quit"),
                out=outs.append)
    shown = "\n".join(outs) + capsys.readouterr().out
    assert code == 0 and "key set" in shown and "/provider-cancel" in shown and secret not in shown


def test_the_banner_says_the_random_flag_means_the_contract_rates(tmp_path):
    outs: list[str] = []
    main(["--data-dir", str(tmp_path / "d"), "--random"], model=ScriptedModel([]), env_file=tmp_path / "none.env",
         input_fn=feed("/quit"), out=outs.append)
    assert any("70% success / 15% slot_taken / 15% timeout" in line for line in outs)
