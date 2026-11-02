"""The eval table generator: columns, ordering, escaping, the required-cases index, and the full-run guard."""

import re
from pathlib import Path
from types import SimpleNamespace

from tests.evals.table import COLUMNS, REQUIRED_CASES, is_full_run, order_key, render_table, eval_test_names

EVALS = Path(__file__).resolve().parent


def rec(i, outcome="pass", scenario="s", expected="e", actual="a", notes=""):
    return SimpleNamespace(id=i, scenario=scenario, expected=expected, actual=actual, notes=notes, outcome=outcome)


def test_columns_are_exactly_scenario_expected_actual_pass_fail_notes():
    assert COLUMNS == ("Scenario", "Expected", "Actual", "Pass/Fail", "Notes")
    text = render_table([rec("B1")], required={})
    assert "| Scenario | Expected | Actual | Pass/Fail | Notes |" in text.splitlines()


def test_rows_are_ordered_escaped_and_counted():
    rows = [rec("P1"), rec("E2"), rec("B1", scenario="a | b\nc", notes="x|y"), rec("S1", outcome="FAIL"), rec("F1", outcome="?")]
    text = render_table(rows, required={})
    body = [ln for ln in text.splitlines() if re.match(r"\| [A-Z]\d ·", ln)]
    assert [ln.split(" · ")[0].strip("| ") for ln in body] == ["B1", "E2", "S1", "F1", "P1"]
    assert "a \\| b c" in body[0] and "x\\|y" in body[0]  # a pipe cannot break the table; newlines collapse
    assert "5 scenarios: 3 pass, 1 FAIL, 1 not run" in text
    cells = [re.split(r"(?<!\\)\|", ln) for ln in body]  # split on unescaped pipes only
    assert all(len(c) == 7 for c in cells)  # 5 columns, whatever the cells contained
    assert [c[4].strip() for c in cells] == ["pass", "pass", "FAIL", "not run", "pass"]


def test_the_required_cases_index_flags_missing_and_failing_scenarios():
    required = {"Case A": ("S1", "S2"), "Case B": ("F1", "F9"), "Case C": ("P1",)}
    text = render_table([rec("S1"), rec("S2", outcome="FAIL"), rec("F1"), rec("P1")], required)
    rows = {ln.split("|")[1].strip(): ln.split("|")[3].strip() for ln in text.splitlines()
            if ln.startswith("| Case ") and "Pass/Fail" not in ln}
    assert rows == {"Case A": "FAIL", "Case B": "MISSING F9", "Case C": "pass"}


def test_every_required_id_exists_in_the_scenario_sources_and_ids_are_unique():
    ids = []
    for f in sorted(EVALS.glob("test_*.py")):
        ids += re.findall(r'scenario\(\s*"([A-Z]\d+)"', f.read_text()) + re.findall(r'\bid="([A-Z]\d+)"', f.read_text())
    assert len(ids) == len(set(ids)) >= 20, "scenario ids must be unique, and the assignment asks for at least 20 scenarios"
    assert {i for group in REQUIRED_CASES.values() for i in group} <= set(ids)
    assert all(order_key(i)[0] < 9 for i in ids)  # every id belongs to a known group, so the table orders them


def test_a_partial_run_is_detected_so_it_cannot_overwrite_the_full_table():
    everything = {f.name: eval_test_names(f) for f in EVALS.glob("test_*.py")}
    assert is_full_run(EVALS, everything)
    one_file = {"test_proactive.py": everything["test_proactive.py"]}
    assert not is_full_run(EVALS, one_file)
    missing_one = {k: set(v) for k, v in everything.items()}
    missing_one["test_safety.py"].pop()  # e.g. `-k` deselected a single scenario
    assert not is_full_run(EVALS, missing_one)
    assert not is_full_run(EVALS / "nowhere", everything)
