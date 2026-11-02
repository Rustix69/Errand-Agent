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
