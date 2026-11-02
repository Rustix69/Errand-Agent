"""Record the timeout-after-success trace deliverable, offline and deterministically.

  python -m demo.sample_trace [--out artifacts/traces/timeout-after-success]

It drives the REAL composition root (``open_demo`` + ``Chat``, the same tracer wiring as the CLI) with a
ScriptedModel and a fake clock, and copies the two run files that matter:
  run_000001.jsonl  "find a plumber ..."  model calls, search, hold, proposal
  run_000002.jsonl  "yes"                  approval by code, book times out AFTER committing, verified through
                                           get_booking_status, confirmed
Token counts in a scripted run are synthetic (usage_source "synthetic") and the cost is the scripted stand-in
price, so the trace is labelled as such. A live trace can be saved with
``python -m errand.trace.cli save <run_id> --dir .errand/traces --out artifacts/traces/``.
A test regenerates this and requires the committed files to be byte-identical.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from demo.chat import Chat, open_demo
from errand.clock import FakeClock
from errand.config import Settings
from errand.model import ScriptedModel, call, last_tool_json

DEFAULT_OUT = Path("artifacts/traces/timeout-after-success")
LINES = ["/timeout", "find a plumber in Indiranagar this week", "yes", "/quit"]
RUN_FILES = ("run_000001.jsonl", "run_000002.jsonl")


def _search(request):
    return call("search_providers", service="plumber", area="Indiranagar",
                date_range={"start": "2026-11-02", "end": "2026-11-08"})


def _pick_first(request):
    p = last_tool_json(request)["providers"][0]
    return call("propose_booking", provider_id=p["provider_id"], slot=p["slots"][0])


def record(out_dir: Path) -> list[Path]:
    lines = iter(LINES)
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock()
        app, demo = open_demo(Settings(api_key_set=True), ScriptedModel([_search, _pick_first]), tmp, clock=clock)
        try:
            Chat(app, demo, lambda text: None, fake_clock=clock).run(lambda prompt: next(lines))
        finally:
            app.close()
            demo.close()
        out_dir.mkdir(parents=True, exist_ok=True)
        return [Path(shutil.copyfile(Path(tmp) / "traces" / name, out_dir / name)) for name in RUN_FILES]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    for path in record(Path(ap.parse_args(argv).out)):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
