"""Skill lifecycle: install, remove, sync, lock (issue #1720, track 1).

This module turns the abstract :class:`~bernstein.core.skills.source.SkillSource`
interface into operator-facing verbs. It does NOT replace the existing
discovery cascade (:mod:`bernstein.core.plugins_core.skill_discovery`) or
the eager loader (:mod:`bernstein.core.skills.loader`); it only writes
into and reads from the well-known directories those subsystems already
scan:

- Project scope: ``<workdir>/.bernstein/skills/<name>/``
- User scope:    ``~/.bernstein/skills/<name>/``

Layout of an installed skill matches the conventional
:class:`~bernstein.core.skills.sources.local_dir.LocalDirSkillSource`
shape::

    <scope-root>/.bernstein/skills/<name>/
        SKILL.md
        references/  (optional)
        scripts/     (optional)
        assets/      (optional)

The lifecycle manifest ``bernstein-skills.toml`` lives at the repo root
and lists every skill the project expects to have installed. ``sync``
makes the filesystem match the manifest. ``skills.lock`` records the
content-addressed digest of every installed skill so a subsequent sync
detects drift.

Out of scope for this module (deferred to follow-up tracks):

- Source types beyond ``local`` (Git, OCI, index).
- Signature verification and trust roots.
- Source-content scans beyond the invisible Unicode install gate and the
  reserved sandbox-profile gate. Strict lint can block ERROR findings; the
  default path remains advisory for backwards compatibility.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypedDict, cast

import yaml
from pydantic import ValidationError

from bernstein.core.security.path_containment import (
    PathContainmentError,
    contained_subpath,
)
from bernstein.core.skills.lint import LintSeverity, lint_skill
from bernstein.core.skills.manifest import SkillManifest, parse_skill_md
from bernstein.core.skills.sanitizer import strip_invisible_tags

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Filename of the lifecycle manifest at repo root.
SKILLS_TOML_FILENAME: str = "bernstein-skills.toml"

#: Filename of the lock file next to the manifest.
SKILLS_LOCK_FILENAME: str = "skills.lock"

#: Directory under each scope root that holds installed skills.
_INSTALL_SUBDIR: tuple[str, str] = (".bernstein", "skills")

#: BLAKE2b digest size in bytes (32 bytes = 64 hex chars). Matches what the
#: RFC reserves room for in ``skills.lock``.
_DIGEST_SIZE: int = 32

#: Source types the foundation PR supports. Track 3 adds ``git``, ``oci``,
#: ``index``; we keep them out of scope here.
_SUPPORTED_SOURCES: frozenset[str] = frozenset({"local"})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SkillLifecycleError(RuntimeError):
    """Raised when an install / remove / sync operation fails."""


class SkillsTomlError(SkillLifecycleError):
    """Raised when ``bernstein-skills.toml`` cannot be parsed or validated."""


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class InstallScope(StrEnum):
    """Where an install lands.

    - :attr:`PROJECT` writes to ``<workdir>/.bernstein/skills/``.
    - :attr:`USER`    writes to ``~/.bernstein/skills/``.
    """

    PROJECT = "project"
    USER = "user"


def scope_root(scope: InstallScope, *, workdir: Path, home: Path | None = None) -> Path:
    """Return the ``.bernstein/skills/`` directory for ``scope``.

    Args:
        scope: Project or user scope.
        workdir: Current project root. Used only when scope is PROJECT.
        home: Override for the user's home directory. Defaults to
            :func:`pathlib.Path.home` so tests can redirect to ``tmp_path``.

    Returns:
        Absolute path to the installation root.
    """
    home_dir = home if home is not None else Path.home()
    base = workdir if scope is InstallScope.PROJECT else home_dir
    return base.joinpath(*_INSTALL_SUBDIR)


# ---------------------------------------------------------------------------
# Manifest schema (bernstein-skills.toml)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillsTomlEntry:
    """One ``[[skills]]`` entry from ``bernstein-skills.toml``.

    Attributes:
        name: Lowercase slug identifying the skill.
        source: Source type; only ``"local"`` is in scope for this PR.
        path: Path string as written in the TOML (relative paths are
            resolved against the TOML file's directory).
    """

    name: str
    source: str
    path: str


@dataclass(frozen=True)
class SkillsToml:
    """Parsed contents of ``bernstein-skills.toml``."""

    entries: tuple[SkillsTomlEntry, ...]
    source_path: Path


def load_skills_toml(path: Path) -> SkillsToml:
    """Parse ``bernstein-skills.toml`` from disk.

    Args:
        path: Absolute path to the TOML file.

    Returns:
        Parsed manifest.

    Raises:
        SkillsTomlError: When the file is missing, unreadable, malformed,
            or declares an entry with an unsupported source type.
    """
    if not path.is_file():
        raise SkillsTomlError(f"{path}: file does not exist")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SkillsTomlError(f"{path}: cannot read file: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise SkillsTomlError(f"{path}: invalid TOML: {exc}") from exc

    raw_entries = data.get("skills", [])
    if not isinstance(raw_entries, list):
        raise SkillsTomlError(f"{path}: 'skills' must be an array of tables")

    entries: list[SkillsTomlEntry] = []
    seen: set[str] = set()
    for idx, item in enumerate(cast("list[object]", raw_entries)):
        if not isinstance(item, dict):
            raise SkillsTomlError(f"{path}: entry #{idx} must be a table")
        item_dict = cast("dict[str, object]", item)
        name = item_dict.get("name")
        source = item_dict.get("source")
        skill_path = item_dict.get("path")
        if not isinstance(name, str) or not name:
            raise SkillsTomlError(f"{path}: entry #{idx} missing 'name'")
        if not isinstance(source, str) or not source:
            raise SkillsTomlError(f"{path}: entry {name!r} missing 'source'")
        if source not in _SUPPORTED_SOURCES:
            raise SkillsTomlError(
                f"{path}: entry {name!r} declares source={source!r}; only "
                f"{sorted(_SUPPORTED_SOURCES)} are supported in this release",
            )
        if not isinstance(skill_path, str) or not skill_path:
            raise SkillsTomlError(f"{path}: entry {name!r} missing 'path'")
        if name in seen:
            raise SkillsTomlError(f"{path}: duplicate entry for skill {name!r}")
        seen.add(name)
        entries.append(SkillsTomlEntry(name=name, source=source, path=skill_path))

    return SkillsToml(entries=tuple(entries), source_path=path)


def resolve_local_source(entry: SkillsTomlEntry, toml_dir: Path) -> Path:
    """Resolve an entry's ``path`` against the TOML directory.

    Args:
        entry: A ``local`` source entry.
        toml_dir: Directory containing the TOML file.

    Returns:
        Absolute path to the source (file or directory).
    """
    raw = Path(entry.path)
    return raw if raw.is_absolute() else (toml_dir / raw).resolve()


# ---------------------------------------------------------------------------
# Content digest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillDigest:
    """Content-addressed digest of an installed skill.

    The digest is BLAKE2b over a canonicalised stream that mixes:

    1. The YAML frontmatter, re-emitted with sorted keys.
    2. The markdown body with normalised line endings.
    3. Every file declared under ``references`` / ``scripts`` / ``assets``,
       in lexical order, each prefixed by its relative path.

    Files not declared in the manifest are deliberately ignored so adding
    a scratch note next to a skill does not invalidate the lock.
    """

    digest: str

    def __str__(self) -> str:
        return self.digest


def _canonical_frontmatter(front_raw: str) -> bytes:
    """Re-emit YAML frontmatter with sorted keys for a stable digest input.

    Unknown or extra keys are preserved verbatim so installs and lints
    converge on the same digest even when the manifest carries non-strict
    fields (the Claude Code ``whenToUse`` key is the canonical example).

    Args:
        front_raw: The YAML text between the ``---`` fences.

    Returns:
        Canonicalised YAML, UTF-8 encoded, terminated by exactly one
        newline.
    """
    try:
        loaded = yaml.safe_load(front_raw)
    except yaml.YAMLError:
        # If the YAML cannot be parsed, fall back to the raw bytes so the
        # digest still reflects the on-disk content. Lint will flag it
        # separately.
        return front_raw.encode("utf-8")
    if loaded is None:
        return b"{}\n"
    return yaml.safe_dump(
        loaded,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
    ).encode("utf-8")


def _split_skill_md(text: str) -> tuple[str, str]:
    """Split a SKILL.md text into frontmatter and body.

    Falls back to ``("", text)`` when no frontmatter is present so the
    digest still includes the whole file.
    """
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return ("", text)
    front: list[str] = []
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() == "---":
            front_text = "\n".join(front)
            body = "\n".join(lines[idx + 1 :]).strip("\n")
            return (front_text, body)
        front.append(lines[idx])
    # Unterminated frontmatter: treat the whole thing as body.
    return ("", text)


def _frontmatter_dict(front_raw: str) -> dict[str, Any]:
    """Best-effort frontmatter parse for digest helpers (no strict checks)."""
    try:
        loaded = yaml.safe_load(front_raw)
    except yaml.YAMLError:
        return {}
    if isinstance(loaded, dict):
        return cast("dict[str, Any]", loaded)
    return {}


def _referenced_files(skill_dir: Path, frontmatter: dict[str, Any]) -> list[Path]:
    """Return the manifest-referenced files in deterministic order.

    Files listed under ``references`` / ``scripts`` / ``assets`` are
    looked up under the matching subdirectory of ``skill_dir``. Missing
    files are silently skipped here; lint surfaces them as errors.
    """
    out: list[Path] = []
    try:
        skill_root = skill_dir.resolve()
    except OSError:
        return out
    for bucket in ("references", "scripts", "assets"):
        values = frontmatter.get(bucket, [])
        if not isinstance(values, list):
            continue
        for entry in cast("list[object]", values):
            if not isinstance(entry, str):
                continue
            # The bucket is part of the *candidate*, not the base, so a
            # bucket that is itself a symlink out of the skill tree is
            # refused rather than followed. Anchoring to a resolved bucket
            # root admitted that escape and then tripped the sort key below
            # with an uncaught ValueError. This matches what lint already
            # reports as ``unsafe-reference-path``.
            try:
                candidate = contained_subpath(skill_root, f"{bucket}/{entry}", label=f"{bucket} entry")
            except (PathContainmentError, OSError):
                # Best-effort by contract: the digest simply does not cover
                # an entry it cannot prove is inside the skill directory.
                # Lint is what surfaces these to the author.
                continue
            if candidate.is_file():
                out.append(candidate)
    # Every path is a proven descendant of skill_root, so the relative key
    # cannot raise.
    out.sort(key=lambda p: str(p.relative_to(skill_root)))
    return out


def compute_skill_digest(skill_dir: Path) -> SkillDigest:
    """Compute the content digest for an installed skill directory.

    The digest covers the canonicalised manifest, the body, and every
    manifest-declared reference / script / asset. Files present on disk
    but not declared in the manifest are ignored on purpose: the digest
    must be reproducible from the manifest alone.

    Args:
        skill_dir: Directory containing ``SKILL.md``.

    Returns:
        :class:`SkillDigest` with a 64-character hex BLAKE2b digest.

    Raises:
        SkillLifecycleError: When ``SKILL.md`` is missing.
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        raise SkillLifecycleError(f"{skill_md}: SKILL.md not found")

    text = skill_md.read_text(encoding="utf-8")
    front_raw, body = _split_skill_md(text)
    frontmatter = _frontmatter_dict(front_raw)

    hasher = hashlib.blake2b(digest_size=_DIGEST_SIZE)
    hasher.update(b"manifest:\n")
    hasher.update(_canonical_frontmatter(front_raw))
    hasher.update(b"body:\n")
    hasher.update(body.replace("\r\n", "\n").encode("utf-8"))

    for path in _referenced_files(skill_dir, frontmatter):
        rel = path.relative_to(skill_dir).as_posix()
        hasher.update(f"file:{rel}\n".encode())
        try:
            hasher.update(path.read_bytes())
        except OSError as exc:  # pragma: no cover - defensive
            raise SkillLifecycleError(f"{path}: cannot read referenced file: {exc}") from exc
        hasher.update(b"\n")

    return SkillDigest(digest=hasher.hexdigest())


# ---------------------------------------------------------------------------
# Install / remove
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallResult:
    """Outcome of installing or syncing a single skill."""

    name: str
    install_dir: Path
    digest: SkillDigest
    changed: bool


@dataclass(frozen=True)
class InitSkillResult:
    """Outcome of scaffolding a new local skill."""

    name: str
    install_dir: Path


class _ScaffoldManifestData(TypedDict):
    """Validated frontmatter fields for a deterministic skill scaffold."""

    manifest_schema: int
    name: str
    description: str
    trigger_keywords: list[str]
    references: list[str]
    scripts: list[str]
    assets: list[str]


def _detect_skill_name(source: Path) -> str:
    """Derive the canonical name for a local source.

    A directory source uses the directory name. A single ``.md`` source
    uses the file stem.
    """
    if source.is_dir():
        return source.name
    return source.stem


def _skill_title(name: str) -> str:
    """Return a deterministic human-readable heading for a skill slug."""
    return name.replace("-", " ").capitalize()


def _scaffold_manifest_data(name: str, description: str) -> _ScaffoldManifestData:
    """Return validated starter frontmatter for a scaffolded skill."""
    return {
        "manifest_schema": 1,
        "name": name,
        "description": description,
        "trigger_keywords": [],
        "references": [],
        "scripts": [],
        "assets": [],
    }


def _scaffold_skill_md(name: str, description: str) -> str:
    """Render the deterministic starter ``SKILL.md`` body."""
    frontmatter = yaml.safe_dump(
        _scaffold_manifest_data(name, description),
        sort_keys=False,
        allow_unicode=False,
    )
    return (
        "---\n"
        f"{frontmatter}"
        "---\n"
        "\n"
        f"# {_skill_title(name)}\n"
        "\n"
        "Describe when to use this skill and the exact workflow it should follow.\n"
    )


def init_skill(
    name: str,
    *,
    scope: InstallScope,
    workdir: Path,
    home: Path | None = None,
    description: str | None = None,
) -> InitSkillResult:
    """Create a deterministic local skill scaffold in the selected scope.

    Args:
        name: Lowercase skill slug.
        scope: Project or user scope.
        workdir: Current project root.
        home: Override for the user's home (tests).
        description: Optional description for the scaffold. Defaults to a
            deterministic valid description derived from ``name``.

    Returns:
        :class:`InitSkillResult` with the target directory.

    Raises:
        SkillLifecycleError: When the name is invalid or the target exists.
    """
    try:
        SkillManifest.validate_name(name)
    except ValueError as exc:
        raise SkillLifecycleError(str(exc)) from exc

    skill_description = (
        description if description is not None else f"Skill {name} scaffolded for deterministic authoring."
    )
    try:
        SkillManifest.model_validate(_scaffold_manifest_data(name, skill_description))
    except ValidationError as exc:
        raise SkillLifecycleError(f"{name}: invalid scaffold manifest: {exc.errors()}") from exc

    dest_root = scope_root(scope, workdir=workdir, home=home)
    install_dir = dest_root / name
    if install_dir.exists():
        raise SkillLifecycleError(f"{name}: skill already exists at {install_dir}")

    staging_dir = dest_root / f".{name}.init-tmp"

    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True)
        (staging_dir / "SKILL.md").write_text(_scaffold_skill_md(name, skill_description), encoding="utf-8")
        for bucket in ("references", "scripts", "assets"):
            (staging_dir / bucket).mkdir()
        staging_dir.replace(install_dir)
    except OSError as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise SkillLifecycleError(f"{name}: failed to initialize scaffold: {exc}") from exc

    return InitSkillResult(name=name, install_dir=install_dir)


