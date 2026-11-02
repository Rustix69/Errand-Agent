"""Memory and the context builder, through the real graph. Asserts context_log rows, recorded ModelRequests,
DB rows and events: never reply wording."""

import json

import pytest

from errand.agent.context import ContextBuilder, ContextOverflow
from errand.memory import PRIOR_SESSION, parse
from errand.model import ScriptedModel, ask, call, say
from tests.agent.helpers import AgentEnv, context_rows, memory_items, request_text
from tests.conftest import WEEK
from tests.core.helpers import Env

USER = "u_demo"


def forbidden_markers(info):
    return [info.markers[i] for i in info.forbidden]


def mixed_session(make_agent, **kw):
    """Several turns on a seeded user: searches, a provider lookup full of injection text, a memory command, a new service."""
    a = make_agent([
        call("search_providers", service="plumber", area="HSR Layout", date_range=WEEK), say("Here are options."),
        call("get_provider", provider_id="pl_01"), say("Reviews look mixed."),
        call("search_providers", service="electrician", area="HSR Layout", date_range=WEEK), say("Electricians listed."),
    ], seed=True, **kw)
    a.say("find me a plumber for my kitchen tap this week, mornings only, under 1500")
    a.say("tell me about the first one")
    a.say("what do you remember")  # handled by code; lists unconfirmed guesses to the USER
    a.say("now I need an electrician, same one as last time")
    return a


# ----------------------------------------------------------------------------- the rule, proved on what was sent


def test_forbidden_facts_never_reach_the_model(make_agent):
    a = mixed_session(make_agent)
    info, rows = a.seed, context_rows(a)
    assert len(rows) == len(a.model.requests) == 6  # one log row per model request
    forbidden = set(info.forbidden)
    for row, req in zip(rows, a.model.requests):
        assert not set(row["memory_ids"]) & forbidden  # the log says they were not sent ...
        text = request_text(req)
        for marker in forbidden_markers(info):
            assert marker not in text, marker  # ... and the text proves it
        sent = {m["id"] for m in memory_items(req.system)}
        assert sent == set(row["memory_ids"])  # the log is exactly what the model saw
        assert not sent & forbidden
    assert any(row["memory_ids"] for row in rows)  # the test is not vacuous: memories WERE sent
    assert all(m["id"] not in forbidden for req in a.model.requests for m in memory_items(req.system))


def test_irrelevant_and_forbidden_stay_out_while_relevant_profile_facts_go_in(make_agent):
    a = mixed_session(make_agent)
    first = {m["key"] for m in memory_items(a.model.requests[0].system)}
    assert {"home_area", "earliest_time", "preferred_technician", "plumber_budget"} <= first
    for req in a.model.requests:
        assert not any(m["id"] in set(a.seed.irrelevant) for m in memory_items(req.system))
        assert not any(mark in request_text(req) for mark in (a.seed.markers[i] for i in a.seed.irrelevant))
    last = {m["key"] for m in memory_items(a.model.requests[-1].system)}
    assert {"electrician_preferred", "electrician_timing"} <= last and "plumber_budget" not in last


def test_the_context_log_tokens_match_the_request_and_the_budget(make_agent):
    a = mixed_session(make_agent)
    for row, req in zip(context_rows(a), a.model.requests):
        assert row["tokens"] == req.approx_input_tokens() <= row["budget"] == 3000
    in_tokens = [d["input_tokens"] for d in a.of("model_call")]
    assert in_tokens == [r["tokens"] for r in context_rows(a)]  # the traced usage agrees with what was logged
    built = a.of("context_built")
    assert [(d["tokens"], d["memory_ids"]) for d in built] == [(r["tokens"], r["memory_ids"]) for r in context_rows(a)]


# ----------------------------------------------------------------------------- the prior conversation


