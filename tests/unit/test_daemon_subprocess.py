"""subprocess-mocked tests for systemctl / launchctl wrappers (op-004)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from bernstein.core.daemon import launchd as launchd_mod
from bernstein.core.daemon import systemd as systemd_mod


def _fake_completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["stub"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_systemctl_start_invokes_user_flag() -> None:
    runner = MagicMock(return_value=_fake_completed())
    systemd_mod.start(unit_name="bernstein.service", scope="user", runner=runner)
    args, _ = runner.call_args
    assert args[0] == ["systemctl", "--user", "start", "bernstein.service"]


def test_systemctl_start_system_scope_omits_user_flag() -> None:
    runner = MagicMock(return_value=_fake_completed())
    systemd_mod.start(unit_name="bernstein.service", scope="system", runner=runner)
    args, _ = runner.call_args
    assert args[0] == ["systemctl", "start", "bernstein.service"]


def test_systemd_status_parses_running() -> None:
    runner = MagicMock(
        return_value=_fake_completed(
            stdout=(
                "bernstein.service - Bernstein user daemon\n"
                "     Loaded: loaded\n"
                "     Active: active (running) since Mon 2026-04-20 10:00:00 UTC; 5min ago\n"
            )
        )
    )
    assert systemd_mod.status(runner=runner) == "Running"


def test_systemd_status_parses_stopped() -> None:
    runner = MagicMock(
        return_value=_fake_completed(
            stdout=("bernstein.service - Bernstein user daemon\n     Loaded: loaded\n     Active: inactive (dead)\n")
        )
    )
    assert systemd_mod.status(runner=runner) == "Stopped"


def test_systemd_status_parses_failed() -> None:
    runner = MagicMock(
        return_value=_fake_completed(
            stdout=(
                "bernstein.service - Bernstein user daemon\n"
                "     Loaded: loaded\n"
                "     Active: failed (Result: exit-code)\n"
            )
        )
    )
    assert systemd_mod.status(runner=runner) == "Failed"


def test_launchctl_load_invokes_launchctl(tmp_path: Path) -> None:
    runner = MagicMock(return_value=_fake_completed())
    plist = tmp_path / "com.bernstein.daemon.plist"
    plist.write_text("<plist/>", encoding="utf-8")
    launchd_mod.load(plist, runner=runner)
    args, _ = runner.call_args
    assert args[0] == ["launchctl", "load", str(plist)]


def test_launchctl_status_parses_running() -> None:
    stdout = '{\n\t"Label" = "com.bernstein.daemon";\n\t"PID" = 4242;\n\t"LastExitStatus" = 0;\n};\n'
    runner = MagicMock(return_value=_fake_completed(stdout=stdout))
    assert launchd_mod.status(runner=runner) == "Running"


def test_launchctl_status_parses_failed() -> None:
    stdout = '{\n\t"Label" = "com.bernstein.daemon";\n\t"LastExitStatus" = 2;\n};\n'
    runner = MagicMock(return_value=_fake_completed(stdout=stdout))
    assert launchd_mod.status(runner=runner) == "Failed"


def test_launchctl_status_unknown_label() -> None:
    runner = MagicMock(return_value=_fake_completed(stdout="", returncode=1))
    assert launchd_mod.status(runner=runner) == "Unknown"
