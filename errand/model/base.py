"""Provider-neutral model interface. ``ScriptedModel`` (offline) and the Step 8 ``AnthropicClient``
both implement ``ModelClient``; the graph only ever sees these types."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


def approx_tokens(text: str) -> int:
    """Deterministic token approximation (4 chars per token). Shared with the context builder."""
    return math.ceil(len(text) / 4) if text else 0


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None  # tool name, for role == "tool"


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0  # recorded when the API reports them; not priced
    cache_write_tokens: int = 0
    source: str = "synthetic"  # "api" = counts reported by the API response; "synthetic" = ScriptedModel


@dataclass(frozen=True)
class ModelRequest:
    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.tools)

    def approx_input_tokens(self) -> int:
        parts = [self.system]
        for m in self.messages:
            parts.append(m.content)
            parts.extend(json.dumps(c.args, sort_keys=True) for c in m.tool_calls)
        return sum(approx_tokens(p) for p in parts) + sum(approx_tokens(t.description) for t in self.tools)


@dataclass(frozen=True)
class ModelTurn:
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = Usage()
    stop_reason: str = "end_turn"
    request_id: str | None = None  # the API's request id, for debugging real calls


class ModelError(Exception):
    """The model backend failed (API down, bad response)."""


class ModelClient(Protocol):
    def complete(self, request: ModelRequest) -> ModelTurn: ...
