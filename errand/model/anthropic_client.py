"""AnthropicClient: the real model behind ``ModelClient``. Maps our provider-neutral messages and the 7 model
tools to the Messages API and records the REAL token usage from each response.

* Auth: the SDK reads ``ANTHROPIC_API_KEY`` from the environment. This module never reads, stores or prints it.
* ``tool_choice`` stays ``auto`` (forced tool use is rejected by this model, and the safety rules do not depend
  on it). No sampling parameters are sent.
* Thinking: by default ``thinking={"type": "between_tools"}`` + ``output_config={"effort": ...}`` so there are no
  thinking blocks to carry between our rebuilt-every-turn requests. If the API REJECTS those settings the client
  steps down to the simplest it accepts (drop ``thinking``, then drop ``effort``), remembers that, and says so via
  a ``model_setting_fallback`` event. It does NOT echo thinking blocks back: if the API then demands them, the
  turn fails with a clear ``ModelError`` instead, and that decision is left to the maintainer.
* Failures (API errors, refusals, truncated tool calls) surface as ``ModelError``; the graph turns them into a
  clean TOOL_FAILURE.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anthropic

from errand.config import ConfigError, Settings
from errand.model.base import Message, ModelError, ModelRequest, ModelTurn, ToolCall, Usage

Emit = Callable[[str, dict[str, Any]], None]
_THINKING_WORDS = ("thinking", "between_tools", "effort", "output_config")


def to_api_messages(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
    """user text -> user; assistant (+tool calls) -> assistant blocks; consecutive tool results -> ONE user message
    of tool_result blocks (the API requires every tool_use of a turn answered together)."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = [{"type": "text", "text": m.content}] if m.content else []
            blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args} for c in m.tool_calls]
            out.append({"role": "assistant", "content": blocks if m.tool_calls else m.content})
        else:
            out.append({"role": "user", "content": m.content})
    return out


class AnthropicClient:
    def __init__(self, model: str, *, client: Any = None, thinking: str | None = "between_tools",
                 effort: str | None = "medium", max_tokens: int = 2048, timeout_s: float = 20.0,
                 emit: Emit | None = None) -> None:
        self.name = model  # the trace prices usage by this name
        self.model, self.max_tokens, self.emit = model, max_tokens, emit or (lambda n, d: None)
        self.thinking, self.effort = thinking, effort
        self.client = client if client is not None else anthropic.Anthropic(timeout=timeout_s, max_retries=1)

    @classmethod
    def from_settings(cls, s: Settings, emit: Emit | None = None) -> "AnthropicClient":
        s.require_key()  # a clear error, before any client exists
        return cls(s.model_name, thinking=s.thinking, effort=s.effort, max_tokens=s.max_tokens,
                   timeout_s=s.timeout_s, emit=emit)

    # ------------------------------------------------------------------ request

    def _kwargs(self, req: ModelRequest) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "model": self.model, "max_tokens": self.max_tokens, "system": req.system,
            "messages": to_api_messages(req.messages),
            "tools": [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in req.tools],
        }
        if self.thinking:
            kw["thinking"] = {"type": self.thinking}
        if self.effort:
            kw["output_config"] = {"effort": self.effort}
        return kw

    def complete(self, request: ModelRequest) -> ModelTurn:
        while True:
            kw = self._kwargs(request)
            try:
                resp = self.client.messages.create(**kw)
            except anthropic.BadRequestError as e:
                if self._step_down(str(e)):
                    continue
                raise ModelError(f"BadRequestError: {e}") from None
            except anthropic.APIError as e:
                raise ModelError(f"{type(e).__name__}: {e}") from None
            return self._turn(resp)

    def _step_down(self, message: str) -> bool:
        """The API rejected a thinking/effort setting: use the next simpler one (never thinking-block echoing)."""
        if not any(w in message.lower() for w in _THINKING_WORDS):
            return False
        before = {"thinking": self.thinking, "effort": self.effort}
        if self.thinking:
            self.thinking = None  # omit the param: the simplest setting
        elif self.effort:
            self.effort = None
        else:
            return False
        self.emit("model_setting_fallback", {"rejected": before, "using": {"thinking": self.thinking, "effort": self.effort}})
        return True

    # ------------------------------------------------------------------ response

    def _turn(self, resp: Any) -> ModelTurn:
        texts: list[str] = []
        calls: list[ToolCall] = []
        for b in resp.content:
            if b.type == "text":
                texts.append(b.text)
            elif b.type == "tool_use":
                calls.append(ToolCall(b.id, b.name, dict(b.input or {})))
            # thinking / redacted_thinking blocks are ignored (we never send them back)
        stop = resp.stop_reason
        if stop == "refusal":
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            raise ModelError(f"refusal{f' ({cat})' if cat else ''}")
        if stop == "max_tokens" and calls:
            raise ModelError("the reply was cut off by max_tokens in the middle of a tool call")
        u = resp.usage
        usage = Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0, output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0, source="api",
        )
        return ModelTurn(text="".join(texts) or None, tool_calls=tuple(calls), usage=usage, stop_reason=stop or "end_turn",
                         request_id=getattr(resp, "_request_id", None))
