"""Proactive follow-up: notice a provider cancellation WITHOUT a user message, decide whether and how to tell
the user, line up a replacement that fits their saved preferences, and leave the booking to the usual proposal +
approval. No model is involved anywhere in this path.

The agent learns of a cancellation only by polling ``list_bookings`` and comparing with what it last saw
(``booking_snapshots``); the simulator's ``provider_cancels`` hook is not a tool it can call.

Rules (all code):
* Worth a notice: a booking the PROVIDER cancelled (``cancelled_by_provider``) whose slot is still in the future.
  A provider cancellation noticed only after the slot has passed is recorded as ``ignored``/``past``. The user's
  own cancellations (``cancelled``) and the agent's own are never events.
* Once per cancellation: ``proactive_events`` is UNIQUE per (user, booking, kind) and ``outbox.event_id`` is UNIQUE.
* Never books: a replacement is only ``hold_slot`` + ``ApprovalGuard.propose_booking``. The user's "yes" is handled
  by the ordinary graph and guard.
* Notice only (no proposal) when the session already has a pending proposal, or no home area is saved.
* Crash-safe write order: event ``detected`` -> hold id -> proposal id -> ONE transaction (event ``notified`` +
  outbox row + snapshot). A restart resumes any ``detected`` event and finishes it exactly once.
* Notice text is built here from trusted fields. The provider's free-text ``cancel_reason`` is never echoed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from errand.agent import outbox
from errand.agent.finalize import approval_request
from errand.agent.graph import summarize_proposal
from errand.clock import IST
from errand.contract.errors import ToolError
from errand.core import Budget, BudgetExhausted, Reader
from errand.core.store import iso

KIND = "provider_cancelled"
PRICE_CAP = 1.2  # a replacement may cost at most 20% more than the cancelled booking
SEARCH_DAYS_AFTER = 3
MAX_HOLD_TRIES = 3


@dataclass
class PollHooks:
    """Test hooks for crash injection. Production passes none."""

    after_detected: Callable[[dict[str, Any]], None] | None = None  # event row written, nothing else done
    after_proposal: Callable[[dict[str, Any]], None] | None = None  # proposal created, event not yet 'notified'


@dataclass
class PollResult:
    run_id: str
    detected: list[int] = field(default_factory=list)  # event ids handled in this poll
    decisions: list[tuple[int, str]] = field(default_factory=list)  # (event id, decision)


def _dt(slot: str) -> datetime:
    return datetime.fromisoformat(slot).replace(tzinfo=IST)


def _minutes(slot: str) -> int:
    return int(slot[-5:-3]) * 60 + int(slot[-2:])


def choose(options: list[dict[str, Any]], original_slot: str, earliest: str | None, now: datetime,
           own_provider: str, price: int) -> list[tuple[dict[str, Any], str]]:
    """Replacement candidates, best first. `options` are search results ({provider_id, price, rating, slots}).
    Rule: a different provider, price <= 120% of the cancelled one, slot in the future and at/after the saved
    earliest time. Rank: nearest day, then nearest time of day, then rating, then price."""
    out = []
    for p in options:
        if p["provider_id"] == own_provider or p["price"] > price * PRICE_CAP:
            continue
        for s in p["slots"]:
            if _dt(s) <= now or (earliest and s[-5:] < earliest):
                continue
            key = (abs((_dt(s).date() - _dt(original_slot).date()).days), abs(_minutes(s) - _minutes(original_slot)),
                   -p["rating"], p["price"], p["provider_id"], s)
            out.append((key, p, s))
    out.sort(key=lambda t: t[0])
    return [(p, s) for _, p, s in out]


class Poller:
    def __init__(self, rt: Any, hooks: PollHooks | None = None) -> None:
        self.rt, self.hooks = rt, hooks or PollHooks()
        self.store = rt.store
        outbox.ensure_schema(self.store)

    # ------------------------------------------------------------------ public

    def poll(self, session_id: str) -> PollResult:
        rt, store = self.rt, self.store
        with store.tx() as c:
            run_id = store.next_id(c, "poll")
        res = PollResult(run_id)
        rt.emit("poll_start", {"run_id": run_id, "session_id": session_id})
        read = Reader(rt.tools, rt.clock, rt.limits, Budget(rt.clock, rt.budget_limits), rt.emit)
        try:
            bookings = read("list_bookings", user_id=rt.user_id).bookings
        except (ToolError, BudgetExhausted) as e:
            rt.emit("poll_error", {"run_id": run_id, "type": type(e).__name__})
            return res
        replaced = self._mark_replaced(run_id)
        for ev in store.all("SELECT * FROM proactive_events WHERE user_id = ? AND status = 'detected' ORDER BY id",
                            (rt.user_id,)):  # a previous poll died part-way: finish it, exactly once
            rt.emit("poll_resumed", {"run_id": run_id, "event_id": ev["id"]})
            self._finish(run_id, session_id, ev["id"], read, res)
        snaps = {r["booking_id"]: r["status"] for r in
                 store.all("SELECT booking_id, status FROM booking_snapshots WHERE user_id = ?", (rt.user_id,))}
        for b in bookings:
            if b.status == "cancelled_by_provider" and snaps.get(b.booking_id) != "cancelled_by_provider":
                self._detect(run_id, session_id, b, read, res)
        self._snapshot(bookings)
        rt.emit("poll_end", {"run_id": run_id, "handled": len(res.detected), "replaced": replaced})
        return res

    # ------------------------------------------------------------------ detection

    def _detect(self, run_id: str, session_id: str, b: Any, read: Reader, res: PollResult) -> None:
        store, now = self.store, self.rt.clock.now()
        with store.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO proactive_events(user_id, booking_id, kind, status, booking_json, detected_at) "
                "VALUES (?,?,?,?,?,?)",
                (self.rt.user_id, b.booking_id, KIND, "detected", b.model_dump_json(), iso(now)))
            inserted = cur.rowcount == 1
        ev = store.one("SELECT * FROM proactive_events WHERE user_id = ? AND booking_id = ? AND kind = ?",
                       (self.rt.user_id, b.booking_id, KIND))
        if not inserted and ev["status"] != "detected":
            return  # this cancellation was already handled (ignored / notified / replaced): never twice
        upcoming = _dt(b.slot) > now
        self.rt.emit("poll_detected", {"run_id": run_id, "event_id": ev["id"], "booking_id": b.booking_id,
                                       "service": b.service, "upcoming": upcoming})
        if self.hooks.after_detected:
            self.hooks.after_detected(dict(ev))
        self._finish(run_id, session_id, ev["id"], read, res)

    def _finish(self, run_id: str, session_id: str, event_id: int, read: Reader, res: PollResult) -> None:
        store, rt = self.store, self.rt
        ev = store.one("SELECT * FROM proactive_events WHERE id = ?", (event_id,))
        if ev is None or ev["status"] != "detected":
            return
        b = json.loads(ev["booking_json"])
        res.detected.append(event_id)

        if _dt(b["slot"]) <= rt.clock.now():  # cancelled by the provider, but the slot has already passed
            with store.tx() as c:
                c.execute("UPDATE proactive_events SET status='ignored', reason='past' WHERE id = ?", (event_id,))
                self._upsert_snapshot(c, b)
            rt.emit("poll_decision", {"run_id": run_id, "event_id": event_id, "decision": "ignored", "reason": "past"})
            res.decisions.append((event_id, "ignored"))
            return

        kind, summary = self._decide(session_id, ev, b, read, run_id)
        if self.hooks.after_proposal and summary:
            self.hooks.after_proposal(dict(store.one("SELECT * FROM proactive_events WHERE id = ?", (event_id,))))
        text = self._notice(kind, b, summary)
        with store.tx() as c:  # the ONE transaction that makes the notice exist
            c.execute("UPDATE proactive_events SET status='notified', notified_at=?, proposal_id=? WHERE id = ?",
                      (iso(rt.clock.now()), summary["proposal_id"] if summary else None, event_id))
            c.execute("INSERT INTO outbox(session_id, event_id, kind, message, created_at) VALUES (?,?,?,?,?)",
                      (session_id, event_id, kind, text, iso(rt.clock.now())))
            self._upsert_snapshot(c, b)
        rt.emit("poll_decision", {"run_id": run_id, "event_id": event_id, "decision": kind,
                                  "proposal_id": summary["proposal_id"] if summary else None})
        res.decisions.append((event_id, kind))

    # ------------------------------------------------------------------ decision: whether and how to notify

    def _decide(self, session_id: str, ev: Any, b: dict[str, Any], read: Reader, run_id: str):
        store, rt = self.store, self.rt
        own = store.one("SELECT * FROM proposals WHERE hold_id = ? ORDER BY id DESC LIMIT 1",
                        (ev["hold_id"],)) if ev["hold_id"] else None
        if own is not None and own["status"] == "pending":  # a restart after our proposal: adopt it, don't redo it
            return "replacement", summarize_proposal(own, [], provider_name=b["provider_name"])
        theirs = store.one("SELECT 1 FROM proposals WHERE session_id = ? AND status = 'pending'", (session_id,))
        if theirs is not None:  # the user is mid-way through something else: tell them, don't interrupt
            return "notice_pending", None
        prefs = {m.key: m.value for m in rt.memory.usable(rt.user_id)}  # SQL-filtered: no forgotten / unconfirmed
        area = prefs.get("home_area")
        if not area:
            return "ask_area", None
        try:
            return self._replacement(session_id, ev, b, area, prefs.get("earliest_time"), read)
        except BudgetExhausted:
            rt.emit("poll_limit", {"run_id": run_id, "event_id": ev["id"]})
            return "no_replacement", None

    def _replacement(self, session_id: str, ev: Any, b: dict[str, Any], area: str, earliest: str | None, read: Reader):
        store, rt = self.store, self.rt
        d0 = _dt(b["slot"]).date()
        try:
            found = read("search_providers", service=b["service"], area=area,
                         date_range={"start": d0.isoformat(), "end": (d0 + timedelta(days=SEARCH_DAYS_AFTER)).isoformat()})
        except ToolError:
            return "no_replacement", None
        options = [{"provider_id": p.provider_id, "name": p.name, "price": p.price, "rating": p.rating,
                    "slots": p.available_slots} for p in found.providers]
        names = {o["provider_id"]: o["name"] for o in options}
        for p, slot in choose(options, b["slot"], earliest, rt.clock.now(), b["provider_id"], b["price"])[:MAX_HOLD_TRIES]:
            try:
                hold = read("hold_slot", provider_id=p["provider_id"], slot=slot)
            except ToolError:
                continue
            with store.tx() as c:
                c.execute("UPDATE proactive_events SET hold_id = ? WHERE id = ?", (hold.hold_id, ev["id"]))
            pid = rt.guard.propose_booking(session_id, rt.user_id, hold)
            with store.tx() as c:
                c.execute("UPDATE proactive_events SET proposal_id = ? WHERE id = ?", (pid, ev["id"]))
            rt.emit("poll_proposal", {"event_id": ev["id"], "proposal_id": pid, "provider_id": p["provider_id"]})
            return "replacement", summarize_proposal(store.proposal(pid), [], provider_name=names[p["provider_id"]],
                                                     search_price=hold.search_price, price_changed=hold.price_changed)
        return "no_replacement", None

    # ------------------------------------------------------------------ notice text (code, trusted fields only)

    @staticmethod
    def _notice(kind: str, b: dict[str, Any], summary: dict[str, Any] | None) -> str:
        what = (f"Heads up: your {b['service'].replace('_', ' ')} with {b['provider_name']} on {b['slot']} was "
                "cancelled by the provider.")
        if kind == "replacement" and summary:
            return f"{what} " + approval_request(summary, "I found a replacement that fits your saved preferences. ")
        if kind == "notice_pending":
            return f"{what} You are in the middle of another request, so I have not started a replacement. Tell me when to."
        if kind == "ask_area":
            return f"{what} I don't have an area saved for you. Which area should I look in for a replacement?"
        return (f"{what} I could not find a replacement that fits your saved preferences. Nothing was booked. "
                "Tell me if you'd like me to look at other times or areas.")

    # ------------------------------------------------------------------ bookkeeping

    def _upsert_snapshot(self, c: Any, b: dict[str, Any]) -> None:
        c.execute(
            "INSERT INTO booking_snapshots(user_id, booking_id, status, slot, provider_id, service, seen_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id, booking_id) DO UPDATE SET status = excluded.status, "
            "slot = excluded.slot, seen_at = excluded.seen_at",
            (self.rt.user_id, b["booking_id"], b["status"], b["slot"], b["provider_id"], b["service"],
             iso(self.rt.clock.now())))

    def _snapshot(self, bookings: list[Any]) -> None:
        """Remember what we saw. A booking whose cancellation is still being handled keeps its OLD status here, so a
        crash cannot lose the detection: the next poll sees the change again (and the event row dedupes it)."""
        busy = {r["booking_id"] for r in self.store.all(
            "SELECT booking_id FROM proactive_events WHERE user_id = ? AND status = 'detected'", (self.rt.user_id,))}
        with self.store.tx() as c:
            for b in bookings:
                if b.booking_id not in busy:
                    self._upsert_snapshot(c, b.model_dump(mode="json"))

    def _mark_replaced(self, run_id: str) -> int:
        with self.store.tx() as c:
            return c.execute("UPDATE proactive_events SET status = 'replaced' WHERE status = 'notified' AND proposal_id IN "
                             "(SELECT id FROM proposals WHERE status = 'executed')").rowcount
