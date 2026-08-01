"""Tests for `bernstein doctor` CLI command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli


class TestDoctorCommand:
    """Tests for the doctor command."""

    def test_doctor_command_exists(self) -> None:
        """bernstein doctor command must be callable."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "self-diagnostics" in result.output.lower()

    def test_doctor_json_flag_exists(self) -> None:
        """bernstein doctor must support --json flag."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "--help"])
        assert "--json" in result.output

    def test_doctor_help_text_contains_key_checks(self) -> None:
        """bernstein doctor help must describe what it checks."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "--help"])
        # Should mention key components being checked
        assert result.exit_code == 0
        help_text = result.output.lower()
        # Help text should describe diagnostic functionality
        assert "diagnos" in help_text or "check" in help_text.lower()

    def test_doctor_runs_without_crash(self) -> None:
        """bernstein doctor must run without crashing even if checks fail."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        # Should not crash; exit code may be 0 or 1 depending on system state
        assert result.exit_code in (0, 1)
        # Should produce output with check results
        assert len(result.output) > 0

    def test_doctor_json_runs_without_crash(self) -> None:
        """bernstein doctor --json must run without crashing."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "--json"])
        # Should not crash; exit code may be 0 or 1 depending on system state
        assert result.exit_code in (0, 1)
        # Should produce some output
        assert len(result.output) > 0

    def test_doctor_reports_python_version(self) -> None:
        """bernstein doctor output must mention Python version."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        # Should mention Python
        assert "Python" in result.output or "version" in result.output.lower()

    def test_doctor_checks_adapters(self) -> None:
        """bernstein doctor must check for CLI adapters."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        # Should mention at least one adapter
        assert "claude" in result.output.lower() or "Adapter" in result.output

    def test_doctor_checks_environment(self) -> None:
        """bernstein doctor must check for auth status of adapters."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        # Should mention authentication checks (Auth: claude, Auth: codex, Auth: gemini)
        assert "Auth:" in result.output or "auth" in result.output.lower()

    def test_doctor_checks_port_availability(self) -> None:
        """bernstein doctor must check port 8052."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        # Should mention port check
        assert "8052" in result.output or "Port" in result.output

    def test_doctor_checks_sdd_workspace(self) -> None:
        """bernstein doctor must check for .sdd/ workspace."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        # Should mention .sdd
        assert ".sdd" in result.output

    def test_doctor_checks_ci_dependencies(self) -> None:
        """bernstein doctor must check CI tool dependencies."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        # Should mention at least one CI tool
        assert (
            "ruff" in result.output.lower()
            or "pytest" in result.output.lower()
            or "pyright" in result.output.lower()
            or "CI" in result.output
        )

    def test_doctor_checks_readiness(self) -> None:
        """bernstein doctor must report overall readiness."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        # Should have some assessment of readiness
        assert "ready" in result.output.lower() or "Status" in result.output or "Check" in result.output

    def test_doctor_fix_flag_exists(self) -> None:
        """bernstein doctor must support --fix flag."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "--help"])
        assert "--fix" in result.output

    def test_doctor_fix_runs_without_crash(self) -> None:
        """bernstein doctor --fix must run without crashing even if checks fail."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "--fix"])
        # Should not crash; exit code may be 0 or 1 depending on system state
        assert result.exit_code in (0, 1)
        # Should produce output with check results
        assert len(result.output) > 0

    def test_doctor_fix_hint_shown_on_failure(self) -> None:
        """bernstein doctor should suggest --fix when issues are found."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])
        if result.exit_code == 1:
            # When checks fail without --fix, hint should appear
            assert "--fix" in result.output


class TestDoctorJsonStdoutContract:
    """``--json`` must always leave a parseable payload on stdout.

    Machine consumers read stdout and parse it; a diagnostic probe that
    cannot answer has to land *in* the payload as a failed check. If it
    escapes instead, the command dies before serialising and the consumer
    gets an empty stream with no way to tell "everything is fine" from
    "the doctor never ran".
    """

    def test_json_payload_survives_a_probe_that_times_out(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A tool probe blowing its budget must not empty stdout."""
        monkeypatch.chdir(tmp_path)
        timeout = subprocess.TimeoutExpired(cmd=["uv", "run", "ruff", "--version"], timeout=10)
        runner = CliRunner()
        with patch("bernstein.core.quality.ci_fix.subprocess.run", side_effect=timeout):
            result = runner.invoke(cli, ["doctor", "--json"])

        payload = _parse_json_payload(result.output)
        assert isinstance(payload["checks"], list)
        assert payload["runtime"] in {"local", "codespace"}
        ci_rows = [c for c in payload["checks"] if str(c["name"]).startswith("CI tool: ")]
        assert ci_rows, "the CI tool probes must still be represented in the payload"
        assert all(row["ok"] is False for row in ci_rows)


def _parse_json_payload(output: str) -> dict[str, Any]:
    """Pull the single top-level JSON object out of the doctor's stdout."""
    start = output.find("{")
    assert start != -1, f"no JSON object in output:\n{output!r}"
    depth = 0
    for idx in range(start, len(output)):
        if output[idx] == "{":
            depth += 1
        elif output[idx] == "}":
            depth -= 1
            if depth == 0:
                payload = json.loads(output[start : idx + 1])
                assert isinstance(payload, dict)
                return payload
    raise AssertionError(f"unterminated JSON in output:\n{output!r}")
