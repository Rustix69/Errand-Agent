"""The rule: forgotten facts and unconfirmed inferred ones never reach the model. Filtered in SQL, before ranking."""

import pytest

import errand.memory.store as store_mod
from errand.memory import words_of
from tests.memory.conftest import USER

QUERIES = [
    "find me a plumber for my kitchen tap this week, mornings only, under 1500",
    "move my AC servicing to Saturday",
    "book the same electrician as last time, but not before 10 a.m.",
    "I need cleaning for my flat",
    # words that appear in the forbidden rows: still must not surface them
    "whitefield", "gate code 4821", "evening slots", "cheapest provider", "bleach allergy", "Volt Masters",
    "pl_02 pl_03 plumber", "98450 11223", "indiranagar", "08:00", "weekend", "office window AC",
]


def test_seed_shape(seeded):
    memory, turns, info, store = seeded
    assert info.total == 56 >= 50
    assert (len(info.current), len(info.confirmed_inferred), len(info.unconfirmed), len(info.forgotten),
            len(info.superseded), len(info.irrelevant)) == (14, 4, 6, 8, 2, 22)
    assert info.prior_turns == 84 == len(turns.session("s_prev"))
    assert len(info.forbidden) == 16 and len(set(info.markers.values())) == len(info.markers)


def test_seeding_twice_changes_nothing(seeded):
    from errand.memory import seed_demo

    memory, turns, info, store = seeded
    assert seed_demo(memory, turns, USER) == info
    assert store.one("SELECT COUNT(*) FROM memories")[0] == 56 and store.one("SELECT COUNT(*) FROM turns")[0] == 84


def test_the_sql_view_holds_the_filter(seeded):
    memory, _, info, store = seeded
    usable_ids = {r["id"] for r in store.all("SELECT id FROM usable_memories WHERE user_id = ?", (USER,))}
    assert usable_ids == set(info.current + info.confirmed_inferred + info.irrelevant) and len(usable_ids) == 40
    assert not usable_ids & set(info.forbidden)
    assert {m.id for m in memory.usable(USER)} == usable_ids


def test_forbidden_rows_never_even_reach_python_ranking(seeded, monkeypatch):
    memory, _, info, _ = seeded
    seen: list[set[str]] = []
    real = store_mod.rank

    def spy(rows, words, *a, **k):
        seen.append({m.id for m in rows})
        return real(rows, words, *a, **k)

    monkeypatch.setattr(store_mod, "rank", spy)
    for q in QUERIES:
        got = memory.relevant(USER, words_of(q))
        assert not {m.id for m in got} & set(info.forbidden), q
    assert len(seen) == len(QUERIES) and all(ids and not ids & set(info.forbidden) for ids in seen)


def test_relevance_uses_profile_tags_and_keywords_not_noise(seeded):
    memory, _, info, _ = seeded
    keys = lambda q: {m.key for m in memory.relevant(USER, words_of(q))}  # noqa: E731
    plumber = keys(QUERIES[0])
    assert {"home_area", "earliest_time", "preferred_technician", "plumber_budget", "plumber_note"} <= plumber
    assert "electrician_preferred" not in plumber and "cleaning_scope" not in plumber
    assert {"electrician_preferred", "electrician_timing"} <= keys(QUERIES[2])
    assert {"ac_servicing_provider", "ac_servicing_day"} <= keys(QUERIES[1])
    for q in QUERIES[:4]:  # irrelevant rows (hobbies, pets ...) never match an errand query
        assert not {m.id for m in memory.relevant(USER, words_of(q))} & set(info.irrelevant)


def test_a_corrected_preference_replaces_the_old_one(seeded):
    memory, _, info, store = seeded
    area = [m for m in memory.usable(USER) if m.key == "home_area"]
    assert [m.value for m in area] == ["HSR Layout"]
    old = store.one("SELECT * FROM memories WHERE id = ?", (info.superseded[0],))
    assert old["superseded_by"] == area[0].id and "ZQS1" in old["value"]
    assert [m.value for m in memory.usable(USER) if m.key == "earliest_time"] == ["10:00"]


def test_inspect_separates_what_is_used_from_what_is_only_a_guess(seeded):
    memory, _, info, _ = seeded
    ins = memory.inspect(USER)
    assert {m.id for m in ins.awaiting} == set(info.unconfirmed)
    assert not {m.id for m in ins.current} & (set(info.unconfirmed) | set(info.forgotten) | set(info.superseded))
    assert ins.forgotten_count == 8 and len(ins.current) == 40


def test_forget_correct_confirm_reject(seeded):
    memory, _, info, store = seeded
    # forget by key: gone from retrieval, kept as a tombstone
    r = memory.forget(USER, "plumber_budget")
    assert r.kind == "forgotten" and store.one("SELECT forgotten FROM memories WHERE id = ?", (r.ids[0],))[0] == 1
    assert "plumber_budget" not in {m.key for m in memory.relevant(USER, words_of(QUERIES[0]))}
    assert memory.forget(USER, "plumber_budget").kind == "not_found"  # nothing current left to forget
    # correct: new explicit row replaces the old; the old value is no longer retrievable
    r = memory.correct(USER, "home_area", "Jadavpur")
    assert r.kind == "corrected" and [m.value for m in memory.usable(USER) if m.key == "home_area"] == ["Jadavpur"]
    # an inferred guess becomes usable ONLY after confirm
    guess = info.unconfirmed[1]  # plumber_guess
    assert guess not in {m.id for m in memory.usable(USER)}
    assert memory.confirm(USER, guess).kind == "confirmed"
    assert guess in {m.id for m in memory.usable(USER)}
    # reject forgets a guess
    other = info.unconfirmed[2]
    assert memory.reject(USER, other).kind == "rejected" and other not in {m.id for m in memory.usable(USER)}
    assert other not in {m.id for m in memory.inspect(USER).awaiting}
    # an explicit row cannot be 'confirmed' (not a guess), unknown ids are not found
    assert memory.confirm(USER, info.current[0]).kind == "not_found" and memory.forget(USER, "m_999999").kind == "not_found"


def test_forbidden_values_cover_forgotten_guesses_and_replaced_but_not_current(seeded):
    memory, _, _, _ = seeded
    bad = memory.forbidden_values(USER)
    assert {"4821 (zqf1)", "prefers evening slots after 6 pm (zqu1)", "indiranagar (zqs1)"} <= bad
    assert "hsr layout" not in bad and "10:00" not in bad


def test_seeded_electrician_memory_matches_the_booking_history(seeded):
    """Regression (BUGS #7): a seeded memory must agree with what the marketplace says."""
    from errand.clock import FakeClock
    from errand.mocks import DEMO_USER_ID, MockWorld, ScenarioConfig
    from errand.wiring import tools_for

    memory, _, _, _ = seeded
    world = MockWorld(":memory:", FakeClock(), ScenarioConfig(mode="forced"))
    bookings = tools_for(world, DEMO_USER_ID).list_bookings(DEMO_USER_ID).bookings
    last = max((b for b in bookings if b.service == "electrician" and not b.is_upcoming), key=lambda b: b.slot)
    values = {m.key: m.value for m in memory.usable(USER)}
    assert last.provider_id in values["electrician_preferred"]
    assert values["preferred_technician"] == last.technician
