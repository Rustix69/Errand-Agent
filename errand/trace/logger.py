"""One JSONL file per run: ``<dir>/<run_id>.jsonl``. Events outside any run (startup recovery) go to
``<dir>/_session.jsonl``.

``TraceLogger`` is a plain callable, so it plugs in wherever ``emit(name, data)`` is accepted
(``Runtime.emit`` and the executor's ``on_event``). Pass the SAME instance to both.

Each line: ``{run, seq, ts, t, event, data}`` plus ``latency_s`` / ``cost_usd`` when known.
* ``ts`` is ``clock.now()`` and ``t`` is seconds since the run's first event on ``clock.monotonic()``, so
  with a FakeClock a trace is fully deterministic.
* ``latency_s`` comes from the emitter for model calls and graph nodes; for core tool calls (which cannot
  be changed) the logger pairs ``tool_call`` with the next ``tool_result`` / ``tool_error`` of that tool.
* Values are sanitized at this boundary: long strings are capped, secret-looking keys redacted. Raw user
  text never reaches events (only its length), and tool output arrives already wrapped as untrusted data.
* Tracing must never change behaviour: any failure while recording is counted in ``write_errors`` and
  swallowed (``BaseException`` such as a simulated process kill still propagates).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from errand.clock import Clock
from errand.trace.tools import SummarizingTools

# USD per 1M tokens (input, output). Only the offline model has an entry: its token counts are synthetic
# and its price is a stand-in so offline traces look like real ones. A model missing from the table gets
# cost_usd = null; real prices are configured when the real client is added.
PRICES: dict[str, tuple[float, float]] = {"scripted": (3.0, 15.0)}

SESSION_TRACE = "_session"
_REDACT = frozenset({"api_key", "apikey", "authorization", "password", "secret", "token"})
_PAIRS = {"tool_result": "tool_call", "tool_error": "tool_call", "execute_outcome": "execute_start"}


def trace_path(trace_dir: str | Path, run_id: str | None) -> Path:
    return Path(trace_dir) / f"{run_id or SESSION_TRACE}.jsonl"


def read_trace(path: str | Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def sanitize(obj: Any, cap: int, depth: int = 0) -> Any:
    if depth > 6:
        return "…"
    if isinstance(obj, str):
        return obj if len(obj) <= cap else f"{obj[:cap]}…[+{len(obj) - cap} chars]"
    if isinstance(obj, dict):
        return {str(k): "[redacted]" if str(k).lower() in _REDACT else sanitize(v, cap, depth + 1)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = list(obj)
        out = [sanitize(v, cap, depth + 1) for v in items[:30]]
        return out + [f"…[+{len(items) - 30} items]"] if len(items) > 30 else out
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    return sanitize(str(obj), cap, depth + 1)


class TraceLogger:
    def __init__(self, clock: Clock, trace_dir: str | Path, prices: dict[str, tuple[float, float]] | None = None,
                 cap: int = 2000) -> None:
        self.clock, self.dir, self.cap = clock, Path(trace_dir), cap
        self.prices = dict(PRICES if prices is None else prices)
        self.write_errors = 0
        self.dir.mkdir(parents=True, exist_ok=True)
        self._current: str | None = None
        self._start: dict[str, float] = {}  # run -> monotonic time of its first event
        self._seq: dict[str, int] = {}
        self._open: dict[tuple[str | None, str, str], list[float]] = {}
        self._summaries: dict[str, dict[str, Any]] = {}  # tool -> summary of its latest successful result

    def note_result(self, tool: str, summary: dict[str, Any]) -> None:
        """Called by the tools wrapper just before the core emits that tool's ``tool_result``."""
        self._summaries[tool] = summary

    def wrap_tools(self, tools: Any) -> SummarizingTools:
        """Wire this where the agent's tools are built, so `tool_result` rows show what came back."""
        return SummarizingTools(tools, self.note_result)

    def __call__(self, name: str, data: dict[str, Any]) -> None:
        try:
            self._record(name, data)
        except Exception:
            self.write_errors += 1

    def _record(self, name: str, data: dict[str, Any]) -> None:
        run = data.get("run_id") or self._current
        if run:
            self._current = run
        mono = self.clock.monotonic()
        path = trace_path(self.dir, run)
        key = run or SESSION_TRACE
        start = self._start.setdefault(key, mono)
        if key not in self._seq:  # first event of this run in this process: continue after what's on disk
            self._seq[key] = len(path.read_text().splitlines()) if path.exists() else 0

        row: dict[str, Any] = {"run": run, "seq": self._seq[key], "ts": self.clock.now().isoformat(timespec="milliseconds"),
                               "t": round(mono - start, 3), "event": name}
        body = {k: v for k, v in data.items() if k != "latency_s"}
        if name == "tool_result" and (summary := self._summaries.pop(str(body.get("tool")), None)):
            body["summary"] = summary
        latency = data.get("latency_s")
        label = str(body.get("tool", "")) if name.startswith("tool_") else ""
        if name in ("tool_call", "execute_start"):
            self._open.setdefault((run, name, label), []).append(mono)
        elif name in _PAIRS:
            stack = self._open.get((run, _PAIRS[name], label))
            if stack:
                latency = mono - stack.pop()
        if latency is not None:
            row["latency_s"] = round(latency, 3)
        if name == "model_call":
            row["cost_usd"] = self._cost(body)
        row["data"] = sanitize(body, self.cap)

        line = json.dumps(row, ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self._seq[key] += 1  # only after a successful write

    def _cost(self, body: dict[str, Any]) -> float | None:
        price = self.prices.get(str(body.get("model")))
        if price is None:
            return None
        return round((body.get("input_tokens", 0) * price[0] + body.get("output_tokens", 0) * price[1]) / 1_000_000, 6)
