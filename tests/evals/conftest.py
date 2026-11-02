"""Eval recorder. Each scenario declares `expected` and records `actual` (from DB state / trace events) through
the `eval_case` / `scenario` fixtures; a hook writes artifacts/eval_table.md (see table.py) after a run that covered
every eval file: scenario, expected, actual, pass/fail, notes. Wording of replies is never asserted."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.agent.conftest import make_agent  # noqa: F401  (shared fixture)
from tests.evals.harness import Run, Turn
from tests.evals.table import is_full_run, render_table

ARTIFACTS = Path(__file__).resolve().parents[2] / "artifacts"
EVALS_DIR = Path(__file__).resolve().parent
_RECORDS: list["EvalRecord"] = []


@dataclass
class EvalRecord:
    scenario: str
    expected: str
    actual: str = ""
    notes: str = ""
    outcome: str = "not run"
    id: str = ""
    extra: dict = field(default_factory=dict)


@pytest.fixture
def eval_case(request):
    def _declare(scenario: str, expected: str, notes: str = "", id: str = "") -> EvalRecord:
        rec = EvalRecord(scenario, expected, notes=notes, id=id)
        request.node._eval_record = rec
        _RECORDS.append(rec)
        return rec

    return _declare


@pytest.fixture
def scenario(make_agent, eval_case):
    """scenario(id, name, expected, script, user=[...], notes=..., env_kw=..., seed=..., ...) -> Run"""

    def _go(sid: str, name: str, expected: str, script, user: list[str | Turn] = (), *, notes: str = "",
            env_kw: dict | None = None, **agent_kw) -> Run:
        rec = eval_case(name, expected, notes, id=sid)
        run = Run(make_agent, make_agent(script, env_kw=env_kw, **agent_kw), rec)
        run.record()
        run.play(list(user))
        return run

    return _go


def pytest_collection_finish(session):
    """Remember whether this run collected EVERY eval test; a partial run must not overwrite the full table."""
    collected: dict[str, set[str]] = {}
    for item in session.items:
        path = Path(str(item.fspath))
        if path.parent == EVALS_DIR:
            collected.setdefault(path.name, set()).add(getattr(item, "originalname", None) or item.name)
    session.config._evals_full = is_full_run(EVALS_DIR, collected)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    rec = getattr(item, "_eval_record", None)
    if rec is not None and rep.when == "call":
        rec.outcome = "pass" if rep.passed else "FAIL"


def pytest_sessionfinish(session, exitstatus):
    if not _RECORDS or not getattr(session.config, "_evals_full", False):
        return
    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / "eval_table.md").write_text(render_table(_RECORDS))


def pytest_terminal_summary(terminalreporter):
    if _RECORDS and not getattr(terminalreporter.config, "_evals_full", False):
        terminalreporter.write_line("partial eval run: artifacts/eval_table.md was NOT updated (run all of tests/evals)")
