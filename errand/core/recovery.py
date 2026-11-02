"""Startup recovery: resolve every PENDING attempt through the source of truth.

Never calls book / cancel / reschedule (it only holds a ``Reader``, which refuses them), so
a restart can never double-book. An attempt it cannot resolve stays PENDING.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from errand.clock import Clock
from errand.contract.errors import ErrorCode, ToolError
from errand.contract.port import ToolPort
from errand.core.store import AgentStore
from errand.core.truth import Emit, Limits, Reader, attempt_terms, query_truth


@dataclass(frozen=True)
class Resolution:
    key: str
    action: str
    result: Literal["confirmed", "not_executed", "hold_expired", "unknown"]
    detail: str = ""


def resolve_pending(
    store: AgentStore,
    tools: ToolPort,
    user_id: str,
    clock: Clock,
    *,
    limits: Limits | None = None,
    on_event: Emit | None = None,
) -> list[Resolution]:
    emit: Emit = on_event or (lambda name, data: None)
    read = Reader(tools, clock, limits or Limits(), None, emit)
    out: list[Resolution] = []
    for att in store.pending_attempts():
        key = att["idempotency_key"]
        terms = attempt_terms(store, att)
        appr = store.approval(att["approval_id"])
        assert appr is not None
        try:
            truth = query_truth(read, user_id, att, terms)
        except ToolError as e:
            out.append(Resolution(key, att["action"], "unknown", f"source_of_truth_unavailable:{e.code}"))
            continue

        if truth.state == "done":
            booking = truth.booking.model_dump(mode="json") if truth.booking else None
            store.update_attempt(key, status="CONFIRMED", result=booking, verified=True, clear_error=True)
            store.set_proposal_status(appr["proposal_id"], "executed")
            out.append(Resolution(key, att["action"], "confirmed"))
        elif truth.state == "not_done":
            if att["error_code"] == ErrorCode.HOLD_EXPIRED:
                # Verified not booked; `execute` may still rebind within the approval window.
                store.update_attempt(key, status="FAILED", error_code=ErrorCode.HOLD_EXPIRED, verified=True)
                out.append(Resolution(key, att["action"], "hold_expired"))
            else:
                store.update_attempt(key, status="NOT_EXECUTED", verified=True)
                store.set_proposal_status(appr["proposal_id"], "failed")
                out.append(Resolution(key, att["action"], "not_executed"))
        else:
            out.append(Resolution(key, att["action"], "unknown", truth.detail))
        emit("recovery_resolved", {"key": key, "result": out[-1].result})
    return out
