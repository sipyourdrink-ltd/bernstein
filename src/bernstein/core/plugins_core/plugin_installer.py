"""Multi-source plugin installation — GitHub, git, npm, file, and directory.

Provides a :class:`PluginSource` union type with five concrete source
variants and a :func:`install_plugin` dispatcher that routes each variant
to its dedicated installer.

Each installer downloads or copies the plugin into *install_dir* and returns
a :class:`PluginInstallResult` describing the outcome.

Usage::

    result = install_plugin(
        GitHubSource(repo="acme/my-plugin", tag="v1.2.0"),
        install_dir=Path("/opt/bernstein/plugins"),
    )
    if result.success:
        print(f"Installed plugin to {result.install_path}")
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GitHub API constants
# ---------------------------------------------------------------------------

_GITHUB_API_RELEASE_URL = "https://api.github.com/repos/{repo}/releases/{tag}"
_GITHUB_API_LATEST = "latest"
_DEFAULT_ASSET_SUFFIXES = (".zip", ".tar.gz", ".tgz")

# ---------------------------------------------------------------------------
# PluginSource union — 5 source variants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitHubSource:
    """Install a plugin from a GitHub release asset.

    The plugin is resolved from the ``owner/repo`` shorthand and the specified
    release tag.  The first matching ``.zip`` or ``.tar.gz`` release asset is
    downloaded and extracted into *install_dir*.

    Attributes:
        repo: GitHub repository in ``owner/repo`` format (e.g. ``acme/my-plugin``).
        tag: Release tag to install (e.g. ``v1.2.0``).  Use ``"latest"`` to
            resolve the most recent release automatically.
        asset: Explicit asset filename to download.  When ``None``, the first
            ``.zip`` or ``.tar.gz`` asset is selected automatically.
    """

    kind: Literal["github"] = field(default="github", init=False)
    repo: str = ""
    tag: str = "latest"
    asset: str | None = None


@dataclass(frozen=True)
class GitSource:
    """Install a plugin by cloning an arbitrary git repository.

    Clones the repository at *url* to a temporary directory, checks out
    *ref*, then copies the result into *install_dir*.

    Attributes:
        url: Git repository URL (``https://`` or ``git@`` form).
        ref: Branch, tag, or commit SHA to check out.  Defaults to ``"HEAD"``.
    """

    kind: Literal["git"] = field(default="git", init=False)
    url: str = ""
    ref: str = "HEAD"


@dataclass(frozen=True)
class NpmSource:
    """Install a plugin from the npm registry.

    Runs ``npm pack <package>@<version>`` to download the package tarball,
    then extracts it into *install_dir*.

    Attributes:
        package: npm package name (e.g. ``@acme/my-plugin``).
        version: Version constraint.  Defaults to ``"latest"``.
    """

    kind: Literal["npm"] = field(default="npm", init=False)
    package: str = ""
    version: str = "latest"


@dataclass(frozen=True)
class FileSource:
    """Install a plugin from a local archive file.

    Supports ``.zip`` and ``.tar.gz`` archives.  The archive is extracted
    into *install_dir*.

    Attributes:
        path: Absolute or relative path to the plugin archive.
    """

    kind: Literal["file"] = field(default="file", init=False)
    path: str = ""


@dataclass(frozen=True)
class DirectorySource:
    """Load a plugin directly from a local directory.

    The directory is copied into *install_dir*.  Useful for local plugin
    development and testing without packaging.

    Attributes:
        path: Absolute or relative path to the plugin directory.
    """

    kind: Literal["directory"] = field(default="directory", init=False)
    path: str = ""


#: Union type covering all five supported plugin source variants.
PluginSource = Annotated[
    GitHubSource | GitSource | NpmSource | FileSource | DirectorySource,
    "PluginSource",
]

# ---------------------------------------------------------------------------
# Install result
# ---------------------------------------------------------------------------


@dataclass
class PluginInstallResult:
    """Result of a plugin installation attempt.

    Attributes:
        success: True when the plugin was installed without errors.
        install_path: Path to the installed plugin directory (or ``None`` on
            failure).
        source_kind: The source variant that performed the install.
        error: Human-readable error message when ``success`` is False.
    """

    success: bool
    install_path: Path | None
    source_kind: str
    error: str = ""


# ---------------------------------------------------------------------------
# GitHub installer
# ---------------------------------------------------------------------------


def _fetch_github_release_asset_url(repo: str, tag: str, asset: str | None) -> str:
    """Return the download URL for a GitHub release asset.

    Args:
        repo: ``owner/repo`` string.
        tag: Release tag, or ``"latest"`` to auto-resolve.
        asset: Optional explicit asset filename.

    Returns:
        The direct download URL for the release asset.

    Raises:
        RuntimeError: When the release or a suitable asset cannot be found.
        urllib.error.URLError: On network failures.
    """
    release_tag = "latest" if tag in ("latest", "") else f"tags/{tag}"
    url = _GITHUB_API_RELEASE_URL.format(repo=repo, tag=release_tag)
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data: dict[str, object] = json.loads(resp.read())

    assets: list[dict[str, object]] = list(data.get("assets", []))  # type: ignore[arg-type]
    if not assets:
        raise RuntimeError(f"GitHub release '{tag}' for '{repo}' has no assets")

    if asset:
        matched = next((a for a in assets if a.get("name") == asset), None)
        if matched is None:
            raise RuntimeError(f"Asset '{asset}' not found in release '{tag}' for '{repo}'")
        return str(matched["browser_download_url"])

    # Auto-select: first zip or tar.gz
    for suffix in _DEFAULT_ASSET_SUFFIXES:
        for a in assets:
            name = str(a.get("name", ""))
            if name.endswith(suffix):
                return str(a["browser_download_url"])

    raise RuntimeError(
        f"No .zip or .tar.gz asset found in release '{tag}' for '{repo}'. Available: {[a.get('name') for a in assets]}"
    )


def _extract_archive(archive_path: Path, dest: Path) -> None:
    """Extract a .zip or .tar.gz archive into *dest*.

    Args:
        archive_path: Path to the archive file.
        dest: Destination directory (created if it does not exist).

    Raises:
        ValueError: When the archive format is not supported.
    """
    dest.mkdir(parents=True, exist_ok=True)
    name = archive_path.name
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            # Validate member paths to prevent Zip Slip (path traversal via
            # absolute paths or ".." components).
            resolved_dest = dest.resolve()
            for info in zf.infolist():
                target = (resolved_dest / info.filename).resolve()
                if not str(target).startswith(str(resolved_dest)):
                    raise ValueError(f"Zip entry would escape target directory: {info.filename}")
            zf.extractall(dest)
    elif name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(dest, filter="data")  # type: ignore[call-arg]
    else:
        raise ValueError(f"Unsupported archive format: {name}")


def _install_github(source: GitHubSource, install_dir: Path) -> PluginInstallResult:
    """Download and extract a GitHub release asset."""
    if not source.repo:
        return PluginInstallResult(success=False, install_path=None, source_kind="github", error="repo is required")

    try:
        download_url = _fetch_github_release_asset_url(source.repo, source.tag, source.asset)
    except (RuntimeError, urllib.error.URLError) as exc:
        return PluginInstallResult(success=False, install_path=None, source_kind="github", error=str(exc))

    plugin_name = source.repo.split("/")[-1]
    plugin_dir = install_dir / plugin_name

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive_name = download_url.rsplit("/", 1)[-1]
        archive_path = tmp_path / archive_name

        logger.info("plugin_installer: downloading %s → %s", download_url, archive_path)
        try:
            urllib.request.urlretrieve(download_url, archive_path)
        except urllib.error.URLError as exc:
            return PluginInstallResult(success=False, install_path=None, source_kind="github", error=str(exc))

        try:
            _extract_archive(archive_path, plugin_dir)
        except (ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
            return PluginInstallResult(success=False, install_path=None, source_kind="github", error=str(exc))

    logger.info("plugin_installer: github plugin installed → %s", plugin_dir)
    return PluginInstallResult(success=True, install_path=plugin_dir, source_kind="github")


# ---------------------------------------------------------------------------
# Git installer
# ---------------------------------------------------------------------------


def _install_git(source: GitSource, install_dir: Path) -> PluginInstallResult:
    """Clone a git repository and copy it into install_dir."""
    if not source.url:
        return PluginInstallResult(success=False, install_path=None, source_kind="git", error="url is required")

    repo_name = source.url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    plugin_dir = install_dir / repo_name

    with tempfile.TemporaryDirectory() as tmp:
        clone_dir = Path(tmp) / repo_name
        clone_cmd = ["git", "clone", "--depth=1", source.url, str(clone_dir)]
        logger.info("plugin_installer: cloning %s (ref=%s)", source.url, source.ref)
        try:
            subprocess.run(clone_cmd, check=True, capture_output=True, timeout=120)
        except subprocess.CalledProcessError as exc:
            return PluginInstallResult(
                success=False,
                install_path=None,
                source_kind="git",
                error=exc.stderr.decode(errors="replace") if exc.stderr else str(exc),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return PluginInstallResult(success=False, install_path=None, source_kind="git", error=str(exc))

        if source.ref not in ("HEAD", ""):
            checkout_cmd = ["git", "-C", str(clone_dir), "checkout", source.ref]
            try:
                subprocess.run(checkout_cmd, check=True, capture_output=True, timeout=30)
            except subprocess.CalledProcessError as exc:
                return PluginInstallResult(
                    success=False,
                    install_path=None,
                    source_kind="git",
                    error=exc.stderr.decode(errors="replace") if exc.stderr else str(exc),
                )

        if plugin_dir.exists():
            shutil.rmtree(plugin_dir)
        shutil.copytree(clone_dir, plugin_dir)

    logger.info("plugin_installer: git plugin installed → %s", plugin_dir)
    return PluginInstallResult(success=True, install_path=plugin_dir, source_kind="git")


# ---------------------------------------------------------------------------
# npm installer
# ---------------------------------------------------------------------------


def _install_npm(source: NpmSource, install_dir: Path) -> PluginInstallResult:
    """Download an npm package and extract it into install_dir."""
    if not source.package:
        return PluginInstallResult(success=False, install_path=None, source_kind="npm", error="package is required")

    pkg_spec = f"{source.package}@{source.version}" if source.version != "latest" else source.package
    safe_name = source.package.lstrip("@").replace("/", "__")
    plugin_dir = install_dir / safe_name

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pack_cmd = ["npm", "pack", pkg_spec, "--pack-destination", str(tmp_path)]
        logger.info("plugin_installer: npm packing %s", pkg_spec)
        try:
            result = subprocess.run(pack_cmd, check=True, capture_output=True, text=True, timeout=120)
        except subprocess.CalledProcessError as exc:
            return PluginInstallResult(
                success=False,
                install_path=None,
                source_kind="npm",
                error=exc.stderr or str(exc),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return PluginInstallResult(success=False, install_path=None, source_kind="npm", error=str(exc))

        tgz_name = result.stdout.strip()
        tgz_path = tmp_path / tgz_name
        if not tgz_path.exists():
            # npm pack may output just the filename; search for any .tgz
            tgz_files = list(tmp_path.glob("*.tgz"))
            if not tgz_files:
                return PluginInstallResult(
                    success=False,
                    install_path=None,
                    source_kind="npm",
                    error=f"npm pack produced no .tgz in {tmp_path}",
                )
            tgz_path = tgz_files[0]

        try:
            _extract_archive(tgz_path, plugin_dir)
        except (ValueError, tarfile.TarError) as exc:
            return PluginInstallResult(success=False, install_path=None, source_kind="npm", error=str(exc))

    logger.info("plugin_installer: npm plugin installed → %s", plugin_dir)
    return PluginInstallResult(success=True, install_path=plugin_dir, source_kind="npm")


# ---------------------------------------------------------------------------
# File installer
# ---------------------------------------------------------------------------


def _install_file(source: FileSource, install_dir: Path) -> PluginInstallResult:
    """Extract a local plugin archive into install_dir."""
    if not source.path:
        return PluginInstallResult(success=False, install_path=None, source_kind="file", error="path is required")

    archive_path = Path(source.path).expanduser().resolve()
    if not archive_path.exists():
        return PluginInstallResult(
            success=False,
            install_path=None,
            source_kind="file",
            error=f"Archive not found: {archive_path}",
        )

    plugin_name = archive_path.stem.removesuffix(".tar")
    plugin_dir = install_dir / plugin_name

    try:
        _extract_archive(archive_path, plugin_dir)
    except (ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
        return PluginInstallResult(success=False, install_path=None, source_kind="file", error=str(exc))

    logger.info("plugin_installer: file plugin installed → %s", plugin_dir)
    return PluginInstallResult(success=True, install_path=plugin_dir, source_kind="file")


# ---------------------------------------------------------------------------
# Directory installer
# ---------------------------------------------------------------------------


def _install_directory(source: DirectorySource, install_dir: Path) -> PluginInstallResult:
    """Copy a local plugin directory into install_dir."""
    if not source.path:
        return PluginInstallResult(success=False, install_path=None, source_kind="directory", error="path is required")

    src_dir = Path(source.path).expanduser().resolve()
    if not src_dir.is_dir():
        return PluginInstallResult(
            success=False,
            install_path=None,
            source_kind="directory",
            error=f"Directory not found: {src_dir}",
        )

    plugin_dir = install_dir / src_dir.name
    if plugin_dir.exists():
        shutil.rmtree(plugin_dir)
    shutil.copytree(src_dir, plugin_dir)

    logger.info("plugin_installer: directory plugin installed → %s", plugin_dir)
    return PluginInstallResult(success=True, install_path=plugin_dir, source_kind="directory")


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


def install_plugin(source: PluginSource, install_dir: Path) -> PluginInstallResult:
    """Install a plugin from the given source into *install_dir*.

    Dispatches to the appropriate installer based on the source variant.

    Args:
        source: A :class:`PluginSource` union describing where to install from.
        install_dir: Directory where the plugin will be installed.  Created
            automatically if it does not exist.

    Returns:
        :class:`PluginInstallResult` describing the outcome.
    """
    install_dir.mkdir(parents=True, exist_ok=True)

    if isinstance(source, GitHubSource):
        return _install_github(source, install_dir)
    if isinstance(source, GitSource):
        return _install_git(source, install_dir)
    if isinstance(source, NpmSource):
        return _install_npm(source, install_dir)
    if isinstance(source, FileSource):
        return _install_file(source, install_dir)
    return _install_directory(source, install_dir)


def update_plugin(source: PluginSource, install_dir: Path) -> PluginInstallResult:
    """Re-install a plugin from *source*, replacing the existing installation.

    For directory sources, the existing copy is removed before re-copying.
    For all other sources, a fresh install is performed (same as
    :func:`install_plugin`).

    Args:
        source: A :class:`PluginSource` union describing where to install from.
        install_dir: Directory that contains the plugin installation.

    Returns:
        :class:`PluginInstallResult` describing the outcome.
    """
    return install_plugin(source, install_dir)
