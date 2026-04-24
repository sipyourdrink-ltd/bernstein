"""CLI-level tests for `bernstein daemon` (op-004)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bernstein.cli.commands.daemon_cmd import daemon_group


def test_cli_install_on_systemd_user(tmp_path: Path) -> None:
    runner = CliRunner()
    unit_dir = tmp_path / "units"
    with (
        patch("bernstein.cli.commands.daemon_cmd.detect_init_system", return_value="systemd"),
        patch(
            "bernstein.cli.commands.daemon_cmd.systemd_mod.install_systemd_user_unit",
            return_value=unit_dir / "bernstein.service",
        ) as fake_install,
    ):
        result = runner.invoke(
            daemon_group,
            [
                "install",
                "--command",
                "bernstein dashboard --headless",
                "--env",
                "BERNSTEIN_TELEGRAM_BOT_TOKEN=secret",
                "--env",
                "FOO=bar",
            ],
        )
    assert result.exit_code == 0, result.output
    fake_install.assert_called_once()
    _, kwargs = fake_install.call_args
    assert kwargs["env"] == {"BERNSTEIN_TELEGRAM_BOT_TOKEN": "secret", "FOO": "bar"}
    assert kwargs["force"] is False


def test_cli_install_macos_system_flag_rejected() -> None:
    runner = CliRunner()
    with patch("bernstein.cli.commands.daemon_cmd.detect_init_system", return_value="launchd"):
        result = runner.invoke(daemon_group, ["install", "--system"])
    assert result.exit_code != 0
    assert "--system is not supported on macOS" in result.output


def test_cli_install_rejects_both_scopes() -> None:
    runner = CliRunner()
    with patch("bernstein.cli.commands.daemon_cmd.detect_init_system", return_value="systemd"):
        result = runner.invoke(daemon_group, ["install", "--user", "--system"])
    assert result.exit_code != 0
    assert "Cannot combine --user and --system" in result.output


def test_cli_env_rejects_bad_pair() -> None:
    runner = CliRunner()
    with patch("bernstein.cli.commands.daemon_cmd.detect_init_system", return_value="systemd"):
        result = runner.invoke(daemon_group, ["install", "--env", "NO_EQUALS_SIGN"])
    assert result.exit_code != 0
    assert "KEY=VAL" in result.output


def test_cli_uninstall_idempotent() -> None:
    runner = CliRunner()
    with (
        patch("bernstein.cli.commands.daemon_cmd.detect_init_system", return_value="systemd"),
        patch("bernstein.cli.commands.daemon_cmd.systemd_mod.uninstall", return_value=False),
    ):
        result = runner.invoke(daemon_group, ["uninstall"])
    assert result.exit_code == 0
    assert "No daemon unit installed" in result.output
