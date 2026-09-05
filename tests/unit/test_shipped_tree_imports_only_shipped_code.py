"""The shipped package may not import repository-only directories.

``src/bernstein/`` is what the wheel contains. ``scripts/``, ``tests/`` and the
other top-level tooling directories are not. An import that crosses from the
first into the second resolves fine from a repository checkout and fails at
startup for everyone who installed the package -- on 2026-08-31 a module-level
``from scripts.gen_distribution_manifests import ...`` in
``bernstein.core.skills.lifecycle`` made the installed console script die with
``ModuleNotFoundError: No module named 'scripts'`` before it printed anything.

The install-smoke job catches this, twenty minutes into a run and only for the
entry points it exercises. This reads the same defect out of the source in
under a second, and covers every module rather than the imported ones.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: Scans the source tree rather than importing it, so no diff produces an
#: import edge to this file. The marker puts it in every pull request's
#: affected slice instead of only the merge group (#5428).
pytestmark = pytest.mark.whole_tree_guard

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "src" / "bernstein"

#: Top-level directories that live in the repository and never in the wheel.
REPO_ONLY_ROOTS = frozenset({"scripts", "tests", "tools", "benchmarks", "docs", "examples"})


def _imported_roots(tree: ast.AST) -> set[str]:
    """Return the top-level module name of every absolute import in *tree*."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_no_shipped_module_imports_a_repository_only_directory() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for root in sorted(_imported_roots(tree) & REPO_ONLY_ROOTS):
            offenders.append(f"{path.relative_to(REPO_ROOT)} imports {root!r}")
    assert offenders == [], (
        "These shipped modules import a directory the wheel does not contain, so "
        "the installed package fails where a repository checkout succeeds:\n  "
        + "\n  ".join(offenders)
        + "\nMove what they need into the package and let the repository script "
        "import it from there."
    )


def test_the_guard_can_see_an_offending_import() -> None:
    """A parser that stopped recognising imports would pass the guard silently."""
    tree = ast.parse("from scripts.gen_distribution_manifests import PLUGIN_SCHEMA_ID")
    assert _imported_roots(tree) & REPO_ONLY_ROOTS == {"scripts"}
