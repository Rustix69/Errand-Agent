# Bugs hit while building

Three real bugs, each fixed and guarded by a regression test that fails without the fix (checked by reverting the fix
and watching the test fail). Five others were logged as they happened and are kept in `artifacts/bugs-other.md`.
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

## 3. The "nothing was booked" note was appended to a harmless options reply (found 8th, phase 9b, finalize)

- **What broke:** Running the real model (claude-sonnet-5-5) through the web UI, an options reply ended with "You'll get a confirmation to approve before anything is booked." The unverified-claim guard fired, appended "(Nothing was booked, cancelled or changed in this step. Only a verified result counts.)" to a perfectly good reply, and recorded it with reason `unverified_claim`. No claim had been made; the note was a false alarm and the trace said a claim was caught.
- **Why:** The guard (added in step 6) searched model text for the bare words booked / cancelled / rescheduled / confirmed, so future ("before anything is booked"), conditional, negated ("nothing is booked") and noun uses ("booked slots") all counted as claims.
- **Fix:** `claims_completed_action` in `errand/agent/finalize.py` counts only first-person completed claims ("I've booked it") and the finished state of the user's own booking ("your booking is confirmed", "It's booked", "Booked!"), and ignores a match that has a hedge word (before, once, if, will, can't, nothing ...) earlier in its sentence. A hedge after a real claim does not cancel it.
- **Regression tests:** `tests/agent/test_finalize.py::test_a_harmless_options_reply_gets_no_note_even_though_it_says_booked_in_the_future` (the exact sentence and the whole reply from the run), `tests/agent/test_finalize.py::test_future_conditional_negated_and_descriptive_wording_is_not_a_claim` and `tests/agent/test_graph_flow.py::test_a_real_looking_options_reply_reaches_the_user_untouched_through_the_graph`. Scenario S3 (`tests/evals/test_safety.py::test_s3_a_model_claiming_it_booked_changes_nothing`) still covers the true positive. Restoring the old regex makes 13 of these fail.
- **How it was found:** by running the real model through the web UI and reading its reply. No automated test could have shown it: scripted replies are written by me, and the only guard test (S3) used a clear positive. I added the guard in step 6 with a deliberately broad pattern, on the reasoning that appending a note is harmless, and never tested the false-positive side. The note is only ever appended, never a replacement, so no information was lost. Known limit that remains: a harmless description of an existing booking worded "your booking is confirmed" is still flagged, since the guard cannot tell it from a false claim.
