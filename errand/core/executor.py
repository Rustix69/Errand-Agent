"""The idempotent executor: the ONLY code that calls book / cancel / reschedule.

``execute(approval_id)`` takes nothing but an approval id. Every tool argument is read from
the stored proposal/approval, so nothing the model says can reach a mutating tool.

Order of operations for a fresh approval:
  1. validate the stored approval (window, single use, not superseded),
  2. read live state and compare its fingerprint to the approval (price/slot/hold moved => stop),
  3. ONE transaction: consume the approval (used_at) + insert the PENDING attempt row,
  4. only then call the tool, with the attempt's deterministic idempotency key,
  5. resolve every ambiguous outcome through the source of truth; never claim success unverified.

``execute`` is a resumable state machine keyed by the approval, so a repeated confirmation, a
re-run of the LangGraph ``execute`` node or a restart all converge on the same single result.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from errand.clock import Clock
from errand.contract.errors import ErrorCode, ToolError
from errand.contract.models import Hold
from errand.contract.port import ToolPort
from errand.core.approval import ApprovalGuard, LiveState, Terms
from errand.core.budget import Budget, BudgetExhausted
from errand.core.store import AgentStore
from errand.core.truth import Emit, Limits, Reader, Truth, query_truth


@dataclass
class ExecutionOutcome:
    kind: str
    approval_id: str
    proposal_id: str | None = None
    attempt_key: str | None = None
    booking: dict[str, Any] | None = None  # verified booking info (confirmed / already_done)
    reason: str | None = None
    diff: dict[str, Any] | None = None
    new_proposal_id: str | None = None  # a fresh proposal the executor created (needs_reapproval)
    tool_calls: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hooks:
    """Test hooks for crash injection. All optional; production passes none."""

    before_tool_call: Callable[[sqlite3.Row], None] | None = None  # attempt committed, tool not yet called
    before_rebind: Callable[[sqlite3.Row], None] | None = None  # seq 0 verified-FAILED, seq 1 not inserted
    in_consume_tx: Callable[[sqlite3.Connection], None] | None = None  # inside the consume+insert tx


class IdempotentExecutor:
    def __init__(
        self,
        store: AgentStore,
        guard: ApprovalGuard,
        tools: ToolPort,
        clock: Clock,
        user_id: str,
        *,
        limits: Limits | None = None,
        hooks: Hooks | None = None,
        on_event: Emit | None = None,
    ) -> None:
        self.store, self.guard, self.tools, self.clock, self.user_id = store, guard, tools, clock, user_id
        self.limits = limits or Limits()
        self.hooks = hooks or Hooks()
        self.emit: Emit = on_event or (lambda name, data: None)

    # ================================================================== public

    def execute(self, approval_id: str, budget: Budget | None = None) -> ExecutionOutcome:
        budget = budget or Budget(self.clock)
        self.emit("execute_start", {"approval_id": approval_id})
        try:
            out = self._execute(approval_id, budget)
        except BudgetExhausted as e:
            out = ExecutionOutcome("limit_reached", approval_id, reason=e.reason)
            appr = self.store.approval(approval_id)
            out.proposal_id = appr["proposal_id"] if appr else None
        out.tool_calls = budget.tool_calls
        self.emit("execute_outcome", {"approval_id": approval_id, "kind": out.kind, "reason": out.reason})
        return out

    # ================================================================== dispatch

    def _execute(self, approval_id: str, budget: Budget) -> ExecutionOutcome:
        appr = self.store.approval(approval_id)
        if appr is None:
            return ExecutionOutcome("approval_invalid", approval_id, reason="unknown_approval")
        prop = self.store.proposal(appr["proposal_id"])
        assert prop is not None
        terms = Terms(
            prop["action"], prop["provider_id"], prop["slot"], prop["price"], prop["booking_id"], prop["new_slot"]
        )
        read = Reader(self.tools, self.clock, self.limits, budget, self.emit)
        att = self.store.latest_attempt(approval_id)
        ctx = _Ctx(appr["id"], prop, terms, budget, read)
        if att is None:
            return self._fresh(ctx)
        return self._resume(ctx, att)

    def _resume(self, ctx: "_Ctx", att: sqlite3.Row) -> ExecutionOutcome:
        status, code = att["status"], att["error_code"]
        if status == "CONFIRMED":
            booking = json.loads(att["result_json"]) if att["result_json"] else None
            return self._out(ctx, "already_done", att, booking=booking)
        if status == "PENDING":
            if code == ErrorCode.HOLD_EXPIRED:
                return self._after_hold_expired(ctx, att)
            truth = self._truth_or_none(ctx, att)
            if truth is None:
                return self._out(ctx, "unknown", att, reason="source_of_truth_unavailable")
            if truth.state == "done":
                return self._confirm(ctx, att, truth)
            if truth.state == "not_done":
                # The old approval was consumed; we never re-book on resume. The caller asks again.
                self.store.update_attempt(att["idempotency_key"], status="NOT_EXECUTED", verified=True)
                self.store.set_proposal_status(ctx.prop["id"], "failed")
                return self._out(ctx, "needs_reapproval", att, reason="not_executed")
            return self._out(ctx, "unknown", att, reason=truth.detail)
        if status == "FAILED" and code == ErrorCode.HOLD_EXPIRED and att["verified_at"]:
            return self._rebind_flow(ctx, att)
        return self._stored_outcome(ctx, att)

    def _stored_outcome(self, ctx: "_Ctx", att: sqlite3.Row) -> ExecutionOutcome:
        code = att["error_code"]
        if att["status"] == "NOT_EXECUTED":
            return self._out(ctx, "needs_reapproval", att, reason="not_executed")
        kind = {ErrorCode.SLOT_TAKEN: "slot_taken", ErrorCode.SLOT_CONFLICT: "slot_conflict"}.get(code, "failed")
        return self._out(ctx, kind, att, reason=code)

    # ================================================================== fresh approval

    def _fresh(self, ctx: "_Ctx") -> ExecutionOutcome:
        v = self.guard.validate(ctx.approval_id)
        if not v.ok:
            return self._invalid(ctx, v.reason or "invalid", v.diff)

        # --- live state, read from the source of truth (read-only) ---------------------------
        try:
            live, hold_state = self._read_live(ctx)
        except _Stop as stop:
            return stop.outcome
        v = self.guard.validate(ctx.approval_id, live)
        if not v.ok:
            return self._invalid(ctx, v.reason or "invalid", v.diff)

        # --- consume the approval + write the PENDING attempt, atomically ---------------------
        key = f"{ctx.terms.action}-{ctx.approval_id}-0"
        expired = hold_state == "expired"
        reason = self._consume_and_open(
            ctx, key=key, seq=0, ref=live.ref, error_code=ErrorCode.HOLD_EXPIRED if expired else None
        )
        if reason is not None:
            return self._invalid(ctx, reason, None)
        att = self.store.one("SELECT * FROM booking_attempts WHERE idempotency_key = ?", (key,))
        assert att is not None
        self.emit("attempt_pending", {"key": key, "seq": 0, "action": ctx.terms.action, "ref": live.ref})
        if expired:
            return self._after_hold_expired(ctx, att)
        return self._call_and_resolve(ctx, att)

    def _read_live(self, ctx: "_Ctx") -> tuple[LiveState, str | None]:
        t = ctx.terms
        if t.action == "book":
            appr = self.store.approval(ctx.approval_id)
            assert appr is not None
            try:
                st = ctx.read("get_booking_status", hold_id=appr["ref"])
            except ToolError as e:
                if e.code == ErrorCode.HOLD_NOT_FOUND:
                    raise _Stop(self._invalid(ctx, "hold_not_found", None))
                raise _Stop(self._out(ctx, "failed", None, reason="precheck_unavailable"))
            hold = st.hold
            if hold is None:
                raise _Stop(self._invalid(ctx, "hold_not_found", None))
            if hold.status == "booked":
                raise _Stop(self._invalid(ctx, "hold_already_booked", None))
            if hold.status == "released":
                self.guard.invalidate(ctx.approval_id, "slot_taken")
                raise _Stop(self._out(ctx, "slot_taken", None, reason="hold_released"))
            live = Terms("book", hold.provider_id, hold.slot, hold.quoted_price)
            return LiveState(live, hold.hold_id), hold.status

        try:
            lb = ctx.read("list_bookings", user_id=self.user_id)
        except ToolError:
            raise _Stop(self._out(ctx, "failed", None, reason="precheck_unavailable"))
        b = next((x for x in lb.bookings if x.booking_id == t.booking_id), None)
        if b is None:
            raise _Stop(self._invalid(ctx, "booking_not_found", None))
        if b.status != "confirmed":
            raise _Stop(self._invalid(ctx, "booking_not_active", {"status": (t.action, b.status)}))
        live = Terms(t.action, b.provider_id, b.slot, b.price, booking_id=b.booking_id, new_slot=t.new_slot)
        return LiveState(live, b.booking_id), None

    def _consume_and_open(
        self, ctx: "_Ctx", *, key: str, seq: int, ref: str, error_code: str | None
    ) -> str | None:
        """Step 3: one transaction. Returns a refusal reason, or None when committed."""
        with self.store.tx() as c:
            reason = self.guard.consume(c, ctx.approval_id)
            if reason is not None:
                return reason
            if self.hooks.in_consume_tx:
                self.hooks.in_consume_tx(c)
            self.store.insert_attempt(
                c, key=key, approval_id=ctx.approval_id, seq=seq, action=ctx.terms.action, ref=ref,
                new_slot=ctx.terms.new_slot, now=self.clock.now(), error_code=error_code,
            )
        return None

    # ================================================================== call + resolve

    def _call_and_resolve(self, ctx: "_Ctx", att: sqlite3.Row) -> ExecutionOutcome:
        if self.hooks.before_tool_call:
            self.hooks.before_tool_call(att)
        key = att["idempotency_key"]
        tries = 0
        while True:
            tries += 1
            try:
                self._mutate(ctx, att)
            except ToolError as e:
                self.emit("tool_error", {"tool": ctx.terms.action, "code": e.code, "retryable": e.retryable})
                if e.code == ErrorCode.HOLD_EXPIRED and ctx.terms.action == "book":
                    return self._after_hold_expired(ctx, att)
                if e.code == ErrorCode.SLOT_TAKEN:
                    return self._fail(ctx, att, "slot_taken", ErrorCode.SLOT_TAKEN)
                if e.code == ErrorCode.SLOT_CONFLICT:
                    return self._fail(ctx, att, "slot_conflict", ErrorCode.SLOT_CONFLICT)
                if not e.retryable:
                    return self._fail(ctx, att, "failed", e.code)
                # Timeout or 503: the side effect MAY have happened. Ask the source of truth first.
                truth = self._truth_or_none(ctx, att)
                if truth is None:
                    return self._out(ctx, "unknown", att, reason="source_of_truth_unavailable")
                if truth.state == "done":
                    return self._confirm(ctx, att, truth)
                if truth.state == "diverged":
                    return self._out(ctx, "unknown", att, reason=truth.detail)
                if tries >= self.limits.max_attempts:
                    return self._fail(ctx, att, "failed", "retries_exhausted")
                self.clock.sleep(self.limits.backoff(tries - 1))
                continue  # same key: replays if it actually committed, so it cannot double-book
            # The tool said success. Never trust that: verify.
            truth = self._truth_or_none(ctx, att)
            if truth is None:
                return self._out(ctx, "unknown", att, reason="verification_unavailable")
            if truth.state == "done":
                return self._confirm(ctx, att, truth)
            return self._out(ctx, "unknown", att, reason=truth.detail or "verification_failed")

    def _mutate(self, ctx: "_Ctx", att: sqlite3.Row) -> Any:
        """The only place a mutating tool is called. Budget is charged first; the attempt's call
        counter is bumped BEFORE the call so a crash mid-call still shows it was made."""
        ctx.budget.charge_tool()
        key = att["idempotency_key"]
        self.store.update_attempt(key, add_tool_calls=1)
        action = att["action"]
        args: dict[str, Any]
        if action == "book":
            args = {"hold_id": att["ref"], "idempotency_key": key}
        elif action == "cancel":
            args = {"booking_id": att["ref"], "idempotency_key": key}
        else:
            args = {"booking_id": att["ref"], "slot": att["new_slot"], "idempotency_key": key}
        self.emit("tool_call", {"tool": action, "args": args})
        result = getattr(self.tools, action)(**args)
        self.emit("tool_result", {"tool": action})
        return result

    def _truth_or_none(self, ctx: "_Ctx", att: sqlite3.Row) -> Truth | None:
        try:
            return query_truth(ctx.read, self.user_id, att, ctx.terms)
        except ToolError:
            return None

    # ================================================================== hold_expired gate + rebind

    def _after_hold_expired(self, ctx: "_Ctx", att: sqlite3.Row) -> ExecutionOutcome:
        """hold_expired is NOT trusted to mean 'nothing booked': confirm through the source of truth
        before a replacement is even considered."""
        key = att["idempotency_key"]
        if att["error_code"] != ErrorCode.HOLD_EXPIRED:
            self.store.update_attempt(key, error_code=ErrorCode.HOLD_EXPIRED)  # stays PENDING
        truth = self._truth_or_none(ctx, att)
        if truth is None:
            return self._out(ctx, "unknown", att, reason="hold_expired_unverified")
        if truth.state == "done":
            return self._confirm(ctx, att, truth)
        if truth.state == "diverged":
            return self._out(ctx, "unknown", att, reason=truth.detail)
        self.store.update_attempt(key, status="FAILED", error_code=ErrorCode.HOLD_EXPIRED, verified=True)
        self.emit("attempt_failed", {"key": key, "code": ErrorCode.HOLD_EXPIRED, "verified": True})
        att = self.store.one("SELECT * FROM booking_attempts WHERE idempotency_key = ?", (key,))  # type: ignore[assignment]
        if self.hooks.before_rebind:
            self.hooks.before_rebind(att)
        return self._rebind_flow(ctx, att)

    def _rebind_flow(self, ctx: "_Ctx", att0: sqlite3.Row) -> ExecutionOutcome:
        """Re-hold, then rebind the SAME approval to the new hold iff nothing material changed."""
        child = self.store.child_proposal(ctx.prop["id"])
        if child is not None:
            return self._out(ctx, "needs_reapproval", att0, reason="reapproval_pending", new_proposal_id=child["id"])
        t = ctx.terms
        try:
            hold: Hold = ctx.read("hold_slot", provider_id=t.provider_id, slot=t.slot)
        except ToolError as e:
            if e.code == ErrorCode.SLOT_UNAVAILABLE:
                self.store.set_proposal_status(ctx.prop["id"], "failed")
                return self._out(ctx, "slot_taken", att0, reason="slot_gone_on_rehold")
            return self._out(ctx, "failed", att0, reason="rehold_unavailable")

        refusal: str | None
        diff: dict[str, Any] | None = None
        new_key = f"{t.action}-{ctx.approval_id}-1"
        with self.store.tx() as c:
            rows = c.execute(
                "SELECT * FROM booking_attempts WHERE approval_id = ? ORDER BY seq", (ctx.approval_id,)
            ).fetchall()
            if any(r["seq"] == 1 for r in rows):
                refusal = "rebind_limit"
            elif not (rows and rows[0]["status"] == "FAILED" and rows[0]["error_code"] == ErrorCode.HOLD_EXPIRED
                      and rows[0]["verified_at"]):
                refusal = "seq0_not_verified"
            else:
                refusal, diff = self.guard.rebind(c, ctx.approval_id, hold)
                if refusal is None:
                    self.store.insert_attempt(
                        c, key=new_key, approval_id=ctx.approval_id, seq=1, action=t.action,
                        ref=hold.hold_id, new_slot=t.new_slot, now=self.clock.now(),
                    )

        if refusal is None:
            self.emit("approval_rebound", {"approval_id": ctx.approval_id, "new_ref": hold.hold_id})
            att1 = self.store.one("SELECT * FROM booking_attempts WHERE idempotency_key = ?", (new_key,))
            assert att1 is not None
            self.emit("attempt_pending", {"key": new_key, "seq": 1, "action": t.action, "ref": hold.hold_id})
            return self._call_and_resolve(ctx, att1)

        # Anything material differs (or the one rebind is spent): ask the user again, with the new quote.
        new_pid = self.guard.propose_booking(
            ctx.prop["session_id"], ctx.prop["user_id"], hold, parent_proposal_id=ctx.prop["id"]
        )
        self.store.set_proposal_status(ctx.prop["id"], "invalidated")
        return self._out(ctx, "needs_reapproval", att0, reason=refusal, diff=diff, new_proposal_id=new_pid)

    # ================================================================== finishing

    def _confirm(self, ctx: "_Ctx", att: sqlite3.Row, truth: Truth) -> ExecutionOutcome:
        booking = truth.booking.model_dump(mode="json") if truth.booking else None
        self.store.update_attempt(
            att["idempotency_key"], status="CONFIRMED", result=booking, verified=True, clear_error=True
        )
        self.store.set_proposal_status(ctx.prop["id"], "executed")
        self.emit("attempt_confirmed", {"key": att["idempotency_key"]})
        return self._out(ctx, "confirmed", att, booking=booking)

    def _fail(self, ctx: "_Ctx", att: sqlite3.Row, kind: str, code: str) -> ExecutionOutcome:
        self.store.update_attempt(att["idempotency_key"], status="FAILED", error_code=code)
        self.store.set_proposal_status(ctx.prop["id"], "failed")
        self.emit("attempt_failed", {"key": att["idempotency_key"], "code": code})
        return self._out(ctx, kind, att, reason=code)

    def _invalid(self, ctx: "_Ctx", reason: str, diff: dict[str, Any] | None) -> ExecutionOutcome:
        if reason not in ("used", "unknown_approval"):
            self.guard.invalidate(ctx.approval_id, reason)
        self.emit("approval_invalid", {"approval_id": ctx.approval_id, "reason": reason})
        return ExecutionOutcome(
            "approval_invalid", ctx.approval_id, ctx.prop["id"], reason=reason, diff=diff
        )

    def _out(
        self, ctx: "_Ctx", kind: str, att: sqlite3.Row | None, *, booking: dict[str, Any] | None = None,
        reason: str | None = None, diff: dict[str, Any] | None = None, new_proposal_id: str | None = None,
    ) -> ExecutionOutcome:
        return ExecutionOutcome(
            kind, ctx.approval_id, ctx.prop["id"], att["idempotency_key"] if att else None,
            booking=booking, reason=reason, diff=diff, new_proposal_id=new_proposal_id,
        )


@dataclass
class _Ctx:
    approval_id: str
    prop: sqlite3.Row
    terms: Terms
    budget: Budget
    read: Reader


class _Stop(Exception):
    """Internal early exit carrying the outcome to return."""

    def __init__(self, outcome: ExecutionOutcome) -> None:
        self.outcome = outcome
