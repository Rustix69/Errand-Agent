"""The web server for the demo UI:  python -m demo.web

THE RULE: the UI can never book anything. There is exactly one route that hands user text to the agent,
``POST /api/message``, and it only calls ``agent.send(text)``. The approval card's Confirm / Decline buttons are
just two more ways to send a chat message (the displayed ``CONFIRM <code>`` / ``no``); whether that message is an
approval is decided by the approval guard reading it, like any message typed in the box. Nothing in this module
creates an approval, executes an action, or proposes one, and a test scans for that.

* The bookings list comes from the SAME contract tools the agent uses (``list_bookings``), never straight from the
  mock marketplace's database file.
* A timer thread polls for provider cancellations. One lock serialises user messages and polls: the timer uses
  try-lock, so a poll is skipped while a message is running and never interleaves; a message that arrives during a
  poll waits for it.
* Routes are plain ``def`` so FastAPI runs them in worker threads: a slow model call blocks neither the server nor
  the timer. The page learns what happened by polling ``GET /api/log?since=N`` about once a second.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from demo.chat import INJECTION_PROMPT, PROJECT_ROOT, prepare
from demo.world import DemoError, DemoWorld
from errand.agent import UnfinishedRun, outbox
from errand.app import App
from errand.clock import Clock, SystemClock
from errand.config import ConfigError
from errand.model.base import ModelClient
from errand.trace import read_trace
from errand.trace.cli import Follower, summary

WEB = Path(__file__).resolve().parent
KEEP_EVENTS = 1000
TRACE_ON_LOAD = 80
MAX_TEXT = 2000

# trace event -> the colour class the page gives its pill (theme tokens only: see index.html)
_BAD = {"tool_error", "limit_reached", "tool_refused", "tool_invalid", "approval_invalid", "attempt_failed",
        "understand_error", "hold_failed", "poll_error", "executor_exception", "verify_downgrade", "untrusted_scan",
        "memory_error", "turn_log_error", "model_setting_fallback", "known_dropped"}
_OK = {"decision", "attempt_pending", "attempt_confirmed", "execute_start", "execute_outcome", "verified",
       "approval_rebound", "proposal_created", "propose_requested", "startup_recovery", "recovery_resolved"}
_POLL = {"poll_start", "poll_end", "poll_detected", "poll_decision", "poll_proposal", "poll_resumed", "poll_limit",
         "memory_op", "memory_captured", "budget_recreated"}


def trace_class(event: str, data: dict[str, Any]) -> str:
    if event == "final":
        return "ok" if data.get("status") in ("DONE", "NEEDS_INPUT") else "bad"
    if event in _BAD:
        return "bad"
    if event in ("model_call", "context_built"):
        return "model"
    if event in ("tool_call", "tool_result", "tool_output"):
        return "tool"
    if event in _OK:
        return "ok"
    if event in _POLL:
        return "poll"
    return "node"


def ui_row(row: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(summary(row).split())
    out = {"run": row.get("run"), "seq": row.get("seq"), "t": row["t"], "event": row["event"],
           "text": text if len(text) <= 160 else text[:159] + "…", "cls": trace_class(row["event"], row.get("data", {}))}
    if "latency_s" in row:
        out["latency_s"] = row["latency_s"]
    return out


class EventLog:
    """Append-only, id-numbered list of what the page should show. The page asks for everything after an id."""

    def __init__(self, keep: int = KEEP_EVENTS) -> None:
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._next = 1
        self._keep = keep

    def emit(self, type_: str, data: dict[str, Any], at: str) -> dict[str, Any]:
        with self._lock:
            ev = {"id": self._next, "type": type_, "at": at, "data": data}
            self._next += 1
            self._events.append(ev)
            del self._events[:-self._keep]
            return ev

    def since(self, last_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["id"] > last_id]

    def last(self, type_: str, n: int) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["type"] == type_][-n:]

    @property
    def last_id(self) -> int:
        with self._lock:
            return self._next - 1


class WebSession:
    """Everything the routes need: the app, the simulator, the event log and the one lock."""

    def __init__(self, app: App, demo: DemoWorld) -> None:
        self.app, self.demo = app, demo
        self.log = EventLog()
        self.lock = threading.Lock()  # user messages and polls take turns
        self.message_running = False
        self._push_lock = threading.Lock()
        self._tail_lock = threading.Lock()
        self._last_state: str | None = None
        self._last_bookings: str | None = None
        self._names: dict[str, str | None] = {}
        self._prefill_trace()

    # ------------------------------------------------------------------ small helpers

    def now(self) -> str:
        return self.app.clock.now().isoformat(timespec="seconds")

    def _emit(self, type_: str, data: dict[str, Any]) -> dict[str, Any]:
        return self.log.emit(type_, data, self.now())

    def _prefill_trace(self) -> None:
        """Show the latest trace on first load, then follow only what is written from now on."""
        d = self.app.data_dir / "traces"
        files = sorted(d.glob("*.jsonl"), key=lambda p: (p.stat().st_mtime, p.name))[-3:] if d.exists() else []
        rows = [r for p in files for r in read_trace(p)][-TRACE_ON_LOAD:]
        for r in rows:
            self._emit("trace", ui_row(r))
        self._tail = Follower(d, replay="none")
        self._tail.poll_rows()  # baseline: end of every existing file

    # ------------------------------------------------------------------ reads (no agent lock)

    def bookings(self) -> list[dict[str, Any]]:
        """What list_bookings returned, through the same contract tools the agent uses, not a direct read of the mock's database."""
        res = self.demo.tools.list_bookings(self.app.user_id)
        keep = ("booking_id", "provider_id", "provider_name", "service", "slot", "price", "status", "is_upcoming")
        return [{k: getattr(b, k) for k in keep} for b in res.bookings]  # cancel_reason (free text) is left out

    def _provider_name(self, provider_id: str, booking_id: str | None = None) -> str | None:
        if booking_id:
            for b in self.bookings():
                if b["booking_id"] == booking_id:
                    return b["provider_name"]
        if provider_id not in self._names:
            try:
                self._names[provider_id] = self.demo.tools.get_provider(provider_id).name
            except Exception:
                self._names[provider_id] = None
        return self._names[provider_id]

    def pending(self) -> dict[str, Any] | None:
        """The proposal awaiting approval, exactly as stored (the same terms the guard fingerprinted)."""
        row = self.app.store.one("SELECT * FROM proposals WHERE session_id = ? AND status = 'pending'",
                                 (self.app.session,))
        if row is None:
            return None
        return {
            "proposal_id": row["id"], "action": row["action"], "provider_id": row["provider_id"],
            "provider_name": self._provider_name(row["provider_id"], row["booking_id"]), "slot": row["slot"],
            "new_slot": row["new_slot"], "price": row["price"], "booking_id": row["booking_id"],
            "confirm_text": f"CONFIRM {row['confirm_code']}", "decline_text": "no", "expires_at": row["expires_at"],
            "expired": self.app.clock.now() >= datetime.fromisoformat(row["expires_at"]),
        }

    def state(self) -> dict[str, Any]:
        s = self.app.settings
        return {"session": self.app.session, "model": s.model_name, "mode": self.demo.mode,
                "busy": self.message_running, "pending": self.pending(), "last_event_id": self.log.last_id}

    def history(self) -> list[dict[str, Any]]:
        """The conversation so far (completed exchanges and notices), oldest first. Code-handled memory commands are
        not logged, so they are not here."""
        rows = [(t.at, 0, i, {"role": "user" if t.role == "user" else "agent", "text": t.content, "at": t.at})
                for i, t in enumerate(self.app.turns.session(self.app.session))]
        rows += [(r["created_at"], 1, r["id"], {"role": "notice", "text": r["message"], "at": r["created_at"],
                                                "kind": r["kind"]})
                 for r in self.app.store.all("SELECT * FROM outbox WHERE session_id = ? ORDER BY id", (self.app.session,))]
        return [dict(item, id=f"h{n}") for n, (_, _, _, item) in enumerate(sorted(rows, key=lambda r: r[:3]))]

    def bootstrap(self) -> dict[str, Any]:
        """Everything the page needs on load, taken at one consistent moment."""
        with self.lock:  # waits for a running message so a reload cannot show it twice
            self.push_trace()
            return {"state": self.state(), "history": self.history(), "bookings": self.bookings(),
                    "trace": [e["data"] | {"id": e["id"]} for e in self.log.last("trace", TRACE_ON_LOAD)],
                    "last_id": self.log.last_id}

    def events_since(self, since: int) -> dict[str, Any]:
        self.push_trace()  # pick up trace rows written by a run that is still going
        return {"events": self.log.since(since), "last_id": self.log.last_id, "busy": self.message_running}

    # ------------------------------------------------------------------ pushing what changed

    def push_trace(self) -> None:
        with self._tail_lock:
            for row in self._tail.poll_rows():
                self._emit("trace", ui_row(row))

    def push_changes(self) -> None:
        """Trace rows, the pending proposal and the bookings list, each only if it changed."""
        self.push_trace()
        with self._push_lock:
            state = json.dumps(self.pending(), sort_keys=True)
            if state != self._last_state:
                self._last_state = state
                self._emit("state", {"pending": json.loads(state)})
            try:
                bookings = json.dumps(self.bookings(), sort_keys=True)
            except Exception:
                return
            if bookings != self._last_bookings:
                self._last_bookings = bookings
                self._emit("bookings", {"bookings": json.loads(bookings)})

    # ------------------------------------------------------------------ the one path from the page to the agent

    def send(self, text: str) -> dict[str, Any]:
        with self.lock:
            self.message_running = True
            start = self.log.last_id
            reply = None
            try:
                self._emit("chat", {"role": "user", "text": text})
                try:
                    reply = self._ask_agent(text)
                    self._emit("chat", {"role": "agent", "text": reply.message, "status": reply.status,
                                        "kind": reply.kind, "run_id": reply.run_id})
                except Exception as e:  # an unexpected server-side error: say so, never hang the page
                    self._emit("chat", {"role": "agent", "text": f"Something went wrong handling that ({type(e).__name__}). "
                                                                  "Nothing was booked or changed without approval.",
                                        "status": "TOOL_FAILURE", "kind": "server_error", "run_id": None})
                self.push_changes()
            finally:
                self.message_running = False
            return {"reply": None if reply is None else {
                "status": reply.status, "kind": reply.kind, "message": reply.message, "waiting": reply.waiting,
                "proposal_id": reply.proposal_id, "reason": reply.reason, "run_id": reply.run_id},
                "events": self.log.since(start)}

    def _ask_agent(self, text: str) -> Any:
        agent = self.app.agent
        try:
            return agent.send(text)
        except UnfinishedRun:  # a crashed run is waiting: finish it first, then handle this message
            agent.resume()
            return agent.send(text)

    # ------------------------------------------------------------------ the proactive timer

    def tick(self) -> str:
        """One poll. Skipped (returns "busy") if a message is running: a poll never interleaves with a run."""
        if not self.lock.acquire(blocking=False):
            return "busy"
        try:
            self.app.poller.poll(self.app.session)
            self._deliver_notices()
            self.push_changes()
            return "ok"
        except Exception as e:
            self._emit("error", {"text": f"poll failed: {type(e).__name__}"})
            return "error"
        finally:
            self.lock.release()

    def _deliver_notices(self) -> None:
        rows = outbox.undelivered(self.app.store, self.app.session)
        for r in rows:
            self._emit("chat", {"role": "notice", "text": r["message"], "kind": r["kind"]})
        if rows:
            outbox.mark_delivered(self.app.store, [r["id"] for r in rows])

    # ------------------------------------------------------------------ simulator buttons (not agent tools)

    def _sim_line(self, text: str) -> dict[str, Any]:
        start = self.log.last_id
        self._emit("chat", {"role": "system", "text": text})
        self.push_changes()
        return {"events": self.log.since(start)}

    def arm_timeout(self, when: str) -> dict[str, Any]:
        kind = self.demo.arm_timeout(when)
        return self._sim_line(f"Simulator: the next booking will be {kind}.")

    def arm_price(self, delta: int) -> dict[str, Any]:
        self.demo.arm_price(delta)
        return self._sim_line(f"Simulator: the next hold quotes a price changed by {delta:+d}.")

    def provider_cancel(self, target: str) -> dict[str, Any]:
        b = self.demo.provider_cancel(target)
        return self._sim_line(f"Simulator: the provider cancelled {b['booking_id']} ({b['service']}, {b['slot']}). "
                              "The agent has not been told: it finds out on its next poll.")


