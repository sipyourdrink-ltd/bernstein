"""Tests for spawn dry-run mode (AGENT-017)."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.spawn_dry_run import (
    DryRunReport,
    SpawnDryRunValidator,
    ValidationCheck,
)


class TestValidationCheck:
    def test_check_fields(self) -> None:
        check = ValidationCheck(name="test", passed=True, detail="ok")
        assert check.name == "test"
        assert check.passed
        assert check.severity == "error"


class TestDryRunReport:
    def test_empty_report_passes(self) -> None:
        report = DryRunReport()
        assert report.passed

    def test_report_with_error_fails(self) -> None:
        report = DryRunReport(
            checks=[
                ValidationCheck(name="test", passed=False, detail="bad", severity="error"),
            ]
        )
        assert not report.passed
        assert len(report.errors) == 1

    def test_report_with_warning_still_passes(self) -> None:
        report = DryRunReport(
            checks=[
                ValidationCheck(name="test", passed=False, detail="meh", severity="warning"),
            ]
        )
        assert report.passed
        assert len(report.warnings) == 1

    def test_summary_format(self) -> None:
        report = DryRunReport(
            adapter_name="mock",
            checks=[
                ValidationCheck(name="c1", passed=True),
                ValidationCheck(name="c2", passed=False, detail="nope"),
            ],
        )
        summary = report.summary()
        assert "mock" in summary
        assert "1/2" in summary


class TestSpawnDryRunValidator:
    def test_validate_mock_adapter(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        validator = SpawnDryRunValidator(repo_root=tmp_path)
        report = validator.validate(
            [{"title": "Test task", "description": "Do something"}],
            adapter_name="mock",
            model="mock-model",
        )
        # mock adapter should pass all checks
        assert report.passed
        assert report.would_spawn >= 1

    def test_validate_empty_tasks(self, tmp_path: Path) -> None:
        validator = SpawnDryRunValidator(repo_root=tmp_path)
        report = validator.validate([], adapter_name="mock")
        # Empty tasks is a warning, not an error
        task_check = next(c for c in report.checks if c.name == "tasks")
        assert not task_check.passed
        assert task_check.severity == "warning"

    def test_validate_invalid_task(self, tmp_path: Path) -> None:
        validator = SpawnDryRunValidator(repo_root=tmp_path)
        report = validator.validate([{}], adapter_name="mock")
        task_check = next(c for c in report.checks if c.name == "tasks")
        assert not task_check.passed

    def test_validate_unknown_adapter(self, tmp_path: Path) -> None:
        validator = SpawnDryRunValidator(repo_root=tmp_path)
        report = validator.validate(
            [{"title": "task"}],
            adapter_name="nonexistent-adapter-xyz",
        )
        adapter_check = next(c for c in report.checks if c.name == "adapter_registered")
        assert not adapter_check.passed

    def test_validate_no_model(self, tmp_path: Path) -> None:
        validator = SpawnDryRunValidator(repo_root=tmp_path)
        report = validator.validate(
            [{"title": "task"}],
            adapter_name="mock",
            model="",
        )
        model_check = next(c for c in report.checks if c.name == "model_valid")
        assert not model_check.passed

    def test_validate_mcp_config(self, tmp_path: Path) -> None:
        validator = SpawnDryRunValidator(repo_root=tmp_path)
        report = validator.validate(
            [{"title": "task"}],
            adapter_name="mock",
            mcp_config={"mcpServers": {"my-server": {}}},
        )
        mcp_check = next(c for c in report.checks if c.name == "mcp_config")
        assert mcp_check.passed

    def test_validate_invalid_mcp_config(self, tmp_path: Path) -> None:
        validator = SpawnDryRunValidator(repo_root=tmp_path)
        report = validator.validate(
            [{"title": "task"}],
            adapter_name="mock",
            mcp_config={"mcpServers": "not a dict"},  # type: ignore[dict-item]
        )
        mcp_check = next(c for c in report.checks if c.name == "mcp_config")
        assert not mcp_check.passed

    def test_validate_repo_root_nonexistent(self) -> None:
        validator = SpawnDryRunValidator(repo_root=Path("/nonexistent/path"))
        report = validator.validate([{"title": "task"}], adapter_name="mock")
        root_check = next(c for c in report.checks if c.name == "repo_root")
        assert not root_check.passed

    def test_validate_disk_space(self, tmp_path: Path) -> None:
        validator = SpawnDryRunValidator(repo_root=tmp_path)
        report = validator.validate([{"title": "task"}], adapter_name="mock")
        disk_check = next(c for c in report.checks if c.name == "disk_space")
        # Should pass (we have more than 1GB on any dev machine)
        assert disk_check.passed

    def test_summary_on_passing_report(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        validator = SpawnDryRunValidator(repo_root=tmp_path)
        report = validator.validate(
            [{"title": "task"}],
            adapter_name="mock",
        )
        summary = report.summary()
        assert "Would spawn" in summary
