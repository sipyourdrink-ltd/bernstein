#!/usr/bin/env python3
"""Fail when a pytest test module under ``tests/`` collects zero items.

A file named like a test module that defines no tests still ``collect``s
cleanly and exits 0, so emptying a suite is indistinguishable from a green
run (issue #4834). This check walks modules matching pytest's default
``python_files`` patterns and requires each one to collect at least one item.

Boundary (naming convention, not a helper allowlist):

* In scope: ``tests/**/test_*.py`` and ``tests/**/*_test.py``.
* Out of scope automatically: ``conftest.py``, ``__init__.py``, shared
  helpers, fixtures packages — they do not match those patterns.
* ``ALLOWLIST`` is only for the rare in-scope module that is intentionally
  collect-empty; stale entries fail the check.

Collection uses a cheap AST pre-count when the module defines local
``test_*`` / ``Test*`` methods. Modules that look empty under AST still go
through ``pytest --collect-only`` so inheritance (conformance bases) and
Hypothesis ``RuleBasedStateMachine.TestCase`` assignments are not
false-positives.

AST counting and pytest collection disagree on a module that fails to
import: pytest reports zero collected items while the AST still sees the
``test_*`` / ``Test*`` definitions. That gap is intentional here — a
collection/import error already fails the test run elsewhere, so preferring
AST when it proves non-empty is the speed trade for Repo hygiene, not an
oversight that a broken import would be treated as "has tests".

Usage::

    python scripts/check_empty_test_modules.py
    python scripts/check_empty_test_modules.py --json
    python scripts/check_empty_test_modules.py --root /path/to/repo
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"

# pytest default ``python_files``; pyproject.toml does not override it.
_TEST_FILE_GLOBS = ("test_*.py", "*_test.py")

# Intentional collect-empty modules matching the naming convention.
# Prefer restoring tests or renaming the file out of ``python_files``.
ALLOWLIST: dict[str, str] = {}


@dataclass
class EmptyModuleReport:
    """Result of scanning test modules for a zero-collection failure."""

    empty: list[str] = field(default_factory=list)
    checked: int = 0
    stale_allowlist: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def is_test_module(path: Path) -> bool:
    """Return True when *path* matches pytest's default ``python_files``."""
    name = path.name
    if name in {"conftest.py", "__init__.py"}:
        return False
    if name.startswith("test_") and name.endswith(".py"):
        return True
    return name.endswith("_test.py")


def discover_test_modules(tests_dir: Path) -> list[Path]:
    """Return sorted in-scope test modules under *tests_dir*."""
    found: set[Path] = set()
    for pattern in _TEST_FILE_GLOBS:
        found.update(tests_dir.rglob(pattern))
    return sorted(p for p in found if p.is_file() and is_test_module(p))


def ast_defined_test_count(source: str) -> int:
    """Count top-level ``test_*`` functions and ``Test*`` methods in *source*."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    count = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            count += 1
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_"):
                    count += 1
    return count


def pytest_collect_count(path: Path, *, python: str = sys.executable, cwd: Path | None = None) -> int:
    """Return how many items pytest collects from *path* (``--collect-only``)."""
    result = subprocess.run(
        [
            python,
            "-m",
            "pytest",
            str(path),
            "--collect-only",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd is not None else str(REPO_ROOT),
    )
    blob = f"{result.stdout}\n{result.stderr}"
    if re.search(r"\bno tests collected\b", blob, re.IGNORECASE):
        return 0
    match = re.search(r"(\d+)\s+tests?\s+collected", blob, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # pytest -q lists one node id per line when collection succeeds without a
    # summary line in some versions; count non-empty non-noise lines.
    nodes = [
        line
        for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("=") and "collected" not in line.lower()
    ]
    return len(nodes)


def collected_test_count(path: Path, *, python: str = sys.executable, cwd: Path | None = None) -> int:
    """Return collected count, preferring AST when it already proves non-empty.

    See the module docstring for why AST may disagree with pytest on an
    import-broken module (collection errors fail the run elsewhere).
    """
    source = path.read_text(encoding="utf-8")
    ast_count = ast_defined_test_count(source)
    if ast_count > 0:
        return ast_count
    return pytest_collect_count(path, python=python, cwd=cwd)


def build_report(
    *,
    tests_dir: Path | None = None,
    allowlist: dict[str, str] | None = None,
    root: Path | None = None,
    python: str = sys.executable,
) -> EmptyModuleReport:
    """Scan in-scope modules and report those that collect zero tests."""
    base = root if root is not None else REPO_ROOT
    suite = tests_dir if tests_dir is not None else base / "tests"
    excuses = ALLOWLIST if allowlist is None else allowlist
    report = EmptyModuleReport()
    empty_set: set[str] = set()

    for path in discover_test_modules(suite):
        rel = path.resolve().relative_to(base.resolve()).as_posix()
        report.checked += 1
        count = collected_test_count(path, python=python, cwd=base)
        report.counts[rel] = count
        if count == 0 and rel not in excuses:
            empty_set.add(rel)

    report.empty = sorted(empty_set)
    for key in excuses:
        if key not in report.counts or report.counts[key] > 0:
            report.stale_allowlist.append(key)
    report.stale_allowlist.sort()
    return report


def format_report(report: EmptyModuleReport) -> str:
    """Human-readable summary naming each empty module and its count."""
    lines: list[str] = []
    if report.empty:
        lines.append("These test modules collect zero items (issue #4834):")
        for rel in report.empty:
            lines.append(f"  {rel}: 0 collected")
        lines.append(
            "Restore at least one collected test, rename the file out of "
            "pytest's python_files patterns, or add a reasoned ALLOWLIST entry."
        )
    if report.stale_allowlist:
        lines.append("Stale ALLOWLIST entries (module missing or no longer empty):")
        for rel in report.stale_allowlist:
            lines.append(f"  {rel}")
    if not lines:
        lines.append(f"OK: {report.checked} test modules each collect at least one item.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root (default: inferred from this script)",
    )
    parser.add_argument(
        "--tests-dir",
        type=Path,
        default=None,
        help="Override the tests/ directory to scan",
    )
    args = parser.parse_args(argv)

    report = build_report(root=args.root, tests_dir=args.tests_dir)
    if args.json:
        print(json.dumps(asdict(report), indent=2, sort_keys=True))
    else:
        print(format_report(report))
    return 1 if report.empty or report.stale_allowlist else 0


if __name__ == "__main__":
    raise SystemExit(main())
