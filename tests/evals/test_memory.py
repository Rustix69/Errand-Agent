"""Memory correction and forgetting, as the user experiences them across messages."""

from errand.model import ask, say
from tests.agent.helpers import context_rows, memory_items
from tests.evals.scripts import remembered, search

USER = "u_demo"


def area_or_ask(service):
    def step(request):
        area = remembered(request, "home_area")
        return search(service, area) if area else ask("Which area, and when?", ["area", "date_range"], {"service": service})
    return step


def test_m1_a_corrected_preference_replaces_the_old_one_everywhere(scenario):
    run = scenario(
        "M1", "Memory correction: 'I'm in Koramangala now' (saved area was HSR Layout)",
        "new explicit home_area; old row superseded; searches use Koramangala; HSR Layout is in no later request",
        [area_or_ask("plumber"), say("Here are plumbers."), area_or_ask("electrician"), say("And electricians.")],
        ["I'm in Koramangala now, find me a plumber", "and an electrician too"], seed=True,
    )
    a = run.a
    cur = [m for m in a.memory.usable(USER) if m.key == "home_area"]
    assert [(m.value, m.source) for m in cur] == [("Koramangala", "explicit")]
    old = a.env.store.all("SELECT * FROM memories WHERE key = 'home_area' AND superseded_by IS NOT NULL")
    assert any(r["value"] == "HSR Layout" and r["superseded_by"] == cur[0].id for r in old)
    assert [d["args"]["area"] for d in a.of("tool_call") if d["tool"] == "search_providers"] == ["Koramangala"] * 2
    assert [d["key"] for d in a.of("memory_captured")] == ["home_area"] and a.of("ask_user") == []
    assert not any("HSR Layout" in req.system for req in a.model.requests)


def test_m2_a_forgotten_fact_stops_being_used_and_the_agent_asks_again(scenario):
    run = scenario(
        "M2", "Memory forgetting: 'forget my area', then 'find me a plumber'",
        "the forget is handled by code (0 model calls); the row is forgotten; the next request asks for the area",
        [area_or_ask("plumber")], [], seed=True,
    )
    a = run.a
    area_id = next(m.id for m in a.memory.usable(USER) if m.key == "home_area")
    r1 = run.say("forget my area")
    assert (r1.status, r1.kind) == ("DONE", "memory_forgotten") and a.model.calls == 0
    assert a.env.store.one("SELECT forgotten FROM memories WHERE id = ?", (area_id,))[0] == 1
    run.say("find me a plumber this week")
    assert [d["missing"] for d in a.of("ask_user")] == [["area", "date_range"]]
    assert area_id not in {i for row in context_rows(a) for i in row["memory_ids"]}
    assert not any("home_area" in {m["key"] for m in memory_items(req.system)} for req in a.model.requests)
    assert not any("HSR Layout" in req.system for req in a.model.requests)  # not even through old conversation text
    assert [m.key for m in a.memory.usable(USER)].count("home_area") == 0
