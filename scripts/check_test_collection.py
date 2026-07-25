#!/usr/bin/env python3
"""Report test files that no CI configuration collects.

A test file that no workflow ever hands to pytest is indistinguishable, from
the outside, from a test file that passes: the suite is green because the file
was never opened. This script derives the set of test files CI actually
collects from the workflow definitions themselves and reports anything left
over.

Derivation (no hardcoded copy of the CI layout):

* every ``run:`` body in ``.github/workflows/*.yml`` is scanned for ``pytest``
  and ``scripts/run_tests.py`` invocations;
* a ``run_tests.py`` invocation collects ``<--test-dir>/**/test_*.py``, with
  the default directory read from ``scripts/run_tests.DEFAULT_TEST_DIR`` -
  that is the same constant argparse uses, so the two cannot drift;
* a ``run_tests.py --affected`` invocation collects the impact-analysis
  universe, read from ``scripts/test_impact.TEST_DIRS``;
* a ``pytest`` invocation collects every ``tests/...`` path token it is given,
  expanded with pytest's own ``python_files`` patterns when the token is a
  directory.

Two deliberately conservative rules:

* an invocation narrowed by ``-k``/``--keyword`` is ignored - it runs a subset
  the expression decides, so it cannot be credited with a whole directory;
* a marker-narrowed invocation (``-m``) is credited, because collection still
  walks the directory and the marker only deselects.

Anything not collected must appear in ``ALLOWLIST`` with a reason. A stale
allowlist entry (one that matches no file) is reported too, so the list cannot
quietly outlive the file it excused.

Usage::

    python scripts/check_test_collection.py           # human-readable report
    python scripts/check_test_collection.py --json    # machine-readable
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
TESTS_DIR = REPO_ROOT / "tests"

# pytest's default ``python_files``; pyproject.toml does not override it, so a
# directory handed to pytest collects both shapes.
PYTEST_FILE_PATTERNS = ("test_*.py", "*_test.py")
# scripts/run_tests.py discovers with rglob("test_*.py") - a ``*_test.py`` file
# under a run_tests.py directory is NOT executed by the shards.
RUN_TESTS_FILE_PATTERN = "test_*.py"

# Test files no CI configuration collects, each with the reason it is excused.
# A key is either a repo-relative file path or a directory prefix ending in
# "/". Directory entries excuse a whole suite that is deliberately operated by
# hand; file entries excuse a single file and are expected to disappear as the
# file is relocated under a collected directory.
#
# Adding an entry is a decision to let a test never run. Prefer moving the file
# into tests/unit/ or tests/integration/, or naming it in a workflow.
ALLOWLIST: dict[str, str] = {
    # --- Deliberately not run in CI -------------------------------------
    "tests/chaos/": (
        "Chaos suite: kills and restarts real server processes and simulates "
        "disk-full/OOM conditions. Operated on demand, not from the PR lane."
    ),
    "tests/perf/": (
        "Wall-clock performance smoke; thresholds are not meaningful on shared "
        "runners. Run on demand (ADR-009 section 12.5)."
    ),
    # --- Not collected today; relocation tracked separately ---------------
    "tests/test_server.py": ("Sits at the tests/ root, so no shard collects it. Pending relocation into tests/unit/."),
    "tests/test_evolution_e2e.py": (
        "Sits at the tests/ root, so no shard collects it. Pending relocation into tests/integration/."
    ),
    "tests/endpoints/test_certify_verify.py": (
        "tests/endpoints/ is outside every shard directory. Binds a loopback HTTP "
        "server, so relocation into tests/integration/ is pending."
    ),
    "tests/observability/test_gate.py": (
        "tests/observability/ is outside every shard directory. Pending relocation into tests/unit/observability/."
    ),
    "tests/plugins/test_background_hooks.py": (
        "tests/plugins/ is outside every shard directory. Pending relocation into tests/unit/plugins/."
    ),
    "tests/security/test_lineage_adversarial.py": (
        "tests/security/ is outside every shard directory. Pending relocation into tests/unit/security/."
    ),
}


@dataclass
class Invocation:
    """One test-running command found in a workflow ``run:`` body."""

    workflow: str
    kind: str
    paths: tuple[str, ...]
    patterns: tuple[str, ...]


@dataclass
class CollectionReport:
    """Outcome of comparing the tests tree against the CI-derived set."""

    collected: dict[str, set[str]] = field(default_factory=dict)
    uncollected: list[str] = field(default_factory=list)
    allowlisted: dict[str, str] = field(default_factory=dict)
    stale_allowlist: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing is uncollected and no allowlist entry is stale."""
        return not self.uncollected and not self.stale_allowlist


