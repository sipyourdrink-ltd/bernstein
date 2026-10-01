"""Tests for doctor unattended parity (#5441).

Probes enter through the spawner path; receipts byte-identical with and without a TTY.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bernstein.cli.commands import doctor_cmd
from bernstein.cli.doctor.adapter_checks import check_adapter_binary
from bernstein.cli.main import cli
from bernstein.core.agents.spawner import build_spawner_env
from bernstein.core.orchestration.preflight import preflight_checks


class TestSpawnerEnvConstruction:
    def test_build_spawner_env_extracts_clean_env(self) -> None:
        base = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/user",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "OPENAI_API_KEY": "sk-openai-test",
            "RANDOM_VAR": "ignore_me",
        }
        spawner_env = build_spawner_env(adapter_name="claude", base_env=base)
        assert spawner_env["PATH"] == "/usr/bin:/bin"
        assert spawner_env["HOME"] == "/home/user"
        assert spawner_env["ANTHROPIC_API_KEY"] == "sk-ant-test"
        # Spawner filters out unrelated vars
        assert "RANDOM_VAR" not in spawner_env


class TestDoctorUnattendedParity:
    def test_receipt_identical_interactive_vs_unattended(self, tmp_path: Path) -> None:
        """A fixture task produces byte-identical receipts interactive vs unattended (modulo timestamp)."""
        adapter = "claude"
        base_env = {
            "PATH": "/opt/bin",
            "ANTHROPIC_API_KEY": "sk-test",
        }
        spawner_env = build_spawner_env(base_env=base_env)

        def _mock_which(name: str, path: str | None = None) -> str | None:
            if name == adapter:
                return f"/opt/bin/{name}"
            return None

        with (
            patch("shutil.which", side_effect=_mock_which),
            patch.object(doctor_cmd, "_probe_adapter_version", return_value="1.0.0"),
        ):
            interactive_entries = doctor_cmd.collect_version_posture(env=base_env)
            unattended_entries = doctor_cmd.collect_version_posture(env=spawner_env)

        assert interactive_entries == unattended_entries

        interactive_receipt = doctor_cmd.build_version_posture_receipt(
            interactive_entries, generated_at="2026-01-01T00:00:00Z"
        )
        unattended_receipt = doctor_cmd.build_version_posture_receipt(
            unattended_entries, generated_at="2026-01-01T00:00:00Z"
        )

        assert interactive_receipt == unattended_receipt
        assert json.dumps(interactive_receipt, sort_keys=True) == json.dumps(unattended_receipt, sort_keys=True)

    def test_probe_fails_when_binary_in_interactive_path_only(self) -> None:
        """A probe that only passes interactively fails under unattended mode, naming failing probe."""
        interactive_path = "/interactive/bin"
        spawner_path = "/spawner/bin"

        def _mock_which(name: str, path: str | None = None) -> str | None:
            if path == interactive_path and name == "claude":
                return f"{interactive_path}/{name}"
            return None

        import asyncio

        with patch("shutil.which", side_effect=_mock_which):
            # Interactive check passes
            interactive_env = {"PATH": interactive_path}
            res_interactive = asyncio.run(check_adapter_binary("claude", "claude", env=interactive_env))
            assert res_interactive.status in ("ok", "warn")

            # Unattended check fails because spawner PATH doesn't have it
            unattended_env = {"PATH": spawner_path}
            res_unattended = asyncio.run(check_adapter_binary("claude", "claude", env=unattended_env))
            assert res_unattended.status == "fail"
            assert "adapter:claude" in res_unattended.name
            assert "not in PATH" in res_unattended.detail

    def test_doctor_unattended_flag_cli(self) -> None:
        """doctor --unattended executes without crash and accepts the flag."""
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "--unattended", "--json"])
        assert result.exit_code in (0, 1)
        assert len(result.output) > 0
        data = json.loads(result.output)
        assert "checks" in data

    def test_preflight_unattended_default_when_not_atty(self) -> None:
        """preflight_checks defaults to unattended when stdin is not a tty."""
        with (
            patch("sys.stdin.isatty", return_value=False),
            patch("bernstein.core.orchestration.preflight._check_port_free"),
            patch("shutil.which", return_value=None),
        ):
            # Binary not found in spawner env PATH -> should exit 1
            import pytest

            with pytest.raises(SystemExit) as exc_info:
                preflight_checks("claude", 8052)
            assert exc_info.value.code == 1
