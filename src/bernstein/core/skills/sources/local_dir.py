"""Skill source that reads from a directory tree on disk.

Directory layout::

    <root>/
        <skill-name>/
            SKILL.md            # required, with YAML frontmatter
            references/         # optional — on-demand bodies
                deep-dive.md
            scripts/            # optional — agent-invocable helpers
                lint.sh
            assets/             # optional — static files (schemas etc.)
                schema.json

This is the default first-party source for Bernstein's own 17 skills
living at ``templates/skills/``. Third-party packs use the same layout via
:class:`~bernstein.core.skills.sources.plugin.PluginSkillSource`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.skills.manifest import (
    SkillManifestError,
    parse_skill_md,
)
from bernstein.core.skills.source import SkillArtifact, SkillSource

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.skills.manifest import SkillManifest


class LocalDirSkillSource(SkillSource):
    """Reads skill packs from immediate subdirectories of ``root``.

    Args:
        root: Directory containing ``<name>/SKILL.md`` entries.
        source_name: Label used in error messages and ``bernstein skills list``.
            Defaults to ``local:<root>``.
    """

    def __init__(self, root: Path, *, source_name: str | None = None) -> None:
        self._root = root
        self._name = source_name or f"local:{root}"

    @property
    def name(self) -> str:
        return self._name

    @property
    def root(self) -> Path:
        """Expose the root path for introspection."""
        return self._root

    def iter_skills(self) -> list[SkillArtifact]:
        if not self._root.is_dir():
            return []

        artifacts: list[SkillArtifact] = []
        for entry in sorted(self._root.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue

            manifest, body = parse_skill_md(skill_md)
            # Cross-check directory name against manifest ``name`` — catches
            # the common copy-paste mistake where someone duplicates a skill
            # dir and forgets to update frontmatter.
            if entry.name != manifest.name:
                raise SkillManifestError(
                    skill_md,
                    f"directory name {entry.name!r} does not match manifest name {manifest.name!r}",
                )
            artifacts.append(
                SkillArtifact(
                    manifest=manifest,
                    body=body,
                    origin=str(skill_md),
                )
            )
        return artifacts

    def read_reference(self, skill_name: str, reference: str) -> str:
        """Return the raw content of a ``references/`` file.

        Args:
            skill_name: Directory name (matches manifest ``name``).
            reference: Filename relative to ``references/``.

        Returns:
            File contents as UTF-8 text.

        Raises:
            FileNotFoundError: When the reference does not exist.
            ValueError: When ``reference`` tries to escape the skill
                directory via ``..`` path segments.
        """
        return _read_child(self._root, skill_name, "references", reference)

    def read_script(self, skill_name: str, script: str) -> str:
        """Return the raw content of a ``scripts/`` file. See :meth:`read_reference`."""
        return _read_child(self._root, skill_name, "scripts", script)

    def read_asset(self, skill_name: str, asset: str) -> str:
        """Return the raw content of an ``assets/`` file. See :meth:`read_reference`."""
        return _read_child(self._root, skill_name, "assets", asset)

    def list_references(self, skill_name: str) -> list[str]:
        """Return the filenames present in ``<skill>/references/``.

        Args:
            skill_name: Directory name.

        Returns:
            Sorted list of filenames; empty when the directory is missing.
        """
        return _list_child(self._root, skill_name, "references")

    def list_scripts(self, skill_name: str) -> list[str]:
        """Return the filenames present in ``<skill>/scripts/``."""
        return _list_child(self._root, skill_name, "scripts")

    def list_assets(self, skill_name: str) -> list[str]:
        """Return the filenames present in ``<skill>/assets/``."""
        return _list_child(self._root, skill_name, "assets")

    def manifest_for(self, name: str) -> SkillManifest | None:
        """Return the manifest for a single skill, or ``None`` when missing.

        Handy for lookups that do not need the full body.
        """
        skill_md = self._root / name / "SKILL.md"
        if not skill_md.is_file():
            return None
        manifest, _body = parse_skill_md(skill_md)
        return manifest


def _safe_child(root: Path, skill_name: str, bucket: str, filename: str) -> Path:
    """Join a child path under ``<root>/<skill>/<bucket>/`` and reject traversal.

    Args:
        root: Skills root (``templates/skills``).
        skill_name: Skill directory name.
        bucket: ``references`` | ``scripts`` | ``assets``.
        filename: Requested filename.

    Returns:
        Fully resolved absolute path inside the skill directory.

    Raises:
        ValueError: When the resolved path escapes ``<root>/<skill>/<bucket>/``.
    """
    base = (root / skill_name / bucket).resolve()
    candidate = (base / filename).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"path {filename!r} escapes skill directory {base}") from exc
    return candidate


def _read_child(root: Path, skill_name: str, bucket: str, filename: str) -> str:
    """Read a file safely from ``<root>/<skill>/<bucket>/<filename>``."""
    path = _safe_child(root, skill_name, bucket, filename)
    if not path.is_file():
        raise FileNotFoundError(f"{bucket} file not found for skill {skill_name!r}: {filename}")
    return path.read_text(encoding="utf-8")


def _list_child(root: Path, skill_name: str, bucket: str) -> list[str]:
    """List filenames under ``<root>/<skill>/<bucket>/``.

    Directories are skipped; symlinks are followed because the directory
    layout is under first-party control and tests may symlink fixtures.
    """
    base = root / skill_name / bucket
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_file())