def _raise_for_strict_lint_errors(skill_dir: Path, *, skill_name: str) -> None:
    """Raise when strict lint finds blocking errors for an installed skill."""
    errors = [
        finding for finding in lint_skill(skill_dir, skill_name=skill_name) if finding.severity is LintSeverity.ERROR
    ]
    if not errors:
        return
    details = "; ".join(f"{finding.code}: {finding.message}" for finding in errors)
    raise SkillLifecycleError(f"{skill_name}: strict lint failed: {details}")


def _raise_for_invisible_unicode(
    skill_dir: Path,
    *,
    skill_name: str,
    allow_invisible_unicode: bool,
) -> None:
    """Raise when ``SKILL.md`` contains invisible Unicode codepoints."""
    if allow_invisible_unicode:
        return
    skill_md = skill_dir / "SKILL.md"
    try:
        content = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillLifecycleError(f"{skill_name}: cannot read SKILL.md for sanitizer gate: {exc}") from exc

    _cleaned, count = strip_invisible_tags(content)
    if count > 0:
        raise SkillLifecycleError(
            f"{skill_name}: SKILL.md contains {count} invisible Unicode codepoint(s); "
            "refusing install unless allow_invisible_unicode=True",
        )


def _raise_for_unsupported_sandbox_profile(
    skill_dir: Path,
    *,
    skill_name: str,
    accept_risk: bool,
) -> None:
    """Raise when a skill asks for sandbox injection that is not available."""
    if accept_risk:
        return
    skill_md = skill_dir / "SKILL.md"
    try:
        content = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillLifecycleError(f"{skill_name}: cannot read SKILL.md for sandbox profile gate: {exc}") from exc

    front_raw, _body = _split_skill_md(content)
    frontmatter = _frontmatter_dict(front_raw)
    if frontmatter.get("sandbox_profile") is None:
        return
    raise SkillLifecycleError(
        f"{skill_name}: sandbox_profile requires sandbox injector support; refusing install unless accept_risk=True",
    )


