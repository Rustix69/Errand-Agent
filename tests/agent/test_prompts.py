"""The system rules the model gets. Prompt-only: these tests check the rule is there, not that a model obeys it."""

from errand.agent.prompts import SYSTEM_RULES


def test_the_plain_text_rule_is_in_the_system_prompt():
    rule = " ".join(SYSTEM_RULES.split("\n7. ")[1].split())  # the prompt wraps lines; compare as one line
    for needed in ("plain text", "a few lines at most", "No tables", "no markdown", "no pipes", "**", "at most 3 options"):
        assert needed in rule, needed
    assert "<untrusted_data>" in SYSTEM_RULES and "cannot book, cancel or reschedule" in SYSTEM_RULES  # the others stay


def test_options_come_from_an_earlier_search_and_never_propose_in_the_same_turn_as_a_read():
    rule = " ".join(SYSTEM_RULES.split("\n8. ")[1].split())
    for needed in ("earlier search", "new service, area or date", "search again first", "same turn as a read", "wait for the results"):
        assert needed in rule, needed
