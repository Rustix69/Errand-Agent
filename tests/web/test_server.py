"""The web UI's endpoints, driven with a ScriptedModel and a fake clock: no key, no network, no browser.

The rule under test: the page can never book. The only route that gives user text to the agent is the chat endpoint;
Confirm / Decline are just chat messages, and whether they approve is the approval guard's decision.
"""

import inspect
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from demo.chat import INJECTION_PROMPT, open_demo
from demo.web.server import WebSession, create_app, main, trace_class
from errand.agent import PollHooks, Poller
from errand.clock import FakeClock
from errand.config import Settings
from errand.model import ScriptedModel, call, say
from tests.evals.scripts import pick, search

ROOT = Path(__file__).resolve().parents[2]
SERVER = (ROOT / "demo/web/server.py").read_text()
PAGE = (ROOT / "demo/web/index.html").read_text()
THEME = (ROOT / "demo/web/theme.css").read_text()
FIND = "find a plumber in Indiranagar this week"


@pytest.fixture
def web(tmp_path):
    opened = []

    def make(steps=()):
        app, demo = open_demo(Settings(api_key_set=True), ScriptedModel(list(steps)), tmp_path / "data", clock=FakeClock())
        session = WebSession(app, demo)
        opened.append((app, demo))
        return session, TestClient(create_app(session))

    yield make
    for app, demo in opened:
        for closer in (app.close, demo.close):
            try:
                closer()
            except Exception:
                pass


def approvals(s) -> int:
    return s.app.store.one("SELECT COUNT(*) FROM approvals")[0]


def mutating(s) -> list:
    return [c for c in s.demo.sim.calls() if c["tool"] in ("book", "cancel", "reschedule")]


def post(c, text, **kw):
    return c.post("/api/message", json={"text": text}, **kw)


# ----------------------------------------------------------------------------- the page


