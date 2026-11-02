"""Trace viewer and saver.

  python -m errand.trace.cli show <run_id | path.jsonl> [--dir .errand/traces]
  python -m errand.trace.cli save <run_id> [--dir .errand/traces] [--out DIR_OR_FILE.jsonl] [--force]
  python -m errand.trace.cli follow [--dir .errand/traces] [--cols N] [--all | --from-now] [--no-color]

``show`` prints one aligned line per event, then totals. ``save`` is a plain copy of the run's JSONL file.
``follow`` is the live view for a second terminal pane: it tails the trace files like ``tail -f``, starts a new
section whenever a new run (or proactive poll) begins, and fits the pane width.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from errand.trace.logger import read_trace, trace_path

WIDTH = 78


def _clip(s: str, n: int = WIDTH) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _compact(d: dict[str, Any], skip: tuple[str, ...] = ("run_id",)) -> str:
    return " ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in d.items() if k not in skip)


def summary(row: dict[str, Any]) -> str:
    e, d = row["event"], row.get("data", {})
    if e in ("node_enter", "node_exit"):
        extra = " ".join(str(d[k]) for k in ("exit_kind", "waiting") if d.get(k))
        return f"{d.get('node')} {extra}".strip()
    if e == "model_call":
        cost = row.get("cost_usd")
        calls = ",".join(d.get("tool_calls") or []) or "reply"
        usage = " api" if d.get("usage_source") == "api" else ""
        return f"turn {d.get('turn')} -> {calls}  in={d.get('input_tokens')} out={d.get('output_tokens')}{usage} " \
               f"{'~$%.6f est' % cost if cost is not None else 'cost n/a'} ({d.get('model')})"
    if e == "tool_call":
        return f"{d.get('tool')} {json.dumps(d.get('args'), ensure_ascii=False, sort_keys=True)}"
    if e == "tool_output":
        return f"{d.get('tool')} -> {d.get('content')}"
    if e == "tool_result":
        sm = d.get("summary") or {}
        keys = sorted(sm, key=lambda k: (k != "status", k))  # status first: it is what the reader wants
        return f"{d.get('tool')} " + " ".join(f"{k}={sm[k]}" for k in keys)
    if e == "tool_error":
        return f"{d.get('tool')} {d.get('code')} retryable={d.get('retryable')}"
    if e == "final":
        return f"{d.get('status')} / {d.get('kind')}" + (f" ({d['reason']})" if d.get("reason") else "")
    return _compact(d)


def render_row(r: dict[str, Any], width: int = WIDTH) -> str:
    lat = f"{r['latency_s']:.3f}s" if "latency_s" in r else ""
    return f"{r['t']:8.3f}  {r['event']:<17} {_clip(summary(r), width):<{width}} {lat:>8}"


def render(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(empty trace)"
    run = rows[0].get("run") or "_session"
    lines = [f"trace {run}  events={len(rows)}  started {rows[0]['ts']}", ""]
    tin = tout = model_calls = tool_calls = 0
    cost, any_cost = 0.0, False
    for r in rows:
        lines.append(render_row(r))
        if r["event"] == "model_call":
            model_calls += 1
            tin += r["data"].get("input_tokens", 0)
            tout += r["data"].get("output_tokens", 0)
            if r.get("cost_usd") is not None:
                cost, any_cost = cost + r["cost_usd"], True
        elif r["event"] == "tool_call":
            tool_calls += 1
    lines += ["", f"totals: model_calls={model_calls} tokens_in={tin} tokens_out={tout} "
                  f"cost={'≈$%.6f (estimate)' % cost if any_cost else 'n/a'} tool_calls={tool_calls} wall={rows[-1]['t']:.3f}s"]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- follow (live pane)

_COLORS = {"model_call": "36", "tool_call": "33", "tool_error": "31", "limit_reached": "31", "untrusted_scan": "35",
           "tool_refused": "35", "approval_invalid": "31", "attempt_failed": "31", "attempt_confirmed": "32",
           "verified": "32", "poll_decision": "35", "poll_detected": "35", "poll_start": "2", "poll_end": "2"}


def _paint(row: dict[str, Any], line: str) -> str:
    code = _COLORS.get(row["event"])
    if row["event"] == "final":
        code = "32" if row.get("data", {}).get("status") in ("DONE", "NEEDS_INPUT") else "31"
    return f"\033[{code}m{line}\033[0m" if code else line


class Follower:
    """Tails ``<dir>/*.jsonl``. ``poll()`` returns the new lines (plain or coloured) since the last call."""

    def __init__(self, trace_dir: str | Path, cols: int | None = None, color: bool = False,
                 replay: str = "latest") -> None:
        self.dir, self.color, self.replay = Path(trace_dir), color, replay
        cols = cols or shutil.get_terminal_size((100, 24)).columns
        self.width = max(20, cols - 37)  # 8 (t) + 2 + 17 (event) + 1 + 1 + 8 (latency)
        self.offsets: dict[Path, int] = {}
        self.current: Path | None = None
        self._started = False

    def _files(self) -> list[Path]:
        return sorted(self.dir.glob("*.jsonl"), key=lambda p: (p.stat().st_mtime, p.name)) if self.dir.exists() else []

    def _read_new(self) -> list[tuple[Path, dict[str, Any]]]:
        """New complete rows since the last call, in file order, with the file each came from."""
        rows: list[tuple[Path, dict[str, Any]]] = []
        files = self._files()
        if not self._started:
            self._started = True
            for i, p in enumerate(files):
                replay = self.replay == "all" or (self.replay == "latest" and i == len(files) - 1)
                self.offsets[p] = 0 if replay else p.stat().st_size
        for p in files:
            self.offsets.setdefault(p, 0)  # a file that appeared after we started: read it from the top
            with p.open("rb") as f:
                f.seek(self.offsets[p])
                data = f.read()
            end = data.rfind(b"\n") + 1  # only whole lines; a half-written one waits for the next poll
            if not end:
                continue
            self.offsets[p] += end
            rows += [(p, json.loads(raw)) for raw in data[:end].decode("utf-8").splitlines() if raw.strip()]
        return rows

    def poll_rows(self) -> list[dict[str, Any]]:
        """The same tail as ``poll`` but as row dicts (the web UI colour-codes them itself)."""
        return [row for _, row in self._read_new()]

    def poll(self) -> list[str]:
        out: list[str] = []
        for p, row in self._read_new():
            if self.current != p:
                self.current = p
                out.append(f"── {p.stem} " + "─" * max(0, self.width + 20 - len(p.stem)))
            line = render_row(row, self.width)
            out.append(_paint(row, line) if self.color else line)
        return out


def follow(trace_dir: str | Path, out: Callable[[str], None] = print, *, cols: int | None = None, color: bool = False,
           replay: str = "latest", poll_s: float = 0.25, stop: Callable[[], bool] | None = None) -> None:
    f = Follower(trace_dir, cols, color, replay)
    try:
        while not (stop and stop()):
            for line in f.poll():
                out(line)
            time.sleep(poll_s)
    except KeyboardInterrupt:
        pass


def _find(arg: str, trace_dir: str) -> Path | None:
    p = Path(arg)
    if p.suffix == ".jsonl" and p.exists():
        return p
    p = trace_path(trace_dir, arg)
    return p if p.exists() else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="errand.trace.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    fol = sub.add_parser("follow", help="live view for a second pane: tail the trace files")
    fol.add_argument("--dir", default=".errand/traces")
    fol.add_argument("--cols", type=int)
    fol.add_argument("--all", action="store_true", help="replay every existing trace first")
    fol.add_argument("--from-now", action="store_true", help="show only what happens from now on")
    fol.add_argument("--no-color", action="store_true")
    show = sub.add_parser("show", help="pretty-print a run's trace")
    show.add_argument("run")
    save = sub.add_parser("save", help="copy one run's trace file")
    save.add_argument("run")
    save.add_argument("--out")
    save.add_argument("--force", action="store_true")
    for p in (show, save):
        p.add_argument("--dir", default=".errand/traces")
    a = ap.parse_args(argv)
    if a.cmd == "follow":
        follow(a.dir, cols=a.cols, color=sys.stdout.isatty() and not a.no_color,
               replay="all" if a.all else "none" if a.from_now else "latest")
        return 0

    src = _find(a.run, a.dir)
    if src is None:
        print(f"no trace for {a.run!r} in {a.dir!r}", file=sys.stderr)
        return 2
    if a.cmd == "show":
        print(render(read_trace(src)))
        return 0
    out = Path(a.out) if a.out else Path("artifacts/traces") / src.name
    if out.is_dir() or out.suffix != ".jsonl":  # a directory (existing or new), not a file name
        out = out / src.name
    if out.exists() and not a.force:
        print(f"{out} exists; use --force to overwrite", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, out)
    print(f"saved {src} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
