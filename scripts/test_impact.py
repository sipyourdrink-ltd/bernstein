#!/usr/bin/env python3
"""Incremental test impact analysis — maps source changes to affected tests.

Builds a dependency map by parsing imports in test files (and transitively in
source files), caches it in .sdd/test_deps.json, and uses it to select only
the tests that cover changed source files.

Usage:
    # Show tests affected by current uncommitted changes
    python scripts/test_impact.py

    # Show tests affected since a specific commit
    python scripts/test_impact.py --base main

    # Force-rebuild the dependency cache
    python scripts/test_impact.py --build

    # Print the full test→source dependency map
    python scripts/test_impact.py --show-deps

    # Explicit file list (skip git detection)
    python scripts/test_impact.py --files src/bernstein/core/models.py
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
CACHE_PATH = ROOT / ".sdd" / "test_deps.json"
SRC_ROOT = ROOT / "src"
TEST_DIRS = [ROOT / "tests" / "unit", ROOT / "tests" / "integration"]
CACHE_VERSION = "1"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _file_hash(path: Path) -> str:
    """Return a short SHA-1 of the file contents."""
    return hashlib.sha1(path.read_bytes()).hexdigest()[:16]


def _path_to_module(path: Path) -> str:
    """Map a source file path to its dotted module name.

    E.g. 'src/bernstein/core/context.py' -> 'bernstein.core.context'
    """
    try:
        rel = path.relative_to(SRC_ROOT)
    except ValueError:
        return str(path)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def _extract_bernstein_imports(path: Path) -> set[str]:
    """Extract all bernstein.* module names imported by a Python file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return set()

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("bernstein"):
                    imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("bernstein"):
                imports.add(module)
    return imports


# ---------------------------------------------------------------------------
# Cache build & load
# ---------------------------------------------------------------------------


def build_dep_map(test_dirs: list[Path] | None = None) -> dict[str, Any]:
    """Parse all test and source files to build the full dependency map.

    Structure::

        {
          "version": "1",
          "test_deps": {
            "tests/unit/test_context.py": {
              "hash": "<short-sha>",
              "imports": ["bernstein.core.context", ...]
            }
          },
          "source_imports": {
            "bernstein.core.context": {
              "hash": "<short-sha>",
              "imports": ["bernstein.core.models", ...]
            }
          }
        }
    """
    if test_dirs is None:
        test_dirs = TEST_DIRS

    # --- Test file deps ---
    test_deps: dict[str, dict[str, Any]] = {}
    for test_dir in test_dirs:
        if not test_dir.exists():
            continue
        for test_file in sorted(test_dir.glob("test_*.py")):
            rel = str(test_file.relative_to(ROOT))
            test_deps[rel] = {
                "hash": _file_hash(test_file),
                "imports": sorted(_extract_bernstein_imports(test_file)),
            }

    # --- Source file deps (for transitive analysis) ---
    source_imports: dict[str, dict[str, Any]] = {}
    if SRC_ROOT.exists():
        for src_file in sorted(SRC_ROOT.rglob("*.py")):
            module = _path_to_module(src_file)
            if not module.startswith("bernstein"):
                continue
            source_imports[module] = {
                "hash": _file_hash(src_file),
                "imports": sorted(_extract_bernstein_imports(src_file)),
            }

    return {
        "version": CACHE_VERSION,
        "test_deps": test_deps,
        "source_imports": source_imports,
    }


def _cache_is_fresh(cached: dict[str, Any]) -> bool:
    """Return True if the cached dep map is still valid."""
    if cached.get("version") != CACHE_VERSION:
        return False

    # Check test files
    for test_dir in TEST_DIRS:
        if not test_dir.exists():
            continue
        for test_file in test_dir.glob("test_*.py"):
            rel = str(test_file.relative_to(ROOT))
            entry = cached["test_deps"].get(rel)
            if entry is None or entry["hash"] != _file_hash(test_file):
                return False

    # Check source files
    if SRC_ROOT.exists():
        for src_file in SRC_ROOT.rglob("*.py"):
            module = _path_to_module(src_file)
            if not module.startswith("bernstein"):
                continue
            entry = cached["source_imports"].get(module)
            if entry is None or entry["hash"] != _file_hash(src_file):
                return False

    return True


def load_or_build_dep_map() -> dict[str, Any]:
    """Return dep map from cache, rebuilding if stale."""
    if CACHE_PATH.exists():
        try:
            cached: dict[str, Any] = json.loads(CACHE_PATH.read_text())
            if _cache_is_fresh(cached):
                return cached
        except (json.JSONDecodeError, KeyError, OSError):
            pass

    dep_map = build_dep_map()
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(dep_map, indent=2))
    return dep_map


# ---------------------------------------------------------------------------
# Git integration
# ---------------------------------------------------------------------------


