"""Regression test suite auto-expansion for agent-produced changes.

When an agent modifies source files, this module identifies which changed files
have no corresponding test coverage (by checking for matching test files in the
``tests/`` tree).  Uncovered files are written to
``.sdd/runtime/needs_coverage.json`` so that a test-writing agent can be
dispatched in a follow-up task.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Directories that contain tests — used to detect test files and to locate
# matching unit-test modules.
_TEST_DIR_NAMES = frozenset({"tests", "test"})

# Source file prefixes/names that carry no meaningful testable logic.
_SKIP_NAMES = frozenset({"__init__.py", "conftest.py"})


@dataclass
class ExpansionResult:
    """Outcome of a test-coverage scan for a set of changed files.

    Attributes:
        uncovered_files: Source files with no matching test file found.
        covered_files: Source files that already have at least one test file.
    """

    uncovered_files: list[str]
    covered_files: list[str]

    @property
    def needs_action(self) -> bool:
        """True when at least one source file lacks test coverage."""
        return bool(self.uncovered_files)


@dataclass
class NeedsCoverageRecord:
    """A single entry in the needs-coverage list.

    Attributes:
        source_file: Repo-relative path of the uncovered source file.
        task_id: ID of the task that produced the change.
    """

    source_file: str
    task_id: str = ""


def _is_test_file(path: str) -> bool:
    """Return True if ``path`` is a test file or lives inside a test directory."""
    parts = Path(path).parts
    # File is a test if any ancestor directory is a known test dir, or the
    # filename starts with ``test_``.
    if any(p in _TEST_DIR_NAMES for p in parts[:-1]):
        return True
    return Path(path).name.startswith("test_")


def _find_test_file(source_file: str, workdir: Path) -> bool:
    """Return True if a matching test file exists for *source_file* in workdir.

    Looks for ``tests/unit/test_<module_name>.py`` as the primary candidate,
    then falls back to any ``test_<module_name>.py`` anywhere under ``tests/``.
    """
    stem = Path(source_file).stem
    test_name = f"test_{stem}.py"

    # Primary: tests/unit/test_<stem>.py
    primary = workdir / "tests" / "unit" / test_name
    if primary.exists():
        return True

    # Secondary: any tests/**/ test_<stem>.py
    for candidate in (workdir / "tests").rglob(test_name) if (workdir / "tests").exists() else []:
        if candidate.exists():
            return True

    return False


def find_uncovered_source_files(
    changed_files: list[str],
    workdir: Path,
) -> ExpansionResult:
    """Identify changed source files that lack a corresponding test file.

    Args:
        changed_files: Repo-relative paths of files changed by the agent.
        workdir: Repository root used to locate the ``tests/`` tree.

    Returns:
        A :class:`ExpansionResult` with the covered/uncovered split.
    """
    uncovered: list[str] = []
    covered: list[str] = []

    for f in changed_files:
        p = Path(f)

        # Only care about Python source files.
        if p.suffix != ".py":
            continue

        # Skip test files themselves and known boilerplate.
        if _is_test_file(f) or p.name in _SKIP_NAMES:
            continue

        if _find_test_file(f, workdir):
            covered.append(f)
        else:
            uncovered.append(f)

    return ExpansionResult(uncovered_files=uncovered, covered_files=covered)


def write_needs_coverage(
    records: list[NeedsCoverageRecord],
    workdir: Path,
) -> Path:
    """Persist uncovered-file records to ``.sdd/runtime/needs_coverage.json``.

    Merges with any existing list, deduplicating by ``source_file``.  Only the
    first record for each unique ``source_file`` is retained so that repeated
    runs do not accumulate duplicates.

    Args:
        records: New records to add.
        workdir: Repository root.

    Returns:
        Path to the written JSON file.
    """
    out_dir = workdir / ".sdd" / "runtime"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "needs_coverage.json"

    existing: list[dict[str, str]] = []
    if out_path.exists():
        try:
            raw: object = json.loads(out_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                existing = [e for e in raw if isinstance(e, dict)]
        except (OSError, json.JSONDecodeError):
            existing = []

    seen: set[str] = {e.get("source_file", "") for e in existing}
    for rec in records:
        if rec.source_file not in seen:
            existing.append(asdict(rec))
            seen.add(rec.source_file)

    out_path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("test_expansion: needs_coverage list updated — %d entries", len(existing))
    return out_path
