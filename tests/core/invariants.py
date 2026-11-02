"""Invariants that must hold after ANY run (reused by later phases).

1. A mutating tool call implies a valid approval: its idempotency key belongs to an attempt whose
   approval was created from a user message and consumed (used_at set) by the executor.
2. At most one booking per idempotency key.
3. At most one booking per approval, across all of its keys (seq 0 and seq 1).
4. Every CONFIRMED attempt was verified against the source of truth.
5. An approval is used at most once: one live-or-confirmed attempt per approval.
"""

from __future__ import annotations

from collections import Counter

from errand.contract.port import MUTATING_TOOLS
from tests.core.helpers import Env


def assert_invariants(env: Env) -> None:
    store = env.store

    # 1. no mutating call without a consumed approval
    for call in env.calls():
        if call["tool"] not in MUTATING_TOOLS:
            continue
        key = call["args"].get("idempotency_key")
        if key in env.out_of_band_keys:
            continue
        att = store.one("SELECT * FROM booking_attempts WHERE idempotency_key = ?", (key,))
        assert att is not None, f"{call['tool']} called with key {key!r} that has no attempt row"
        appr = store.approval(att["approval_id"])
        assert appr is not None and appr["user_message"], "approval missing or not created from a user message"
        assert appr["used_at"], f"{call['tool']} ran under an approval that was never consumed"

    # 2. at most one booking per idempotency key
    per_key = Counter(b["idempotency_key"] for b in env.user_bookings() if b["idempotency_key"])
    assert all(n == 1 for n in per_key.values()), per_key

    # 3. at most one booking per approval (book action), across seq 0 and seq 1
    by_approval: Counter[str] = Counter()
    keys = {a["idempotency_key"]: a for a in store.attempts()}
    for b in env.user_bookings():
        if b["idempotency_key"] in keys:
            by_approval[keys[b["idempotency_key"]]["approval_id"]] += 1
    assert all(n <= 1 for n in by_approval.values()), by_approval

    # 4. no success without verification
    for a in store.attempts():
        if a["status"] == "CONFIRMED":
            assert a["verified_at"], f"{a['idempotency_key']} CONFIRMED without verification"

    # 5. one live-or-confirmed attempt per approval
    live = Counter(a["approval_id"] for a in store.attempts() if a["status"] in ("PENDING", "CONFIRMED"))
    assert all(n <= 1 for n in live.values()), live
