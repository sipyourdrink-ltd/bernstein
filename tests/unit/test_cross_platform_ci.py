"""TEST-016: Cross-platform test matrix configuration.

Validates the GitHub Actions CI workflow for cross-platform testing
and verifies platform-specific code paths work correctly.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Any, cast

import yaml

# ---------------------------------------------------------------------------
# CI workflow validation
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_CROSS_PLATFORM_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "cross-platform.yml"

# The cross-platform workflow content we generate/validate
CROSS_PLATFORM_WORKFLOW_CONTENT = {
    "name": "Cross-Platform Tests",
    "on": {
        "push": {"branches": ["main"]},
        "pull_request": {"branches": ["main"]},
    },
    "permissions": {"contents": "read"},
    "concurrency": {
        "group": "cross-platform-${{ github.ref }}",
        "cancel-in-progress": True,
    },
    "jobs": {
        "test": {
            "strategy": {
                "fail-fast": False,
                "matrix": {
                    "os": ["ubuntu-latest", "macos-latest"],
                    "python-version": ["3.12", "3.13"],
                },
            },
            "runs-on": "${{ matrix.os }}",
            "timeout-minutes": 20,
            "steps": [
                {"uses": "actions/checkout@v6"},
                {
                    "uses": "astral-sh/setup-uv@v7",
                    "with": {"python-version": "${{ matrix.python-version }}"},
                },
                {
                    "name": "Install dependencies",
                    "run": "uv pip install -e '.[dev]' --system",
                },
                {
                    "name": "Run tests",
                    "run": "uv run python scripts/run_tests.py -x",
                },
            ],
        },
    },
}


def _load_ci_workflow() -> dict[object, Any]:
    """Load the CI workflow as a typed mapping for assertions."""
    data = yaml.safe_load(_CI_WORKFLOW.read_text())
    assert isinstance(data, dict)
    return cast("dict[object, Any]", data)


def _ci_test_steps(data: dict[object, Any]) -> list[dict[str, Any]]:
    """Return the step list for the main CI test job."""
    jobs = cast("dict[str, Any]", data["jobs"])
    test_job = cast("dict[str, Any]", jobs["test"])
    steps = cast("list[Any]", test_job["steps"])
    typed_steps: list[dict[str, Any]] = []
    for raw_step in steps:
        if isinstance(raw_step, dict):
            typed_steps.append(cast("dict[str, Any]", raw_step))
    return typed_steps


class TestCIWorkflowExists:
    """Validate the existing CI workflow file."""

    def test_ci_workflow_exists(self) -> None:
        assert _CI_WORKFLOW.exists(), "CI workflow file not found"

    def test_ci_workflow_valid_yaml(self) -> None:
        data = _load_ci_workflow()
        assert "name" in data
        assert "jobs" in data

    def test_ci_runs_on_ubuntu(self) -> None:
        data = _load_ci_workflow()
        # At least one job should run on ubuntu
        has_ubuntu = False
        jobs = cast("dict[str, Any]", data["jobs"])
        for _job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            typed_job = cast("dict[str, Any]", job)
            runs_on = str(typed_job.get("runs-on", ""))
            if "ubuntu" in runs_on:
                has_ubuntu = True
                break
        assert has_ubuntu, "CI should have at least one Ubuntu job"

    def test_ci_uses_main_branch(self) -> None:
        data = _load_ci_workflow()
        # PyYAML parses bare `on:` as boolean True, so check both keys
        on_section_raw = data["on"] if "on" in data else data.get(True, {})
        on_section = cast("dict[str, Any]", on_section_raw if isinstance(on_section_raw, dict) else {})
        push = cast("dict[str, Any]", on_section.get("push", {}))
        branches = cast("list[str]", push.get("branches", []))
        assert "main" in branches, "CI push trigger must include 'main' branch"

    def test_pull_request_test_job_fetches_base_ref_for_impacted_tests(self) -> None:
        data = _load_ci_workflow()
        steps = _ci_test_steps(data)
        fetch_steps = [step for step in steps if step.get("name") == "Fetch base ref for impacted-test selection"]
        assert len(fetch_steps) == 1
        fetch_step = fetch_steps[0]
        assert fetch_step.get("if") == "github.event_name == 'pull_request' && runner.os != 'Windows'"
        assert "refs/heads/${{ github.base_ref }}" in fetch_step.get("run", "")

    def test_pull_request_test_job_uses_affected_runner_with_fallback(self) -> None:
        data = _load_ci_workflow()
        steps = _ci_test_steps(data)
        run_steps = [step for step in steps if (step.get("name") or "").startswith("Run isolated test suite")]
        assert run_steps, "expected at least one 'Run isolated test suite' step"
        # The Linux/macOS variant carries the --affected fallback logic.
        unix_steps = [step for step in run_steps if "Linux/macOS" in (step.get("name") or "")]
        assert len(unix_steps) == 1
        run_script = unix_steps[0].get("run", "")
        assert "--affected" in run_script
        assert "refs/remotes/origin/${{ github.base_ref }}" in run_script
        assert "uv run python scripts/run_tests.py -x --parallel 4" in run_script

    def test_coverage_reporting_only_runs_on_push(self) -> None:
        data = _load_ci_workflow()
        steps = _ci_test_steps(data)
        coverage_steps = [
            step
            for step in steps
            if "3.13 only" in step.get("name", "")
            or "coverage" in step.get("name", "").lower()
            or "Codecov" in step.get("name", "")
        ]
        assert coverage_steps, "expected coverage-related steps in CI workflow"
        for step in coverage_steps:
            condition = step.get("if", "")
            assert "github.event_name == 'push'" in condition


class TestCrossPlatformWorkflowContent:
    """Validate the generated cross-platform workflow content."""

    def test_has_matrix_strategy(self) -> None:
        jobs = cast("dict[str, Any]", CROSS_PLATFORM_WORKFLOW_CONTENT["jobs"])
        job = cast("dict[str, Any]", jobs["test"])
        strategy = cast("dict[str, Any]", job["strategy"])
        matrix = cast("dict[str, list[str]]", strategy["matrix"])
        assert "os" in matrix
        assert "python-version" in matrix

    def test_includes_macos(self) -> None:
        jobs = cast("dict[str, Any]", CROSS_PLATFORM_WORKFLOW_CONTENT["jobs"])
        job = cast("dict[str, Any]", jobs["test"])
        strategy = cast("dict[str, Any]", job["strategy"])
        matrix = cast("dict[str, list[str]]", strategy["matrix"])
        assert "macos-latest" in matrix["os"]

    def test_includes_linux(self) -> None:
        jobs = cast("dict[str, Any]", CROSS_PLATFORM_WORKFLOW_CONTENT["jobs"])
        job = cast("dict[str, Any]", jobs["test"])
        strategy = cast("dict[str, Any]", job["strategy"])
        matrix = cast("dict[str, list[str]]", strategy["matrix"])
        assert "ubuntu-latest" in matrix["os"]

    def test_includes_python_312(self) -> None:
        jobs = cast("dict[str, Any]", CROSS_PLATFORM_WORKFLOW_CONTENT["jobs"])
        job = cast("dict[str, Any]", jobs["test"])
        strategy = cast("dict[str, Any]", job["strategy"])
        matrix = cast("dict[str, list[str]]", strategy["matrix"])
        assert "3.12" in matrix["python-version"]

    def test_fail_fast_disabled(self) -> None:
        jobs = cast("dict[str, Any]", CROSS_PLATFORM_WORKFLOW_CONTENT["jobs"])
        job = cast("dict[str, Any]", jobs["test"])
        strategy = cast("dict[str, Any]", job["strategy"])
        assert strategy["fail-fast"] is False

    def test_generates_valid_yaml(self) -> None:
        """The workflow content must serialize to valid YAML."""
        dumped = yaml.dump(CROSS_PLATFORM_WORKFLOW_CONTENT, default_flow_style=False)
        reparsed = yaml.safe_load(dumped)
        assert reparsed == CROSS_PLATFORM_WORKFLOW_CONTENT

    def test_targets_main_branch(self) -> None:
        on = cast("dict[str, dict[str, list[str]]]", CROSS_PLATFORM_WORKFLOW_CONTENT["on"])
        assert "main" in on["push"]["branches"]
        assert "main" in on["pull_request"]["branches"]


# ---------------------------------------------------------------------------
# Platform compatibility
# ---------------------------------------------------------------------------


class TestPlatformCompatibility:
    """Test that platform-specific utilities work on the current OS."""

    def test_platform_detected(self) -> None:
        assert platform.system() in ("Darwin", "Linux", "Windows")

    def test_path_separator(self) -> None:
        """Path operations work regardless of separator."""
        p = Path("src") / "bernstein" / "core" / "models.py"
        assert p.parts[-1] == "models.py"

    def test_python_version_adequate(self) -> None:
        assert sys.version_info >= (3, 12), "Python 3.12+ required"

    def test_platform_compat_module_importable(self) -> None:
        """The platform_compat module must be importable everywhere."""
        from bernstein.core.platform_compat import kill_process_group, process_alive

        assert callable(kill_process_group)
        assert callable(process_alive)

    def test_signal_handling_available(self) -> None:
        """Signal handling must be available on this platform."""
        import signal

        # SIGTERM is available on all supported platforms
        assert hasattr(signal, "SIGTERM")
        if platform.system() != "Windows":
            assert hasattr(signal, "SIGKILL")
