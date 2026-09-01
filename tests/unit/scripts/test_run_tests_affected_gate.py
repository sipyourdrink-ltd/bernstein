"""Tests for the fail-closed rule in ``scripts/run_tests.py --affected``.

On a pull request each CI shard runs ``run_tests.py --shard i/N --affected
<base>``. When the affected-test selector returns nothing, the runner has to
decide between two very different situations:

- the change touched code the shards are responsible for and the selector
  still found no test, which is a coverage hole and must fail closed; and
- the change touched only surfaces the shards are not responsible for, which
  is a legitimate no-op and must exit 0.

Getting that split wrong is expensive in both directions. Failing open hides a
regression behind a green suite. Failing closed on a suite the selector cannot
map makes every change to that suite permanently unmergeable, because no
content of the change can ever produce a non-empty affected set.

The second failure mode is the one these tests exist to prevent. The selector's
dependency map indexes only ``scripts/test_impact.TEST_DIRS``; the remaining
suites under ``tests/`` are run in full by their own required jobs. The
exemption list in ``run_tests._SELF_COVERED_TEST_PREFIXES`` encodes that, and
the drift guards below pin the two facts that make each entry safe:

1. the suite is genuinely outside the selector's index, so requiring an
   affected test from it is unsatisfiable; and
2. a job in ``ci.yml`` runs the whole suite unconditionally on every pull
   request and the CI gate depends on that job, so the coverage the shards are
   being excused from actually exists somewhere else.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_tests.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
GATE_JOB_ID = "ci-gate"


@pytest.fixture
def run_tests_module() -> Generator[ModuleType, None, None]:
    """Load scripts/run_tests.py as an importable module."""
    spec = importlib.util.spec_from_file_location("run_tests_gate_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _ci_jobs() -> dict[str, Any]:
    """Return the job table of the main CI workflow."""
    yaml = pytest.importorskip("yaml", reason="pyyaml is required to read the workflow")
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "ci.yml has no jobs table"
    return jobs


def _jobs_running_directory(jobs: dict[str, Any], prefix: str) -> list[str]:
    """Return ids of jobs whose run steps hand the whole ``prefix`` to pytest."""
    matches: list[str] = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            body = step.get("run")
            if not isinstance(body, str):
                continue
            for token in body.split():
                # A directory token, with or without its trailing slash, is
                # the only form that collects the whole suite. A single file
                # under the suite would not excuse the shards.
                if token.rstrip("/") + "/" == prefix and "pytest" in body:
                    matches.append(job_id)
                    break
            else:
                continue
            break
    return matches


# --- the fail-closed decision ----------------------------------------------


def test_source_change_without_affected_tests_fails_closed(run_tests_module: ModuleType) -> None:
    """A source change that selects no test is a coverage hole."""
    assert run_tests_module.changed_files_require_tests(["src/bernstein/core/wal.py"]) is True


def test_indexed_test_change_without_affected_tests_fails_closed(
    run_tests_module: ModuleType,
) -> None:
    """A unit test the selector indexes must map back to itself."""
    assert run_tests_module.changed_files_require_tests(["tests/unit/test_wal.py"]) is True


def test_shared_test_helper_change_fails_closed(run_tests_module: ModuleType) -> None:
    """Helpers under tests/ that indexed suites import are not exempt.

    ``tests/support`` and friends have no dedicated job, so an empty affected
    set for them still has to be loud rather than silently green.
    """
    assert run_tests_module.changed_files_require_tests(["tests/support/wal_helpers.py"]) is True


def test_workflow_change_without_affected_tests_fails_closed(run_tests_module: ModuleType) -> None:
    """A workflow change that selects no test still fails closed."""
    assert run_tests_module.changed_files_require_tests([".github/workflows/ci.yml"]) is True


@pytest.mark.parametrize("prefix", ["tests/contract/", "tests/property/", "tests/snapshot/"])
def test_self_covered_suite_change_alone_does_not_fail_closed(
    run_tests_module: ModuleType,
    prefix: str,
) -> None:
    """A change confined to a self-covered suite is a legitimate shard no-op."""
    assert run_tests_module.changed_files_require_tests([f"{prefix}test_something.py"]) is False


def test_self_covered_suite_mixed_with_source_still_fails_closed(
    run_tests_module: ModuleType,
) -> None:
    """The exemption applies per path, so it cannot launder a source change."""
    changed = ["tests/property/test_wal_chain_properties.py", "src/bernstein/core/wal.py"]
    assert run_tests_module.changed_files_require_tests(changed) is True


def test_empty_change_set_does_not_fail_closed(run_tests_module: ModuleType) -> None:
    """No changed files means nothing to cover."""
    assert run_tests_module.changed_files_require_tests([]) is False


def test_deleted_test_file_alone_does_not_fail_closed(run_tests_module: ModuleType) -> None:
    """Removing a test file is the unsatisfiable case, not a coverage hole.

    The only test the selector could map ``tests/unit/test_scanner.py`` to is
    itself, and the change deletes it. Judging the path anyway makes a pull
    request that only removes a test permanently unmergeable.
    """
    changed = ["tests/unit/test_scanner.py"]
    assert run_tests_module.changed_files_require_tests(changed, changed) is False


def test_deleted_test_file_mixed_with_source_still_fails_closed(
    run_tests_module: ModuleType,
) -> None:
    """The exemption applies per path, so a deletion cannot launder a source change."""
    changed = ["tests/unit/test_scanner.py", "src/bernstein/core/wal.py"]
    deleted = ["tests/unit/test_scanner.py"]
    assert run_tests_module.changed_files_require_tests(changed, deleted) is True


def test_deleted_source_file_still_fails_closed(run_tests_module: ModuleType) -> None:
    """Only deleted tests are exempt: a removed module can still have covering tests."""
    changed = ["src/bernstein/core/wal.py"]
    assert run_tests_module.changed_files_require_tests(changed, changed) is True


def test_modified_test_file_is_not_mistaken_for_a_deleted_one(
    run_tests_module: ModuleType,
) -> None:
    """A test file that is edited rather than removed keeps failing closed."""
    assert run_tests_module.changed_files_require_tests(["tests/unit/test_wal.py"], []) is True


# --- drift guards on the exemption list ------------------------------------


def test_exempt_suites_are_outside_the_selector_index(run_tests_module: ModuleType) -> None:
    """Each exemption must name a suite the selector genuinely cannot map.

    If a suite is indexed, the selector can return tests for it and the
    exemption would suppress a real coverage hole instead of an unsatisfiable
    rule.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        from check_test_collection import affected_test_dirs
    finally:
        sys.path.pop(0)

    indexed = tuple(f"{entry.rstrip('/')}/" for entry in affected_test_dirs())
    assert indexed, "the selector must index at least one test directory"

    for prefix in run_tests_module._SELF_COVERED_TEST_PREFIXES:
        assert not prefix.startswith(indexed), (
            f"{prefix} is indexed by the affected-test selector, so it must not be exempt "
            f"from the fail-closed rule (indexed: {indexed})"
        )


