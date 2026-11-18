"""CI coverage artifact production and gate policy."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WF = REPO_ROOT / ".github/workflows/ci.yml"


@pytest.fixture(scope="module")
def ci_doc() -> dict[str, object]:
    """Parse the CI workflow once per module."""
    return yaml.safe_load(CI_WF.read_text(encoding="utf-8"))


def _jobs(ci_doc: dict[str, object]) -> dict[str, object]:
    jobs = ci_doc.get("jobs")
    assert isinstance(jobs, dict)
    return jobs


def _steps(job: object) -> list[dict[str, object]]:
    assert isinstance(job, dict)
    steps = job.get("steps")
    assert isinstance(steps, list)
    return [step for step in steps if isinstance(step, dict)]


def test_main_full_suite_coverage_runs_inside_ubuntu_313_shards(ci_doc: dict[str, object]) -> None:
    """Coverage must reuse the sharded test pass, not start a serial full rerun."""
    test_job = _jobs(ci_doc).get("test")
    steps = _steps(test_job)
    run_step = next(step for step in steps if step.get("name") == "Run isolated test suite (Linux/macOS)")
    run_body = str(run_step.get("run", ""))

    assert "--coverage" in run_body
    assert "PYTHON_VERSION" in run_step.get("env", {})
    assert "matrix.python-version" in str(run_step.get("env", {}).get("PYTHON_VERSION"))
    assert "3.13" in run_body
    assert "push" in run_body
    assert "workflow_dispatch" in run_body
    assert "refs/heads/main" in run_body

    step_names = [str(step.get("name", "")) for step in steps]
    assert "Generate coverage and JUnit reports (ubuntu, 3.13, shard 1 only)" not in step_names


def test_ubuntu_313_shards_upload_coverage_data(ci_doc: dict[str, object]) -> None:
    """Each Ubuntu 3.13 shard must publish its coverage data under a unique name."""
    test_job = _jobs(ci_doc).get("test")
    steps = _steps(test_job)

    prepare = next(step for step in steps if step.get("name") == "Prepare coverage shard artifact")
    assert "workflow_dispatch" in str(prepare.get("if", ""))
    assert "refs/heads/main" in str(prepare.get("if", ""))
    assert "matrix.shard" in str(prepare.get("run", ""))
    assert ".coverage.${{ matrix.shard }}" in str(prepare.get("run", ""))

    upload = next(step for step in steps if step.get("name") == "Upload coverage shard artifact")
    assert "workflow_dispatch" in str(upload.get("if", ""))
    assert "actions/upload-artifact" in str(upload.get("uses", ""))
    with_block = upload.get("with")
    assert isinstance(with_block, dict)
    assert with_block.get("name") == "coverage-data-${{ matrix.shard }}"
    assert ".coverage.${{ matrix.shard }}" in str(with_block.get("path", ""))
    assert with_block.get("if-no-files-found") == "error"
    assert with_block.get("include-hidden-files") is True


def test_coverage_report_job_merges_shard_data(ci_doc: dict[str, object]) -> None:
    """Full main runs combine shard data into a single coverage.xml."""
    coverage_job = _jobs(ci_doc).get("coverage-report")
    assert isinstance(coverage_job, dict), "CI workflow must define a coverage-report job"
    assert coverage_job.get("name") == "Coverage report"
    assert coverage_job.get("needs") == ["test"]
    condition = " ".join(str(coverage_job.get("if", "")).split())
    assert condition.startswith("!cancelled() && needs.test.result == 'success' &&")
    assert "github.event_name == 'workflow_dispatch'" in condition
    assert "github.event_name == 'push'" in condition
    assert "chore(release)" in condition
    assert "release:" in condition
    assert "refs/heads/main" in condition

    steps = _steps(coverage_job)
    download = next(step for step in steps if step.get("name") == "Download coverage shard artifacts")
    assert "actions/download-artifact" in str(download.get("uses", ""))
    with_block = download.get("with")
    assert isinstance(with_block, dict)
    assert with_block.get("pattern") == "coverage-data-*"
    assert with_block.get("merge-multiple") is True

    merge = next(step for step in steps if step.get("name") == "Merge coverage shards")
    merge_run = str(merge.get("run", ""))
    assert "coverage combine" in merge_run
    assert "coverage xml --ignore-errors -o coverage.xml" in merge_run

    upload = next(step for step in steps if step.get("name") == "Upload coverage report artifact")
    assert "actions/upload-artifact" in str(upload.get("uses", ""))
    upload_with = upload.get("with")
    assert isinstance(upload_with, dict)
    assert upload_with.get("name") == "coverage-report"
    assert upload_with.get("path") == "coverage.xml"
    assert upload_with.get("if-no-files-found") == "error"


@pytest.mark.parametrize(
    ("event", "ref", "head_commit_message", "coverage_required"),
    [
        ("workflow_dispatch", "refs/heads/main", "", True),
        ("push", "refs/heads/main", "chore(release): bump version", True),
        ("push", "refs/heads/main", "release: bump version", True),
        ("push", "refs/heads/main", "fix: repair a regression", False),
        ("pull_request", "refs/pull/123/merge", "", False),
        ("merge_group", "refs/heads/gh-readonly-queue/main/pr-123", "", False),
        ("workflow_dispatch", "refs/heads/topic", "", False),
    ],
)
@pytest.mark.parametrize("coverage_result", ["success", "skipped", "failure", "cancelled"])
def test_ci_gate_requires_coverage_report_on_full_main_runs(
    ci_doc: dict[str, object],
    tmp_path: Path,
    event: str,
    ref: str,
    head_commit_message: str,
    coverage_required: bool,
    coverage_result: str,
) -> None:
    """Coverage skips are accepted only on events outside the full main lane."""
    gate = _jobs(ci_doc).get("ci-gate")
    assert isinstance(gate, dict)
    needs = gate.get("needs")
    assert isinstance(needs, list)
    assert "coverage-report" in needs

    rollup = next(step for step in _steps(gate) if step.get("id") == "roll-up")
    run_body = str(rollup.get("run", ""))
    script = run_body.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    results = {
        "determine-changes": {"result": "success"},
        "test": {"result": "success"},
        "coverage-report": {"result": coverage_result},
    }
    (tmp_path / "results.json").write_text(json.dumps(results), encoding="utf-8")
    # The failed cadence run classified its tip as docs-only even though the
    # full suite ran. That classification must never exempt coverage on main.
    plan = {"docs_only": "true", "python_changed": "false"}
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    event_file = tmp_path / "event.json"
    event_file.write_text("{}", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "EVENT_NAME": event,
            "REF_NAME": ref,
            "HEAD_COMMIT_MESSAGE": head_commit_message,
            "GITHUB_EVENT_PATH": str(event_file),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    expect_success = coverage_result == "success" or (coverage_result == "skipped" and not coverage_required)
    assert result.returncode == (0 if expect_success else 1), result.stdout + result.stderr
    if not expect_success:
        assert f"coverage-report: result={coverage_result}" in result.stdout
