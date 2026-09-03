"""Bridge between catalog entries and the existing plugin installer.

Translates a :class:`SkillSourceSpec` into the
:class:`bernstein.core.plugins_core.plugin_installer.PluginSource`
variant that already covers github / git / npm / file / directory and
delegates the actual fetch + extract to
:func:`install_plugin`. After the source lands in a staging directory
this module promotes it into the standard skill layout under
``<scope-root>/.bernstein/skills/<name>/`` by reusing
:func:`bernstein.core.skills.lifecycle.install_local`.

Promotion runs under ``strict_lint``: catalog content is third-party
prompt-space code, so an ERROR lint finding refuses the install while the
content is still staged and nothing lands in the skill directory.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.plugins_core.plugin_installer import (
    DirectorySource,
    FileSource,
    GitHubSource,
    GitSource,
    NpmSource,
    install_plugin,
)
from bernstein.core.skills.lifecycle import (
    SkillLifecycleError,
    SkillLintRefusedError,
    compute_skill_digest,
    install_local,
    scope_root,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.core.plugins_core.plugin_installer import (
        PluginInstallResult,
        PluginSource,
    )
    from bernstein.core.skills.catalog.manifest import SkillCatalogEntry, SkillSourceSpec
    from bernstein.core.skills.lifecycle import InstallResult, InstallScope
    from bernstein.core.skills.lint import LintFinding

    #: Signature of the pluggable installer dispatcher.
    InstallerCallable = Callable[[PluginSource, Path], PluginInstallResult]

logger = logging.getLogger(__name__)


class CatalogInstallError(SkillLifecycleError):
    """Raised when a catalog install fails (download, extract, or layout)."""


class CatalogTrustGateError(CatalogInstallError):
    """Raised when the trust gate refuses a staged catalog entry.

    A skill body is prompt-space code, so a catalog entry is admitted only
    after ``lint_skill`` clears it: hostile instruction shapes
    (``prompt-space-risk``) and structurally invalid manifests both refuse the
    install while the content is still in the staging directory. The blocking
    findings ride along so the caller can record a refusal receipt naming the
    reason.
    """

    def __init__(self, message: str, *, findings: tuple[LintFinding, ...]) -> None:
        super().__init__(message)
        self.findings = findings

    @property
    def reason_code(self) -> str:
        """Machine-readable refusal reason for the audit chain."""
        if any(finding.code == "prompt-space-risk" for finding in self.findings):
            return "prompt_space_risk"
        return "lint_error"


@dataclass(frozen=True)
class CatalogInstallResult:
    """Outcome of :func:`install_catalog_entry`."""

    name: str
    install_dir: Path
    content_digest: str
    source_kind: str
    used_staging_dir: Path


def resolve_plugin_source(spec: SkillSourceSpec) -> PluginSource:
    """Coerce a :class:`SkillSourceSpec` into a plugin installer source.

    Source variants supported here are exactly the ones the plugin
    installer already implements: github, git, npm, file, directory.
    """
    if spec.kind == "github":
        return GitHubSource(repo=spec.repo, tag=spec.tag, asset=spec.asset)
    if spec.kind == "git":
        return GitSource(url=spec.url, ref=spec.ref)
    if spec.kind == "npm":
        return NpmSource(package=spec.package, version=spec.version)
    if spec.kind == "file":
        return FileSource(path=spec.path)
    if spec.kind == "directory":
        return DirectorySource(path=spec.path)
    raise CatalogInstallError(f"unsupported source kind {spec.kind!r}")


def _locate_skill_root(staging_dir: Path) -> Path:
    """Find the SKILL.md root inside the staged plugin contents.

    The plugin installer extracts archives into a subdirectory named
    after the source (e.g. ``acme-my-skill`` for a github source). The
    SKILL.md can live directly under that root or one level deeper
    (top-level repo dir). We prefer a shallow match so we don't
    accidentally pick up a nested fixture.
    """
    if (staging_dir / "SKILL.md").is_file():
        return staging_dir
    candidates = sorted(staging_dir.glob("*/SKILL.md"))
    if not candidates:
        # Allow two-level descents for github archives that wrap the
        # repo in ``<owner>-<repo>-<sha>/`` and then ``<skill-name>/``.
        candidates = sorted(staging_dir.glob("*/*/SKILL.md"))
    if not candidates:
        raise CatalogInstallError(
            f"installed source under {staging_dir} contains no SKILL.md",
        )
    return candidates[0].parent


def install_catalog_entry(
    entry: SkillCatalogEntry,
    *,
    scope: InstallScope,
    workdir: Path,
    home: Path | None = None,
    plugin_installer: InstallerCallable | None = None,
) -> CatalogInstallResult:
    """Install a catalog entry into the standard skill layout.

    Args:
        entry: The validated catalog entry to install.
        scope: Project or user scope.
        workdir: Current project root.
        home: Override for the user's home (tests).
        plugin_installer: Pluggable installer dispatcher. Defaults to
            :func:`install_plugin`; tests pass a mock that copies a
            fixture instead of touching the network.

    Returns:
        :class:`CatalogInstallResult` with the on-disk install location
        and recomputed content digest.

    Raises:
        CatalogTrustGateError: When strict lint refuses the staged content.
        CatalogInstallError: On any other failure during download,
            extraction, layout promotion, or digest validation.
    """
    plugin_source = resolve_plugin_source(entry.source)
    installer = plugin_installer if plugin_installer is not None else install_plugin

    with tempfile.TemporaryDirectory(prefix="bernstein-skill-catalog-") as tmp:
        staging_root = Path(tmp)
        try:
            install_result: PluginInstallResult = installer(plugin_source, staging_root)
        except Exception as exc:  # pragma: no cover - defensive
            raise CatalogInstallError(f"plugin installer raised: {exc}") from exc

        if not install_result.success or install_result.install_path is None:
            raise CatalogInstallError(
                f"plugin installer failed for {entry.id!r}: {install_result.error or 'unknown error'}",
            )

        skill_root = _locate_skill_root(install_result.install_path)

        try:
            promote_result: InstallResult = install_local(
                skill_root,
                scope=scope,
                workdir=workdir,
                home=home,
                override_name=entry.name,
                strict_lint=True,
            )
        except SkillLintRefusedError as exc:
            raise CatalogTrustGateError(
                f"trust gate refused catalog entry {entry.id!r}: {exc}",
                findings=exc.findings,
            ) from exc
        except SkillLifecycleError as exc:
            raise CatalogInstallError(f"layout promotion failed for {entry.id!r}: {exc}") from exc

    # Recompute digest against the installed layout (not the staging copy
    # the installer wrote to). The catalog manifest's content_digest is
    # the contract; mismatches are surfaced to the operator immediately.
    try:
        digest = compute_skill_digest(promote_result.install_dir).digest
    except SkillLifecycleError as exc:
        raise CatalogInstallError(f"failed to compute installed digest: {exc}") from exc

    return CatalogInstallResult(
        name=promote_result.name,
        install_dir=promote_result.install_dir,
        content_digest=digest,
        source_kind=entry.source.kind,
        used_staging_dir=install_result.install_path,
    )


def remove_catalog_install(
    entry_id: str,
    *,
    scope: InstallScope,
    workdir: Path,
    home: Path | None = None,
) -> bool:
    """Remove a catalog-installed skill from the chosen scope."""
    install_dir = scope_root(scope, workdir=workdir, home=home) / entry_id
    if not install_dir.is_dir():
        return False
    shutil.rmtree(install_dir)
    return True


__all__ = [
    "CatalogInstallError",
    "CatalogInstallResult",
    "CatalogTrustGateError",
    "install_catalog_entry",
    "remove_catalog_install",
    "resolve_plugin_source",
]
