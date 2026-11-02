"""Approval: a stored record tied to the exact action, created only by this module.

* ``ApprovalGuard.on_user_message`` is the ONLY code path that inserts an approval row. It
  takes the raw user text and runs the deterministic ``classify_reply``; no model output,
  tool argument or proposal field can produce an approval.
* An approval binds the material terms (action, provider, slot, price, booking id/new slot)
  and a reference (hold id for book, booking id for cancel/reschedule) through a fingerprint.
* It has its own short window (not tied to the hold), is single-use (``used_at``), and may be
  rebound to a replacement hold at most once, only when provider, slot and price are
  identical, with an audit row.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any, Literal

from errand.clock import Clock
from errand.contract.models import BookingInfo, Hold
from errand.core.store import AgentStore, iso, parse

Action = Literal["book", "cancel", "reschedule"]
Reply = Literal["approve", "decline", "other"]

# --------------------------------------------------------------------------- terms


@dataclass(frozen=True)
class Terms:
    """Material terms of an action. Any change to these invalidates an approval."""

    action: str
    provider_id: str
    slot: str  # book: the slot; cancel: booking's slot; reschedule: the CURRENT slot
    price: int
    booking_id: str | None = None
    new_slot: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fp(self) -> str:
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()


def fingerprint(terms: Terms, ref: str) -> str:
    """Binds terms to the hold (book) or booking (cancel/reschedule) the approval is for."""
    return hashlib.sha256(f"{terms.fp()}|{ref}".encode()).hexdigest()


def terms_of(proposal: sqlite3.Row) -> Terms:
    return Terms(
        action=proposal["action"],
        provider_id=proposal["provider_id"],
        slot=proposal["slot"],
        price=proposal["price"],
        booking_id=proposal["booking_id"],
        new_slot=proposal["new_slot"],
    )


def diff_terms(approved: Terms, live: Terms) -> dict[str, tuple[Any, Any]]:
    a, b = approved.as_dict(), live.as_dict()
    return {k: (a[k], b[k]) for k in a if a[k] != b[k]}


@dataclass(frozen=True)
class LiveState:
    """What the executor just read from the source of truth."""

    terms: Terms
    ref: str


# --------------------------------------------------------------------------- reply classification

_APPROVE = frozenset(
    {"yes", "y", "yep", "ok", "okay", "sure", "confirm", "go ahead", "proceed", "do it"}
)
_ACTION_PHRASES: dict[str, frozenset[str]] = {
    "book": frozenset({"book it"}),
    "cancel": frozenset({"cancel it"}),
    "reschedule": frozenset({"move it", "reschedule it"}),
}
_DECLINE = frozenset({"no", "nope", "no thanks", "don't", "dont", "stop", "not now", "never mind", "nevermind"})


def _normalize(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"[.!]+$", "", t).strip()


def classify_reply(text: str, action: str, confirm_code: str) -> Reply:
    """Pure code. A reply approves only if, after normalisation, it EXACTLY equals an allowlisted
    affirmative, this action's phrase, or the displayed ``CONFIRM <code>``. Anything else,
    e.g. 'yes but make it 11', is not an approval."""
    t = _normalize(text)
    if t in _APPROVE or t in _ACTION_PHRASES.get(action, frozenset()) or t == f"confirm {confirm_code.lower()}":
        return "approve"
    if t in _DECLINE:
        return "decline"
    return "other"


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class Decision:
    """Result of feeding a user message to the guard."""

    kind: Literal["approved", "declined", "repeat", "expired", "other"]
    proposal_id: str | None = None
    approval_id: str | None = None


@dataclass(frozen=True)
class Validation:
    ok: bool
    reason: str | None = None
    diff: dict[str, tuple[Any, Any]] | None = None


# --------------------------------------------------------------------------- guard


class ApprovalGuard:
    def __init__(
        self,
        store: AgentStore,
        clock: Clock,
        *,
        approval_ttl_s: int = 180,
        proposal_ttl_s: int = 900,
    ) -> None:
        self.store = store
        self.clock = clock
        self.approval_ttl_s = approval_ttl_s
        self.proposal_ttl_s = proposal_ttl_s

    # ------------------------------------------------------------------ proposals

    def propose_booking(
        self, session_id: str, user_id: str, hold: Hold, *, parent_proposal_id: str | None = None
    ) -> str:
        """Terms come from the HOLD's quoted price, never from the search result."""
        terms = Terms("book", hold.provider_id, hold.slot, hold.quoted_price)
        return self._propose(session_id, user_id, terms, hold.hold_id, parent_proposal_id)

    def propose_cancel(self, session_id: str, user_id: str, booking: BookingInfo) -> str:
        terms = Terms("cancel", booking.provider_id, booking.slot, booking.price, booking_id=booking.booking_id)
        return self._propose(session_id, user_id, terms, booking.booking_id, None)

    def propose_reschedule(self, session_id: str, user_id: str, booking: BookingInfo, new_slot: str) -> str:
        terms = Terms(
            "reschedule", booking.provider_id, booking.slot, booking.price,
            booking_id=booking.booking_id, new_slot=new_slot,
        )
        return self._propose(session_id, user_id, terms, booking.booking_id, None)

    def _propose(
        self, session_id: str, user_id: str, terms: Terms, ref: str, parent: str | None
    ) -> str:
        now = self.clock.now()
        with self.store.tx() as c:
            self._supersede(c, session_id)
            pid = self.store.next_id(c, "prop")
            code = hashlib.sha256(pid.encode()).hexdigest()[:4].upper()
            c.execute(
                "INSERT INTO proposals(id, session_id, user_id, action, provider_id, slot, price, hold_id,"
                " booking_id, new_slot, terms_fp, fingerprint, confirm_code, parent_proposal_id, status,"
                " created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
                (
                    pid, session_id, user_id, terms.action, terms.provider_id, terms.slot, terms.price,
                    ref if terms.action == "book" else None, terms.booking_id, terms.new_slot,
                    terms.fp(), fingerprint(terms, ref), code, parent, iso(now),
                    iso(now + timedelta(seconds=self.proposal_ttl_s)),
                ),
            )
        return pid

    @staticmethod
    def _supersede(c: sqlite3.Connection, session_id: str) -> None:
        """A new proposal replaces anything still awaiting a decision or execution, and disarms
        the repeat window so a later 'yes' can only ever apply to the new proposal."""
        c.execute(
            "UPDATE approvals SET invalidated_reason = 'superseded' WHERE used_at IS NULL"
            " AND invalidated_reason IS NULL AND proposal_id IN"
            " (SELECT id FROM proposals WHERE session_id = ? AND status = 'approved')",
            (session_id,),
        )
        c.execute(
            "UPDATE proposals SET status = 'superseded' WHERE session_id = ? AND (status = 'pending' OR"
            " (status = 'approved' AND id IN (SELECT proposal_id FROM approvals WHERE used_at IS NULL)))",
            (session_id,),
        )
        c.execute("DELETE FROM repeat_window WHERE session_id = ?", (session_id,))

    # ------------------------------------------------------------------ user messages

    def on_user_message(self, session_id: str, text: str) -> Decision:
        """Feed EVERY raw user message here. The only path that creates an approval."""
        now = self.clock.now()
        with self.store.tx() as c:
            # The repeat window covers only the very next reply: read it and always disarm it.
            win = c.execute("SELECT proposal_id FROM repeat_window WHERE session_id = ?", (session_id,)).fetchone()
            c.execute("DELETE FROM repeat_window WHERE session_id = ?", (session_id,))

            pending = c.execute(
                "SELECT * FROM proposals WHERE session_id = ? AND status = 'pending'", (session_id,)
            ).fetchone()

            if pending is not None:
                reply = classify_reply(text, pending["action"], pending["confirm_code"])
                if now >= parse(pending["expires_at"]):
                    c.execute("UPDATE proposals SET status = 'expired' WHERE id = ?", (pending["id"],))
                    return Decision("expired" if reply == "approve" else "other", pending["id"])
                if reply == "approve":
                    return self._approve(c, pending, text, now)
                if reply == "decline":
                    c.execute("UPDATE proposals SET status = 'declined' WHERE id = ?", (pending["id"],))
                    return Decision("declined", pending["id"])
                return Decision("other", pending["id"])  # change request / anything else: still pending

            if win is not None:
                armed = c.execute("SELECT * FROM proposals WHERE id = ?", (win["proposal_id"],)).fetchone()
                if armed is not None and classify_reply(text, armed["action"], armed["confirm_code"]) == "approve":
                    appr = c.execute("SELECT id FROM approvals WHERE proposal_id = ?", (armed["id"],)).fetchone()
                    return Decision("repeat", armed["id"], appr["id"] if appr else None)
            return Decision("other")

    def _approve(self, c: sqlite3.Connection, proposal: sqlite3.Row, text: str, now: Any) -> Decision:
        cur = c.execute("UPDATE proposals SET status = 'approved' WHERE id = ? AND status = 'pending'", (proposal["id"],))
        assert cur.rowcount == 1
        ref = proposal["hold_id"] if proposal["action"] == "book" else proposal["booking_id"]
        aid = self.store.next_id(c, "apr")
        c.execute(
            "INSERT INTO approvals(id, proposal_id, ref, terms_fp, fingerprint, user_message, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                aid, proposal["id"], ref, proposal["terms_fp"], proposal["fingerprint"], text,
                iso(now), iso(now + timedelta(seconds=self.approval_ttl_s)),
            ),
        )
        # A second "yes" right after approving is a repeat, not a new approval.
        c.execute(
            "INSERT OR REPLACE INTO repeat_window(session_id, proposal_id, armed_at) VALUES (?,?,?)",
            (proposal["session_id"], proposal["id"], iso(now)),
        )
        return Decision("approved", proposal["id"], aid)

    def mark_result_reported(self, session_id: str, proposal_id: str) -> None:
        """Call when the user has just been told the outcome: the next reply may be a repeat."""
        with self.store.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO repeat_window(session_id, proposal_id, armed_at) VALUES (?,?,?)",
                (session_id, proposal_id, iso(self.clock.now())),
            )

    def approval_id_for(self, proposal_id: str) -> str | None:
        row = self.store.one("SELECT id FROM approvals WHERE proposal_id = ?", (proposal_id,))
        return row["id"] if row else None

    # ------------------------------------------------------------------ validation

    def validate(self, approval_id: str, live: LiveState | None = None) -> Validation:
        """Re-check an approval. With ``live`` (just read from the source of truth) it also compares
        the fingerprint, so a moved price or slot makes the approval invalid."""
        row = self.store.approval(approval_id)
        if row is None:
            return Validation(False, "unknown_approval")
        if row["invalidated_reason"]:
            return Validation(False, row["invalidated_reason"])
        if row["used_at"]:
            return Validation(False, "used")
        if self.clock.now() >= parse(row["expires_at"]):
            return Validation(False, "expired")
        if live is not None and fingerprint(live.terms, live.ref) != row["fingerprint"]:
            approved = terms_of(self.store.proposal(row["proposal_id"]))  # type: ignore[arg-type]
            diff = diff_terms(approved, live.terms)
            if live.ref != row["ref"]:
                diff["ref"] = (row["ref"], live.ref)
            return Validation(False, "fingerprint_mismatch", diff)
        return Validation(True)

    def invalidate(self, approval_id: str, reason: str) -> None:
        with self.store.tx() as c:
            c.execute(
                "UPDATE approvals SET invalidated_reason = ? WHERE id = ? AND invalidated_reason IS NULL",
                (reason, approval_id),
            )
            c.execute(
                "UPDATE proposals SET status = 'invalidated' WHERE status = 'approved' AND id ="
                " (SELECT proposal_id FROM approvals WHERE id = ?)",
                (approval_id,),
            )

    # ------------------------------------------------------------------ in-transaction switches (executor)

    def consume(self, c: sqlite3.Connection, approval_id: str) -> str | None:
        """Flip ``used_at`` exactly once. None on success, else the refusal reason."""
        cur = c.execute(
            "UPDATE approvals SET used_at = ? WHERE id = ? AND used_at IS NULL"
            " AND invalidated_reason IS NULL AND expires_at > ?",
            (iso(self.clock.now()), approval_id, iso(self.clock.now())),
        )
        if cur.rowcount == 1:
            return None
        row = c.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return "unknown_approval"
        if row["invalidated_reason"]:
            return row["invalidated_reason"]
        if row["used_at"]:
            return "used"
        return "expired"

    def rebind(
        self, c: sqlite3.Connection, approval_id: str, new_hold: Hold
    ) -> tuple[str | None, dict[str, tuple[Any, Any]] | None]:
        """Move a CONSUMED approval to a replacement hold. At most once per approval, only inside
        the approval window, only when provider, slot and price are exactly the approved ones
        (one rupee off is a different price). Returns (refusal_reason, diff) or (None, None)."""
        row = c.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return "unknown_approval", None
        prop = c.execute("SELECT * FROM proposals WHERE id = ?", (row["proposal_id"],)).fetchone()
        if prop["action"] != "book":
            return "not_rebindable", None
        if row["invalidated_reason"]:
            return row["invalidated_reason"], None
        if row["rebind_count"] >= 1:
            return "rebind_limit", None
        if self.clock.now() >= parse(row["expires_at"]):
            return "approval_window_closed", None
        approved = terms_of(prop)
        live = Terms("book", new_hold.provider_id, new_hold.slot, new_hold.quoted_price)
        if live.fp() != approved.fp():
            return "terms_changed", diff_terms(approved, live)
        new_fp = fingerprint(live, new_hold.hold_id)
        cur = c.execute(
            "UPDATE approvals SET rebind_count = 1, ref = ?, fingerprint = ? WHERE id = ?"
            " AND used_at IS NOT NULL AND rebind_count = 0 AND invalidated_reason IS NULL",
            (new_hold.hold_id, new_fp, approval_id),
        )
        if cur.rowcount != 1:
            return "rebind_limit", None
        c.execute(
            "INSERT INTO approval_rebinds(approval_id, old_ref, new_ref, old_fingerprint, new_fingerprint,"
            " terms_fp, at) VALUES (?,?,?,?,?,?,?)",
            (approval_id, row["ref"], new_hold.hold_id, row["fingerprint"], new_fp, approved.fp(), iso(self.clock.now())),
        )
        return None, None
