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

import json
import os
import re
import subprocess
import sys
import textwrap
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


def _shard_count_for(os_name: str, event: str = "pull_request") -> int:
    """The `--shard i/N` denominator this row resolves to.

    Read out of the expression rather than restated, so the test cannot pass against a workflow that
    no longer says what it says here.
    """
    expression = str(_test_job()["env"]["TEST_SHARD_COUNT"])
    counts = re.findall(r"'(\d+)'", expression)
    assert "github.event_name == 'merge_group'" in expression
    assert len(counts) == 3, f"expected queue, Windows and Ubuntu shard counts, got {expression!r}"
    queue_count, windows_count, other_count = (int(value) for value in counts)
    assert WINDOWS in expression, "the shard count no longer keys off the matrix row"
    if event == "merge_group":
        return queue_count
    return windows_count if os_name == WINDOWS else other_count


def _declared_shards(event: str = "pull_request") -> list[int]:
    expression = _test_job()["strategy"]["matrix"]["shard"]
    match = re.fullmatch(
        r"\$\{\{ github.event_name == 'merge_group' && fromJSON\('([^']+)'\) \|\| fromJSON\('([^']+)'\) \}\}",
        expression,
    )
    assert match, f"expected a merge-group-only shard override, got {expression!r}"
    shards = json.loads(match.group(1 if event == "merge_group" else 2))
    return [int(value) for value in shards]


def _excluded_shards_for(os_name: str) -> set[int]:
    excludes = _test_job()["strategy"]["matrix"].get("exclude", [])
    return {
        int(row["shard"]) for row in excludes if isinstance(row, dict) and row.get("os") == os_name and "shard" in row
    }


def _shards_that_run_on(os_name: str, event: str = "pull_request") -> list[int]:
    excluded = _excluded_shards_for(os_name)
    return [shard for shard in _declared_shards(event) if shard not in excluded]


@pytest.mark.parametrize("os_name", [UBUNTU, WINDOWS])
@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch", "schedule"])
def test_every_row_runs_exactly_its_own_shard_count(os_name: str, event: str) -> None:
    """`--shard i/N` for i beyond N is either a hard failure or a silent no-op."""
    running = _shards_that_run_on(os_name, event)
    assert running == list(range(1, _shard_count_for(os_name, event) + 1)), (
        f"{os_name} runs shards {running} against a count of {_shard_count_for(os_name, event)}"
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


def test_merge_group_runs_one_complete_ubuntu_shard() -> None:
    matrix = _test_job()["strategy"]["matrix"]
    assert matrix["os"] == (
        "${{ github.event_name == 'merge_group' && fromJSON('[\"ubuntu-latest\"]') "
        '|| fromJSON(\'["ubuntu-latest", "windows-latest"]\') }}'
    )
    assert matrix["python-version"] == (
        "${{ (github.event_name == 'push' || github.event_name == 'workflow_dispatch') "
        '&& fromJSON(\'["3.12", "3.13"]\') || fromJSON(\'["3.13"]\') }}'
    )
    assert _shards_that_run_on(UBUNTU, "merge_group") == [1]
    assert _shard_count_for(UBUNTU, "merge_group") == 1


@pytest.mark.parametrize(
    ("event", "test_result", "windows_result", "expected"),
    [
        ("merge_group", "success", "skipped", 0),
        ("merge_group", "failure", "skipped", 1),
        ("merge_group", "cancelled", "skipped", 1),
        ("merge_group", "skipped", "skipped", 1),
        ("merge_group", None, "skipped", 1),
        ("merge_group", "success", "failure", 1),
        ("merge_group", "success", "cancelled", 1),
        ("pull_request", "success", "skipped", 1),
        ("push", "success", "skipped", 1),
        ("workflow_dispatch", "success", "skipped", 1),
    ],
)
def test_queue_platform_skip_keeps_ci_gate_honest(
    tmp_path: Path, event: str, test_result: str | None, windows_result: str, expected: int
) -> None:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = doc["jobs"]
    assert jobs["adapter-conformance-windows"]["if"] == "github.event_name != 'merge_group'"
    gate = jobs["ci-gate"]
    assert gate["if"] == "always() && !cancelled()"
    needs = {name: {"result": "success"} for name in gate["needs"]}
    needs["test"]["result"] = test_result
    needs["adapter-conformance-windows"]["result"] = windows_result
    (tmp_path / "results.json").write_text(json.dumps(needs))
    (tmp_path / "plan.json").write_text(json.dumps({"docs_only": "false", "macos_sensitive": "true"}))
    rollup = next(step["run"] for step in gate["steps"] if step.get("id") == "roll-up")
    match = re.search(r"<<'PY'\n(.*?)\n\s*PY\b", rollup, re.DOTALL)
    assert match
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(match.group(1))],
        cwd=tmp_path,
        env={**os.environ, "EVENT_NAME": event},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == expected, proc.stdout + proc.stderr
