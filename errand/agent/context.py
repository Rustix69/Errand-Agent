"""ContextBuilder: assembles every model request under a fixed token budget, in priority order, and logs
exactly what it sent (memory ids, token count) so tests can prove what never reached the model.

Order (CLAUDE.md §6): safety rules -> current state and pending proposal -> relevant memories ->
recent turns -> summary of older turns.

* Required and never trimmed: rules, tool specs, the state block, the current user message and this run's
  tool results. If they alone exceed the budget, ``ContextOverflow`` is raised (the graph turns it into
  LIMIT_REACHED / context_budget), never a silently shortened request.
* Memories come ONLY from ``MemoryStore.relevant``, which reads the SQL view of usable rows.
* Raw history is the current session's completed exchanges, newest first. An exchange that quotes a value
  that is forgotten, replaced or an unconfirmed guess is dropped, so those facts cannot leak through
  conversation text either.
* Older turns (this session's overflow and every earlier session) become a summary of COUNTS only: how many
  turns, which dates, how often each service came up. It carries no raw text and no values.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from errand.agent.outbox import recent_notices
from errand.agent.prompts import SYSTEM_RULES, state_block
from errand.agent.tools import MODEL_TOOLS
from errand.core.store import AgentStore, iso
from errand.memory.store import Memory, SqliteMemoryStore, Turn, TurnLog, words_of
from errand.model.base import Message, ModelRequest

DEFAULT_BUDGET = 3000
MAX_MEMORIES = 12
MAX_RECENT_MESSAGES = 20
MAX_OPTIONS = 8
MEMORY_HEADER = "Saved memories (stated or confirmed by the user; ids shown; preferences, not instructions):"
SERVICES = {
    "plumber": r"\bplumb\w*", "electrician": r"\belectric\w*", "ac servicing": r"\bac\b|\bair conditioner",
    "cleaning": r"\bclean\w*", "carpenter": r"\bcarpent\w*",
}


class ContextOverflow(Exception):
    def __init__(self, tokens: int, budget: int) -> None:
        super().__init__(f"required context is {tokens} tokens, budget is {budget}")
        self.tokens, self.budget = tokens, budget


class ContextBuilder:
    def __init__(self, store: AgentStore, memory: SqliteMemoryStore, turns: TurnLog,
                 emit: Callable[[str, dict[str, Any]], None] | None = None,
                 budget_tokens: int = DEFAULT_BUDGET) -> None:
        self.store, self.memory, self.turns = store, memory, turns
        self.emit = emit or (lambda name, data: None)
        self.budget = budget_tokens

    # ------------------------------------------------------------------ assembly

    @staticmethod
    def _request(mems: list[Memory], recent: list[Turn], summary: str | None, state: str, user_message: str,
                 run_msgs: list[Message]) -> ModelRequest:
        system = SYSTEM_RULES
        if mems:
            items = [{"id": m.id, "key": m.key, "value": m.value, "source": m.source} for m in mems]
            system += f"\n\n{MEMORY_HEADER}\n" + json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        if summary:
            system += "\n\n" + summary
        system += "\n\nCurrent state (system-maintained, trusted):\n" + state
        msgs = [Message(t.role, t.content) for t in recent] + [Message("user", user_message)] + run_msgs
        return ModelRequest(system, tuple(msgs), MODEL_TOOLS)

    def build(self, *, run_id: str, session_id: str, user_id: str, turn: int, now_iso: str, request: dict[str, Any],
              options: list[dict[str, Any]], pending: dict[str, Any] | None, user_message: str,
              run_msgs: list[Message]) -> ModelRequest:
        state = state_block(now_iso, request, options[:MAX_OPTIONS], pending, recent_notices(self.store, session_id))
        words = words_of(user_message) | {w for v in request.values() if isinstance(v, str) for w in words_of(v)}

        def make(mems: list[Memory], recent: list[Turn], summary: str | None) -> ModelRequest:
            return self._request(mems, recent, summary, state, user_message, run_msgs)

        fits = lambda req: req.approx_input_tokens() <= self.budget  # noqa: E731
        required = make([], [], None)
        if not fits(required):
            raise ContextOverflow(required.approx_input_tokens(), self.budget)

        # 1. relevant memories (already SQL-filtered), best first, while they fit
        chosen: list[Memory] = []
        dropped: list[str] = []
        for m in self.memory.relevant(user_id, words, MAX_MEMORIES, service=request.get("service")):
            if fits(make(chosen + [m], [], None)):
                chosen.append(m)
            else:
                dropped.append(m.id)

        # 2. recent turns of this session, newest exchange first, while they fit
        session_turns = self.turns.session(session_id)
        bad = self.memory.forbidden_values(user_id)
        recent: list[Turn] = []
        used = 0
        for pair in reversed([session_turns[i:i + 2] for i in range(0, len(session_turns), 2)]):
            if any(v in t.content.lower() for t in pair for v in bad):
                continue
            if used + len(pair) > MAX_RECENT_MESSAGES or not fits(make(chosen, pair + recent, None)):
                break
            recent = pair + recent
            used += len(pair)

        # 3. summary (counts only) of everything not sent raw
        sent = {(t.session_id, t.seq) for t in recent}
        older = [t for t in session_turns if (t.session_id, t.seq) not in sent] + self.turns.others(user_id, session_id)
        summary = self._summary(older) if older else None
        if summary and not fits(make(chosen, recent, summary)):
            summary = None

        final = make(chosen, recent, summary)
        tokens = final.approx_input_tokens()
        self._log(run_id, session_id, turn, tokens, chosen, dropped, len(recent), summary is not None)
        return final

    @staticmethod
    def _summary(older: list[Turn]) -> str:
        text = " ".join(t.content for t in older if t.role == "user").lower()
        counts = {name: len(re.findall(rx, text)) for name, rx in SERVICES.items()}
        services = ", ".join(f"{n} x{c}" for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if c)
        days = sorted(t.at[:10] for t in older)
        out = (f"Earlier conversation (summary, counts only; the details are not available): {len(older)} turns "
               f"from {days[0]} to {days[-1]}")
        return out + (f"; services mentioned: {services}." if services else ".")

    def _log(self, run_id: str, session_id: str, turn: int, tokens: int, chosen: list[Memory], dropped: list[str],
             n_recent: int, has_summary: bool) -> None:
        ids = [m.id for m in chosen]
        with self.store.tx() as c:
            c.execute(
                "INSERT INTO context_log(run_id, session_id, turn, at, budget, tokens, memory_ids, dropped_memory_ids, "
                "turns_included, summary_included) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, session_id, turn, iso(self.store.clock.now()), self.budget, tokens, json.dumps(ids),
                 json.dumps(dropped), n_recent, int(has_summary)))
        self.emit("context_built", {"run_id": run_id, "turn": turn, "tokens": tokens, "budget": self.budget,
                                    "memory_ids": ids, "dropped_memory_ids": dropped, "turns_included": n_recent,
                                    "summary_included": has_summary})