def install_local(
    source: Path,
    *,
    scope: InstallScope,
    workdir: Path,
    home: Path | None = None,
    override_name: str | None = None,
    strict_lint: bool = False,
    allow_invisible_unicode: bool = False,
    accept_risk: bool = False,
) -> InstallResult:
    """Install a skill from a local path into the chosen scope.

    The source may be:

    - A directory containing ``SKILL.md`` (full layout with optional
      ``references/``, ``scripts/``, ``assets/`` siblings).
    - A standalone ``.md`` file (only ``SKILL.md`` is written; no
      referenced files are copied).

    Args:
        source: Absolute or relative path to the source.
        scope: Project or user scope.
        workdir: Current project root.
        home: Override for the user's home (tests).
        override_name: Override the auto-detected skill name. The TOML
            ``name`` field wins over filesystem layout when supplied.
        strict_lint: When ``True``, ERROR lint findings abort the install.
            WARNING findings remain advisory.
        allow_invisible_unicode: When ``True``, bypass the install-time
            invisible Unicode refusal for controlled reproduction.
        accept_risk: When ``True``, bypass explicit-risk install refusals
            such as reserved ``sandbox_profile`` injection metadata.

    Returns:
        :class:`InstallResult` with the target directory and digest.

    Raises:
        SkillLifecycleError: When the source does not exist, when the
            single-file source is not a ``.md`` file, or when the
            destination cannot be written.
    """
    if not source.exists():
        raise SkillLifecycleError(f"{source}: source path does not exist")

    name = override_name or _detect_skill_name(source)
    dest_root = scope_root(scope, workdir=workdir, home=home)
    install_dir = dest_root / name
    dest_root.mkdir(parents=True, exist_ok=True)
    # Stage into a sibling temp directory first so a previously working
    # install is preserved if validation or copy fails. The final swap is
    # atomic via Path.replace.
    staging_dir = dest_root / f".{name}.tmp"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)

    try:
        if source.is_dir():
            skill_md = source / "SKILL.md"
            if not skill_md.is_file():
                raise SkillLifecycleError(f"{source}: directory does not contain SKILL.md")
            _copy_skill_tree(source, staging_dir)
        else:
            if source.suffix != ".md":
                raise SkillLifecycleError(f"{source}: local file source must have a .md extension")
            try:
                content = source.read_text(encoding="utf-8")
            except OSError as exc:
                raise SkillLifecycleError(f"{source}: cannot read source: {exc}") from exc
            (staging_dir / "SKILL.md").write_text(content, encoding="utf-8")

        _raise_for_invisible_unicode(
            staging_dir,
            skill_name=name,
            allow_invisible_unicode=allow_invisible_unicode or accept_risk,
        )
        _raise_for_unsupported_sandbox_profile(
            staging_dir,
            skill_name=name,
            accept_risk=accept_risk,
        )
        digest = compute_skill_digest(staging_dir)
        if strict_lint:
            _raise_for_strict_lint_errors(staging_dir, skill_name=name)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    if install_dir.exists():
        shutil.rmtree(install_dir)
    staging_dir.replace(install_dir)
    return InstallResult(name=name, install_dir=install_dir, digest=digest, changed=True)