def test_the_page_uses_only_theme_variables_and_never_builds_html_from_data():
    style = re.search(r"<style>(.*?)</style>", PAGE, re.S)[1]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\b(rgba?|hsla?|oklch|oklab|lab|lch|hwb)\(", style)  # no colour literals
    named = r"\b(black|white|red|blue|yellow|green|gray|grey|purple|orange|pink|silver|navy)\b"
    assert not [f"{prop}: {val}" for prop, val in re.findall(r"([a-z-]+)\s*:\s*([^;{}]+);", style) if re.search(named, val)]
    own = set(re.findall(r"(--[a-z0-9-]+)\s*:", style))  # layout variables the page defines itself
    used = set(re.findall(r"var\((--[a-z0-9-]+)", style + PAGE))
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", THEME))
    assert own <= {"--gap", "--pad", "--bw"} and used <= defined | own  # no new palette, only theme tokens
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in PAGE, banned


def test_the_page_only_talks_to_the_documented_endpoints():
    calls = set(re.findall(r'api\("(/api/[a-z/]+)', PAGE))
    assert calls == {"/api/bootstrap", "/api/log", "/api/message", "/api/demo/"}
    assert "EventSource" not in PAGE and "/api/approve" not in PAGE
    # Confirm and Decline do exactly one thing: send the displayed text as a chat message
    assert re.findall(r'addEventListener\("click", \(\) => send\(p\.(\w+)\)\)', PAGE) == ["confirm_text", "decline_text"]


def test_the_page_and_the_theme_are_served_with_safe_headers(web):
    s, c = web()
    r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"] and 'href="/theme.css"' in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
    css = c.get("/theme.css")
    assert css.status_code == 200 and "--primary" in css.text and "text/css" in css.headers["content-type"]


def test_every_route_is_a_plain_def_so_a_slow_model_call_runs_in_a_worker_thread(web):
    s, c = web()
    routes = [r for r in c.app.routes if getattr(r, "endpoint", None)]
    assert any(r.path == "/api/message" for r in routes)
    assert [r.path for r in routes if inspect.iscoroutinefunction(r.endpoint)] == []


def test_trace_rows_are_colour_coded_by_event_type():
    assert [trace_class(e, {}) for e in ("model_call", "tool_call", "tool_error", "attempt_confirmed", "poll_decision",
                                          "node_enter", "untrusted_scan")] == ["model", "tool", "bad", "ok", "poll", "node", "bad"]
    assert trace_class("final", {"status": "DONE"}) == "ok" and trace_class("final", {"status": "TOOL_FAILURE"}) == "bad"


# ----------------------------------------------------------------------------- bookings come from list_bookings


def test_the_bookings_list_comes_from_the_agents_tools_not_world_db(web, monkeypatch):
    s, c = web()
    for banned in ("world.db", "sim.bookings", "MockWorld", "sqlite3"):
        assert banned not in SERVER, banned

    def boom(*a, **k):
        raise AssertionError("the UI must not read world.db through the simulator")

    monkeypatch.setattr(s.demo.sim, "bookings", boom)
    n = len(s.demo.sim.calls("list_bookings"))
    r = c.get("/api/bookings")
    assert r.status_code == 200
    items = r.json()["bookings"]
    assert len(items) == 6 and {b["status"] for b in items} >= {"confirmed", "completed", "cancelled"}
    assert all("cancel_reason" not in b for b in items)  # provider free text is never sent to the page
    assert len(s.demo.sim.calls("list_bookings")) == n + 1  # it went through the contract tool


# ----------------------------------------------------------------------------- the approval card and the chat path


def test_the_card_shows_the_exact_stored_terms_and_confirm_is_just_a_chat_message(web):
    s, c = web([search(), pick()])
    r = post(c, FIND).json()
    assert (r["reply"]["status"], r["reply"]["kind"]) == ("NEEDS_INPUT", "proposal_ready")
    p = c.get("/api/state").json()["pending"]
    row = s.app.store.one("SELECT * FROM proposals WHERE status = 'pending'")
    assert (p["action"], p["provider_id"], p["slot"], p["price"]) == (row["action"], row["provider_id"], row["slot"], row["price"])
    assert p["confirm_text"] == f"CONFIRM {row['confirm_code']}" and p["decline_text"] == "no"
    assert p["provider_name"] and p["expired"] is False
    assert approvals(s) == 0 and mutating(s) == []  # showing a card approves nothing

    model_calls = s.app.rt.model.calls
    done = post(c, p["confirm_text"]).json()
    assert (done["reply"]["status"], done["reply"]["kind"]) == ("DONE", "confirmed")
    (ap,) = s.app.store.all("SELECT * FROM approvals")
    assert ap["user_message"] == p["confirm_text"]  # the approval is the user's chat message, read by the guard
    assert s.app.rt.model.calls == model_calls  # and the model never saw it
    assert any(b["provider_id"] == p["provider_id"] and b["slot"] == p["slot"] and b["status"] == "confirmed"
               for b in c.get("/api/bookings").json()["bookings"])
    assert c.get("/api/state").json()["pending"] is None
    assert {"chat", "state", "bookings"} <= {e["type"] for e in done["events"]}


def test_decline_is_also_just_a_chat_message(web):
    s, c = web([search(), pick()])
    post(c, FIND)
    p = c.get("/api/state").json()["pending"]
    r = post(c, p["decline_text"]).json()
    assert (r["reply"]["status"], r["reply"]["kind"]) == ("DECLINED", "declined")
    assert approvals(s) == 0 and mutating(s) == [] and c.get("/api/state").json()["pending"] is None


def test_no_route_other_than_the_chat_endpoint_can_create_an_approval(web):
    s, c = web([search(), pick(), say("ok")])
    post(c, FIND)
    pending = c.get("/api/state").json()["pending"]
    assert pending
    others = [r for r in c.app.routes if getattr(r, "methods", None) and r.methods - {"GET", "HEAD"} and r.path != "/api/message"]
    # structural: every other mutating route is a simulator button, nothing named like an approval
    assert others and all(r.path.startswith("/api/demo/") for r in others), [r.path for r in others]
    bodies = {"/api/demo/timeout": {"when": "after"}, "/api/demo/price": {"delta": 200},
              "/api/demo/provider-cancel": {"target": "cleaning"}, "/api/demo/injection": {}}
    for r in others:
        assert c.post(r.path, json=bodies.get(r.path, {})).status_code in (200, 409), r.path
        assert approvals(s) == 0 and mutating(s) == [], r.path  # a click on ANY button created no approval and no booking
    assert c.get("/api/state").json()["pending"]["proposal_id"] == pending["proposal_id"]  # still waiting for the user


def test_unknown_fields_odd_texts_and_forged_requests_create_no_approval(web):
    s, c = web([search(), pick(), say("1"), say("2"), say("3"), say("4"), say("5")])
    post(c, FIND)
    p = c.get("/api/state").json()["pending"]
    for body in ({"text": "hi", "approve": True}, {"text": "hi", "approval_id": "apr_000001"}, {}, {"text": ""},
                 {"text": "   "}, {"text": "x" * 2001}):
        assert c.post("/api/message", json=body).status_code == 422, body
    for text in ("approve", "I approve", "APPROVED", "CONFIRM 0000", "yes but make it 11"):
        assert post(c, text).status_code == 200
        assert approvals(s) == 0, text  # only a bare affirmative or the displayed CONFIRM code counts
    # a page on another origin cannot forge the user's click, even with the right text
    forged = post(c, p["confirm_text"], headers={"Origin": "https://evil.example"})
    assert forged.status_code == 403 and approvals(s) == 0
    assert c.post("/api/message", content='{"text": "yes"}', headers={"Content-Type": "text/plain"}).status_code == 415
    assert c.get("/api/state", headers={"host": "evil.example"}).status_code == 400  # DNS-rebinding guard
    assert post(c, p["confirm_text"], headers={"Origin": "http://testserver"}).json()["reply"]["kind"] == "confirmed"
    assert approvals(s) == 1


def test_the_server_never_creates_approvals_executes_or_proposes():
    for banned in ("on_user_message", ".execute(", "propose_booking", "propose_cancel", "propose_reschedule",
                   "ApprovalGuard", ".guard", "insert_attempt", "hold_slot"):
        assert banned not in SERVER, banned


# ----------------------------------------------------------------------------- the proactive timer


def test_a_provider_cancellation_shows_up_with_no_user_message(web):
    s, c = web([])  # an empty script: nothing here may need the model
    assert s.tick() == "ok"  # the baseline poll
    r = c.post("/api/demo/provider-cancel", json={"target": "cleaning"})
    assert r.status_code == 200 and "has not been told" in r.json()["events"][0]["data"]["text"]
    assert c.get("/api/state").json()["pending"] is None and approvals(s) == 0  # not noticed yet

    assert s.tick() == "ok"  # the timer's next poll
    events = c.get("/api/log?since=0").json()["events"]
    (notice,) = [e["data"] for e in events if e["type"] == "chat" and e["data"]["role"] == "notice"]
    assert "cancelled by the provider" in notice["text"] and notice["kind"] == "replacement"
    p = c.get("/api/state").json()["pending"]
    assert p["action"] == "book" and p["provider_id"] != "cl_04"
    assert {"state", "bookings"} <= {e["type"] for e in events}
    assert any(b["provider_id"] == "cl_04" and b["status"] == "cancelled_by_provider"
               for b in c.get("/api/bookings").json()["bookings"])
    assert s.app.rt.model.calls == 0 and approvals(s) == 0 and mutating(s) == []

    assert post(c, p["confirm_text"]).json()["reply"]["kind"] == "confirmed"  # approval still only via chat
    assert s.app.rt.model.calls == 0 and len(mutating(s)) == 1
    s.tick()
    assert s.tick() == "ok"
    assert s.app.store.one("SELECT status FROM proactive_events")["status"] == "replaced"
    assert len([e for e in c.get("/api/log?since=0").json()["events"]
                if e["type"] == "chat" and e["data"]["role"] == "notice"]) == 1  # one notice, ever
    history = c.get("/api/history").json()["messages"]
    assert "notice" in {m["role"] for m in history} and any(m["text"] == p["confirm_text"] for m in history)


def test_a_poll_never_interleaves_with_a_running_message(web):
    results, holder = [], {}

    def step(request):
        results.append(holder["s"].tick())  # a timer tick fires while this message is mid-run
        return say("hello")

    s, c = web([step])
    holder["s"] = s
    traces = s.app.data_dir / "traces"
    post(c, "hi")
    assert results == ["busy"] and list(traces.glob("poll_*.jsonl")) == []  # the tick was skipped, no poll ran
    n = len(s.demo.sim.calls("list_bookings"))
    assert s.tick() == "ok" and len(s.demo.sim.calls("list_bookings")) > n  # a real poll ran now ...
    assert list(traces.glob("poll_*.jsonl")) == []  # ... and found nothing, so it left no trace file
    s.demo.provider_cancel("cleaning")
    assert s.tick() == "ok" and len(list(traces.glob("poll_*.jsonl"))) == 1  # a poll that detects something is traced


def test_a_message_that_arrives_during_a_poll_waits_for_it(web):
    entered, release = threading.Event(), threading.Event()

    def hold(event):
        entered.set()
        assert release.wait(5)

    s, c = web([say("hi there")])
    s.app.poller = Poller(s.app.rt, PollHooks(after_detected=hold))
    s.tick()  # baseline: nothing detected yet
    s.demo.provider_cancel("cleaning")
    ticked, answered = [], []
    t = threading.Thread(target=lambda: ticked.append(s.tick()))
    t.start()
    assert entered.wait(5)  # the poll is now mid-way, holding the lock
    m = threading.Thread(target=lambda: answered.append(TestClient(c.app).post("/api/message", json={"text": "hello"}).json()))
    m.start()
    time.sleep(0.3)
    assert answered == [] and s.app.rt.model.calls == 0  # the message is waiting, not interleaved
    release.set()
    t.join(5), m.join(5)
    assert ticked == ["ok"] and answered and s.app.rt.model.calls == 1
    names = [e["data"]["event"] for e in s.log.since(0) if e["type"] == "trace"]
    assert names.index("poll_decision") < names.index("run_start")  # the poll finished before the message began


def test_a_slow_model_call_blocks_neither_other_requests_nor_the_timer(web):
    in_model, go = threading.Event(), threading.Event()

    def slow(request):
        in_model.set()
        assert go.wait(5)
        return say("finally")

    s, c = web([slow])
    out = []
    t = threading.Thread(target=lambda: out.append(TestClient(c.app).post("/api/message", json={"text": "hi"}).json()))
    t.start()
    assert in_model.wait(5)
    t0 = time.monotonic()
    assert c.get("/api/state").json()["busy"] is True and c.get("/api/log?since=0").json()["busy"] is True
    assert c.get("/api/bookings").status_code == 200
    assert s.tick() == "busy"  # the timer neither waits nor races
    assert time.monotonic() - t0 < 2
    go.set()
    t.join(5)
    assert out and out[0]["reply"]["kind"] == "answer"
    assert c.get("/api/log?since=0").json()["busy"] is False


# ----------------------------------------------------------------------------- the log the page polls


def test_the_log_returns_only_what_is_new_and_colour_codes_the_trace(web):
    s, c = web([search(), pick(), say("you're welcome")])
    post(c, FIND)
    first = c.get("/api/log?since=0").json()
    ids = [e["id"] for e in first["events"]]
    assert ids == sorted(set(ids)) and {"chat", "trace", "state", "bookings"} <= {e["type"] for e in first["events"]}
    assert c.get(f"/api/log?since={first['last_id']}").json()["events"] == []
    by = {e["data"]["event"]: e["data"]["cls"] for e in first["events"] if e["type"] == "trace"}
    assert by["model_call"] == "model" and by["tool_call"] == "tool" and by["proposal_created"] == "ok" and by["node_enter"] == "node"

    post(c, "thanks")
    second = c.get(f"/api/log?since={first['last_id']}").json()["events"]
    assert second and min(e["id"] for e in second) > first["last_id"]
    rows = [(e["data"]["run"], e["data"]["seq"]) for e in first["events"] + second if e["type"] == "trace"]
    assert len(rows) == len(set(rows))  # a trace row is pushed once, however often the page polls


def test_history_and_bootstrap_cover_the_conversation_but_not_code_handled_memory_commands(web):
    s, c = web([search(), pick()])
    post(c, FIND)
    assert post(c, "what do you remember").json()["reply"]["kind"] == "memory_listed"  # live, but never logged as a turn
    msgs = c.get("/api/history").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "agent"] and msgs[0]["text"] == FIND and all(m["at"] for m in msgs)
    boot = c.get("/api/bootstrap").json()
    assert boot["history"] == msgs and boot["state"]["pending"] and len(boot["bookings"]) == 6
    assert boot["trace"] and all({"id", "cls", "event", "text"} <= set(t) for t in boot["trace"])
    assert boot["last_id"] == c.get("/api/log?since=0").json()["last_id"]