def test_prior_conversation_reaches_the_model_only_as_a_counts_summary(make_agent):
    a = make_agent([say("hi")], seed=True)
    a.say("find me a plumber")
    (row,) = context_rows(a)
    assert row["summary_included"] == 1
    summary = a.model.requests[0].system.split("Earlier conversation")[1].split("\n")[0]
    assert "84 turns" in summary and "2026-10-" in summary and "plumber x" in summary
    text = request_text(a.model.requests[0])
    for raw in ("Looking for home cleaning for this weekend", "Can we do Saturday instead", "4821", "ZQF1", "ZQS1"):
        assert raw not in text  # no raw prior-turn text, and nothing quoted from forgotten / replaced facts


def test_continuing_the_old_session_redacts_exchanges_that_quote_forgotten_or_replaced_facts(make_agent):
    a = make_agent([say("hi")], seed=True, session=PRIOR_SESSION)
    a.say("anything new today?")
    (row,) = context_rows(a)
    text = request_text(a.model.requests[0])
    assert row["turns_included"] > 0 and "Show me the cheaper options" in text  # raw history IS used ...
    for leaked in ("4821", "ZQF1", "Indiranagar", "ZQS1"):  # ... but never the forgotten gate code or old area
        assert leaked not in text


# ----------------------------------------------------------------------------- the budget


def test_required_context_over_budget_ends_in_limit_reached_not_a_truncated_request(make_agent):
    a = make_agent([say("never used")], context_budget=200)
    r = a.say("find me a plumber")
    assert (r.status, r.kind, r.reason) == ("LIMIT_REACHED", "limit", "context_budget")
    assert a.model.calls == 0 and a.of("limit_reached")[0]["limit"] == "context_budget"
    assert len(a.of("final")) == len(a.of("run_start")) == 1
    a.model.steps.clear()
    a.model._i = 0  # nothing was consumed


def measured(a, **state):
    """(tokens with everything optional, tokens of the required part only) for a standard turn."""
    base = dict(run_id="r", session_id=PRIOR_SESSION, user_id=USER, turn=1, now_iso="2026-11-02T09:00",
                request={}, options=[], pending=None, user_message="find me a plumber, mornings, under 1500", run_msgs=[])
    base.update(state)
    full = ContextBuilder(a.env.store, a.memory, a.turns, budget_tokens=10**6).build(**base).approx_input_tokens()
    lo = 0
    for budget in range(full, 0, -10):
        try:
            ContextBuilder(a.env.store, a.memory, a.turns, budget_tokens=budget).build(**base)
            lo = budget
        except ContextOverflow:
            break
    return full, lo


