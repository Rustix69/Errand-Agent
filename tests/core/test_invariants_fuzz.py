"""Seeded-random failures (the 70/15/15 book mix, price drift on holds) over many bookings.
The invariants from tests/core/invariants.py run at teardown of every `make_env` test, so this
checks them under real randomness, not only under hand-forced cases."""

from collections import Counter

from errand.contract import ToolError
from errand.mocks import ScenarioConfig
from tests.conftest import WEEK
from errand.contract.port import MUTATING_TOOLS


def _targets(env, n):
    out = []
    for service, area in (("plumber", "Indiranagar"), ("electrician", "Whitefield"),
                          ("ac_servicing", "HSR Layout"), ("carpenter", "Jayanagar"),
                          ("home_cleaning", "Koramangala")):
        for p in env.tools.search_providers(service, area, WEEK).providers:
            out += [(p.provider_id, s) for s in p.available_slots[:2]]
    return out[:n]


def test_invariants_hold_under_seeded_random_failures(make_env):
    env = make_env(config=ScenarioConfig(seed=11, mode="seeded", price_change_rate=0.3))
    kinds: Counter[str] = Counter()
    for i, (provider, slot) in enumerate(_targets(env, 60)):
        session = f"s{i}"
        try:
            hold = env.tools.hold_slot(provider, slot)
        except ToolError:
            continue
        env.guard.propose_booking(session, "u_demo", hold)
        d = env.guard.on_user_message(session, "yes")
        out = env.executor.execute(d.approval_id)
        resumes = 0
        while out.kind in ("unknown", "limit_reached") and resumes < 3:
            out, resumes = env.executor.execute(d.approval_id), resumes + 1
        if out.kind == "needs_reapproval" and out.new_proposal_id:
            d2 = env.guard.on_user_message(session, "yes")
            assert d2.kind == "approved" and d2.proposal_id == out.new_proposal_id
            out = env.executor.execute(d2.approval_id)
        kinds[out.kind] += 1

        if out.kind == "confirmed":  # saying yes again right after the result never re-books
            env.guard.mark_result_reported(session, d.proposal_id)
            before = len(env.calls())
            rep = env.guard.on_user_message(session, "yes")
            if rep.kind == "repeat":
                assert env.executor.execute(rep.approval_id).kind == "already_done"
            assert len(env.calls()) == before

    assert {"confirmed", "slot_taken"} <= set(kinds), kinds  # the mix really produced both
    confirmed = [a for a in env.store.attempts() if a["status"] == "CONFIRMED"]
    keyed = [b for b in env.user_bookings() if b["idempotency_key"]]
    # Every CONFIRMED attempt is exactly one real booking, and every confirmed outcome is one attempt.
    assert len(confirmed) == len(keyed) == kinds["confirmed"]
    # The seeded mix exercised the ambiguous paths, not just clean successes.
    book_outcomes = Counter(c["outcome"] for c in env.calls("book"))
    assert any(o.startswith("ok:timeout_after_commit") or o.startswith("error:timeout_after_commit")
               for o in book_outcomes), book_outcomes
    assert any("timeout_before_commit" in o for o in book_outcomes), book_outcomes
    assert sum(1 for c in env.calls() if c["tool"] in MUTATING_TOOLS) >= 30