# ---------------------------------------------------------------------------
# Agent Plugins directory layout (#3772, slice 1 of #3540)
# ---------------------------------------------------------------------------

#: Filename of the Agent Plugins v1.0.0 root manifest.
PLUGIN_MANIFEST_FILENAME: str = "plugin.json"

#: Lockfile ``source`` tag for skills installed from a plugin directory.
_PLUGIN_LOCK_SOURCE: str = "plugin"


@dataclass(frozen=True)
class SkippedSkill:
    """A skill inside a plugin tree that was not installed."""

    name: str
    reason: str


@dataclass(frozen=True)
class PluginInstallResult:
    """Outcome of installing every skill under an Agent Plugins directory."""

    installed: list[InstallResult]
    skipped: list[SkippedSkill]


def _resolve_plugin_skills_dir(source: Path, data: dict[str, object]) -> Path | None:
    """Resolve the ``skills`` field of a plugin manifest, containment-checked.

    ``plugin.json`` is untrusted input: it arrives with whatever tree the
    user was handed. An absolute value, a ``../`` component, or an
    ordinary-looking component that is a symlink out of the tree would
    otherwise make ``skills_dir.iterdir()`` walk a directory outside the
    plugin root, and every ``SKILL.md`` found there would be copied into
    the install scope. ``contained_subpath`` pairs a string-shape check
    (rejects absolute and ``..`` shapes before any filesystem call) with a
    realpath comparison (catches the symlink case), so a manifest that
    escapes the root is treated as not-a-plugin.

    Returns ``None`` on any refusal so layout detection and install share
    one resolution path and cannot drift apart.
    """
    skills_field = data.get("skills")
    if not isinstance(skills_field, str) or not skills_field:
        return None
    try:
        return contained_subpath(source, skills_field, label="plugin skills directory")
    except (PathContainmentError, OSError):
        return None


