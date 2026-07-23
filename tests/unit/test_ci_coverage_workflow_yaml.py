"""Structural assertions for CI coverage artifact production."""

from __future__ import annotations

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


def test_push_coverage_runs_inside_ubuntu_313_shards(ci_doc: dict[str, object]) -> None:
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

    step_names = [str(step.get("name", "")) for step in steps]
    assert "Generate coverage and JUnit reports (ubuntu, 3.13, shard 1 only)" not in step_names


def test_ubuntu_313_shards_upload_coverage_data(ci_doc: dict[str, object]) -> None:
    """Each Ubuntu 3.13 shard must publish its coverage data under a unique name."""
    test_job = _jobs(ci_doc).get("test")
    steps = _steps(test_job)

    prepare = next(step for step in steps if step.get("name") == "Prepare coverage shard artifact")
    assert "matrix.shard" in str(prepare.get("run", ""))
    assert ".coverage.${{ matrix.shard }}" in str(prepare.get("run", ""))

    upload = next(step for step in steps if step.get("name") == "Upload coverage shard artifact")
    assert "actions/upload-artifact" in str(upload.get("uses", ""))
    with_block = upload.get("with")
    assert isinstance(with_block, dict)
    assert with_block.get("name") == "coverage-data-${{ matrix.shard }}"
    assert ".coverage.${{ matrix.shard }}" in str(with_block.get("path", ""))
    assert with_block.get("if-no-files-found") == "error"
    assert with_block.get("include-hidden-files") is True


def test_coverage_report_job_merges_shard_data(ci_doc: dict[str, object]) -> None:
    """A separate push-only job must combine shard data into a single coverage.xml."""
    coverage_job = _jobs(ci_doc).get("coverage-report")
    assert isinstance(coverage_job, dict), "CI workflow must define a coverage-report job"
    assert coverage_job.get("name") == "Coverage report"
    assert coverage_job.get("needs") == ["test"]
    assert "github.event_name == 'push'" in str(coverage_job.get("if", ""))

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


def test_ci_gate_requires_coverage_report_on_push(ci_doc: dict[str, object]) -> None:
    """The aggregator must fail push CI if the coverage artifact job fails."""
    gate = _jobs(ci_doc).get("ci-gate")
    assert isinstance(gate, dict)
    needs = gate.get("needs")
    assert isinstance(needs, list)
    assert "coverage-report" in needs

    rollup = next(step for step in _steps(gate) if step.get("id") == "roll-up")
    run_body = str(rollup.get("run", ""))
    assert "PUSH_ONLY" in run_body
    assert '"coverage-report"' in run_body
    assert 'event != "push"' in run_body
