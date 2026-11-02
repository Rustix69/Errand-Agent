"""AnthropicClient against a FAKE SDK client: no key, no network. Asserts what goes on the wire and how the
response is mapped, never model behaviour."""

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from errand.agent.tools import MODEL_TOOL_NAMES, MODEL_TOOLS
from errand.config import ConfigError, Settings
from errand.model import AnthropicClient, Message, ModelError, ModelRequest, ToolCall, to_api_messages

REQ = httpx2.Request("POST", "https://api.invalid/v1/messages")


def api_error(cls, message, status=400):
    return cls(message, response=httpx2.Response(status, request=REQ), body=None)


def block(**kw):
    return SimpleNamespace(**kw)


def response(content, *, stop="end_turn", usage=None, rid="req_test", details=None):
    u = usage or SimpleNamespace(input_tokens=11, output_tokens=7, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(content=content, stop_reason=stop, usage=u, stop_details=details, _request_id=rid)


class FakeMessages:
    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def create(self, **kw):
        self.calls.append(kw)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(*items, **kw):
    fake = SimpleNamespace(messages=FakeMessages(*items))
    return AnthropicClient("claude-sonnet-5-5", client=fake, **kw), fake.messages


def request(*messages, system="SYS"):
    return ModelRequest(system, tuple(messages), MODEL_TOOLS)


# ----------------------------------------------------------------------------- what goes on the wire


def test_the_wire_request_has_exactly_the_seven_tools_and_no_forced_or_sampling_params():
    c, calls = client(response([block(type="text", text="hi")]))
    c.complete(request(Message("user", "hello")))
    (kw,) = calls.calls
    assert [t["name"] for t in kw["tools"]] == list(MODEL_TOOL_NAMES) and len(kw["tools"]) == 7
    assert all(set(t) == {"name", "description", "input_schema"} for t in kw["tools"])
    assert kw["model"] == "claude-sonnet-5-5" and kw["system"] == "SYS" and kw["max_tokens"] == 2048
    assert not {"temperature", "top_p", "top_k", "tool_choice"} & set(kw)  # auto tool choice, no sampling
    assert kw["thinking"] == {"type": "between_tools"} and kw["output_config"] == {"effort": "medium"}


def test_messages_map_history_tool_calls_and_merge_consecutive_tool_results():
    msgs = (
        Message("user", "earlier question"), Message("assistant", "earlier answer"),
        Message("user", "find a plumber"),
        Message("assistant", "", (ToolCall("tu_1", "search_providers", {"service": "plumber"}),
                                   ToolCall("tu_2", "list_bookings", {}))),  # no text: no empty text block
        Message("tool", '{"a":1}', tool_call_id="tu_1", name="search_providers"),
        Message("tool", '{"b":2}', tool_call_id="tu_2", name="list_bookings"),
        Message("assistant", "let me check", (ToolCall("tu_3", "get_provider", {"provider_id": "pl_01"}),)),
        Message("tool", '{"c":3}', tool_call_id="tu_3", name="get_provider"),
    )
    assert to_api_messages(msgs) == [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "find a plumber"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_1", "name": "search_providers", "input": {"service": "plumber"}},
            {"type": "tool_use", "id": "tu_2", "name": "list_bookings", "input": {}}]},
        {"role": "user", "content": [  # ONE message answering both tool_use blocks
            {"type": "tool_result", "tool_use_id": "tu_1", "content": '{"a":1}'},
            {"type": "tool_result", "tool_use_id": "tu_2", "content": '{"b":2}'}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "tu_3", "name": "get_provider", "input": {"provider_id": "pl_01"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_3", "content": '{"c":3}'}]},
    ]


# ----------------------------------------------------------------------------- what comes back


def test_text_tool_calls_and_real_usage_are_mapped_and_thinking_blocks_ignored():
    usage = SimpleNamespace(input_tokens=1200, output_tokens=85, cache_read_input_tokens=900, cache_creation_input_tokens=50)
    c, _ = client(response([block(type="thinking", thinking="..."), block(type="text", text="Searching."),
                            block(type="tool_use", id="tu_9", name="search_providers", input={"service": "plumber"})],
                           stop="tool_use", usage=usage, rid="req_abc"))
    turn = c.complete(request(Message("user", "x")))
    assert turn.text == "Searching." and turn.tool_calls == (ToolCall("tu_9", "search_providers", {"service": "plumber"}),)
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (1200, 85)
    assert (turn.usage.cache_read_tokens, turn.usage.cache_write_tokens, turn.usage.source) == (900, 50, "api")
    assert turn.request_id == "req_abc" and turn.stop_reason == "tool_use"


def test_refusals_truncated_tool_calls_and_api_errors_become_model_errors():
    refusal = response([], stop="refusal", details=SimpleNamespace(category="cyber"))
    c, _ = client(refusal)
    with pytest.raises(ModelError, match="refusal.*cyber"):
        c.complete(request(Message("user", "x")))
    cut = response([block(type="tool_use", id="t", name="list_bookings", input={})], stop="max_tokens")
    c, _ = client(cut)
    with pytest.raises(ModelError, match="max_tokens"):
        c.complete(request(Message("user", "x")))
    for err in (api_error(anthropic.RateLimitError, "slow down", 429), anthropic.APIConnectionError(request=REQ),
                api_error(anthropic.AuthenticationError, "invalid x-api-key", 401),
                api_error(anthropic.BadRequestError, "messages: roles must alternate")):
        c, _ = client(err)
        with pytest.raises(ModelError):
            c.complete(request(Message("user", "x")))
    c, _ = client(response([block(type="text", text="partial")], stop="max_tokens"))
    assert c.complete(request(Message("user", "x"))).text == "partial"  # a cut-off TEXT reply is still usable


# ----------------------------------------------------------------------------- thinking settings the API rejects


def test_a_rejected_thinking_setting_steps_down_to_the_simplest_the_api_accepts():
    events = []
    ok = response([block(type="text", text="fine")])
    c, calls = client(api_error(anthropic.BadRequestError, "thinking.type: between_tools is not supported"),
                      api_error(anthropic.BadRequestError, "output_config.effort is not supported"), ok,
                      response([block(type="text", text="again")]),
                      emit=lambda name, data: events.append((name, data)))
    assert c.complete(request(Message("user", "x"))).text == "fine"
    first, second, third = calls.calls
    assert "thinking" in first and "output_config" in first
    assert "thinking" not in second and "output_config" in second  # step 1: drop `thinking`
    assert "thinking" not in third and "output_config" not in third  # step 2: drop `effort` too
    assert [n for n, _ in events] == ["model_setting_fallback"] * 2
    assert events[0][1] == {"rejected": {"thinking": "between_tools", "effort": "medium"}, "using": {"thinking": None, "effort": "medium"}}
    assert c.complete(request(Message("user", "y"))).text == "again" and len(calls.calls) == 4  # remembered: no retry dance


def test_it_never_echoes_thinking_blocks_even_when_the_api_demands_them():
    demand = api_error(anthropic.BadRequestError, "thinking blocks in the last assistant message must be passed back")
    c, calls = client(demand, demand, demand, demand)
    with pytest.raises(ModelError, match="thinking"):
        c.complete(request(Message("user", "x"), Message("assistant", "", (ToolCall("t", "list_bookings", {}),)),
                           Message("tool", "{}", tool_call_id="t", name="list_bookings")))
    sent = [m for kw in calls.calls for m in kw["messages"]]
    assert not any(b.get("type") in ("thinking", "redacted_thinking")
                   for m in sent if isinstance(m["content"], list) for b in m["content"])


def test_an_unrelated_bad_request_is_not_retried():
    c, calls = client(api_error(anthropic.BadRequestError, "messages: roles must alternate"))
    with pytest.raises(ModelError):
        c.complete(request(Message("user", "x")))
    assert len(calls.calls) == 1 and c.thinking == "between_tools"


def test_settings_without_a_key_fail_clearly_before_any_client_exists():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        AnthropicClient.from_settings(Settings(api_key_set=False))