def is_agent_plugins_layout(source: Path) -> bool:
    """Strict Agent Plugins v1.0.0 layout detection.

    A directory counts as an Agent Plugins layout only when a root
    ``plugin.json`` parses with a non-empty ``name`` field and a ``skills``
    field that resolves to a ``skills/`` subdirectory. Strict by design
    (#3772 decision): avoids misfiring on unrelated directories that merely
    happen to contain a ``skills/`` folder. The ``skills`` field must stay
    inside the plugin root (see :func:`_resolve_plugin_skills_dir`).
    """
    if not source.is_dir():
        return False
    manifest_path = source / PLUGIN_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return False
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict):
        return False
    data = cast("dict[str, object]", raw)
    name = data.get("name")
    if not isinstance(name, str) or not name:
        return False
    skills_dir = _resolve_plugin_skills_dir(source, data)
    return skills_dir is not None and skills_dir.is_dir()


def install_plugin_local(
    source: Path,
    *,
    scope: InstallScope,
    workdir: Path,
    home: Path | None = None,
    strict_lint: bool = False,
    accept_risk: bool = False,
) -> PluginInstallResult:
    """Install every skill under an Agent Plugins directory layout.

    A non-plugin directory raises :class:`SkillLifecycleError`. Each
    conformant ``skills/<name>/SKILL.md`` is installed through
    :func:`install_local` and recorded in ``skills.lock`` with its content
    digest (``source="plugin"``) so a later ``sync`` can detect drift. An
    invalid individual skill is skipped with a diagnostic naming it rather
    than aborting the whole plugin install.
    """
    if not source.is_dir():
        raise SkillLifecycleError(
            f"{source}: not an Agent Plugins directory layout "
            f"(root {PLUGIN_MANIFEST_FILENAME} with name + skills/ required)"
        )
    manifest_path = source / PLUGIN_MANIFEST_FILENAME
    try:
        manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SkillLifecycleError(
            f"{source}: not an Agent Plugins directory layout "
            f"(root {PLUGIN_MANIFEST_FILENAME} with name + skills/ required)"
        ) from exc
    if not isinstance(manifest_raw, dict):
        raise SkillLifecycleError(
            f"{source}: not an Agent Plugins directory layout "
            f"(root {PLUGIN_MANIFEST_FILENAME} with name + skills/ required)"
        )
    manifest = cast("dict[str, object]", manifest_raw)
    name_field = manifest.get("name")
    if not isinstance(name_field, str) or not name_field:
        raise SkillLifecycleError(
            f"{source}: not an Agent Plugins directory layout "
            f"(root {PLUGIN_MANIFEST_FILENAME} with name + skills/ required)"
        )
    # Resolved once, containment-checked: the same value is used for the
    # walk below and for the lockfile paths, never re-joined from the raw
    # manifest string.
    skills_dir = _resolve_plugin_skills_dir(source, manifest)
    if skills_dir is None or not skills_dir.is_dir():
        raise SkillLifecycleError(
            f"{source}: plugin manifest 'skills' must resolve to a directory inside the plugin root"
        )

    installed: list[InstallResult] = []
    skipped: list[SkippedSkill] = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        name = skill_dir.name
        # Per-skill isolation: one bad SKILL.md must not abort the pack.
        try:
            # The field-level check proved skills_dir is inside the plugin;
            # this proves the entry is too. A skill directory symlinked out
            # of the pack would install content the operator never saw.
            if _escapes(skills_dir, skill_dir):
                raise SkillLifecycleError(f"{skill_dir}: skill directory resolves outside the plugin")
            parse_skill_md(skill_md)
            installed.append(
                install_local(
                    skill_dir,
                    scope=scope,
                    workdir=workdir,
                    home=home,
                    strict_lint=strict_lint,
                    accept_risk=accept_risk,
                )
            )
        except Exception as exc:  # per-skill skip contract: one bad SKILL.md must not abort the pack
            skipped.append(SkippedSkill(name=name, reason=str(exc)))

    if installed:
        _record_plugin_lock(installed, skills_dir, workdir=workdir)
    return PluginInstallResult(installed=installed, skipped=skipped)


