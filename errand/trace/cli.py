"""Trace viewer and saver.

  python -m errand.trace.cli show <run_id | path.jsonl> [--dir traces]
  python -m errand.trace.cli save <run_id> [--dir traces] [--out DIR_OR_FILE.jsonl] [--force]

``show`` prints one aligned line per event, then totals. ``save`` is a plain copy of the run's JSONL file.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
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
        return f"turn {d.get('turn')} -> {calls}  in={d.get('input_tokens')} out={d.get('output_tokens')} " \
               f"{'$%.6f' % cost if cost is not None else 'cost n/a'} ({d.get('model')})"
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


def render(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(empty trace)"
    run = rows[0].get("run") or "_session"
    lines = [f"trace {run}  events={len(rows)}  started {rows[0]['ts']}", ""]
    tin = tout = model_calls = tool_calls = 0
    cost, any_cost = 0.0, False
    for r in rows:
        lat = f"{r['latency_s']:.3f}s" if "latency_s" in r else ""
        lines.append(f"{r['t']:8.3f}  {r['event']:<17} {_clip(summary(r)):<{WIDTH}} {lat:>8}")
        if r["event"] == "model_call":
            model_calls += 1
            tin += r["data"].get("input_tokens", 0)
            tout += r["data"].get("output_tokens", 0)
            if r.get("cost_usd") is not None:
                cost, any_cost = cost + r["cost_usd"], True
        elif r["event"] == "tool_call":
            tool_calls += 1
    lines += ["", f"totals: model_calls={model_calls} tokens_in={tin} tokens_out={tout} "
                  f"cost={'$%.6f' % cost if any_cost else 'n/a'} tool_calls={tool_calls} wall={rows[-1]['t']:.3f}s"]
    return "\n".join(lines)


def _find(arg: str, trace_dir: str) -> Path | None:
    p = Path(arg)
    if p.suffix == ".jsonl" and p.exists():
        return p
    p = trace_path(trace_dir, arg)
    return p if p.exists() else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="errand.trace.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show", help="pretty-print a run's trace")
    show.add_argument("run")
    save = sub.add_parser("save", help="copy one run's trace file")
    save.add_argument("run")
    save.add_argument("--out")
    save.add_argument("--force", action="store_true")
    for p in (show, save):
        p.add_argument("--dir", default="traces")
    a = ap.parse_args(argv)

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
