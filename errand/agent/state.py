"""Graph state. JSON-only values so every checkpoint serialises and survives a restart."""

from __future__ import annotations

from typing import Any, TypedDict


class ErrandState(TypedDict, total=False):
    session_id: str
    user_id: str
    run_id: str
    user_message: str
    decision: dict[str, Any]  # what the guard decided about this message (code, before the model)
    approval_id: str | None
    notes: list[str]  # short confirmations from `capture` (memories saved from this message), appended by finalize
    request: dict[str, Any]  # facts collected so far: service, area, date_range, time_window, max_price ...
    options: list[dict[str, Any]]  # compact result of the last search
    pending: dict[str, Any] | None  # summary of the proposal awaiting approval
    proposal_request: dict[str, Any] | None  # what the model proposed; consumed by `hold`
    outcome: dict[str, Any] | None  # executor outcome
    exit: dict[str, Any] | None  # normalised exit, input to finalize
    final: dict[str, Any] | None
    waiting: str | None  # "approval" | "input" | None