def _load_yaml() -> Any:
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - dev env has pyyaml
        raise RuntimeError("pyyaml is required to read the workflow definitions") from exc
    return yaml


def _import_script(name: str) -> Any:
    """Import a module from ``scripts/`` so constants are read, not copied.

    Loaded under a private module name so this never collides with a test
    module of the same stem in the running interpreter.
    """
    alias = f"_ci_collection_{name}"
    cached = sys.modules.get(alias)
    if cached is not None:
        return cached
    scripts_dir = str(REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location(alias, REPO_ROOT / "scripts" / f"{name}.py")
    if spec is None or spec.loader is None:  # pragma: no cover - path is in-tree
        raise RuntimeError(f"cannot load scripts/{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def default_test_dir() -> str:
    """Return the directory ``run_tests.py`` discovers when no flag is given."""
    run_tests = _import_script("run_tests")
    return str(run_tests.DEFAULT_TEST_DIR)


def affected_test_dirs() -> tuple[str, ...]:
    """Return the directories the impact analyser can select tests from."""
    test_impact = _import_script("test_impact")
    dirs: list[str] = []
    for entry in test_impact.TEST_DIRS:
        path = Path(entry)
        rel = path.relative_to(REPO_ROOT) if path.is_absolute() else path
        dirs.append(rel.as_posix())
    return tuple(dirs)


def iter_test_files() -> list[str]:
    """Return every repo-relative test file under ``tests/``, sorted."""
    found: set[str] = set()
    for pattern in PYTEST_FILE_PATTERNS:
        for path in TESTS_DIR.rglob(pattern):
            if path.is_file():
                found.add(path.relative_to(REPO_ROOT).as_posix())
    return sorted(found)


def _join_continuations(body: str) -> str:
    """Collapse POSIX ``\\`` and PowerShell `````` line continuations."""
    body = re.sub(r"\\\s*\n\s*", " ", body)
    return re.sub(r"`\s*\n\s*", " ", body)


def _split_commands(body: str) -> list[str]:
    """Split a run body into individual commands."""
    normalised = _join_continuations(body)
    return [part for part in re.split(r"[\n;]|&&|\|\||\|", normalised) if part.strip()]


def _tokenise(command: str) -> list[str]:
    """Tokenise a command, tolerating GitHub expression syntax and quoting."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    return [token.strip("`'\"") for token in tokens if token.strip("`'\"")]


def _test_path_tokens(tokens: list[str]) -> list[str]:
    """Return the ``tests/...`` path arguments in a token list."""
    paths: list[str] = []
    for token in tokens:
        if not token.startswith("tests/") and token != "tests":
            continue
        if "$" in token or "*" in token:
            continue
        paths.append(token.rstrip("/.,"))
    return paths


def _has_keyword_filter(tokens: list[str]) -> bool:
    """True when the invocation is narrowed by a ``-k`` expression."""
    return any(token in {"-k", "--keyword"} or token.startswith("--keyword=") for token in tokens)


def _test_dir_argument(tokens: list[str]) -> str | None:
    """Return the explicit ``--test-dir`` value, if the invocation has one."""
    for index, token in enumerate(tokens):
        if token == "--test-dir" and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith("--test-dir="):
            return token.split("=", 1)[1]
    return None


def parse_command(workflow: str, command: str) -> Invocation | None:
    """Return the collection an individual command performs, if it runs tests."""
    if "run_tests.py" not in command and not re.search(r"(?<![\w-])pytest(?![\w-])", command):
        return None
    tokens = _tokenise(command)
    if not tokens or _has_keyword_filter(tokens):
        return None

    if any(token.endswith("run_tests.py") for token in tokens):
        paths: list[str] = []
        explicit = _test_dir_argument(tokens)
        paths.append(explicit if explicit is not None else default_test_dir())
        if any(token == "--affected" or token.startswith("--affected=") for token in tokens):
            paths.extend(affected_test_dirs())
        return Invocation(workflow, "run_tests", tuple(paths), (RUN_TESTS_FILE_PATTERN,))

    paths = _test_path_tokens(tokens)
    if not paths:
        return None
    return Invocation(workflow, "pytest", tuple(paths), PYTEST_FILE_PATTERNS)


def iter_invocations() -> list[Invocation]:
    """Return every test-running invocation declared by a workflow."""
    yaml = _load_yaml()
    invocations: list[Invocation] = []
    for workflow_path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            continue
        jobs = document.get("jobs")
        if not isinstance(jobs, dict):
            continue
        name = workflow_path.name
        for job in jobs.values():
            if not isinstance(job, dict):
                continue
            steps = job.get("steps")
            if not isinstance(steps, list):
                continue
            for step in steps:
                if not isinstance(step, dict):
                    continue
                run_body = step.get("run")
                if not isinstance(run_body, str):
                    continue
                for command in _split_commands(run_body):
                    invocation = parse_command(name, command)
                    if invocation is not None:
                        invocations.append(invocation)
    return invocations


def _expand(invocation: Invocation) -> set[str]:
    """Expand one invocation into the repo-relative files it collects."""
    files: set[str] = set()
    for raw in invocation.paths:
        target = REPO_ROOT / raw
        if target.is_file():
            files.add(target.relative_to(REPO_ROOT).as_posix())
            continue
        if not target.is_dir():
            continue
        for pattern in invocation.patterns:
            for path in target.rglob(pattern):
                if path.is_file():
                    files.add(path.relative_to(REPO_ROOT).as_posix())
    return files


def _allowlist_reason(rel_path: str, allowlist: dict[str, str]) -> str | None:
    """Return the reason excusing ``rel_path``, or None when it is not excused."""
    if rel_path in allowlist:
        return allowlist[rel_path]
    for key, reason in allowlist.items():
        if key.endswith("/") and rel_path.startswith(key):
            return reason
    return None


def build_report(allowlist: dict[str, str] | None = None) -> CollectionReport:
    """Compare the tests tree against the CI-derived collected set."""
    entries = ALLOWLIST if allowlist is None else allowlist
    collected: dict[str, set[str]] = {}
    for invocation in iter_invocations():
        for rel_path in _expand(invocation):
            collected.setdefault(rel_path, set()).add(invocation.workflow)

    report = CollectionReport(collected=collected)
    used: set[str] = set()
    for rel_path in iter_test_files():
        if rel_path in collected:
            continue
        reason = _allowlist_reason(rel_path, entries)
        if reason is None:
            report.uncollected.append(rel_path)
            continue
        report.allowlisted[rel_path] = reason
        for key in entries:
            if key == rel_path or (key.endswith("/") and rel_path.startswith(key)):
                used.add(key)
    report.stale_allowlist = sorted(set(entries) - used)
    return report


def _print_report(report: CollectionReport) -> None:
    """Print a human-readable summary of the collection report."""
    print(f"Test files collected by CI: {len(report.collected)}")
    print(f"Allowlisted (not collected): {len(report.allowlisted)}")
    for rel_path, reason in sorted(report.allowlisted.items()):
        print(f"  - {rel_path}: {reason}")
    if report.stale_allowlist:
        print(f"Stale allowlist entries: {len(report.stale_allowlist)}")
        for key in report.stale_allowlist:
            print(f"  ! {key} matches no test file - remove it")
    if report.uncollected:
        print(f"Uncollected test files: {len(report.uncollected)}")
        for rel_path in report.uncollected:
            print(f"  x {rel_path}")
    else:
        print("Uncollected test files: 0")


def main() -> int:
    """Entry point: report uncollected test files, non-zero when any exist."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    args = parser.parse_args()

    report = build_report()
    if args.json:
        print(
            json.dumps(
                {
                    "collected": {path: sorted(sources) for path, sources in sorted(report.collected.items())},
                    "uncollected": report.uncollected,
                    "allowlisted": report.allowlisted,
                    "stale_allowlist": report.stale_allowlist,
                },
                indent=2,
            )
        )
    else:
        _print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
