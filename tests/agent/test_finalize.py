import pytest

from errand.agent.finalize import STATUS_BY_KIND, Exit, claims_completed_action, finalize

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


# ----------------------------------------------------------------------------- the unverified-claim guard (BUGS #2)

OPTIONS_REPLY = """Morning slots from 10:00 (I read "morning" as 10:00 to 12:00), within ₹1,500:

| Provider | Technician | Price | Rating | Morning slots (Nov 3) | Notes |
|---|---|---|---|---|---|
| **AquaFix Services** | Suresh M. (your preferred technician) | ₹1,450 | 3.9 (49) | 10:00, 11:00 | Verified, 30-day warranty, leak fixing |
| **Shree Sai Plumbers** | Imran S. | ₹1,000 | 4.2 (86) | 11:00 | Verified, 13 yrs experience, 90-day warranty |

**Left out:**
- DripStop Experts costs ₹1,750, which is over budget.
- All the slots found are today (Nov 3), apart from one DripStop slot on Nov 4.

**My pick:** AquaFix with Suresh M. at 10:00 today. If you'd rather pay less and get a longer warranty, Shree Sai at 11:00 is a good alternative.

I can't tell from the listings whether either plumber brings spare washers, which you like for your recurring tap issue.

Would you like me to propose a booking for one of these? You'll get a confirmation to approve before anything is booked."""
EXACT = ("Would you like me to propose a booking for one of these? You'll get a confirmation to approve before "
         "anything is booked.")


def test_a_harmless_options_reply_gets_no_note_even_though_it_says_booked_in_the_future():
    """Regression (BUGS #2): the real model wrote "before anything is booked" and the note "Nothing was booked,
    cancelled or changed" was appended, with reason unverified_claim."""
    for text in (EXACT, OPTIONS_REPLY):
        f = finalize(Exit("answer", text=text))
        assert (f.status, f.kind, f.reason) == ("DONE", "answer", None)
        assert f.message == text  # unchanged: no note appended


@pytest.mark.parametrize("text", [
    "All set! I've booked the plumber for you.",  # the S3 scenario
    "I booked it for 10:00.", "I have cancelled your cleaning.", "I've rescheduled your AC servicing to Friday.",
    "Your booking is confirmed.", "Your appointment has been rescheduled to Friday.", "Booked!", "All set, booked.",
    "It's booked.", "Great, that's booked for Tuesday.", "I've confirmed it.", "Done. I just cancelled your booking.",
    "I've booked it, and you can reschedule later.",  # a hedge AFTER the claim does not cancel it
])
def test_first_person_completed_claims_still_get_the_note(text):
    f = finalize(Exit("answer", text=text))
    assert f.reason == "unverified_claim" and f.message.startswith(text) and "Nothing was booked" in f.message


@pytest.mark.parametrize("text", [
    "I can book it once you approve.", "It will be booked after you confirm.", "If you approve, it'll be booked for 10:00.",
    "Nothing is booked yet.", "I haven't booked anything.", "I can't confirm it's booked until you approve.",
    "Want me to cancel it?", "You'll get a confirmation.", "The 10:00 slot is booked, so I used 11:00.",
    "Booked slots are shown below.", "Before anything is booked you will approve it.", "I'll have it booked once you say yes.",
    "I confirmed the slot is free.", "Your AC servicing is booked for Wednesday.",
])
def test_future_conditional_negated_and_descriptive_wording_is_not_a_claim(text):
    assert claims_completed_action(text) is False
    assert finalize(Exit("answer", text=text)).message == text


def test_the_note_is_still_only_ever_added_to_a_model_answer():
    """Executor outcomes and other exits keep their own code-written text; the claim guard is for `answer` only."""
    f = finalize(Exit("ask", text="I've booked nothing yet. Which area?", missing=["area"]))
    assert f.reason is None and f.message == "I've booked nothing yet. Which area?"
