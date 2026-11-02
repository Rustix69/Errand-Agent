"""The written deliverables match the code and the tests: paths exist, numbers are the code's numbers, the trace
excerpt is real, every bug cites a real regression test. A claim that drifts from the code fails here."""

import re
from pathlib import Path

from errand.agent.context import DEFAULT_BUDGET
from errand.agent.poller import PRICE_CAP
from errand.agent.tools import MODEL_TOOLS
from errand.config import PRICE_TABLE
from errand.core import BudgetLimits
from errand.core.approval import ApprovalGuard
from errand.trace import read_trace
from errand.trace.cli import render

ROOT = Path(__file__).resolve().parent.parent
DESIGN, README, BUGS = ((ROOT / n).read_text() for n in ("DESIGN.md", "README.md", "BUGS.md"))
TRACES = ROOT / "artifacts" / "traces" / "timeout-after-success"
SECTIONS = ("State model", "Approval", "Idempotency and recovery", "Memory and context", "Observability", "Trade-offs")


def norm(s: str) -> str:
    return " ".join(s.split())


def paths_in(text: str) -> set[str]:
    """Repo-looking paths in backticks or link targets, e.g. `errand/config.py` or (artifacts/eval_table.md)."""
    found = set(re.findall(r"`((?:[\w.-]+/)+[\w.-]+\.\w+|[\w-]+\.md)`", text)) | set(re.findall(r"\]\(([^)#]+)\)", text))
    return {p for p in found if not p.startswith("http") and "<" not in p and "*" not in p}


# ----------------------------------------------------------------------------- the design note


def test_the_design_note_fits_one_page_and_has_every_required_section():
    assert len(DESIGN.split()) <= 700 and DESIGN.count("\n") <= 64
    for name in SECTIONS:
        assert f"**{name}" in DESIGN, name


def test_numbers_in_the_design_note_are_the_codes_numbers():
    limits, guard_defaults = BudgetLimits(), ApprovalGuard.__init__.__kwdefaults__
    assert "8 model turns, 12 tool calls, 30 s" in DESIGN
    assert (limits.max_model_turns, limits.max_tool_calls, limits.wall_clock_s) == (8, 12, 30.0)
    assert f"lasts {guard_defaults['approval_ttl_s']} s" in DESIGN
    assert f"{DEFAULT_BUDGET:,}-token budget" in DESIGN
    assert f"{len(MODEL_TOOLS)} tools" in DESIGN and len(MODEL_TOOLS) == 7
    assert round((PRICE_CAP - 1) * 100) == 20 and round(PRICE_CAP * 100) == 120
    assert "20% price cap" in DESIGN and "120% of the cancelled booking" in DESIGN
    assert PRICE_TABLE["claude-sonnet-5-5"] == (2.0, 10.0) and "$2 per million" in README and "$10 per million" in README


def test_the_trace_excerpt_in_the_design_note_is_real():
    rendered = {n: norm(render(read_trace(TRACES / n))) for n in ("run_000001.jsonl", "run_000002.jsonl")}
    block = re.search(r"```\n(.*?)```", DESIGN, re.S)[1].strip().splitlines()
    assert len(block) >= 8
    for line in block:
        assert norm(line) in rendered["run_000002.jsonl"], line
    model_line = re.search(r"reads `(turn 1 -> [^`]+)`", norm(DESIGN))[1]
    assert model_line in rendered["run_000001.jsonl"]


# ----------------------------------------------------------------------------- the bugs


def test_three_bugs_each_with_the_five_fields_and_real_regression_tests():
    entries = re.split(r"(?m)^## ", BUGS)[1:]
    assert len(entries) == 3
    for e in entries:
        for field in ("What broke", "Why", "Fix", "Regression test", "How it was found"):
            assert f"**{field}" in e, (e[:60], field)
        cited = re.findall(r"`(tests/[\w/]+\.py)::(\w+)`", e)
        assert cited, e[:60]
        for path, name in cited:
            assert re.search(rf"^\s*(async )?def {name}\(", (ROOT / path).read_text(), re.M), (path, name)
    assert len(re.split(r"(?m)^## ", (ROOT / "artifacts" / "bugs-other.md").read_text())[1:]) == 6


# ----------------------------------------------------------------------------- the README


def test_every_path_the_readme_and_design_note_mention_exists():
    missing = sorted(p for p in paths_in(README) | paths_in(DESIGN) if not (ROOT / p).exists())
    assert missing == []


def test_the_readme_maps_every_deliverable_and_says_what_random_does():
    for needed in ("DESIGN.md", "artifacts/eval_table.md", "BUGS.md", "artifacts/traces/timeout-after-success/",
                   "artifacts/transcripts/", "pytest", "python -m demo", "errand.trace.cli follow"):
        assert needed in README, needed
    assert "70/15/15" in README and "70% success, 15% `slot_taken`, 15% timeout" in README
    assert (ROOT / "artifacts" / "transcripts").is_dir() and any((ROOT / "artifacts" / "transcripts").glob("0*.md"))
