# Bugs hit while building

Logged when they happened. The three for the deliverable get picked from this list at the end.

## 1. The demo user's booking history spread across unrelated areas (phase 1, mocks)

- **What broke:** `test_reschedule_to_free_saturday_slot` raised `StopIteration`: searching AC servicing in Indiranagar didn't return `ac_01`, the provider of the user's upcoming AC booking.
- **Why:** The seeded bookings for `u_demo` used providers picked by ID (`el_05`, `cl_01`, ...) without checking their service areas. `ac_01` serves HSR Layout, Whitefield and Jayanagar, but not Indiranagar. No single "home area" covered the user's whole history, so "move my AC servicing to Saturday" or "same electrician as last time" could never surface that provider in a search of the user's area.
- **Fix:** Added `DEMO_USER_AREA = "HSR Layout"` in `errand/mocks/catalog.py` and re-pointed the seeded bookings at providers that serve it (`el_02`, `cl_03`, `cl_04`).
- **Regression test:** `tests/mocks/test_cancel_reschedule_list.py::test_demo_history_providers_all_serve_home_area`

## 2. Reopening `world.db` with an explicit config dropped the seeded reschedule conflict (phase 1, mocks)

- **What broke:** The demo conflict (`ac_01`, next Saturday 10:00) was merged into `ScenarioConfig.reschedule_conflict_slots` at seed time. A restart that passed its own `ScenarioConfig` replaced the stored config, so the conflict disappeared and `reschedule` to that slot succeeded. Found while reviewing the restart path, before any test caught it.
- **Why:** Seed-derived data (computed from the clock at seed time) was stored inside caller-owned config, so it got overwritten whenever the config was replaced.
- **Fix:** Seed conflicts are stored in `world_meta.seed_conflicts` and merged at lookup time by `MockWorld.conflict_slots()`, still gated by `seed_default_conflicts`.
- **Regression test:** `tests/mocks/test_cancel_reschedule_list.py::test_seeded_conflict_survives_reopen_with_new_config`

## 3. A confirmed attempt kept a stale `hold_expired` error code (phase 2, executor)

- **What broke:** When `book` reported `hold_expired`, the executor wrote `error_code='hold_expired'` on the still-PENDING attempt while it asked the source of truth. If the truth then said the booking had in fact happened (timeout-after-commit, hold expiring during backoff), the attempt became `CONFIRMED` but kept `error_code='hold_expired'`. The booking and the user message were right; the audit row said a successful booking had failed with an expiry. Startup recovery had the same gap.
- **How it was found:** by reading the code while designing the "timeout after commit, hold expires during backoff" test, before any test failed. No test had existed for it yet.
- **Why:** `update_attempt` only wrote fields that were given, so nothing ever cleared the code set earlier on the same row.
- **Fix:** `update_attempt(..., clear_error=True)`; `IdempotentExecutor._confirm` and `resolve_pending` pass it when marking CONFIRMED.
- **Regression tests:** `tests/core/test_executor_hold_expired.py::test_hold_expired_claim_is_verified_not_trusted` (fails if `clear_error` is removed from `_confirm`, checked by hand) and `tests/core/test_recovery.py::test_crash_after_commit_is_recovered_to_confirmed_and_yes_returns_it` (asserts `error_code is None` after recovery).

## 4. `propose_booking` reached the graph with the wrong action name (phase 3, graph)

- **What broke:** The very first graph test ended in `TOOL_FAILURE` / `KeyError`. The router turned the tool name `propose_booking` into the action `"booking"` with `removeprefix("propose_")`, but the `hold` node and the guard use `"book"`. No proposal was ever created.
- **Why:** The action vocabulary (`book | cancel | reschedule`) and the tool names (`propose_booking | propose_cancel | propose_reschedule`) are not the same words, and the code derived one from the other by string surgery.
- **Fix:** An explicit `_ACTION` map in `errand/agent/tools.py`.
- **Regression test:** every propose test in `tests/agent/test_graph_flow.py` (e.g. `test_approval_is_decided_by_code_before_the_model_sees_the_message`) fails without it.

## 5. After a re-quote the reply pointed at the OLD proposal (phase 3, finalize)

- **What broke:** When the executor returned `needs_reapproval` with a fresh proposal (the replacement hold cost one rupee more), the `Reply` still carried the id of the old, invalidated proposal while the message and the pending state showed the new one. A client or trace keyed on `Reply.proposal_id` would have tracked the wrong proposal.
- **Why:** `finalize` copied `Exit.proposal_id` (the proposal that was executed) into `Final.proposal_id`, but for a request for approval the relevant id is the new proposal's.
- **Fix:** `finalize` uses the new proposal's id whenever it is asking for approval.
- **Regression test:** `tests/agent/test_graph_flow.py::test_changed_quote_after_approval_asks_again_and_book_is_not_called`.

## 6. `trace save --out artifacts/traces` wrote a file named `traces` (phase 4, trace CLI)

- **What broke:** `save` only treated `--out` as a directory if it already existed. With a new path like `artifacts/traces` it copied the run's JSONL to a regular file called `traces`, so the next `save` to that path failed with `NotADirectoryError`.
- **Why:** the check was `out.is_dir()`, which is false for a directory that doesn't exist yet.
- **Fix:** `--out` is a directory unless it ends in `.jsonl` (or is an existing directory).
- **Regression test:** `tests/trace/test_trace_cli.py::test_save_is_a_plain_copy_of_only_that_run`.

## 7. The seeded "preferred electrician" memory contradicted the demo user's booking history (phase 5 seeds, found in phase 6)

- **What broke:** The Step 5 seed saved `electrician_preferred = "Same electrician as last time: el_02"` and `preferred_technician = "Karthik R."`. The marketplace's own history says the most recent electrician is `el_01` (technician Suresh M.), and `tests/mocks/test_cancel_reschedule_list.py` pins that. So a seeded user asking "book the same electrician as last time" would have been handed a memory pointing at the wrong provider, and the model could book the wrong one with the user's approval looking legitimate.
- **How it was found:** by reading the catalog while planning the "same electrician as last time" eval scenario, before any test failed. No test compared the seeded memories to the world.
- **Why:** I picked `el_02` from the Phase 1 bug-1 fix (the provider ids re-pointed to serve HSR Layout) instead of deriving the value from `list_bookings`, and invented the technician name.
- **Fix:** the seed now says `el_01` / `Suresh M.`.
- **Regression test:** `tests/memory/test_memory_store.py::test_seeded_electrician_memory_matches_the_booking_history` (compares the seeded memory with `list_bookings`) and the "same electrician" eval scenario.
