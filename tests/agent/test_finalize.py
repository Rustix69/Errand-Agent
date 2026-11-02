import pytest

from errand.agent.finalize import STATUS_BY_KIND, Exit, finalize

BOOKING = {"booking_id": "bk_9", "provider_name": "Acme", "provider_id": "pl_1", "slot": "2026-11-03T09:00",
           "price": 800, "status": "confirmed"}
PROPOSAL = {"proposal_id": "prop_1", "action": "book", "provider_id": "pl_1", "provider_name": "Acme",
            "slot": "2026-11-03T09:00", "price": 800, "confirm_code": "AB12"}

# The nine executor outcome kinds, hard-coded.
EXECUTOR_KINDS = ["confirmed", "already_done", "approval_invalid", "needs_reapproval", "slot_taken",
                  "slot_conflict", "failed", "unknown", "limit_reached"]
EXPECTED = {"confirmed": "DONE", "already_done": "DONE", "approval_invalid": "NEEDS_INPUT",
            "needs_reapproval": "NEEDS_INPUT", "slot_taken": "NEEDS_INPUT", "slot_conflict": "NEEDS_INPUT",
            "failed": "TOOL_FAILURE", "unknown": "TOOL_FAILURE", "limit_reached": "LIMIT_REACHED"}


@pytest.mark.parametrize("kind", EXECUTOR_KINDS)
def test_every_executor_outcome_gets_a_status_and_a_message(kind):
    f = finalize(Exit(kind, reason="x", action="book", booking=BOOKING, proposal=PROPOSAL))
    assert f.status == EXPECTED[kind] and f.message.strip()


@pytest.mark.parametrize("kind", ["declined", "expired", "proposal_ready", "ask", "answer", "limit",
                                  "slot_unavailable", "booking_not_found", "hold_failed", "model_error", "unexpected"])
def test_every_graph_exit_gets_a_message(kind):
    f = finalize(Exit(kind, proposal=PROPOSAL, text="hi"))
    assert f.status == STATUS_BY_KIND[kind] and f.message.strip()


def test_an_unmapped_kind_still_yields_a_failure_message():
    f = finalize(Exit("some_new_kind"))
    assert f.status == "TOOL_FAILURE" and f.message.strip() and f.reason == "unmapped_kind"


@pytest.mark.parametrize("kind", ["confirmed", "already_done"])
def test_success_is_never_claimed_without_a_verified_booking(kind):
    f = finalize(Exit(kind, action="book", booking=None))
    assert f.status == "TOOL_FAILURE" and f.reason == "unverified_success"


def test_waiting_state_follows_the_exit():
    assert finalize(Exit("proposal_ready", proposal=PROPOSAL)).waiting == "approval"
    assert finalize(Exit("needs_reapproval", proposal=PROPOSAL)).waiting == "approval"
    assert finalize(Exit("ask", text="area?")).waiting == "input"
    assert finalize(Exit("confirmed", action="book", booking=BOOKING)).waiting is None
    assert finalize(Exit("limit", reason="wall_clock")).waiting is None


def test_approval_request_comes_from_stored_terms_and_shows_the_confirm_phrase():
    f = finalize(Exit("proposal_ready", proposal={**PROPOSAL, "price": 950, "price_changed": True, "search_price": 800}))
    for needle in ("Acme", "2026-11-03T09:00", "₹950", "₹800", "CONFIRM AB12"):
        assert needle in f.message


def test_limit_messages_name_the_limit():
    for reason, word in (("model_turns", "model-turn"), ("tool_calls", "tool-call"), ("wall_clock", "time")):
        assert word in finalize(Exit("limit", reason=reason)).message
