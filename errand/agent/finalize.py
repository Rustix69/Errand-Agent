"""The ONE place a user-facing message is produced, for every exit path.

Pure code, no model. Success text is built only from a verified booking record, and the approval
request is built from the stored proposal terms, so nothing the model says can change what the user
is told was done or is asked to approve. An unmapped kind still produces a message (TOOL_FAILURE).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["DONE", "NEEDS_INPUT", "LIMIT_REACHED", "TOOL_FAILURE", "DECLINED"]
Waiting = Literal["approval", "input"]

STATUS_BY_KIND: dict[str, Status] = {
    # executor outcomes
    "confirmed": "DONE", "already_done": "DONE",
    "approval_invalid": "NEEDS_INPUT", "needs_reapproval": "NEEDS_INPUT",
    "slot_taken": "NEEDS_INPUT", "slot_conflict": "NEEDS_INPUT",
    "failed": "TOOL_FAILURE", "unknown": "TOOL_FAILURE", "limit_reached": "LIMIT_REACHED",
    # graph exits
    "declined": "DECLINED", "expired": "NEEDS_INPUT", "proposal_ready": "NEEDS_INPUT",
    "ask": "NEEDS_INPUT", "answer": "DONE", "limit": "LIMIT_REACHED",
    "slot_unavailable": "NEEDS_INPUT", "booking_not_found": "NEEDS_INPUT",
    "hold_failed": "TOOL_FAILURE", "model_error": "TOOL_FAILURE", "unexpected": "TOOL_FAILURE",
    # memory commands (handled by code; the model is not involved)
    "memory_listed": "DONE", "memory_saved": "DONE", "memory_corrected": "DONE", "memory_forgotten": "DONE",
    "memory_confirmed": "DONE", "memory_rejected": "DONE", "memory_unchanged": "DONE",
    "memory_not_found": "NEEDS_INPUT", "memory_ambiguous": "NEEDS_INPUT", "memory_error": "TOOL_FAILURE",
}


@dataclass
class Exit:
    kind: str
    reason: str | None = None
    action: str | None = None  # book | cancel | reschedule
    booking: dict[str, Any] | None = None  # VERIFIED booking info (confirmed / already_done)
    diff: dict[str, Any] | None = None
    proposal: dict[str, Any] | None = None  # summary of the proposal awaiting approval
    proposal_id: str | None = None
    text: str | None = None  # model's own text (answer / ask only)
    missing: list[str] = field(default_factory=list)
    memory: dict[str, Any] | None = None  # data for memory_* kinds
    notes: list[str] = field(default_factory=list)  # appended to the message (memories saved from this turn)


@dataclass
class Final:
    status: Status
    kind: str
    message: str
    waiting: Waiting | None = None
    proposal_id: str | None = None
    reason: str | None = None


_REASONS = {
    "fingerprint_mismatch": "the price or slot changed after you approved",
    "expired": "the approval expired",
    "used": "that approval was already used",
    "superseded": "a newer request replaced it",
    "slot_taken": "someone else took that slot",
    "hold_not_found": "the held slot no longer exists",
    "hold_already_booked": "that hold was already booked",
    "booking_not_found": "I can't find that booking",
    "booking_not_active": "that booking is no longer active",
    "unknown_approval": "I have no matching approval",
}
_LIMITS = {"model_turns": "model-turn", "tool_calls": "tool-call", "wall_clock": "time", "context_budget": "context-size"}


# A model reply is not a source of truth. If its text CLAIMS an action happened, code adds the real status.
_CLAIM = re.compile(r"\b(booked|cancell?ed|rescheduled|confirmed)\b", re.I)
_UNVERIFIED = " (Nothing was booked, cancelled or changed in this step. Only a verified result counts.)"


def _inr(n: Any) -> str:
    return f"₹{n:,}" if isinstance(n, int) else f"₹{n}"


def _diff_text(diff: dict[str, Any] | None) -> str:
    if not diff:
        return ""
    parts = []
    for k, v in diff.items():
        a, b = (v[0], v[1]) if isinstance(v, (list, tuple)) and len(v) == 2 else (None, v)
        parts.append(f"{k}: {_inr(a) if k == 'price' else a} -> {_inr(b) if k == 'price' else b}")
    return " (" + "; ".join(parts) + ")"


def describe_proposal(p: dict[str, Any]) -> str:
    who = p.get("provider_name") or p["provider_id"]
    if p["action"] == "book":
        note = ""
        if p.get("price_changed"):
            note = f" The price changed from {_inr(p.get('search_price'))} when I checked availability."
        return f"Book {who} on {p['slot']} for {_inr(p['price'])}.{note}"
    if p["action"] == "cancel":
        return f"Cancel your booking {p['booking_id']} with {who} on {p['slot']} ({_inr(p['price'])})."
    return f"Move your booking {p['booking_id']} with {who} from {p['slot']} to {p['new_slot']}."


def _approval_request(p: dict[str, Any], lead: str = "") -> str:
    return (f"{lead}{describe_proposal(p)} Nothing happens until you approve: reply 'yes' "
            f"(or CONFIRM {p['confirm_code']}), or tell me what to change.")


def _done(e: Exit) -> str:
    b = e.booking or {}
    who = b.get("provider_name") or b.get("provider_id", "the provider")
    again = " Nothing was run again." if e.kind == "already_done" else ""
    if e.action == "cancel" or b.get("status") == "cancelled":
        return f"Cancelled: booking {b.get('booking_id')} with {who} is confirmed cancelled.{again}"
    if e.action == "reschedule":
        return f"Rescheduled: booking {b.get('booking_id')} with {who} is now {b.get('slot')}.{again}"
    return (f"Booked and verified: {who} on {b.get('slot')} for {_inr(b.get('price'))}. "
            f"Booking ID {b.get('booking_id')}.{again}")


def _mem_line(m: dict[str, Any]) -> str:
    return f"- {m['id']} {m['key']}: {m['value']}"


def _memory_message(kind: str, d: dict[str, Any]) -> str:
    if kind == "memory_listed":
        if not d["current"] and not d["awaiting"]:
            return "I have nothing saved about you yet."
        lines = ["Here is what I have saved about you:", *[_mem_line(m) for m in d["current"]]]
        if d["awaiting"]:
            lines += ["", "Guesses of mine that I am NOT using until you confirm them "
                      "(say 'confirm <id>' to keep or 'reject <id>' to drop):", *[_mem_line(m) for m in d["awaiting"]]]
        if d["forgotten"]:
            lines += ["", f"{d['forgotten']} item(s) you asked me to forget are kept out of use."]
        return "\n".join(lines)
    m = d.get("memory") or {}
    what = f"{m.get('key')} = {m.get('value')} ({m.get('id')})"
    return {
        "memory_saved": f"Saved: {what}. Say 'forget {m.get('key')}' to remove it.",
        "memory_unchanged": f"I already have that saved: {what}.",
        "memory_corrected": f"Updated: {what}. The old value is no longer used.",
        "memory_forgotten": f"Forgotten: {m.get('key')} ({', '.join(d.get('ids', []))}). I will not use it again.",
        "memory_confirmed": f"Confirmed: {what}. I can use it now.",
        "memory_rejected": f"Dropped {m.get('key')} ({m.get('id')}). I will not use it.",
        "memory_not_found": f"I couldn't find a saved memory matching '{d.get('target')}'. "
                            "Say 'what do you remember' to see the list.",
        "memory_ambiguous": "More than one saved memory matches. Which one?\n"
                            + "\n".join(_mem_line(c) for c in d.get("candidates", [])),
    }[kind]


def finalize(e: Exit) -> Final:
    kind = e.kind
    status = STATUS_BY_KIND.get(kind)
    waiting: Waiting | None = None
    claim: str | None = None
    if status is None:
        return Final("TOOL_FAILURE", kind, "Something unexpected happened and I stopped. Nothing was changed "
                     "without your approval.", None, e.proposal_id, "unmapped_kind")

    if kind in ("confirmed", "already_done"):
        if not e.booking:  # never claim success without a verified record
            return Final("TOOL_FAILURE", "unknown", "I could not verify the result, so I am not claiming it "
                         "succeeded. I will not retry blindly.", None, e.proposal_id, "unverified_success")
        msg = _done(e)
    elif kind == "approval_invalid":
        why = _REASONS.get(e.reason or "", e.reason or "it is no longer valid")
        msg = f"I did not go ahead: {why}{_diff_text(e.diff)}. Nothing was changed. Want me to set it up again?"
        waiting = "input"
    elif kind == "needs_reapproval":
        if e.proposal:
            msg = _approval_request(e.proposal, "The terms changed, so I need a fresh approval. ")
            waiting = "approval"
        else:
            msg = "Nothing was booked (the earlier attempt did not go through). Want me to try again?"
            waiting = "input"
    elif kind in ("slot_taken", "slot_conflict"):
        what = "taken by someone else" if kind == "slot_taken" else "not available for rescheduling"
        msg = f"That slot is {what}. Nothing was changed. Want me to look at other times?"
        waiting = "input"
    elif kind == "failed":
        msg = f"I could not complete that ({e.reason or 'tool error'}). No change was made."
    elif kind == "unknown":
        msg = ("I could not confirm whether that went through, so I am NOT claiming success and will not retry "
               "blindly. I will check its status before doing anything else.")
    elif kind in ("limit_reached", "limit"):
        which = _LIMITS.get(e.reason or "", e.reason or "work")
        msg = (f"I hit my {which} limit for this message and stopped. Anything half-done is checked against the "
               "provider's records before any retry, so nothing will be duplicated.")
    elif kind == "declined":
        msg = "Okay, I won't do that. Nothing was changed."
    elif kind == "expired":
        msg = "That request timed out before approval, so nothing was done. Want me to set it up again?"
        waiting = "input"
    elif kind == "proposal_ready":
        assert e.proposal is not None
        msg, waiting = _approval_request(e.proposal), "approval"
    elif kind == "ask":
        msg, waiting = e.text or "What details can you share?", "input"
    elif kind == "answer":
        msg = e.text or "Is there anything else you'd like to arrange?"
        if _CLAIM.search(msg):  # an `answer` never follows a verified outcome: a claim in it is unverified
            msg, claim = msg + _UNVERIFIED, "unverified_claim"
    elif kind == "slot_unavailable":
        msg, waiting = "That option can't be held any more. Want me to search again?", "input"
    elif kind == "booking_not_found":
        msg, waiting = "I can't find that booking in your list. Which one did you mean?", "input"
    elif kind.startswith("memory_") and kind != "memory_error":
        msg = _memory_message(kind, e.memory or {})
        waiting = "input" if kind in ("memory_not_found", "memory_ambiguous") else None
    elif kind == "hold_failed":
        msg = "I couldn't reserve that slot because the provider service is unavailable. Nothing was booked."
    else:  # model_error / unexpected
        msg = "I ran into an unexpected problem and stopped. Nothing was booked or changed."
    # The reply points at the proposal the user is being asked about (a fresh one after a re-quote).
    pid = e.proposal["proposal_id"] if (waiting == "approval" and e.proposal) else e.proposal_id
    if e.notes:
        msg = f"{msg} {' '.join(e.notes)}"
    return Final(status, kind, msg, waiting, pid, claim or e.reason)