def get_changed_files(base: str = "HEAD") -> list[str]:
    """Return paths of files changed relative to *base*.

    With base="HEAD": staged changes + working-tree changes vs HEAD.
    With base="main": all commits between main and HEAD.
    """
    try:
        if base == "HEAD":
            unstaged = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            staged = subprocess.run(
                ["git", "diff", "--name-only", "--cached"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            files = list(set(unstaged) | set(staged))
        else:
            files = subprocess.run(
                ["git", "diff", "--name-only", f"{base}...HEAD"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
        return [f for f in files if f]
    except subprocess.CalledProcessError:
        return []


# ---------------------------------------------------------------------------
# Impact analysis
# ---------------------------------------------------------------------------


def get_affected_tests(
    changed_files: list[str],
    dep_map: dict[str, Any] | None = None,
) -> list[Path]:
    """Return test files that are affected by *changed_files*.

    Strategy:
    - conftest.py changed → run all tests
    - Test file changed → include that file directly
    - Source file changed → find all tests that (transitively) import it
    """
    if dep_map is None:
        dep_map = load_or_build_dep_map()

    # conftest change → run everything
    for f in changed_files:
        if "conftest.py" in f:
            return sorted(ROOT / t for t in dep_map["test_deps"])

    # Build reverse map: module -> set[test_rel_path]
    module_to_tests: dict[str, set[str]] = {}
    for test_rel, entry in dep_map["test_deps"].items():
        for mod in entry["imports"]:
            module_to_tests.setdefault(mod, set()).add(test_rel)

    # Determine which source modules changed
    changed_modules: set[str] = set()
    for f in changed_files:
        path = ROOT / f
        if path.suffix == ".py" and path.is_relative_to(SRC_ROOT):
            changed_modules.add(_path_to_module(path))

    # BFS: expand to all source modules that import any changed module
    source_imports = dep_map.get("source_imports", {})
    all_affected: set[str] = set(changed_modules)
    worklist = set(changed_modules)
    while worklist:
        current = worklist.pop()
        for mod, info in source_imports.items():
            if current in info["imports"] and mod not in all_affected:
                all_affected.add(mod)
                worklist.add(mod)

    # Collect affected test files
    affected_tests: set[str] = set()

    # Direct test file changes
    for f in changed_files:
        path = ROOT / f
        if path.suffix == ".py" and path.name.startswith("test_"):
            rel = str(path.relative_to(ROOT))
            if rel in dep_map["test_deps"]:
                affected_tests.add(rel)

    # Tests that import any affected module (exact or parent package)
    for mod in all_affected:
        if mod in module_to_tests:
            affected_tests.update(module_to_tests[mod])
        # Parent packages: 'bernstein.core.models' also hits 'bernstein.core'
        parts = mod.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            if parent in module_to_tests:
                affected_tests.update(module_to_tests[parent])

    return sorted(ROOT / t for t in affected_tests)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Incremental test impact analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Force-rebuild the dependency map cache",
    )
    parser.add_argument(
        "--base",
        default="HEAD",
        metavar="REF",
        help="Git ref for diff base (default: HEAD = staged+unstaged changes)",
    )
    parser.add_argument(
        "--show-deps",
        action="store_true",
        help="Print the full test→imports dependency map",
    )
    parser.add_argument(
        "--files",
        nargs="+",
        metavar="FILE",
        help="Explicit changed file paths (skip git detection)",
    )
    parser.add_argument(
        "--print-paths",
        action="store_true",
        help="Print only affected test paths, one per line (for scripting)",
    )
    args = parser.parse_args()

    if args.build:
        print("Building dependency map...", flush=True)
        dep_map = build_dep_map()
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(dep_map, indent=2))
        n_tests = len(dep_map["test_deps"])
        n_src = len(dep_map["source_imports"])
        print(f"Cached {n_tests} test files, {n_src} source modules → {CACHE_PATH.relative_to(ROOT)}")
        return

    dep_map = load_or_build_dep_map()

    if args.show_deps:
        for test_file, entry in sorted(dep_map["test_deps"].items()):
            print(f"\n{test_file}")
            for imp in entry["imports"]:
                print(f"  {imp}")
        return

    changed = args.files if args.files else get_changed_files(args.base)

    if not changed:
        if not args.print_paths:
            print("No changed files detected.")
        sys.exit(0)

    affected = get_affected_tests(changed, dep_map)

    if args.print_paths:
        for t in affected:
            print(t)
        sys.exit(0)

    print(f"Changed ({len(changed)}):")
    for f in changed:
        print(f"  {f}")

    if not affected:
        print("\nNo tests affected.")
        sys.exit(0)

    print(f"\nAffected tests ({len(affected)} / {len(dep_map['test_deps'])} total):")
    for t in affected:
        print(f"  {t.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
