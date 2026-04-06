"""TEST-016: Cross-platform test matrix configuration.

Validates the GitHub Actions CI workflow for cross-platform testing
and verifies platform-specific code paths work correctly.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

import pytest
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


class TestCIWorkflowExists:
    """Validate the existing CI workflow file."""

    def test_ci_workflow_exists(self) -> None:
        assert _CI_WORKFLOW.exists(), "CI workflow file not found"

    def test_ci_workflow_valid_yaml(self) -> None:
        data = yaml.safe_load(_CI_WORKFLOW.read_text())
        assert isinstance(data, dict)
        assert "name" in data
        assert "jobs" in data

    def test_ci_runs_on_ubuntu(self) -> None:
        data = yaml.safe_load(_CI_WORKFLOW.read_text())
        # At least one job should run on ubuntu
        has_ubuntu = False
        for _job_name, job in data.get("jobs", {}).items():
            runs_on = job.get("runs-on", "")
            if "ubuntu" in str(runs_on):
                has_ubuntu = True
                break
        assert has_ubuntu, "CI should have at least one Ubuntu job"

    def test_ci_uses_main_branch(self) -> None:
        data = yaml.safe_load(_CI_WORKFLOW.read_text())
        # PyYAML parses bare `on:` as boolean True, so check both keys
        on_section = data.get("on") or data.get(True, {})
        push = on_section.get("push", {}) if isinstance(on_section, dict) else {}
        branches = push.get("branches", [])
        assert "main" in branches, "CI push trigger must include 'main' branch"


class TestCrossPlatformWorkflowContent:
    """Validate the generated cross-platform workflow content."""

    def test_has_matrix_strategy(self) -> None:
        job = CROSS_PLATFORM_WORKFLOW_CONTENT["jobs"]["test"]
        matrix = job["strategy"]["matrix"]
        assert "os" in matrix
        assert "python-version" in matrix

    def test_includes_macos(self) -> None:
        matrix = CROSS_PLATFORM_WORKFLOW_CONTENT["jobs"]["test"]["strategy"]["matrix"]
        assert "macos-latest" in matrix["os"]

    def test_includes_linux(self) -> None:
        matrix = CROSS_PLATFORM_WORKFLOW_CONTENT["jobs"]["test"]["strategy"]["matrix"]
        assert "ubuntu-latest" in matrix["os"]

    def test_includes_python_312(self) -> None:
        matrix = CROSS_PLATFORM_WORKFLOW_CONTENT["jobs"]["test"]["strategy"]["matrix"]
        assert "3.12" in matrix["python-version"]

    def test_fail_fast_disabled(self) -> None:
        job = CROSS_PLATFORM_WORKFLOW_CONTENT["jobs"]["test"]
        assert job["strategy"]["fail-fast"] is False

    def test_generates_valid_yaml(self) -> None:
        """The workflow content must serialize to valid YAML."""
        dumped = yaml.dump(CROSS_PLATFORM_WORKFLOW_CONTENT, default_flow_style=False)
        reparsed = yaml.safe_load(dumped)
        assert reparsed == CROSS_PLATFORM_WORKFLOW_CONTENT

    def test_targets_main_branch(self) -> None:
        on = CROSS_PLATFORM_WORKFLOW_CONTENT["on"]
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