def _record_plugin_lock(
    installed: list[InstallResult],
    skills_dir: Path,
    *,
    workdir: Path,
) -> None:
    """Merge plugin-installed skills into ``skills.lock`` ([[skills]] rows).

    Each row's ``path`` names the ``skills/<name>/`` directory the skill
    came from (not the plugin root), so a later ``sync``/drift reader can
    locate the individual skill tree.
    """
    lock_path = workdir / SKILLS_LOCK_FILENAME
    entries = _read_lock(lock_path)
    for result in installed:
        entries[result.name] = LockEntry(
            name=result.name,
            source=_PLUGIN_LOCK_SOURCE,
            path=str(skills_dir / result.name),
            digest=result.digest.digest,
        )
    _write_lock(lock_path, list(entries.values()))


def _escapes(root: Path, candidate: Path) -> bool:
    """Whether *candidate* resolves outside *root* once symlinks are followed.

    The string-shape half of the containment barrier does not apply here:
    these paths come from ``iterdir``/``rglob`` over a tree we were handed,
    so the only lie available is a symlink, and ``realpath`` is the check
    that sees through it.
    """
    prefix = os.path.join(os.path.realpath(root), "")
    return not os.path.realpath(candidate).startswith(prefix)


def _copy_skill_tree(source: Path, dest: Path) -> None:
    """Copy a skill directory tree, preserving SKILL.md + sibling buckets.

    Only ``SKILL.md`` and the three known buckets (``references``,
    ``scripts``, ``assets``) are mirrored; anything else under the source
    directory is ignored so a dotfile from the author's editor cannot leak
    into the installed copy.

    Every copied path must resolve inside *source*: the tree is untrusted
    input, and a symlinked bucket or nested file would otherwise publish
    content from outside the tree the operator inspected -- the same escape
    :func:`_resolve_plugin_skills_dir` refuses one level up.
    """
    skill_md = source / "SKILL.md"
    if _escapes(source, skill_md):
        raise SkillLifecycleError(f"{skill_md}: SKILL.md resolves outside the skill directory")
    shutil.copy2(skill_md, dest / "SKILL.md")
    for bucket in ("references", "scripts", "assets"):
        src_bucket = source / bucket
        if not src_bucket.is_dir():
            continue
        dst_bucket = dest / bucket
        dst_bucket.mkdir(exist_ok=True)
        # Walk the bucket recursively so nested manifest paths like
        # ``references/guides/deep.md`` survive the install. Empty
        # directories are skipped on purpose so the destination only
        # contains paths that the manifest can actually address.
        for child in sorted(src_bucket.rglob("*")):
            if not child.is_file():
                continue
            if _escapes(source, child):
                raise SkillLifecycleError(f"{child}: {bucket} entry resolves outside the skill directory")
            rel = child.relative_to(src_bucket)
            target = dst_bucket / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