# ----------------------------------------------------------------------------- the HTTP app


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # {"text": "hi", "approve": true} is rejected, not half-honoured
    text: str = Field(min_length=1, max_length=MAX_TEXT)

    @field_validator("text")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message is empty")
        return v.strip()


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    when: Literal["after", "before"] = "after"


class PriceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delta: int = Field(200, ge=-500, le=1000)


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field("cleaning", min_length=1, max_length=40)


def create_app(session: WebSession, *, ticker: bool = False, poll_seconds: float = 3.0,
               allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "testserver")) -> FastAPI:
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(poll_seconds):
            session.tick()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        thread = threading.Thread(target=loop, name="errand-poller", daemon=True) if ticker else None
        if thread:
            thread.start()
        yield
        stop.set()

    api = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    api.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))  # DNS-rebinding guard

    @api.middleware("http")
    async def guard_requests(request: Request, call_next):
        if request.method != "GET":
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"detail": "send application/json"}, status_code=415)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'")
        return response

    def refuse(e: DemoError) -> HTTPException:
        return HTTPException(status_code=409, detail=str(e))

    @api.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB / "index.html", media_type="text/html")

    @api.get("/theme.css")
    def theme() -> FileResponse:
        return FileResponse(WEB / "theme.css", media_type="text/css")

    @api.get("/api/state")
    def state() -> dict[str, Any]:
        return session.state()

    @api.get("/api/bookings")
    def bookings() -> dict[str, Any]:
        return {"bookings": session.bookings()}

    @api.get("/api/history")
    def history() -> dict[str, Any]:
        with session.lock:
            return {"messages": session.history(), "last_id": session.log.last_id}

    @api.get("/api/bootstrap")
    def bootstrap() -> dict[str, Any]:
        return session.bootstrap()

    @api.get("/api/log")
    def log(since: int = 0) -> dict[str, Any]:
        return session.events_since(max(since, 0))

    # A plain `def` (not `async def`): FastAPI runs it in a worker thread, so a slow model call blocks neither the
    # server nor the timer. This is the ONLY route that gives user text to the agent.
    @api.post("/api/message")
    def message(body: MessageIn) -> dict[str, Any]:
        return session.send(body.text)

    @api.post("/api/demo/timeout")
    def demo_timeout(body: TimeoutIn) -> dict[str, Any]:
        try:
            return session.arm_timeout(body.when)
        except DemoError as e:
            raise refuse(e) from None

    @api.post("/api/demo/price")
    def demo_price(body: PriceIn) -> dict[str, Any]:
        return session.arm_price(body.delta)

    @api.post("/api/demo/provider-cancel")
    def demo_provider_cancel(body: CancelIn) -> dict[str, Any]:
        try:
            return session.provider_cancel(body.target)
        except DemoError as e:
            raise refuse(e) from None

    @api.post("/api/demo/injection")
    def demo_injection(body: Empty) -> dict[str, Any]:
        return session.send(INJECTION_PROMPT)  # the same chat path as typing it

    return api