# The one job condition that cannot exclude a pull request: it only skips
# docs-only merge-queue entries, so the suite still runs on every PR, which
# is the fact the exemption relies on.
_DOCS_ONLY_MERGE_GROUP_GUARD = re.compile(
    r"\$\{\{\s*!\(github\.event_name == 'merge_group'\s*&&\s*"
    r"needs\.determine-changes\.outputs\.docs_only == 'true'\)\s*\}\}"
)


def test_exempt_suites_have_an_unconditional_gated_job(run_tests_module: ModuleType) -> None:
    """Each exemption must be paid for by a required job that runs the suite."""
    jobs = _ci_jobs()
    gate = jobs.get(GATE_JOB_ID)
    assert isinstance(gate, dict), f"ci.yml has no {GATE_JOB_ID} job"
    gate_needs = set(gate.get("needs") or [])

    for prefix in run_tests_module._SELF_COVERED_TEST_PREFIXES:
        owners = _jobs_running_directory(jobs, prefix)
        assert owners, (
            f"{prefix} is exempt from the fail-closed rule but no ci.yml job runs the whole suite, so nothing covers it"
        )
        gated = [job_id for job_id in owners if job_id in gate_needs]
        assert gated, f"{prefix} is only run by {owners}, none of which the CI gate depends on"
        for job_id in gated:
            job = jobs[job_id]
            condition = job.get("if")
            if condition is None:
                continue
            assert _DOCS_ONLY_MERGE_GROUP_GUARD.fullmatch(str(condition).strip()), (
                f"{job_id} runs {prefix} but is conditional ({condition!r}) in a way that "
                f"can skip a pull request; the exemption assumes the suite runs on every "
                f"pull request"
            )