def remove_skill(
    name: str,
    *,
    scope: InstallScope,
    workdir: Path,
    home: Path | None = None,
) -> bool:
    """Remove an installed skill from the given scope.

    Args:
        name: Skill name (directory name under the scope root).
        scope: Project or user scope.
        workdir: Current project root.
        home: Override for the user's home (tests).

    Returns:
        ``True`` if the skill was removed; ``False`` if nothing was there.
    """
    install_dir = scope_root(scope, workdir=workdir, home=home) / name
    if not install_dir.exists():
        return False
    shutil.rmtree(install_dir)
    return True


# ---------------------------------------------------------------------------
# Sync + lock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncOutcome:
    """One row in the result of :func:`sync_skills`."""

    name: str
    action: str  # "installed" | "updated" | "unchanged"
    digest: SkillDigest
    install_dir: Path


@dataclass(frozen=True)
class LockEntry:
    """One ``[[skills]]`` row in ``skills.lock``."""

    name: str
    source: str
    path: str
    digest: str


def _read_lock(path: Path) -> dict[str, LockEntry]:
    """Read ``skills.lock`` from disk, keyed by skill name."""
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return {}
    raw = data.get("skills", [])
    if not isinstance(raw, list):
        return {}
    out: dict[str, LockEntry] = {}
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            continue
        item_dict = cast("dict[str, object]", item)
        name = item_dict.get("name")
        source = item_dict.get("source")
        path_value = item_dict.get("path")
        digest = item_dict.get("digest")
        if (
            isinstance(name, str)
            and isinstance(source, str)
            and isinstance(path_value, str)
            and isinstance(digest, str)
        ):
            out[name] = LockEntry(name=name, source=source, path=path_value, digest=digest)
    return out


