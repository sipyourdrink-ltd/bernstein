"""Tests for multi-source plugin installation (plugin_installer.py)."""

from __future__ import annotations

import json
import stat
import sys
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.plugin_installer import (
    DirectorySource,
    FileSource,
    GitHubSource,
    GitSource,
    NpmSource,
    _extract_archive,
    install_plugin,
    update_plugin,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_zip(dest: Path, contents: dict[str, str]) -> Path:
    """Create a zip archive at *dest* containing *contents* mapping."""
    with zipfile.ZipFile(dest, "w") as zf:
        for name, data in contents.items():
            zf.writestr(name, data)
    return dest


def _make_zip_with_mode(dest: Path, name: str, data: str, mode: int) -> Path:
    """Create a zip archive containing one entry with a Unix mode."""
    info = zipfile.ZipInfo(filename=name)
    info.external_attr = mode << 16
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr(info, data)
    return dest


def _make_tgz(dest: Path, contents: dict[str, str]) -> Path:
    """Create a .tar.gz archive at *dest*."""
    import io

    with tarfile.open(dest, "w:gz") as tf:
        for name, data in contents.items():
            encoded = data.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(encoded)
            tf.addfile(info, io.BytesIO(encoded))
    return dest


def _make_github_api_response(asset_name: str, download_url: str) -> bytes:
    return json.dumps(
        {
            "tag_name": "v1.0.0",
            "assets": [{"name": asset_name, "browser_download_url": download_url}],
        }
    ).encode()


# ---------------------------------------------------------------------------
# PluginSource union - structural tests
# ---------------------------------------------------------------------------


class TestPluginSourceVariants:
    def test_github_source_kind(self) -> None:
        src = GitHubSource(repo="acme/my-plugin")
        assert src.kind == "github"

    def test_git_source_kind(self) -> None:
        src = GitSource(url="https://github.com/acme/plugin.git")
        assert src.kind == "git"

    def test_npm_source_kind(self) -> None:
        src = NpmSource(package="@acme/plugin")
        assert src.kind == "npm"

    def test_file_source_kind(self) -> None:
        src = FileSource(path="/tmp/plugin.zip")
        assert src.kind == "file"

    def test_directory_source_kind(self) -> None:
        src = DirectorySource(path="/tmp/plugin")
        assert src.kind == "directory"

    def test_github_source_defaults(self) -> None:
        src = GitHubSource(repo="acme/my-plugin")
        assert src.tag == "latest"
        assert src.asset is None

    def test_git_source_default_ref(self) -> None:
        src = GitSource(url="https://example.com/repo.git")
        assert src.ref == "HEAD"

    def test_npm_source_default_version(self) -> None:
        src = NpmSource(package="my-plugin")
        assert src.version == "latest"


# ---------------------------------------------------------------------------
# _extract_archive
# ---------------------------------------------------------------------------


class TestExtractArchive:
    def test_extract_zip(self, tmp_path: Path) -> None:
        archive = _make_zip(tmp_path / "plugin.zip", {"hello.txt": "world"})
        dest = tmp_path / "out"
        _extract_archive(archive, dest)
        assert (dest / "hello.txt").read_text() == "world"

    def test_extract_zip_rejects_sibling_prefix_escape(self, tmp_path: Path) -> None:
        archive = _make_zip(tmp_path / "plugin.zip", {"../out_evil/pwned.txt": "owned"})
        dest = tmp_path / "out"

        with pytest.raises(ValueError, match="Zip entry would escape target directory"):
            _extract_archive(archive, dest)

        assert not (tmp_path / "out_evil" / "pwned.txt").exists()

    def test_extract_zip_rejects_backslash_traversal_entry(self, tmp_path: Path) -> None:
        """A '..\\' entry must be refused on every host, not just Windows.

        A bare ``(dest / name).resolve()`` treats a backslash as an ordinary
        filename character on POSIX, so a zip built to attack a Windows
        target extracted inertly-but-not-safely on POSIX before this site
        went through the shared containment helper, which splits on both
        separators regardless of platform.
        """
        archive = _make_zip(tmp_path / "plugin.zip", {"..\\out_evil\\pwned.txt": "owned"})
        dest = tmp_path / "out"

        with pytest.raises(ValueError, match="Zip entry would escape target directory"):
            _extract_archive(archive, dest)

        assert not (tmp_path / "out_evil" / "pwned.txt").exists()

    def test_extract_zip_preserves_unix_file_mode(self, tmp_path: Path) -> None:
        """Zip extraction preserves the Unix mode on POSIX; extracts on Windows.

        Runs on every OS. POSIX asserts the 0o755 mode is preserved; Windows
        does not carry Unix mode bits, so it asserts the entry was extracted
        with the expected content instead of skipping the lane.
        """
        archive = _make_zip_with_mode(tmp_path / "plugin.zip", "bin/plugin", "#!/bin/sh\n", 0o755)
        dest = tmp_path / "out"

        _extract_archive(archive, dest)

        extracted = dest / "bin" / "plugin"
        if sys.platform == "win32":
            assert extracted.read_text() == "#!/bin/sh\n"
        else:
            assert stat.S_IMODE(extracted.stat().st_mode) == 0o755

    def test_extract_tar_gz(self, tmp_path: Path) -> None:
        archive = _make_tgz(tmp_path / "plugin.tar.gz", {"readme.md": "# Plugin"})
        dest = tmp_path / "out"
        _extract_archive(archive, dest)
        assert (dest / "readme.md").read_text() == "# Plugin"

    def test_extract_tgz_alias(self, tmp_path: Path) -> None:
        archive = _make_tgz(tmp_path / "plugin.tgz", {"main.py": "pass"})
        dest = tmp_path / "out"
        _extract_archive(archive, dest)
        assert (dest / "main.py").exists()

    def test_unsupported_format_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "plugin.tar.bz2"
        bad.write_bytes(b"not real")
        dest = tmp_path / "out"
        with pytest.raises(ValueError, match="Unsupported archive format"):
            _extract_archive(bad, dest)


# ---------------------------------------------------------------------------
# FileSource
# ---------------------------------------------------------------------------


class TestFileSourceInstall:
    def test_installs_zip(self, tmp_path: Path) -> None:
        archive = _make_zip(tmp_path / "myplugin.zip", {"plugin.py": "# plugin"})
        install_dir = tmp_path / "plugins"
        result = install_plugin(FileSource(path=str(archive)), install_dir)
        assert result.success
        assert result.source_kind == "file"
        assert result.install_path is not None
        assert (result.install_path / "plugin.py").exists()

    def test_installs_tar_gz(self, tmp_path: Path) -> None:
        archive = _make_tgz(tmp_path / "myplugin.tar.gz", {"plugin.py": "# plugin"})
        install_dir = tmp_path / "plugins"
        result = install_plugin(FileSource(path=str(archive)), install_dir)
        assert result.success

    def test_missing_file_returns_failure(self, tmp_path: Path) -> None:
        result = install_plugin(FileSource(path="/nonexistent/plugin.zip"), tmp_path / "plugins")
        assert not result.success
        assert "not found" in result.error.lower()

    def test_empty_path_returns_failure(self, tmp_path: Path) -> None:
        result = install_plugin(FileSource(path=""), tmp_path / "plugins")
        assert not result.success
        assert "path is required" in result.error


# ---------------------------------------------------------------------------
# DirectorySource
# ---------------------------------------------------------------------------


class TestDirectorySourceInstall:
    def test_copies_directory(self, tmp_path: Path) -> None:
        plugin_src = tmp_path / "my_plugin"
        plugin_src.mkdir()
        (plugin_src / "plugin.py").write_text("# plugin")

        install_dir = tmp_path / "plugins"
        result = install_plugin(DirectorySource(path=str(plugin_src)), install_dir)

        assert result.success
        assert result.source_kind == "directory"
        assert result.install_path is not None
        assert (result.install_path / "plugin.py").exists()

    def test_replaces_existing_installation(self, tmp_path: Path) -> None:
        plugin_src = tmp_path / "my_plugin"
        plugin_src.mkdir()
        (plugin_src / "new.py").write_text("# new")

        install_dir = tmp_path / "plugins"
        # First install with stale file
        first_install = install_dir / "my_plugin"
        first_install.mkdir(parents=True)
        (first_install / "stale.py").write_text("# stale")

        result = install_plugin(DirectorySource(path=str(plugin_src)), install_dir)
        assert result.success
        assert not (result.install_path / "stale.py").exists()  # type: ignore[operator]
        assert (result.install_path / "new.py").exists()  # type: ignore[operator]

    def test_missing_directory_returns_failure(self, tmp_path: Path) -> None:
        result = install_plugin(DirectorySource(path="/nonexistent/plugin"), tmp_path / "plugins")
        assert not result.success
        assert "not found" in result.error.lower()

    def test_empty_path_returns_failure(self, tmp_path: Path) -> None:
        result = install_plugin(DirectorySource(path=""), tmp_path / "plugins")
        assert not result.success


# ---------------------------------------------------------------------------
# GitHubSource - mocked network
# ---------------------------------------------------------------------------


class TestGitHubSourceInstall:
    def test_installs_from_github_release(self, tmp_path: Path) -> None:
        """Plugin installs from a GitHub release URL (mocked network)."""
        archive = _make_zip(tmp_path / "plugin.zip", {"plugin.py": "# github plugin"})
        download_url = "https://github.com/acme/my-plugin/releases/download/v1.0.0/plugin.zip"
        api_response = _make_github_api_response("plugin.zip", download_url)

        def fake_urlopen(req: object, timeout: int = 30) -> MagicMock:
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.read.return_value = api_response
            return ctx

        def fake_urlretrieve(url: str, dest: str) -> None:
            import shutil

            shutil.copy(str(archive), dest)

        with (
            patch("bernstein.core.plugins_core.plugin_installer.urllib.request.urlopen", side_effect=fake_urlopen),
            patch(
                "bernstein.core.plugins_core.plugin_installer.urllib.request.urlretrieve", side_effect=fake_urlretrieve
            ),
        ):
            install_dir = tmp_path / "plugins"
            result = install_plugin(
                GitHubSource(repo="acme/my-plugin", tag="v1.0.0"),
                install_dir,
            )

        assert result.success, result.error
        assert result.source_kind == "github"
        assert result.install_path is not None
        assert (result.install_path / "plugin.py").exists()

    def test_empty_repo_returns_failure(self, tmp_path: Path) -> None:
        result = install_plugin(GitHubSource(repo=""), tmp_path / "plugins")
        assert not result.success
        assert "repo is required" in result.error

    def test_no_matching_asset_returns_failure(self, tmp_path: Path) -> None:
        api_response = json.dumps(
            {
                "tag_name": "v1.0.0",
                "assets": [{"name": "plugin.exe", "browser_download_url": "http://x/plugin.exe"}],
            }
        ).encode()

        def fake_urlopen(req: object, timeout: int = 30) -> MagicMock:
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.read.return_value = api_response
            return ctx

        with patch("bernstein.core.plugins_core.plugin_installer.urllib.request.urlopen", side_effect=fake_urlopen):
            result = install_plugin(GitHubSource(repo="acme/plugin"), tmp_path / "plugins")

        assert not result.success
        assert "No .zip or .tar.gz" in result.error

    def test_specific_asset_selected(self, tmp_path: Path) -> None:
        archive = _make_zip(tmp_path / "specific.zip", {"specific.py": "# specific"})
        download_url = "https://github.com/acme/plugin/releases/download/v1.0.0/specific.zip"
        api_response = json.dumps(
            {
                "tag_name": "v1.0.0",
                "assets": [
                    {"name": "generic.zip", "browser_download_url": "http://x/generic.zip"},
                    {"name": "specific.zip", "browser_download_url": download_url},
                ],
            }
        ).encode()

        def fake_urlopen(req: object, timeout: int = 30) -> MagicMock:
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.read.return_value = api_response
            return ctx

        def fake_urlretrieve(url: str, dest: str) -> None:
            import shutil

            shutil.copy(str(archive), dest)

        with (
            patch("bernstein.core.plugins_core.plugin_installer.urllib.request.urlopen", side_effect=fake_urlopen),
            patch(
                "bernstein.core.plugins_core.plugin_installer.urllib.request.urlretrieve", side_effect=fake_urlretrieve
            ),
        ):
            result = install_plugin(
                GitHubSource(repo="acme/plugin", asset="specific.zip"),
                tmp_path / "plugins",
            )

        assert result.success
        assert (result.install_path / "specific.py").exists()  # type: ignore[operator]


# ---------------------------------------------------------------------------
# GitSource - mocked subprocess
# ---------------------------------------------------------------------------


class TestGitSourceInstall:
    def test_installs_from_git_url(self, tmp_path: Path) -> None:
        plugin_src = tmp_path / "repo_src"
        plugin_src.mkdir()
        (plugin_src / "plugin.py").write_text("# git plugin")

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            if "clone" in cmd:
                clone_dest = cmd[-1]
                import shutil

                shutil.copytree(str(plugin_src), clone_dest)
            return MagicMock(returncode=0, stderr=b"")

        with patch("bernstein.core.plugins_core.plugin_installer.subprocess.run", side_effect=fake_run):
            result = install_plugin(
                GitSource(url="https://github.com/acme/plugin.git"),
                tmp_path / "plugins",
            )

        assert result.success, result.error
        assert result.source_kind == "git"
        assert (result.install_path / "plugin.py").exists()  # type: ignore[operator]

    def test_empty_url_returns_failure(self, tmp_path: Path) -> None:
        result = install_plugin(GitSource(url=""), tmp_path / "plugins")
        assert not result.success
        assert "url is required" in result.error

    def test_clone_failure_returns_failure(self, tmp_path: Path) -> None:
        import subprocess

        def fake_run(cmd: list[str], **kwargs: object) -> None:
            raise subprocess.CalledProcessError(1, cmd, stderr=b"fatal: repo not found")

        with patch("bernstein.core.plugins_core.plugin_installer.subprocess.run", side_effect=fake_run):
            result = install_plugin(
                GitSource(url="https://github.com/acme/nonexistent.git"),
                tmp_path / "plugins",
            )

        assert not result.success
        assert "fatal" in result.error.lower() or "repo not found" in result.error.lower()


# ---------------------------------------------------------------------------
# update_plugin delegates to install_plugin
# ---------------------------------------------------------------------------


class TestUpdatePlugin:
    def test_update_replaces_directory_plugin(self, tmp_path: Path) -> None:
        plugin_src = tmp_path / "my_plugin"
        plugin_src.mkdir()
        (plugin_src / "v2.py").write_text("# v2")

        install_dir = tmp_path / "plugins"
        result = update_plugin(DirectorySource(path=str(plugin_src)), install_dir)

        assert result.success
        assert (result.install_path / "v2.py").exists()  # type: ignore[operator]
