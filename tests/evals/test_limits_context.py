"""Bounded execution (model turns, tool calls, wall clock) and the context budget."""

import pytest

from errand.model import calls, call, say
from tests.agent.helpers import context_rows, memory_items
from tests.evals.scripts import search


def one_final_limit(run, limit):
    (r,) = run.replies
    assert (r.status, r.kind, r.reason) == ("LIMIT_REACHED", "limit", limit)
    assert [d["limit"] for d in run.a.of("limit_reached")] == [limit]
    assert len(run.a.of("final")) == len(run.a.of("run_start")) == 1 and run.a.world_mutations() == []


def test_l1_model_turn_limit(scenario):
    run = scenario(
        "L1", "Limit: a model that never stops searching",
        "LIMIT_REACHED model_turns after exactly 8 model calls; one final; nothing mutated",
        [search()] * 12, ["find me a plumber"], context_budget=20_000,
        notes="context budget raised so only the turn limit is in play",
    )
    one_final_limit(run, "model_turns")
    assert run.a.model.calls == 8


def test_l2_tool_call_limit(scenario):
    run = scenario(
        "L2", "Limit: 4 read tools per model turn, repeatedly",
        "LIMIT_REACHED tool_calls once 12 calls are used, well before the model-turn limit",
        [calls(*[("list_bookings", {})] * 4)] * 8, ["show my bookings"], context_budget=20_000,
        notes="context budget raised so only the tool-call limit is in play",
    )
    one_final_limit(run, "tool_calls")
    assert len(run.a.of("tool_call")) == 12 and run.a.model.calls == 4


def test_l3_wall_clock_limit(scenario, clock):
    def slow(request):
        clock.advance(31)  # the model "takes" 31 s on the fake clock; the budget is 30 s
        return search()

    run = scenario(
        "L3", "Limit: a model turn that takes 31 s against a 30 s budget",
        "LIMIT_REACHED wall_clock; the tool is never called; nothing mutated", [slow, search()], ["find me a plumber"],
    )
    one_final_limit(run, "wall_clock")
    assert run.a.of("tool_call") == []


def test_c1_context_budget_trims_memories_and_never_exceeds_it(scenario):
    budget = 1800
    run = scenario(
        "C1", "Context budget 1,800 tokens, seeded user (56 memories, 84 prior turns), 3 messages",
        "every request <= budget; some relevant memories dropped and logged; no forbidden/irrelevant id ever sent",
        [lambda r: search("plumber", "HSR Layout"), say("Options listed."), say("Noted."), say("Fine.")],
        ["find me a plumber for my kitchen tap this week, mornings only, under 1500", "thanks", "and one more thing"],
        seed=True, context_budget=budget,
    )
    a, rows = run.a, context_rows(run.a)
    assert [r.kind for r in run.replies] == ["answer"] * 3
    forbidden = set(a.seed.forbidden) | set(a.seed.irrelevant)
    for row, req in zip(rows, a.model.requests):
        assert row["tokens"] == req.approx_input_tokens() <= budget == row["budget"]
        assert not (set(row["memory_ids"]) | set(row["dropped_memory_ids"])) & forbidden
        assert {m["id"] for m in memory_items(req.system)} == set(row["memory_ids"])
    dropped = [row for row in rows if row["dropped_memory_ids"]]
    run.extra.update(requests=len(rows), max_tokens=max(r["tokens"] for r in rows), rows_with_dropped=len(dropped))
    run.record()
    assert dropped, "the budget was meant to be tight enough to drop some relevant memories"


def test_c2_required_context_over_budget_stops_cleanly(scenario):
    run = scenario(
        "C2", "Context budget smaller than the required part (rules + state + message)",
        "LIMIT_REACHED context_budget before any model call; one final", [say("never used")], ["find me a plumber"],
        context_budget=200,
    )
    one_final_limit(run, "context_budget")
    assert run.a.model.calls == 0


def test_c3_tool_results_that_outgrow_the_budget_stop_the_run_cleanly(scenario):
    run = scenario(
        "C3", "Context budget: repeated searches pile up tool results inside ONE message (default 3,000 budget)",
        "LIMIT_REACHED context_budget before the model-turn limit; no request ever exceeded the budget; nothing mutated",
        [search()] * 8, ["find me a plumber"],
        notes="this run's tool results are required context and are never trimmed, so the run stops instead",
    )
    one_final_limit(run, "context_budget")
    a = run.a
    assert 1 < a.model.calls < 8 and all(r["tokens"] <= 3000 for r in context_rows(a))
    assert all(req.approx_input_tokens() <= 3000 for req in a.model.requests)
