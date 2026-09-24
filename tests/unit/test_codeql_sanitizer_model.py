"""The CodeQL sanitizer model pack must actually match the log sanitizers.

Python models-as-data resolves the first column of a row with
``API::moduleImport``, which only knows top-level packages. The rows used to
name the dotted module (``"bernstein.core.log_safe"``); that matches no node,
CodeQL reports nothing, and every ``for_log``/``sanitize_log`` call site kept
raising ``py/log-injection``. Nothing turns red when a row goes dead, so its
shape is asserted here, together with the two other ways the barrier can
silently stop applying: the workflow not loading the pack, and a call site
importing the sanitizer through a path the model does not name.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_DIR = REPO_ROOT / ".github" / "codeql" / "models"
MODEL = PACK_DIR / "bernstein-sanitizers.model.yml"
QLPACK = PACK_DIR / "qlpack.yml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "codeql.yml"
CONFIG = REPO_ROOT / ".github" / "codeql" / "codeql-config.yml"

CANONICAL_MODULE = "bernstein.core.security.sanitize"

_MEMBER = re.compile(r"Member\[(\w+)\]")


def _barrier_rows() -> list[list[str]]:
    doc = yaml.safe_load(MODEL.read_text(encoding="utf-8"))
    rows: list[list[str]] = []
    for ext in doc["extensions"]:
        if ext["addsTo"]["extensible"] == "barrierModel":
            rows.extend(ext["data"])
    return rows


def _resolve(package: str, path: str) -> object:
    """Walk ``Member[...]`` tokens the way an API graph does, importing submodules."""
    tokens = path.split(".")
    assert tokens[-1] == "ReturnValue", f"{path}: barrier must be on the return value"
    names = [_MEMBER.fullmatch(t) for t in tokens[:-1]]
    assert all(names), f"{path}: only Member[...] tokens are expected before ReturnValue"
    obj: object = importlib.import_module(package)
    dotted = package
    for match in names:
        name = cast("re.Match[str]", match).group(1)
        dotted = f"{dotted}.{name}"
        obj = getattr(obj, name, None) or importlib.import_module(dotted)
    return obj


@pytest.mark.parametrize("row", _barrier_rows(), ids=lambda r: r[1])
def test_every_row_names_a_top_level_package(row: list[str]) -> None:
    """A dotted first column matches no API-graph node: the row would be dead."""
    package, _path, _kind = row
    assert "." not in package, f"{package!r}: use the top-level package and Member[...] tokens"


@pytest.mark.parametrize("row", _barrier_rows(), ids=lambda r: r[1])
def test_every_row_resolves_to_a_real_function(row: list[str]) -> None:
    package, path, kind = row
    assert kind == "log-injection"
    assert callable(_resolve(package, path))


def test_both_log_sanitizers_are_modelled() -> None:
    from bernstein.core.log_safe import for_log
    from bernstein.core.security.sanitize import sanitize_log

    modelled = {id(_resolve(pkg, path)) for pkg, path, _ in _barrier_rows()}
    assert id(sanitize_log) in modelled
    assert id(for_log) in modelled


def test_the_workflow_loads_the_pack_through_extra_options() -> None:
    """A pack named only in the config file, or only by path, is ignored."""
    pack_name = yaml.safe_load(QLPACK.read_text(encoding="utf-8"))["name"]
    doc = cast("dict[str, Any]", yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))
    steps = doc["jobs"]["analyze"]["steps"]
    analyze = next(s for s in steps if str(s.get("uses", "")).startswith("github/codeql-action/analyze@"))
    raw = analyze["env"]["CODEQL_ACTION_EXTRA_OPTIONS"].replace("${{ github.workspace }}", str(REPO_ROOT))
    flags = json.loads(raw)["database"]["run-queries"]
    assert flags[flags.index("--model-packs") + 1] == pack_name
    assert Path(flags[flags.index("--additional-packs") + 1]) == PACK_DIR.parent
    assert "packs" not in (yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {})


# Call sites that imported ``sanitize_log`` through the ``bernstein.core.sanitize``
# redirect alias, which the model cannot see. Each now imports the canonical
# module; the alias resolves to the same function object, so output is unchanged.
_REROUTED_CALL_SITES = [
    "src/bernstein/core/agents/agent_trust.py",
    "src/bernstein/core/approval/queue.py",
    "src/bernstein/core/cost/cost_tracker.py",
    "src/bernstein/core/quality/graduation.py",
    "src/bernstein/core/routes/approvals.py",
    "src/bernstein/core/routes/discord.py",
    "src/bernstein/core/routes/slack.py",
    "src/bernstein/core/routes/status_lifecycle.py",
    "src/bernstein/core/trigger_sources/receipt.py",
    "src/bernstein/core/trigger_sources/webhook_node.py",
    "src/bernstein/github_app/webhooks.py",
]


def _sanitize_log_import_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and any(a.name == "sanitize_log" for a in node.names)
    ]


def test_the_alias_is_the_canonical_function() -> None:
    """Rerouting is byte-identical only because both paths yield one object."""
    alias = importlib.import_module("bernstein.core.sanitize")
    canonical = importlib.import_module(CANONICAL_MODULE)
    assert alias.sanitize_log is canonical.sanitize_log


@pytest.mark.parametrize("rel", _REROUTED_CALL_SITES)
def test_rerouted_call_site_imports_the_canonical_module(rel: str) -> None:
    modules = _sanitize_log_import_modules(REPO_ROOT / rel)
    assert modules, f"{rel} no longer imports sanitize_log"
    assert set(modules) == {CANONICAL_MODULE}


def test_no_source_file_imports_sanitize_log_through_an_alias() -> None:
    """Any other import path is invisible to the barrier model."""
    offenders = [
        f"{py.relative_to(REPO_ROOT)}: {module}"
        for root in ("src", "scripts")
        for py in sorted((REPO_ROOT / root).rglob("*.py"))
        for module in _sanitize_log_import_modules(py)
        if module != CANONICAL_MODULE
    ]
    assert offenders == []
