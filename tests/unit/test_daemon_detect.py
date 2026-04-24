"""Platform detection tests for the daemon installer (op-004)."""

from __future__ import annotations

from unittest.mock import patch

from bernstein.core.daemon.detect import detect_init_system


def test_detect_returns_launchd_on_darwin() -> None:
    with patch("bernstein.core.daemon.detect.sys.platform", "darwin"):
        assert detect_init_system() == "launchd"


def test_detect_returns_systemd_on_linux_with_systemctl() -> None:
    with (
        patch("bernstein.core.daemon.detect.sys.platform", "linux"),
        patch("bernstein.core.daemon.detect.shutil.which", return_value="/usr/bin/systemctl"),
    ):
        assert detect_init_system() == "systemd"


def test_detect_returns_unsupported_on_linux_without_systemctl() -> None:
    with (
        patch("bernstein.core.daemon.detect.sys.platform", "linux"),
        patch("bernstein.core.daemon.detect.shutil.which", return_value=None),
    ):
        assert detect_init_system() == "unsupported"


def test_detect_returns_unsupported_on_windows() -> None:
    with patch("bernstein.core.daemon.detect.sys.platform", "win32"):
        assert detect_init_system() == "unsupported"