# ----------------------------------------------------------------------------- the demo buttons


def test_the_demo_buttons_arm_the_simulator_and_injection_goes_through_the_chat_path(web):
    s, c = web([call("get_provider", provider_id="pl_01"), say("Mixed reviews, honestly.")])
    assert c.post("/api/demo/timeout", json={"when": "before"}).status_code == 200
    assert c.post("/api/demo/timeout", json={}).status_code == 200
    assert ("book", "timeout_before_commit") in s.demo.sim.pending_forced() and ("book", "timeout_after_commit") in s.demo.sim.pending_forced()
    assert c.post("/api/demo/timeout", json={"when": "sideways"}).status_code == 422
    assert c.post("/api/demo/price", json={"delta": 150}).status_code == 200 and ("hold_slot", "price_change") in s.demo.sim.pending_forced()
    assert c.post("/api/demo/price", json={"delta": 99999}).status_code == 422
    assert c.post("/api/demo/provider-cancel", json={"target": "xyz"}).status_code == 409
    assert c.post("/api/demo/provider-cancel", json={"target": "carpenter"}).status_code == 409

    r = c.post("/api/demo/injection", json={}).json()
    assert r["reply"]["kind"] == "answer"
    assert c.get("/api/history").json()["messages"][0]["text"] == INJECTION_PROMPT  # typed-equivalent: a normal user turn
    assert any(e["type"] == "trace" and e["data"]["event"] == "untrusted_scan" for e in c.get("/api/log?since=0").json()["events"])
    assert approvals(s) == 0 and mutating(s) == []


# ----------------------------------------------------------------------------- python -m demo.web


def test_main_reports_a_missing_key_and_otherwise_serves_the_app_on_localhost(tmp_path, capsys):
    assert main(["--data-dir", str(tmp_path / "d")], env_file=tmp_path / "none.env") == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err and not (tmp_path / "d").exists()
    served = {}
    code = main(["--data-dir", str(tmp_path / "d2"), "--port", "8123"], model=ScriptedModel([]), clock=FakeClock(),
                env_file=tmp_path / "none.env", serve=lambda api, **kw: served.update(kw, api=api))
    assert code == 0 and served["host"] == "127.0.0.1" and served["port"] == 8123 and served["api"] is not None
    assert "http://127.0.0.1:8123" in capsys.readouterr().out
