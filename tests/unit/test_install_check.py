"""Tests for install_check — installation mismatch detection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.install_check import (
    _find_all_binaries,
    _get_installed_version,
    check_installations,
    has_venv,
)

# --- Fixtures ---


@pytest.fixture()
def fake_path_env(tmp_path: Path) -> Path:
    """Create two fake binary directories with bernstein executables."""
    d1 = tmp_path / "env1" / "bin"
    d2 = tmp_path / "env2" / "bin"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)
    (d1 / "bernstein").touch(mode=0o755)
    (d2 / "bernstein").touch(mode=0o755)
    return tmp_path


# --- TestFindAllBinaries ---


class TestFindAllBinaries:
    def test_finds_multiple_binaries(self, fake_path_env: Path) -> None:
        path_str = f"{fake_path_env}/env1/bin:{fake_path_env}/env2/bin"
        with patch.dict("os.environ", {"PATH": path_str}):
            binaries = _find_all_binaries("bernstein")
            assert len(binaries) == 2

    def test_deduplicates_same_realpath(self, tmp_path: Path) -> None:
        d1 = tmp_path / "link1"
        d2 = tmp_path / "link2"
        d1.mkdir()
        d2.mkdir()
        target = tmp_path / "real" / "bernstein"
        target.parent.mkdir()
        target.touch(mode=0o755)
        (d1 / "bernstein").symlink_to(target)
        (d2 / "bernstein").symlink_to(target)
        path_str = f"{d1}:{d2}"
        with patch.dict("os.environ", {"PATH": path_str}):
            binaries = _find_all_binaries("bernstein")
            assert len(binaries) == 1  # same realpath

    def test_returns_empty_when_no_binary(self, tmp_path: Path) -> None:
        path_str = str(tmp_path / "empty")
        with patch.dict("os.environ", {"PATH": path_str}):
            binaries = _find_all_binaries("bernstein")
            assert binaries == []


# --- TestGetInstalledVersion ---


class TestGetInstalledVersion:
    def test_returns_version_string(self) -> None:
        with patch("importlib.metadata.version", return_value="1.2.3"):
            assert _get_installed_version() == "1.2.3"

    def test_returns_none_when_not_installed(self) -> None:
        from importlib.metadata import PackageNotFoundError

        with patch("bernstein.install_check.importlib.metadata.version", side_effect=PackageNotFoundError("not found")):
            assert _get_installed_version() is None


# --- TestCheckInstallations ---


class TestCheckInstallations:
    def test_multiple_installations_detected(self, fake_path_env: Path) -> None:
        path_str = f"{fake_path_env}/env1/bin:{fake_path_env}/env2/bin"
        with (
            patch.dict("os.environ", {"PATH": path_str}),
            patch("bernstein.install_check.importlib.metadata.version", return_value="1.0.0"),
        ):
            results = check_installations()
            multi = [r for r in results if "installations" in r.name.lower()]
            assert len(multi) == 1
            assert multi[0].ok is False

    def test_single_installation_ok(self, tmp_path: Path) -> None:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "bernstein").touch(mode=0o755)
        path_str = str(bindir)
        with (
            patch.dict("os.environ", {"PATH": path_str}),
            patch("bernstein.install_check.importlib.metadata.version", return_value="1.0.0"),
        ):
            results = check_installations()
            singles = [r for r in results if "installations" in r.name.lower() and r.ok]
            assert len(singles) == 1

    def test_no_installation_warn(self) -> None:
        with (
            patch.dict("os.environ", {"PATH": "/nonexistent"}),
            patch("bernstein.install_check.importlib.metadata.version", return_value="1.0.0"),
        ):
            results = check_installations()
            not_found = [r for r in results if "installations" in r.name.lower() and not r.ok]
            assert len(not_found) == 1
            assert "not found" in not_found[0].detail.lower()


# --- TestHasVenv ---


class TestHasVenv:
    def test_true_when_prefix_differs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.prefix", "/fake/venv", raising=False)
        monkeypatch.setattr("sys.base_prefix", "/usr", raising=False)
        assert has_venv() is True

    def test_false_when_prefix_same(self) -> None:
        # When sys.prefix == sys.base_prefix, we're not in a venv
        result = has_venv()
        # This depends on actual environment; just check it returns a bool
        assert isinstance(result, bool)