def test_every_request_fits_the_budget_and_dropped_memories_are_logged(make_agent):
    probe = make_agent([], seed=True, session=PRIOR_SESSION)
    full, floor = measured(probe)
    assert full > floor + 300  # there is real optional content to trim
    by_budget = {}
    for budget in (3000, (full + floor) // 2, floor + 40):
        b = ContextBuilder(probe.env.store, probe.memory, probe.turns, budget_tokens=budget)
        req = b.build(run_id="r", session_id=PRIOR_SESSION, user_id=USER, turn=1, now_iso="2026-11-02T09:00",
                      request={}, options=[], pending=None, user_message="find me a plumber, mornings, under 1500",
                      run_msgs=[])
        row = by_budget[budget] = context_rows(probe)[-1]
        assert row["tokens"] == req.approx_input_tokens() <= budget == row["budget"]
        assert not set(row["dropped_memory_ids"]) & set(row["memory_ids"])
        sent = {m["id"] for m in memory_items(req.system)}
        assert sent == set(row["memory_ids"]) and not sent & set(probe.seed.forbidden)
    assert by_budget[3000]["dropped_memory_ids"] == []  # at 3000 everything relevant fit
    assert by_budget[floor + 40]["dropped_memory_ids"]  # at the tightest budget some did not, and the log says which


def test_builder_priority_required_first_then_memories_then_turns_then_summary(make_agent):
    a = make_agent([], seed=True, session=PRIOR_SESSION)
    full, floor = measured(a)

    def shape(budget):
        req = ContextBuilder(a.env.store, a.memory, a.turns, budget_tokens=budget).build(
            run_id="r", session_id=PRIOR_SESSION, user_id=USER, turn=1, now_iso="2026-11-02T09:00", request={},
            options=[], pending=None, user_message="find me a plumber, mornings, under 1500", run_msgs=[])
        return len(memory_items(req.system)), len(req.messages) - 1, "Earlier conversation" in req.system

    n_mem, n_turns, has_summary = shape(full)
    assert n_mem > 0 and n_turns > 0 and has_summary  # everything fits
    assert shape(floor) == (0, 0, False)  # only the required part fits: nothing optional is sent
    with pytest.raises(ContextOverflow):
        shape(floor - 10)  # the required part alone does not fit: stop, never truncate it
    # shrinking never adds anything back: each optional part only gets smaller as the budget falls
    sizes = [shape(b) for b in range(full, floor - 1, -40)]
    for (m1, t1, s1), (m2, t2, s2) in zip(sizes, sizes[1:]):
        assert m2 <= m1 and t2 <= t1 and s2 <= s1
    # memories have first claim: with room for them but little else, memories are in and turns are not
    assert any(m > 0 and t == 0 for m, t, _ in sizes)


# ----------------------------------------------------------------------------- memory is handled by code


def test_memory_commands_bypass_the_model_and_stay_out_of_the_conversation(make_agent):
    a = make_agent([say("Looking for plumbers."), say("done")], seed=True)
    turns_before = len(a.turns.session("s1"))
    r = a.say("what do you remember")
    assert (r.status, r.kind) == ("DONE", "memory_listed") and a.model.calls == 0
    assert a.of("memory_op")[0]["command"] == "inspect"
    assert all(i in r.message for i in a.seed.unconfirmed)  # the USER sees which guesses are awaiting confirmation
    assert len(a.turns.session("s1")) == turns_before  # not logged as a turn
    a.say("find me a plumber")
    a.say("book one")
    for req in a.model.requests:
        text = request_text(req)
        assert "what do you remember" not in text
        for marker in forbidden_markers(a.seed):
            assert marker not in text  # the listing of guesses did not leak into later context


def test_forget_correct_and_confirm_by_command(make_agent):
    a = make_agent([say("a"), say("b"), say("c")], seed=True)
    area_id = next(m.id for m in a.memory.usable(USER) if m.key == "home_area")
    assert a.say("forget my area").kind == "memory_forgotten"
    assert area_id not in {m.id for m in a.memory.usable(USER)}
    a.say("find me a plumber")
    assert "home_area" not in {m["key"] for m in memory_items(a.model.requests[0].system)}
    guess = a.seed.unconfirmed[1]
    assert a.say(f"confirm {guess}").kind == "memory_confirmed"
    a.say("find me a plumber again")
    assert guess in {m["id"] for m in memory_items(a.model.requests[1].system)}  # usable only after confirmation
    assert a.say("correct my earliest time to 11:00").kind == "memory_corrected"
    assert a.say("forget it, I changed my mind").kind == "answer"  # a chat message for the model, not a command
    assert a.model.calls == 3


def test_a_pending_proposal_survives_a_memory_command(make_agent):
    from tests.agent.test_graph_flow import BOOKING_SCRIPT, to_pending

    a = make_agent(BOOKING_SCRIPT)
    to_pending(a)
    pending = a.rt.store.one("SELECT id FROM proposals WHERE status = 'pending'")["id"]
    a.say("remember my area is HSR Layout")
    assert a.rt.store.one("SELECT status FROM proposals WHERE id = ?", (pending,))["status"] == "pending"
    assert a.env.count("book") == 0


# ----------------------------------------------------------------------------- stable preferences across sessions


def area_aware_script():
    """Reads what the context says it knows: searches with the remembered area, else asks for it."""
    def step(request):
        area = next((m["value"] for m in memory_items(request.system) if m["key"] == "home_area"), None)
        if area:
            return call("search_providers", service="plumber", area=area, date_range=WEEK)
        return ask("Which area are you in, and when?", ["area", "date_range"], {"service": "plumber"})

    return [step, say("Options listed.")]


def test_a_stable_preference_from_an_earlier_session_saves_the_user_from_being_asked(make_agent):
    first = make_agent([], session="session_a")
    assert first.say("remember my area is HSR Layout").kind == "memory_saved"
    area_id = next(m.id for m in first.memory.usable(USER) if m.key == "home_area")
    # a later session (new connections, new graph) on the same agent.db

    b = make_agent(area_aware_script(), session="session_b")
    b.say("find me a plumber this week")
    assert not b.of("ask_user")  # not asked again
    (search,) = [d for d in b.of("tool_call") if d["tool"] == "search_providers"]
    assert search["args"]["area"] == "HSR Layout"
    (row,) = context_rows(b)[:1]
    assert area_id in row["memory_ids"]  # it was the remembered fact that did it

    # control: forget it and the very same script has to ask
    c = make_agent(area_aware_script(), session="session_c")
    assert c.say("forget my area").kind == "memory_forgotten"
    c.say("find me a plumber this week")
    assert [d["missing"] for d in c.of("ask_user")] == [["area", "date_range"]]


# ----------------------------------------------------------------------------- plain statements are saved by code


def test_plain_statements_are_saved_as_explicit_memories_by_code(make_agent):
    from errand.memory import capture

    a = make_agent([call("search_providers", service="plumber", area="Jadavpur", date_range=WEEK), say("ok")], seed=True)
    old = next(m for m in a.memory.usable(USER) if m.key == "home_area")
    r = a.say("I'm in Jadavpur, find me a plumber")
    new = next(m for m in a.memory.usable(USER) if m.key == "home_area")
    assert new.value == "Jadavpur" and new.source == "explicit" and new.id != old.id
    assert a.env.store.one("SELECT superseded_by FROM memories WHERE id = ?", (old.id,))[0] == new.id
    (cap,) = a.of("memory_captured")
    assert cap["id"] == new.id and a.model.calls == 2 and r.run_id == cap["run_id"]  # model still ran and searched
    assert new.id in context_rows(a)[0]["memory_ids"]  # already in THAT turn's context
    assert new.id in r.message  # the short confirmation, written by code
    # the patterns: time normalisation, technician, and look-alikes that must NOT be captured
    got = lambda t: [(c.key, c.value) for c in capture(t)]  # noqa: E731
    assert got("not before 10 a.m.") == [("earliest_time", "10:00")]
    assert got("no earlier than 9:30am") == [("earliest_time", "09:30")]
    assert got("my preferred technician is Suresh") == [("preferred_technician", "Suresh")]
    assert got("I'm in a hurry") == [] and got("not before Friday") == [] and got("not before 10 days from now") == []


# ----------------------------------------------------------------------------- service-aware relevance


def test_a_plumber_query_never_gets_other_services_notes(make_agent):
    a = make_agent([call("search_providers", service="plumber", area="HSR Layout", date_range=WEEK), say("Options.")],
                   seed=True)
    a.say("find me a plumber for my kitchen tap this week, mornings only, under 1500")
    first, later = a.model.requests  # service not yet known / service known to be plumber
    assert '"service":"plumber"' not in first.system.split("trusted):\n")[1] and '"service":"plumber"' in later.system
    for req in (first, later):
        keys = {m["key"] for m in memory_items(req.system)}
        assert {"home_area", "earliest_time", "preferred_technician"} <= keys  # profile rows
        assert {"plumber_budget", "plumber_note"} <= keys  # plumber rows
        assert not {k for k in keys if k.startswith(("carpenter_", "electrician_", "ac_servicing_"))}
        assert "cleaning_scope" not in keys
        text = request_text(req)
        assert not any(mark in text for mark in a.seed.markers.values())  # still no forbidden / irrelevant rows
    # the other half of the rule: keyword overlap still works for memories with no service tag
    a.memory.add(USER, "noise_note", "Dislikes loud drilling in the kitchen")
    assert "noise_note" in {m.key for m in a.memory.relevant(USER, {"kitchen"}, service="plumber")}
