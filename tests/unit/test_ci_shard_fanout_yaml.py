"""The unit-test fan-out is the critical path, and its three declarations must agree.

Measured 2026-09-03..09-10, a single `Test (ubuntu-latest, …)` shard was the critical path of every
merge-queue and trunk run: 28.7 min median shard wall against 30.8 min median to `CI gate` — under
three minutes for everything else in the workflow. The suite runs each of ~1.4k files in its own
subprocess, so per-shard wall is close to linear in file count and the fan-out is the whole lever
(#5794).

The fan-out is declared in three places that GitHub cannot cross-check: `TEST_SHARD_COUNT` (the
`--shard i/N` denominator), the `shard:` matrix list, and the `exclude` rows that trim the list on
rows running fewer shards. A disagreement is silent in the worst direction — `--shard 7/4` either
fails the run or, on a slicer that clamps, tests nothing and reports success — so it is asserted
here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

UBUNTU = "ubuntu-latest"
WINDOWS = "windows-latest"


def _test_job() -> dict[str, Any]:
    doc = cast("dict[str, Any]", yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))
    job = doc["jobs"]["test"]
    assert isinstance(job, dict)
    return cast("dict[str, Any]", job)


def _shard_count_for(os_name: str) -> int:
    """The `--shard i/N` denominator this row resolves to.

    Read out of the expression rather than restated, so the test cannot pass against a workflow that
    no longer says what it says here.
    """
    expression = str(_test_job()["env"]["TEST_SHARD_COUNT"])
    counts = re.findall(r"'(\d+)'", expression)
    assert len(counts) == 2, f"expected a two-branch shard-count expression, got {expression!r}"
    windows_count, other_count = (int(value) for value in counts)
    assert WINDOWS in expression, "the shard count no longer keys off the matrix row"
    return windows_count if os_name == WINDOWS else other_count


def _declared_shards() -> list[int]:
    shards = _test_job()["strategy"]["matrix"]["shard"]
    assert isinstance(shards, list), "the shard list is an expression again; keep it literal"
    return [int(value) for value in shards]


def _excluded_shards_for(os_name: str) -> set[int]:
    excludes = _test_job()["strategy"]["matrix"].get("exclude", [])
    return {
        int(row["shard"]) for row in excludes if isinstance(row, dict) and row.get("os") == os_name and "shard" in row
    }


def _shards_that_run_on(os_name: str) -> list[int]:
    excluded = _excluded_shards_for(os_name)
    return [shard for shard in _declared_shards() if shard not in excluded]


@pytest.mark.parametrize("os_name", [UBUNTU, WINDOWS])
def test_every_row_runs_exactly_its_own_shard_count(os_name: str) -> None:
    """`--shard i/N` for i beyond N is either a hard failure or a silent no-op."""
    running = _shards_that_run_on(os_name)
    assert running == list(range(1, _shard_count_for(os_name) + 1)), (
        f"{os_name} runs shards {running} against a count of {_shard_count_for(os_name)}"
    )


@pytest.mark.parametrize("os_name", [UBUNTU, WINDOWS])
def test_the_slices_cover_the_file_list_once_each(os_name: str) -> None:
    """Disjoint AND complete: a gap is a test that never ran and reported green."""
    running = _shards_that_run_on(os_name)
    assert len(running) == len(set(running)), f"{os_name} declares a duplicate shard"
    assert min(running) == 1
    assert max(running) == _shard_count_for(os_name)


def test_the_critical_path_row_carries_the_wider_fan_out() -> None:
    """Ubuntu is the slowest row and the one on the critical path; it gets the shards.

    Windows deliberately stays narrower: it is not the critical path, and a windows runner-minute
    costs more than a linux one.
    """
    assert _shard_count_for(UBUNTU) > _shard_count_for(WINDOWS)


def test_the_matrix_list_is_the_widest_row() -> None:
    """A matrix dimension cannot be computed from another dimension.

    So the list has to be the widest row and the narrower ones trim it with `exclude`. If the list
    ever shrinks below the ubuntu count, ubuntu quietly stops running its last shards.
    """
    assert max(_declared_shards()) == _shard_count_for(UBUNTU)
    assert _excluded_shards_for(UBUNTU) == set()
