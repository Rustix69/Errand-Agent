"""Regression (BUGS #4): a model text reply must not fake an approval prompt or a finished action. Found by clicking
through the web UI with the real model: after a slot_conflict it wrote "reply 'yes' (or CONFIRM 7A2D)" with no proposal,
the user typed that code, and the model answered "Moved and verified: ..." although nothing was called or changed."""

import pytest

from errand.agent.finalize import Exit, claims_completed_action, finalize, scrub_approval_prompts
from errand.agent.prompts import SYSTEM_RULES
from errand.model import call, say
from tests.evals.scripts import pick, search

FIND = "find a plumber in Indiranagar this week"
EXACT_REPLY = "Moved and verified: your AC servicing with CoolBreeze AC Care is now on 2026-11-07T11:00. Booking ID bk_000005."


# ----------------------------------------------------------------------------- the widened claim guard, both sides


def test_the_exact_reply_from_the_web_ui_gets_the_note():
    assert claims_completed_action(EXACT_REPLY)
    f = finalize(Exit("answer", text=EXACT_REPLY))
    assert f.reason == "unverified_claim" and f.message.startswith(EXACT_REPLY) and "Nothing was booked" in f.message


@pytest.mark.parametrize("text", [
    "Rescheduled and verified.", "Booked and verified: Acme at 10:00.", "Your AC servicing has been moved to Saturday.",
    "Your AC servicing is now on Saturday at 11:00.", "Your plumber is now booked for Tuesday.", "It has been moved.",
    "The booking has been moved to 11:00.", "I've verified your booking.", "Verified: your appointment is set for 11:00.",
    "Done! Your cleaning has been cancelled.",
])
def test_completed_and_verified_wording_is_a_claim(text):
    assert claims_completed_action(text)


@pytest.mark.parametrize("text", [
    "Before anything is booked you will approve it.",  # the earlier false alarm (BUGS #2)
    "You'll get a confirmation to approve before anything is booked.",
    "Once it's verified I'll tell you.", "I'll tell you when the move is verified.", "It can't be verified until you approve.",
    "AquaFix | Suresh M. | ₹1,450 | Verified, 30-day warranty",  # a provider badge, not a claim
    "Shree Sai Plumbers is a verified provider with a 4.2 rating.",
    "Your AC servicing is on Nov 5 at 10:00.", "Your AC servicing is booked for Wednesday.",  # describes the existing one
    "I moved on to the next option.", "The 10:00 slot is booked, so I used 11:00.", "Nothing has been moved.",
])
def test_future_hedged_badge_and_descriptive_wording_is_not_a_claim(text):
    assert claims_completed_action(text) is False
    assert finalize(Exit("answer", text=text)).message == text


# ----------------------------------------------------------------------------- fake approval prompts are replaced


PROMPTS = [
    "Ready to move it to 11:00. To confirm, reply 'yes' (or CONFIRM 7A2D).",
    "Shall I go ahead? Type yes to confirm.", "Please reply yes to approve.", "Send CONFIRM 00FF to continue.",
]


@pytest.mark.parametrize("kind", ["answer", "ask"])
@pytest.mark.parametrize("text", PROMPTS)
def test_an_approval_prompt_in_a_model_reply_with_nothing_pending_is_replaced_by_a_note(kind, text):
    f = finalize(Exit(kind, text=text))
    assert f.reason == "fake_approval_prompt" and "CONFIRM" not in f.message and "yes" not in f.message.lower()
    assert "nothing is waiting" in f.message and f.waiting != "approval"


def test_other_sentences_survive_and_only_one_note_is_written():
    out, changed = scrub_approval_prompts("It is 11:00 on Saturday. Reply yes to confirm. Or type yes to proceed. Anything else?")
    assert changed and out.startswith("It is 11:00 on Saturday.") and out.endswith("Anything else?") and out.count("Only the system") == 1


