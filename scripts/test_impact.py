#!/usr/bin/env python3
"""Incremental test impact analysis CLI and compatibility wrapper."""

from __future__ import annotations

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

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from bernstein.core.test_impact import _path_to_module as _core_path_to_module  # noqa: E402
from bernstein.core.test_impact import (  # noqa: E402
    build_compat_dep_map,
    compat_cache_is_fresh,
    compat_get_affected_tests,
    extract_project_imports,
)


def _path_to_module(path: Path) -> str:
    """Map a source file path to a dotted module name."""
    return _core_path_to_module(path, SRC_ROOT)


def _extract_bernstein_imports(path: Path) -> set[str]:
    """Extract imported ``bernstein.*`` module names from a file."""
    return extract_project_imports(path, {"bernstein"})


def build_dep_map(test_dirs: list[Path] | None = None) -> dict[str, Any]:
    """Build the legacy dependency-map structure for compatibility."""
    return build_compat_dep_map(ROOT, SRC_ROOT, test_dirs or TEST_DIRS, {"bernstein"})


def _cache_is_fresh(cached: dict[str, Any]) -> bool:
    """Return whether the cached dependency map still matches the repo."""
    return compat_cache_is_fresh(cached, root=ROOT, src_root=SRC_ROOT, test_dirs=TEST_DIRS)


def load_or_build_dep_map() -> dict[str, Any]:
    """Return the cached dependency map, rebuilding it if stale."""
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


def get_changed_files(base: str = "HEAD") -> list[str]:
    """Return repo file paths changed relative to ``base``."""
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


def get_affected_tests(changed_files: list[str], dep_map: dict[str, Any] | None = None) -> list[Path]:
    """Return test file paths affected by the changed-file set."""
    return compat_get_affected_tests(
        changed_files,
        dep_map or load_or_build_dep_map(),
        root=ROOT,
        src_root=SRC_ROOT,
    )


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Incremental test impact analysis")
    parser.add_argument("--build", action="store_true", help="Force-rebuild the dependency map cache")
    parser.add_argument(
        "--base",
        default="HEAD",
        metavar="REF",
        help="Git ref for diff base (default: HEAD = staged+unstaged changes)",
    )
    parser.add_argument("--show-deps", action="store_true", help="Print the full test→imports dependency map")
    parser.add_argument("--files", nargs="+", metavar="FILE", help="Explicit changed file paths")
    parser.add_argument("--print-paths", action="store_true", help="Print only affected test paths")
    args = parser.parse_args()

    if args.build:
        print("Building dependency map...", flush=True)
        dep_map = build_dep_map()
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(dep_map, indent=2))
        print(
            f"Cached {len(dep_map['test_deps'])} test files, {len(dep_map['source_imports'])} source modules"
            f" -> {CACHE_PATH.relative_to(ROOT)}"
        )
        return

    dep_map = load_or_build_dep_map()
    if args.show_deps:
        for test_file, entry in sorted(dep_map["test_deps"].items()):
            print(f"\n{test_file}")
            for imported in entry["imports"]:
                print(f"  {imported}")
        return

    changed = args.files if args.files else get_changed_files(args.base)
    if not changed:
        if not args.print_paths:
            print("No changed files detected.")
        sys.exit(0)

    affected = get_affected_tests(changed, dep_map)
    if args.print_paths:
        for test_file in affected:
            print(test_file)
        sys.exit(0)

    print(f"Changed ({len(changed)}):")
    for file_path in changed:
        print(f"  {file_path}")

    if not affected:
        print("\nNo tests affected.")
        sys.exit(0)

    print(f"\nAffected tests ({len(affected)} / {len(dep_map['test_deps'])} total):")
    for test_file in affected:
        print(f"  {test_file.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