def _write_lock(path: Path, entries: list[LockEntry]) -> None:
    """Write a deterministic TOML lock file.

    We hand-roll the TOML rather than pulling in ``tomli_w`` so the output
    is bit-identical across runs (sorted keys, fixed quoting, single
    blank line between tables).
    """
    sorted_entries = sorted(entries, key=lambda entry: entry.name)
    lines: list[str] = [
        "# bernstein skills lock file - regenerated by `bernstein skills sync`.",
        "# Do not edit by hand.",
        "",
    ]
    for entry in sorted_entries:
        lines.extend(
            (
                "[[skills]]",
                f"name = {_toml_quote(entry.name)}",
                f"source = {_toml_quote(entry.source)}",
                f"path = {_toml_quote(entry.path)}",
                f"digest = {_toml_quote(entry.digest)}",
                "",
            )
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _toml_quote(value: str) -> str:
    """Quote a value as a TOML basic string.

    The escape table covers control characters and the two characters TOML
    treats as syntactically significant inside basic strings (``"`` and
    ``\\``). Skill names are slugs and paths are POSIX-like so the slow
    path almost never triggers.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def sync_skills(
    toml_path: Path,
    *,
    scope: InstallScope = InstallScope.PROJECT,
    workdir: Path | None = None,
    home: Path | None = None,
    strict_lint: bool = False,
    allow_invisible_unicode: bool = False,
    accept_risk: bool = False,
) -> list[SyncOutcome]:
    """Reconcile ``bernstein-skills.toml`` with the chosen scope.

    Re-runs are idempotent: when the installed digest matches the source
    digest, the install is skipped and the lock file is left untouched.
    Drift (a manual edit, a re-published source) reinstalls and rewrites
    the lock row.

    Args:
        toml_path: Path to ``bernstein-skills.toml``.
        scope: Where to install. Defaults to PROJECT.
        workdir: Project root. Defaults to ``toml_path.parent``.
        home: Override for the user's home (tests).
        strict_lint: When ``True``, ERROR lint findings abort changed and
            unchanged installs. WARNING findings remain advisory.
        allow_invisible_unicode: When ``True``, bypass the install-time
            invisible Unicode refusal for controlled reproduction.
        accept_risk: When ``True``, bypass explicit-risk install refusals
            such as reserved ``sandbox_profile`` injection metadata.

    Returns:
        One :class:`SyncOutcome` per skill, in declaration order.

    Raises:
        SkillsTomlError: When the manifest cannot be parsed.
        SkillLifecycleError: When an install step fails.
    """
    if workdir is None:
        workdir = toml_path.parent

    manifest = load_skills_toml(toml_path)
    toml_dir = toml_path.parent
    lock_path = toml_dir / SKILLS_LOCK_FILENAME
    previous_lock = _read_lock(lock_path)

    outcomes: list[SyncOutcome] = []
    new_lock: list[LockEntry] = []

    for entry in manifest.entries:
        source_path = resolve_local_source(entry, toml_dir)
        # Pre-compute the digest of the *source* so we can short-circuit
        # before touching the install directory at all. For a single-file
        # source we have to land it in a temp dir first because the digest
        # algorithm operates on installed-shape directories; for a real
        # directory source we can read it in place.
        existing_install = scope_root(scope, workdir=workdir, home=home) / entry.name
        prior_digest: str | None
        if existing_install.is_dir():
            try:
                prior_digest = compute_skill_digest(existing_install).digest
            except SkillLifecycleError:
                # A malformed prior install (e.g. missing SKILL.md after a
                # partial write) counts as drift so sync can self-heal by
                # reinstalling instead of aborting the whole batch.
                prior_digest = None
        else:
            prior_digest = None

        if source_path.is_dir():
            source_digest = compute_skill_digest(source_path).digest
        else:
            # For a single-file source, the digest depends only on the
            # SKILL.md content; we synthesise an empty-bucket directory
            # view by hashing the canonicalised frontmatter + body.
            try:
                text = source_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise SkillLifecycleError(f"{source_path}: cannot read source: {exc}") from exc
            front_raw, body = _split_skill_md(text)
            hasher = hashlib.blake2b(digest_size=_DIGEST_SIZE)
            hasher.update(b"manifest:\n")
            hasher.update(_canonical_frontmatter(front_raw))
            hasher.update(b"body:\n")
            hasher.update(body.replace("\r\n", "\n").encode("utf-8"))
            source_digest = hasher.hexdigest()

        if prior_digest == source_digest and existing_install.is_dir():
            _raise_for_invisible_unicode(
                existing_install,
                skill_name=entry.name,
                allow_invisible_unicode=allow_invisible_unicode or accept_risk,
            )
            _raise_for_unsupported_sandbox_profile(
                existing_install,
                skill_name=entry.name,
                accept_risk=accept_risk,
            )
            if strict_lint:
                _raise_for_strict_lint_errors(existing_install, skill_name=entry.name)
            outcomes.append(
                SyncOutcome(
                    name=entry.name,
                    action="unchanged",
                    digest=SkillDigest(digest=source_digest),
                    install_dir=existing_install,
                )
            )
            new_lock.append(
                LockEntry(
                    name=entry.name,
                    source=entry.source,
                    path=entry.path,
                    digest=source_digest,
                )
            )
            continue

        result = install_local(
            source_path,
            scope=scope,
            workdir=workdir,
            home=home,
            override_name=entry.name,
            strict_lint=strict_lint,
            allow_invisible_unicode=allow_invisible_unicode,
            accept_risk=accept_risk,
        )
        action = "updated" if entry.name in previous_lock else "installed"
        outcomes.append(
            SyncOutcome(
                name=entry.name,
                action=action,
                digest=result.digest,
                install_dir=result.install_dir,
            )
        )
        new_lock.append(
            LockEntry(
                name=entry.name,
                source=entry.source,
                path=entry.path,
                digest=result.digest.digest,
            )
        )

    _write_lock(lock_path, new_lock)
    return outcomes


def read_lock_entries(toml_dir: Path) -> list[LockEntry]:
    """Public helper for tests: read the lock file as a sorted list."""
    entries = _read_lock(toml_dir / SKILLS_LOCK_FILENAME)
    return sorted(entries.values(), key=lambda entry: entry.name)


__all__ = [
    "SKILLS_LOCK_FILENAME",
    "SKILLS_TOML_FILENAME",
    "InitSkillResult",
    "InstallResult",
    "InstallScope",
    "LockEntry",
    "SkillDigest",
    "SkillLifecycleError",
    "SkillsToml",
    "SkillsTomlEntry",
    "SkillsTomlError",
    "SyncOutcome",
    "compute_skill_digest",
    "init_skill",
    "install_local",
    "load_skills_toml",
    "read_lock_entries",
    "remove_skill",
    "resolve_local_source",
    "scope_root",
    "sync_skills",
]
