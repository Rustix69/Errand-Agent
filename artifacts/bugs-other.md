# Other bugs logged while building

These were real and fixed, each with a regression test, but are not part of the three documented in `BUGS.md`. Kept verbatim, with their original numbers (the order they were found in).

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

## 4. `propose_booking` reached the graph with the wrong action name (phase 3, graph)

- **What broke:** The first graph test I ran ended in `TOOL_FAILURE` with a `KeyError` in the `hold` node. The router turned the tool name `propose_booking` into the action `"booking"` with `removeprefix("propose_")`, but the guard and the `proposals` table use `"book"`. No proposal was ever created.
- **Why:** The action vocabulary (`book | cancel | reschedule`) and the tool names (`propose_booking | propose_cancel | propose_reschedule`) are different words, and the code derived one from the other by string surgery.
- **Fix:** an explicit `_ACTION` map in `errand/agent/tools.py`.
- **Regression test:** `tests/agent/test_tools_and_untrusted.py::test_each_propose_tool_maps_to_the_action_the_guard_understands` (added after the fact, because the first guard was only that every propose test in `tests/agent/test_graph_flow.py` failed).
- **How it was found:** by the first graph test, which failed immediately. It was caught by my own test run, never by a user, and it blocked the whole propose flow rather than corrupting anything.

## 5. After a re-quote the reply pointed at the OLD proposal (phase 3, finalize)

- **What broke:** When the executor returned `needs_reapproval` with a fresh proposal (the replacement hold cost one rupee more), `Reply.proposal_id` still carried the id of the old, invalidated proposal while the message and the pending state showed the new one. A client or trace keyed on `Reply.proposal_id` would have tracked the wrong proposal. The booking itself was never affected.
- **Why:** `finalize` copied `Exit.proposal_id` (the proposal that was executed) into `Final.proposal_id`, but for a request for approval the relevant id is the new proposal's.
- **Fix:** `finalize` uses the new proposal's id whenever it is asking for approval.
- **Regression test:** `tests/agent/test_graph_flow.py::test_changed_quote_after_approval_asks_again_and_book_is_not_called`.
- **How it was found:** by a test I wrote for the changed-quote-after-approval flow. Its check that the proposal the reply points at is the re-quote (it must have a parent proposal) failed, because the reply pointed at the original one. I fixed `finalize`, then reverted the fix once to confirm the test fails without it. The same test also failed once for a mistake of my own (`a.state()` instead of `a.agent.state()`), which was not a product bug and is not logged.

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
