"""Tests for CLI-006: comprehensive doctor health checks."""

from __future__ import annotations

from click.testing import CliRunner

from bernstein.cli.doctor_cmd import (
    check_config_valid,
    check_disk_space,
    check_git_installed,
    check_python_version,
    check_sdd_workspace,
    run_all_checks,
)
from bernstein.cli.main import cli


class TestDoctorCmd:
    """Tests for the doctor command module."""

    def test_doctor_command_exists(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "--help"])
        assert result.exit_code == 0

    def test_doctor_runs_without_crash(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        assert result.exit_code in (0, 1)
        assert len(result.output) > 0

    def test_check_python_version_passes(self) -> None:
        result = check_python_version()
        assert result["name"] == "Python version"
        # We are running on Python 3.12+
        assert result["status"] == "PASS"

    def test_check_disk_space(self) -> None:
        result = check_disk_space()
        assert result["name"] == "Disk space"
        assert result["status"] in ("PASS", "WARN")
        assert "free" in result["detail"] or "could not check" in result["detail"]

    def test_check_git_installed(self) -> None:
        result = check_git_installed()
        assert result["name"] == "Git"
        # Git should be installed in CI / dev
        assert result["status"] == "PASS"
        assert "git version" in result["detail"]

    def test_check_config_valid_no_file(self) -> None:
        """When no bernstein.yaml exists, should return WARN."""
        import os
        import tempfile

        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as td:
                os.chdir(td)
                result = check_config_valid()
                assert result["status"] == "WARN"
                assert "not found" in result["detail"]
        finally:
            os.chdir(old_cwd)

    def test_check_sdd_workspace_missing(self) -> None:
        """When .sdd/ does not exist, should return WARN."""
        import os
        import tempfile

        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as td:
                os.chdir(td)
                result = check_sdd_workspace()
                assert result["status"] == "WARN"
                assert "missing" in result["detail"]
        finally:
            os.chdir(old_cwd)

    def test_run_all_checks(self) -> None:
        checks = run_all_checks()
        assert len(checks) > 5
        names = [c["name"] for c in checks]
        assert "Python version" in names
        assert "Disk space" in names
        assert "Git" in names
