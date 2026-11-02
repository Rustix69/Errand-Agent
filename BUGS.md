# Bugs hit while building

Three real bugs, each fixed and guarded by a regression test that fails without the fix (checked by reverting the fix
and watching the test fail). Four others were logged as they happened and are kept in `artifacts/bugs-other.md`.
The numbers in parentheses are the order they were found in.

## 1. A confirmed attempt kept a stale `hold_expired` error code (found 3rd, phase 2, executor)

- **What broke:** When `book` reported `hold_expired`, the executor wrote `error_code='hold_expired'` on the still-PENDING attempt while it asked the source of truth. If the truth then said the booking had in fact happened (timeout-after-commit, hold expiring during backoff), the attempt became `CONFIRMED` but kept `error_code='hold_expired'`. The booking and the user message were right; the audit row said a successful booking had failed with an expiry. Startup recovery had the same gap.
- **Why:** `update_attempt` only wrote the fields it was given, so nothing ever cleared a code set earlier on the same row.
- **Fix:** `update_attempt(..., clear_error=True)`; `IdempotentExecutor._confirm` and `resolve_pending` pass it when marking an attempt CONFIRMED.
- **Regression tests:** `tests/core/test_executor_hold_expired.py::test_hold_expired_claim_is_verified_not_trusted` (fails without `clear_error` in `_confirm`) and `tests/core/test_recovery.py::test_recovery_clears_a_stale_hold_expired_code_when_the_booking_did_happen` (fails without it in startup recovery). The recovery test was added when the documentation was written: the first recovery test I had cited never reached that gap (its attempt had no stale code), which the revert check showed.
- **How it was found:** by reading the code while designing the "timeout after commit, then the hold expires during backoff" test, before any test failed. No existing test covered that path, so nothing would have caught it. The impact was an inconsistent audit row, not a wrong booking or message.

## 2. `propose_booking` reached the graph with the wrong action name (found 4th, phase 3, graph)

- **What broke:** The first graph test I ran ended in `TOOL_FAILURE` with a `KeyError` in the `hold` node. The router turned the tool name `propose_booking` into the action `"booking"` with `removeprefix("propose_")`, but the guard and the `proposals` table use `"book"`. No proposal was ever created.
- **Why:** The action vocabulary (`book | cancel | reschedule`) and the tool names (`propose_booking | propose_cancel | propose_reschedule`) are different words, and the code derived one from the other by string surgery.
- **Fix:** an explicit `_ACTION` map in `errand/agent/tools.py`.
- **Regression test:** `tests/agent/test_tools_and_untrusted.py::test_each_propose_tool_maps_to_the_action_the_guard_understands` (added after the fact, because the first guard was only that every propose test in `tests/agent/test_graph_flow.py` failed).
- **How it was found:** by the first graph test, which failed immediately. It was caught by my own test run, never by a user, and it blocked the whole propose flow rather than corrupting anything.

## 3. After a re-quote the reply pointed at the OLD proposal (found 5th, phase 3, finalize)

- **What broke:** When the executor returned `needs_reapproval` with a fresh proposal (the replacement hold cost one rupee more), `Reply.proposal_id` still carried the id of the old, invalidated proposal while the message and the pending state showed the new one. A client or trace keyed on `Reply.proposal_id` would have tracked the wrong proposal. The booking itself was never affected.
- **Why:** `finalize` copied `Exit.proposal_id` (the proposal that was executed) into `Final.proposal_id`, but for a request for approval the relevant id is the new proposal's.
- **Fix:** `finalize` uses the new proposal's id whenever it is asking for approval.
- **Regression test:** `tests/agent/test_graph_flow.py::test_changed_quote_after_approval_asks_again_and_book_is_not_called`.
- **How it was found:** by a test I wrote for the changed-quote-after-approval flow. Its check that the proposal the reply points at is the re-quote (it must have a parent proposal) failed, because the reply pointed at the original one. I fixed `finalize`, then reverted the fix once to confirm the test fails without it. The same test also failed once for a mistake of my own (`a.state()` instead of `a.agent.state()`), which was not a product bug and is not logged.