# ----------------------------------------------------------------------------- python -m demo.web


def main(argv: list[str] | None = None, *, model: ModelClient | None = None, clock: Clock | None = None,
         env_file: Path | None = PROJECT_ROOT / ".env", serve: Callable[..., Any] = uvicorn.run) -> int:
    ap = argparse.ArgumentParser(prog="python -m demo.web", description="The errand agent in the browser.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data-dir", default=".errand", help="use a different folder than the terminal demo if both run")
    ap.add_argument("--fresh", action="store_true", help="wipe the demo data (only a folder this tool created) and reseed")
    ap.add_argument("--random", action="store_true",
                    help="NEW worlds only: contract failure rates (book: 70%% success, 15%% slot_taken, 15%% timeout)")
    ap.add_argument("--session", default="demo")
    ap.add_argument("--poll-seconds", type=float, default=3.0, help="how often the server polls for cancellations")
    a = ap.parse_args(argv)
    try:
        settings, app, demo = prepare(Path(a.data_dir), fresh=a.fresh, random=a.random, session=a.session,
                                      clock=clock or SystemClock(), model=model, env_file=env_file)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    web = WebSession(app, demo)
    api = create_app(web, ticker=True, poll_seconds=a.poll_seconds, allowed_hosts=(a.host, "localhost", "127.0.0.1"))
    print(f"errand agent web UI: http://{a.host}:{a.port}  ·  model {settings.model_name} · "
          f"key {'set' if settings.api_key_set else 'missing'} · data in {a.data_dir}")
    try:
        serve(api, host=a.host, port=a.port, log_level="warning")
    finally:
        app.close()
        demo.close()
    return 0
