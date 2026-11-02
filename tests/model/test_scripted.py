import json

import pytest

from errand.model import (
    Message, ModelRequest, ScriptedModel, ScriptExhausted, ToolSpec, approx_tokens, ask, call, calls,
    last_tool_json, say,
)

TOOLS = (ToolSpec("t", "a tool", {"type": "object"}),)


def req(*msgs: Message, system: str = "sys") -> ModelRequest:
    return ModelRequest(system, tuple(msgs), TOOLS)


def test_plays_steps_in_order_and_records_requests():
    m = ScriptedModel([say("one"), call("t", a=1), say("three")])
    r1, r2, r3 = req(Message("user", "x")), req(Message("user", "y")), req(Message("user", "z"))
    assert [m.complete(r).text for r in (r1,)] == ["one"]
    t2 = m.complete(r2)
    assert t2.tool_calls[0].name == "t" and t2.tool_calls[0].args == {"a": 1}
    assert m.complete(r3).text == "three"
    assert m.requests == [r1, r2, r3] and m.calls == 3
    m.assert_consumed()


def test_callable_step_sees_the_request_and_the_last_tool_result():
    seen = []

    def step(request):
        seen.append(request)
        return say(str(last_tool_json(request)["n"]))

    m = ScriptedModel([step])
    out = m.complete(req(Message("user", "q"), Message("tool", json.dumps({"n": 7}), tool_call_id="c", name="t")))
    assert out.text == "7" and len(seen) == 1


def test_running_past_the_script_fails_loudly():
    m = ScriptedModel([say("only")])
    m.complete(req())
    with pytest.raises(ScriptExhausted):
        m.complete(req())


def test_unused_steps_are_detected():
    m = ScriptedModel([say("a"), say("b")])
    m.complete(req())
    with pytest.raises(AssertionError, match="1 scripted step"):
        m.assert_consumed()


def test_tool_call_ids_are_deterministic_and_unique():
    m = ScriptedModel([calls(("t", {}), ("t", {"x": 1})), call("t")])
    ids = [c.id for c in m.complete(req()).tool_calls] + [c.id for c in m.complete(req()).tool_calls]
    assert ids == ["call_1", "call_2", "call_3"]


def test_synthetic_usage_is_deterministic_and_tracks_size():
    a = ScriptedModel([say("hello world")]).complete(req(Message("user", "hi")))
    b = ScriptedModel([say("hello world")]).complete(req(Message("user", "hi")))
    assert a.usage == b.usage and a.usage.input_tokens > 0 and a.usage.output_tokens == approx_tokens("hello world")
    big = ScriptedModel([say("hello world")]).complete(req(Message("user", "hi " * 400)))
    assert big.usage.input_tokens > a.usage.input_tokens


def test_ask_helper_builds_an_ask_user_call():
    t = ScriptedModel([ask("Which area?", ["area"], {"service": "plumber"})]).complete(req())
    c = t.tool_calls[0]
    assert c.name == "ask_user" and c.args == {"question": "Which area?", "missing": ["area"], "known": {"service": "plumber"}}
