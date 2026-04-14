"""Tests for the dep_audit quality gate — checks for vulnerable dependencies."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from bernstein.core.gate_runner import (
    GatePipelineStep,
    GateRunner,
    _is_dep_file,
    build_default_pipeline,
)
from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.quality_gates import QualityGatesConfig


def _make_task(*, owned_files: list[str] | None = None) -> Task:
    return Task(
        id="T-dep-1",
        title="Dep audit task",
        description="Test dep audit gate.",
        role="backend",
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
        owned_files=owned_files or [],
    )


# ---------------------------------------------------------------------------
# _is_dep_file
# ---------------------------------------------------------------------------


class TestIsDepFile:
    def test_pyproject_toml(self) -> None:
        assert _is_dep_file("pyproject.toml")

    def test_nested_pyproject_toml(self) -> None:
        assert _is_dep_file("subdir/pyproject.toml")

    def test_requirements_txt(self) -> None:
        assert _is_dep_file("requirements.txt")

    def test_requirements_dev_txt(self) -> None:
        assert _is_dep_file("requirements-dev.txt")

    def test_setup_py(self) -> None:
        assert _is_dep_file("setup.py")

    def test_pipfile(self) -> None:
        assert _is_dep_file("Pipfile")

    def test_pipfile_lock(self) -> None:
        assert _is_dep_file("Pipfile.lock")

    def test_poetry_lock(self) -> None:
        assert _is_dep_file("poetry.lock")

    def test_uv_lock(self) -> None:
        assert _is_dep_file("uv.lock")

    def test_regular_python_file(self) -> None:
        assert not _is_dep_file("src/bernstein/core/gate_runner.py")

    def test_readme(self) -> None:
        assert not _is_dep_file("README.md")

    def test_config_toml(self) -> None:
        assert not _is_dep_file("config.toml")


# ---------------------------------------------------------------------------
# build_default_pipeline includes dep_audit when enabled
# ---------------------------------------------------------------------------


class TestBuildDefaultPipeline:
    def test_dep_audit_absent_by_default(self) -> None:
        config = QualityGatesConfig()
        pipeline = build_default_pipeline(config)
        names = [step.name for step in pipeline]
        assert "dep_audit" not in names

    def test_dep_audit_present_when_enabled(self) -> None:
        config = QualityGatesConfig(dep_audit=True)
        pipeline = build_default_pipeline(config)
        names = [step.name for step in pipeline]
        assert "dep_audit" in names

    def test_dep_audit_condition_is_deps_changed(self) -> None:
        config = QualityGatesConfig(dep_audit=True)
        pipeline = build_default_pipeline(config)
        dep_step = next(s for s in pipeline if s.name == "dep_audit")
        assert dep_step.condition == "deps_changed"
        assert dep_step.required is True


# ---------------------------------------------------------------------------
# Gate runner: dep_audit execution
# ---------------------------------------------------------------------------


class TestDepAuditGateRunner:
    def test_dep_audit_passes_when_no_vulns(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        config = QualityGatesConfig(
            dep_audit=True,
            pipeline=[GatePipelineStep(name="dep_audit", required=True, condition="deps_changed")],
            cache_enabled=False,
        )
        runner = GateRunner(config, tmp_path)
        task = _make_task(owned_files=["pyproject.toml"])

        with patch(
            "bernstein.core.quality.quality_gates._run_command", return_value=(True, "No vulnerabilities found")
        ):
            report = asyncio.run(runner.run_all(task, tmp_path))

        assert report.overall_pass
        result = next(r for r in report.results if r.name == "dep_audit")
        assert result.status == "pass"
        assert not result.blocked
        assert "no vulnerable dependencies found" in result.details

    def test_dep_audit_blocks_when_vulns_found(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        config = QualityGatesConfig(
            dep_audit=True,
            pipeline=[GatePipelineStep(name="dep_audit", required=True, condition="deps_changed")],
            cache_enabled=False,
        )
        runner = GateRunner(config, tmp_path)
        task = _make_task(owned_files=["pyproject.toml"])

        with patch(
            "bernstein.core.quality_gates._run_command",
            return_value=(False, "Found 2 known vulnerabilities in 1 package"),
        ):
            report = asyncio.run(runner.run_all(task, tmp_path))

        assert not report.overall_pass
        result = next(r for r in report.results if r.name == "dep_audit")
        assert result.status == "fail"
        assert result.blocked

    def test_dep_audit_skipped_when_no_dep_files_changed(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.py").write_text("print('ok')\n", encoding="utf-8")
        config = QualityGatesConfig(
            dep_audit=True,
            pipeline=[GatePipelineStep(name="dep_audit", required=True, condition="deps_changed")],
            cache_enabled=False,
        )
        runner = GateRunner(config, tmp_path)
        task = _make_task(owned_files=["src/app.py"])

        with patch("bernstein.core.quality.quality_gates._run_command", return_value=(True, "ok")) as mock_cmd:
            report = asyncio.run(runner.run_all(task, tmp_path))

        result = next(r for r in report.results if r.name == "dep_audit")
        assert result.status == "skipped"
        mock_cmd.assert_not_called()

    def test_dep_audit_uses_custom_command(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("flask==2.0.0\n", encoding="utf-8")
        config = QualityGatesConfig(
            dep_audit=True,
            dep_audit_command="pip-audit --strict",
            pipeline=[GatePipelineStep(name="dep_audit", required=True, condition="deps_changed")],
            cache_enabled=False,
        )
        runner = GateRunner(config, tmp_path)
        task = _make_task(owned_files=["requirements.txt"])

        captured_command: str | None = None

        def fake_run(command: str, _cwd: Path, _timeout_s: int) -> tuple[bool, str]:
            nonlocal captured_command
            captured_command = command
            return True, "ok"

        with patch("bernstein.core.quality.quality_gates._run_command", side_effect=fake_run):
            asyncio.run(runner.run_all(task, tmp_path))

        assert captured_command is not None
        assert captured_command == "pip-audit --strict"

    def test_dep_audit_command_override_from_pipeline_step(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        config = QualityGatesConfig(
            dep_audit=True,
            pipeline=[
                GatePipelineStep(
                    name="dep_audit",
                    required=True,
                    condition="deps_changed",
                    command_override="safety check",
                ),
            ],
            cache_enabled=False,
        )
        runner = GateRunner(config, tmp_path)
        task = _make_task(owned_files=["pyproject.toml"])

        captured_command: str | None = None

        def fake_run(command: str, _cwd: Path, _timeout_s: int) -> tuple[bool, str]:
            nonlocal captured_command
            captured_command = command
            return True, "ok"

        with patch("bernstein.core.quality.quality_gates._run_command", side_effect=fake_run):
            asyncio.run(runner.run_all(task, tmp_path))

        assert captured_command is not None
        assert captured_command == "safety check"

    def test_dep_audit_non_required_does_not_block(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        config = QualityGatesConfig(
            dep_audit=True,
            pipeline=[GatePipelineStep(name="dep_audit", required=False, condition="deps_changed")],
            cache_enabled=False,
        )
        runner = GateRunner(config, tmp_path)
        task = _make_task(owned_files=["pyproject.toml"])

        with patch(
            "bernstein.core.quality_gates._run_command",
            return_value=(False, "Found 1 vulnerability"),
        ):
            report = asyncio.run(runner.run_all(task, tmp_path))

        assert report.overall_pass
        result = next(r for r in report.results if r.name == "dep_audit")
        assert result.status == "fail"
        assert not result.blocked
