"""Install/uninstall/force/idempotency tests for the daemon installer (op-004)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.daemon import launchd as launchd_mod
from bernstein.core.daemon import systemd as systemd_mod
from bernstein.core.daemon.errors import UnitExistsError


def test_install_systemd_user_unit_writes_file(tmp_path: Path) -> None:
    path = systemd_mod.install_systemd_user_unit(
        command="bernstein dashboard --headless",
        unit_dir=tmp_path,
        env={"A": "1"},
        workdir="/srv/bernstein",
        path_env="/bin",
    )
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "ExecStart=bernstein dashboard --headless" in text
    assert 'Environment="A=1"' in text


def test_install_systemd_refuses_existing_without_force(tmp_path: Path) -> None:
    systemd_mod.install_systemd_user_unit(
        command="bernstein dashboard --headless",
        unit_dir=tmp_path,
        workdir="/srv/bernstein",
        path_env="/bin",
    )
    with pytest.raises(UnitExistsError):
        systemd_mod.install_systemd_user_unit(
            command="bernstein dashboard --headless",
            unit_dir=tmp_path,
            workdir="/srv/bernstein",
            path_env="/bin",
        )


def test_install_systemd_overwrites_with_force(tmp_path: Path) -> None:
    systemd_mod.install_systemd_user_unit(
        command="bernstein dashboard --headless",
        unit_dir=tmp_path,
        workdir="/srv/bernstein",
        path_env="/bin",
    )
    path = systemd_mod.install_systemd_user_unit(
        command="bernstein dashboard --headless --debug",
        unit_dir=tmp_path,
        workdir="/srv/bernstein",
        path_env="/bin",
        force=True,
    )
    text = path.read_text(encoding="utf-8")
    assert "--debug" in text


def test_uninstall_systemd_is_idempotent(tmp_path: Path) -> None:
    # First call: unit never existed.
    assert systemd_mod.uninstall(unit_dir=tmp_path) is False
    # Install then uninstall twice.
    systemd_mod.install_systemd_user_unit(
        command="bernstein dashboard --headless",
        unit_dir=tmp_path,
        workdir="/srv/bernstein",
        path_env="/bin",
    )
    assert systemd_mod.uninstall(unit_dir=tmp_path) is True
    assert systemd_mod.uninstall(unit_dir=tmp_path) is False


def test_install_launchd_plist_writes_file(tmp_path: Path) -> None:
    path = launchd_mod.install_launchd_plist(
        command="bernstein dashboard --headless",
        plist_dir=tmp_path,
        env={"TOKEN": "abc"},
        workdir="/srv/bernstein",
        path_env="/bin",
    )
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "<string>bernstein</string>" in text
    assert "<key>TOKEN</key>" in text


def test_uninstall_launchd_is_idempotent(tmp_path: Path) -> None:
    assert launchd_mod.uninstall(plist_dir=tmp_path) is False
    launchd_mod.install_launchd_plist(
        command="bernstein dashboard --headless",
        plist_dir=tmp_path,
        workdir="/srv/bernstein",
        path_env="/bin",
    )
    assert launchd_mod.uninstall(plist_dir=tmp_path) is True
    assert launchd_mod.uninstall(plist_dir=tmp_path) is False
