"""ScriptedModel: a deterministic, offline ``ModelClient`` for evals and tests. No key, no network.

A script is a list of steps. A step is a ``ModelTurn`` or a ``callable(request) -> ModelTurn``; the
callable form lets a script read the last tool result (to pick a provider) or advance a fake clock.
Running past the script raises ``ScriptExhausted``; leftover steps are caught by ``assert_consumed``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from errand.model.base import ModelRequest, ModelTurn, ToolCall, Usage, approx_tokens

Step = ModelTurn | Callable[[ModelRequest], ModelTurn]


class ScriptExhausted(Exception):
    """The agent asked the model for more turns than the script has."""


def say(text: str) -> ModelTurn:
    return ModelTurn(text=text)


def call(name: str, **args: Any) -> ModelTurn:
    return ModelTurn(tool_calls=(ToolCall("", name, args),), stop_reason="tool_use")


def calls(*pairs: tuple[str, dict[str, Any]]) -> ModelTurn:
    return ModelTurn(tool_calls=tuple(ToolCall("", n, a) for n, a in pairs), stop_reason="tool_use")


def ask(question: str, missing: Sequence[str], known: dict[str, Any] | None = None) -> ModelTurn:
    return call("ask_user", question=question, missing=list(missing), known=known or {})


def last_tool_json(request: ModelRequest) -> Any:
    """The JSON payload of the most recent tool message (for callable steps)."""
    for m in reversed(request.messages):
        if m.role == "tool":
            return json.loads(m.content)
    raise LookupError("no tool message in the request")


class ScriptedModel:
    name = "scripted"

    def __init__(self, steps: Sequence[Step]) -> None:
        self.steps = list(steps)
        self.requests: list[ModelRequest] = []
        self.turns: list[ModelTurn] = []
        self._i = 0
        self._ids = 0

    @property
    def calls(self) -> int:
        return len(self.requests)

    def complete(self, request: ModelRequest) -> ModelTurn:
        self.requests.append(request)
        if self._i >= len(self.steps):
            raise ScriptExhausted(f"script has {len(self.steps)} steps; model call #{self._i + 1} has none")
        step = self.steps[self._i]
        self._i += 1
        turn = step(request) if callable(step) else step
        turn = self._finish(request, turn)
        self.turns.append(turn)
        return turn

    def assert_consumed(self) -> None:
        assert self._i == len(self.steps), f"{len(self.steps) - self._i} scripted step(s) never used"

    def _finish(self, request: ModelRequest, turn: ModelTurn) -> ModelTurn:
        """Assign deterministic tool-call ids and synthetic token counts."""
        fixed = []
        for c in turn.tool_calls:
            self._ids += 1
            fixed.append(c if c.id else replace(c, id=f"call_{self._ids}"))
        out = (turn.text or "") + "".join(c.name + json.dumps(c.args, sort_keys=True) for c in fixed)
        usage = turn.usage if turn.usage != Usage() else Usage(request.approx_input_tokens(), approx_tokens(out))
        return replace(turn, tool_calls=tuple(fixed), usage=usage)
