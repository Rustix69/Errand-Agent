"""Agent-side code may touch the marketplace ONLY through the 8 contract tools.

* Nothing under ``errand/`` outside ``errand/mocks/`` may import ``errand.mocks`` (statically or via
  importlib/__import__), with exactly one exception: the composition root ``errand/wiring.py`` may
  import ``MockWorld`` from ``errand.mocks`` to build the ``ContractTools`` handed to the agent.
* The mocks must not depend on agent code (the dependency points one way).
* ``ContractTools`` exposes exactly the 8 tools and matches the mock's signatures.
* Only the executor ever calls a mutating tool.

The scanner is itself tested against synthetic offenders so the test cannot pass vacuously.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from errand.contract.port import MUTATING_TOOLS, TOOL_NAMES, ContractTools
from errand.mocks import MarketplaceTools, MockWorld
from errand.mocks.catalog import DEMO_USER_ID
from errand.wiring import tools_for

PKG = Path(__file__).resolve().parent.parent / "errand"
FORBIDDEN = "errand.mocks"
WIRING = PKG / "wiring.py"
WIRING_ALLOWED = {("errand.mocks", "MockWorld")}


def module_name(path: Path) -> str:
    rel = path.relative_to(PKG.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def package_of(path: Path) -> str:
    name = module_name(path)
    return name if path.name == "__init__.py" else name.rpartition(".")[0]


def imports(source: str, path: Path) -> list[tuple[int, str, str | None]]:
    """(lineno, fully-resolved module, imported name or None). Relative imports are resolved."""
    out: list[tuple[int, str, str | None]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name, None) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                pkg = package_of(path).split(".")
                pkg = pkg[: len(pkg) - (node.level - 1)]
                base = ".".join(pkg + ([node.module] if node.module else []))
            out += [(node.lineno, base, a.name) for a in node.names]
    return out


def touches(module: str, name: str | None, target: str) -> bool:
    full = f"{module}.{name}" if name else module
    return module == target or module.startswith(target + ".") or full == target or full.startswith(target + ".")


DYNAMIC = re.compile(r"\b(importlib|__import__|import_module)\b")


def violations(source: str, path: Path) -> list[str]:
    found = [
        f"{path.name}:{ln}: imports {mod}{'.' + n if n else ''}"
        for ln, mod, n in imports(source, path)
        if touches(mod, n, FORBIDDEN)
    ]
    if DYNAMIC.search(source) and "mocks" in source:
        found.append(f"{path.name}: dynamic import near a reference to 'mocks'")
    return found


def agent_side_files() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py") if "mocks" not in p.relative_to(PKG).parts)


def wiring_violations(source: str, path: Path) -> list[str]:
    bad = []
    for ln, mod, n in imports(source, path):
        if touches(mod, n, FORBIDDEN) and (mod, n) not in WIRING_ALLOWED:
            bad.append(f"{path.name}:{ln}: imports {mod}.{n}, only MockWorld is allowed")
    return bad


# ----------------------------------------------------------------------------- the real check


def test_agent_side_never_imports_the_mocks():
    files = agent_side_files()
    assert len(files) >= 10  # we are scanning the real tree
    problems = []
    for f in files:
        if f == WIRING:
            problems += wiring_violations(f.read_text(), f)
        else:
            problems += violations(f.read_text(), f)
    assert problems == []


def test_wiring_is_the_only_bridge_and_returns_contract_tools_only():
    assert any(touches(m, n, FORBIDDEN) for _, m, n in imports(WIRING.read_text(), WIRING))  # it IS the bridge
    assert {(m, n) for _, m, n in imports(WIRING.read_text(), WIRING) if touches(m, n, FORBIDDEN)} == WIRING_ALLOWED
    assert type(tools_for(_world(), DEMO_USER_ID)) is ContractTools


def _world():
    from errand.clock import FakeClock

    return MockWorld(":memory:", FakeClock())


def test_mocks_do_not_depend_on_agent_code():
    for f in sorted((PKG / "mocks").rglob("*.py")):
        for ln, mod, n in imports(f.read_text(), f):
            assert not touches(mod, n, "errand.core") and not touches(mod, n, "errand.wiring"), f"{f.name}:{ln}"


# ----------------------------------------------------------------------------- the scanner is not vacuous


@pytest.mark.parametrize(
    "snippet",
    [
        "from errand.mocks.world import MockWorld",
        "from errand.mocks import Simulator",
        "import errand.mocks",
        "import errand.mocks.tools as t",
        "from errand import mocks",
        "from ..mocks import world",
        "from ..mocks.simulator import Simulator",
        "import importlib\nimportlib.import_module('errand.mocks')",
        "m = __import__('errand.mocks.world')",
    ],
)
def test_scanner_flags_synthetic_offenders(snippet):
    fake = PKG / "core" / "fake_module.py"
    assert violations(snippet, fake), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        "from errand.contract.port import ContractTools",
        "from errand.core.store import AgentStore",
        "import errand.clock",
        "from .store import AgentStore",
        "from errand.mocking_helpers import x",  # prefix lookalike must not be flagged
    ],
)
def test_scanner_accepts_legitimate_imports(snippet):
    assert violations(snippet, PKG / "core" / "fake_module.py") == []


@pytest.mark.parametrize(
    "snippet", ["from errand.mocks import Simulator", "from errand.mocks import MarketplaceTools",
                "from errand.mocks.world import MockWorld", "import errand.mocks"],
)
def test_wiring_exception_is_exactly_mockworld(snippet):
    assert wiring_violations(snippet, WIRING)
    assert wiring_violations("from errand.mocks import MockWorld", WIRING) == []


# ----------------------------------------------------------------------------- the surface the agent gets


def test_contract_tools_exposes_exactly_the_eight_tools():
    public = {n for n, _ in inspect.getmembers(ContractTools, callable) if not n.startswith("_")}
    assert public == set(TOOL_NAMES) and len(TOOL_NAMES) == 8
    assert not [n for n in vars(ContractTools) if not n.startswith("_") and n not in TOOL_NAMES]
    tools = tools_for(_world(), DEMO_USER_ID)
    for name in ("sim", "world", "_w", "config", "clock", "tx", "force", "provider_cancels"):
        assert not hasattr(tools, name), name
    with pytest.raises(AttributeError):
        tools.book = lambda *a: None  # type: ignore[misc]


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_contract_signatures_match_the_mock(name):
    mock = [p for p in inspect.signature(getattr(MarketplaceTools, name)).parameters if p != "self"]
    wrapped = [p for p in inspect.signature(getattr(ContractTools, name)).parameters if p != "self"]
    assert mock == wrapped


def test_contract_tools_delegate_to_the_mock():
    world = _world()
    tools = tools_for(world, DEMO_USER_ID)
    assert tools.list_bookings(DEMO_USER_ID).bookings
    assert [c["tool"] for c in world.sim.calls()] == ["list_bookings"]


# ----------------------------------------------------------------------------- only the executor mutates


def mutating_refs(source: str, name: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in MUTATING_TOOLS and isinstance(node.ctx, ast.Load):
            found.append(f"{name}:{node.lineno}: .{node.attr}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and node.args[1].value in MUTATING_TOOLS:
                found.append(f"{name}:{node.lineno}: getattr(..., {node.args[1].value!r})")
    return found


def test_only_the_executor_calls_mutating_tools():
    """No agent-side module other than the executor may call book/cancel/reschedule, by attribute or by a
    literal getattr name. Limit: a computed name (``getattr(t, name)``) is invisible to a static scan; the
    executor itself dispatches that way, and read paths are guarded at runtime by ``Reader`` (see test_recovery)."""
    allowed = {PKG / "core" / "executor.py", PKG / "contract" / "port.py"}  # port.py only declares/delegates
    problems = [r for f in agent_side_files() if f not in allowed for r in mutating_refs(f.read_text(), f.name)]
    assert problems == [], problems


@pytest.mark.parametrize(
    "snippet", ["tools.book(h, k)", "self.t.cancel(b, k)", "x.reschedule(b, s, k)", "getattr(tools, 'book')(h, k)"]
)
def test_mutating_scanner_flags_synthetic_offenders(snippet):
    assert mutating_refs(snippet, "fake.py")