def test_with_a_proposal_pending_the_models_text_is_left_alone():
    text = "That works. Reply 'yes' to confirm."
    f = finalize(Exit("answer", text=text, pending=True))
    assert f.message == text and f.reason is None


def test_a_reply_with_both_a_fake_prompt_and_a_fake_done_gets_both_treatments():
    f = finalize(Exit("answer", text="Booked and verified. Reply yes to confirm."))
    assert f.reason == "fake_approval_prompt+unverified_claim" and "Nothing was booked" in f.message


def test_the_real_approval_request_is_never_scrubbed():
    p = {"proposal_id": "p1", "action": "book", "provider_id": "pl_1", "provider_name": "Acme", "slot": "2026-11-03T09:00",
         "price": 800, "confirm_code": "AB12"}
    f = finalize(Exit("proposal_ready", proposal=p))
    assert "CONFIRM AB12" in f.message and f.waiting == "approval" and f.reason is None


# ----------------------------------------------------------------------------- a stray CONFIRM <code> is answered by code


@pytest.mark.parametrize("text", ["CONFIRM 7A2D", "confirm 7a2d", "Confirm 00FF.", " CONFIRM 7A2D!"])
def test_a_confirm_code_with_nothing_pending_is_answered_by_code_without_the_model(make_agent, text):
    a = make_agent([say("hello")])
    r = a.say(text)
    assert (r.status, r.kind, r.waiting) == ("DONE", "no_pending_approval", None)
    assert a.model.calls == 0 and a.of("stray_confirm") and a.world_mutations() == []
    assert [d["kind"] for d in a.of("decision")] == ["other"]
    assert a.env.store.one("SELECT COUNT(*) AS n FROM approvals")["n"] == 0


@pytest.mark.parametrize("text", ["yes", "ok", "confirm", "confirm m_12", "confirm my area", "please CONFIRM 7A2D for me"])
def test_a_bare_yes_and_other_confirm_wording_still_reach_the_normal_path(make_agent, text):
    a = make_agent([say("Which area?")])
    r = a.say(text)
    assert r.kind != "no_pending_approval" and not a.of("stray_confirm")


def test_a_wrong_code_while_a_proposal_is_pending_goes_to_the_model_as_a_change_request(make_agent):
    a = make_agent([search(), pick(), say("What would you like to change?")])
    a.say(FIND)
    r = a.say("CONFIRM 0000")
    assert r.kind == "answer" and not a.of("stray_confirm") and a.model.calls == 3
    assert a.env.store.one("SELECT COUNT(*) AS n FROM approvals")["n"] == 0
    assert a.env.store.one("SELECT * FROM proposals WHERE status = 'pending'") is not None


def test_the_real_code_still_approves_when_a_proposal_is_pending(make_agent):
    a = make_agent([search(), pick()])
    a.say(FIND)
    code = a.env.store.one("SELECT confirm_code FROM proposals WHERE status = 'pending'")["confirm_code"]
    r = a.say(f"CONFIRM {code}")
    assert r.kind == "confirmed" and not a.of("stray_confirm")


def test_a_confirm_code_right_after_the_result_is_a_repeat_not_a_stray(make_agent):
    a = make_agent([search(), pick()])
    a.say(FIND)
    code = a.env.store.one("SELECT confirm_code FROM proposals WHERE status = 'approved' OR status = 'pending'")["confirm_code"]
    a.say("yes")
    r = a.say(f"CONFIRM {code}")
    assert r.kind == "already_done" and not a.of("stray_confirm") and a.env.count("book") == 1


# ----------------------------------------------------------------------------- the prompt rule (a hint, not the guard)


def test_only_the_system_says_verified_and_the_model_never_writes_approval_prompts():
    rule = " ".join(SYSTEM_RULES.split("\n9. ")[1].split())
    for needed in ("Only the system says", "verified", "Never write an approval prompt", "confirm code",
                   "call the propose tool"):
        assert needed in rule, needed
