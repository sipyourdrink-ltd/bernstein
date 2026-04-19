"""Orchestrates :class:`~bernstein.core.skills.source.SkillSource` instances.

The loader merges every registered source into a single lookup table and
fails fast when two sources publish a skill with the same name. This is
the central piece that makes progressive disclosure safe:

- ``role_resolver`` asks the loader for a skill matching a role; if one
  exists, its body is NOT injected into the prompt — only the index is.
- When an agent calls ``load_skill`` via MCP, the tool delegates to the
  loader to fetch the body / reference / script on demand.
- Startup conflict detection prevents two plugins from silently shadowing
  each other.

The loader is intentionally stateless with respect to *which* source it
belongs to: sources are provided once at construction and indexed
eagerly. Re-registering a source requires a new :class:`SkillLoader`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from bernstein.core.skills.sources.local_dir import LocalDirSkillSource

if TYPE_CHECKING:
    from bernstein.core.skills.source import SkillArtifact, SkillSource

# Signature of ``read_reference`` / ``read_script`` on sources that support
# on-demand file reads. Sources that can't serve bucketed files simply omit
# the attribute — see the ``getattr(..., None)`` dance below. The return type
# is ``object`` so third-party plugin implementations cannot trick the static
# checker into skipping the runtime string-validation in :func:`_call_reader`.
_ReaderFn = Callable[[str, str], object]


class DuplicateSkillError(RuntimeError):
    """Raised at startup when two sources define a skill with the same name.

    The message lists both offending origins so the operator can disable
    whichever is wrong.
    """

    def __init__(self, name: str, first_origin: str, second_origin: str) -> None:
        super().__init__(f"duplicate skill {name!r}: defined in both {first_origin} and {second_origin}")
        self.skill_name = name
        self.first_origin = first_origin
        self.second_origin = second_origin


class SkillNotFoundError(KeyError):
    """Raised when a caller asks for a skill that no source provides."""


@dataclass(frozen=True)
class LoadedSkill:
    """A skill after the loader has indexed it.

    Attributes:
        name:        Canonical skill name.
        description: One-paragraph description used in the index.
        body:        The SKILL.md body, without frontmatter.
        references:  Filenames under ``references/`` (from the manifest).
        scripts:     Filenames under ``scripts/`` (from the manifest).
        assets:      Filenames under ``assets/`` (from the manifest).
        origin:      Where the skill came from (path or plugin name).
        source_name: Label of the :class:`SkillSource` that owns it.
        trigger_keywords: Optional keyword hints for matching.
    """

    name: str
    description: str
    body: str
    references: tuple[str, ...]
    scripts: tuple[str, ...]
    assets: tuple[str, ...]
    origin: str
    source_name: str
    trigger_keywords: tuple[str, ...]


class SkillLoader:
    """Registry of loaded skills across all sources.

    Construct with a list of sources; the loader calls ``iter_skills`` on
    each eagerly, detects conflicts, and exposes :meth:`get`,
    :meth:`list_all`, and :meth:`find_source_for` for downstream callers.
    """

    def __init__(self, sources: list[SkillSource]) -> None:
        self._sources: list[SkillSource] = list(sources)
        self._skills: dict[str, LoadedSkill] = {}
        self._source_by_skill: dict[str, SkillSource] = {}
        self._reload()

    @property
    def sources(self) -> tuple[SkillSource, ...]:
        """Expose the sources the loader was constructed with."""
        return tuple(self._sources)

    def _reload(self) -> None:
        """Re-scan every source. Separate method so tests can force a refresh."""
        self._skills.clear()
        self._source_by_skill.clear()

        for source in self._sources:
            artifacts = source.iter_skills()
            for artifact in artifacts:
                self._register(source, artifact)

    def _register(self, source: SkillSource, artifact: SkillArtifact) -> None:
        """Add a single artifact to the index, raising on name conflicts."""
        name = artifact.manifest.name
        existing = self._skills.get(name)
        if existing is not None:
            raise DuplicateSkillError(
                name=name,
                first_origin=existing.origin,
                second_origin=artifact.origin,
            )

        self._skills[name] = LoadedSkill(
            name=name,
            description=artifact.manifest.description,
            body=artifact.body,
            references=tuple(artifact.manifest.references),
            scripts=tuple(artifact.manifest.scripts),
            assets=tuple(artifact.manifest.assets),
            origin=artifact.origin,
            source_name=source.name,
            trigger_keywords=tuple(artifact.manifest.trigger_keywords),
        )
        self._source_by_skill[name] = source

    def get(self, name: str) -> LoadedSkill:
        """Return the loaded skill with this name.

        Args:
            name: Skill name.

        Returns:
            :class:`LoadedSkill` for the requested name.

        Raises:
            SkillNotFoundError: When no source provides the skill.
        """
        skill = self._skills.get(name)
        if skill is None:
            raise SkillNotFoundError(name)
        return skill

    def has(self, name: str) -> bool:
        """Return whether a skill with the given name is registered."""
        return name in self._skills

    def list_all(self) -> list[LoadedSkill]:
        """Return every loaded skill, sorted by name for deterministic output."""
        return [self._skills[name] for name in sorted(self._skills)]

    def find_source_for(self, name: str) -> SkillSource:
        """Return the source that owns a given skill name.

        Raises:
            SkillNotFoundError: When no source provides the skill.
        """
        source = self._source_by_skill.get(name)
        if source is None:
            raise SkillNotFoundError(name)
        return source

    def read_reference(self, name: str, reference: str) -> str:
        """Read a file from a skill's ``references/`` directory.

        Args:
            name: Skill name.
            reference: Filename under ``references/``.

        Returns:
            File content.

        Raises:
            SkillNotFoundError: When the skill is not registered.
            FileNotFoundError: When the reference does not exist.
            ValueError:        When ``reference`` escapes the skill directory.
            RuntimeError:      When the owning source cannot serve references
                (e.g. a remote source that bundles only SKILL.md).
        """
        source = self.find_source_for(name)
        reader = _resolve_reader(source, "read_reference")
        return _call_reader(reader, name, reference)

    def read_script(self, name: str, script: str) -> str:
        """Read a file from a skill's ``scripts/`` directory. See :meth:`read_reference`."""
        source = self.find_source_for(name)
        reader = _resolve_reader(source, "read_script")
        return _call_reader(reader, name, script)


def default_skills_root(templates_dir: object) -> object:
    """Return the conventional ``templates/skills`` path for a templates dir.

    The argument is typed loosely to avoid a circular dependency on
    :mod:`pathlib` at module load — the caller always passes a ``Path``.

    Args:
        templates_dir: Path to ``templates/`` (e.g. ``templates/roles/``'s
            parent is the right starting point for the default layout).

    Returns:
        Path to ``<templates_dir>/skills/``.
    """
    return templates_dir / "skills"  # type: ignore[operator]


def default_loader_from_templates(
    templates_roles_dir: object,
    *,
    include_plugins: bool = True,
) -> SkillLoader:
    """Build a :class:`SkillLoader` from the conventional directory layout.

    Args:
        templates_roles_dir: Path to ``templates/roles/`` — the loader looks
            at the sibling ``skills/`` directory automatically.
        include_plugins: Whether to also load ``bernstein.skill_sources``
            entry points. Tests disable this to isolate behaviour.

    Returns:
        Configured :class:`SkillLoader`. When no skills dir exists and no
        plugins are installed, the loader is empty — callers treat this as
        "fall back to the legacy role template".
    """
    from pathlib import Path as _Path  # local import to keep top-level list short

    # templates/roles -> templates/skills
    roles_path = templates_roles_dir if isinstance(templates_roles_dir, _Path) else _Path(str(templates_roles_dir))
    skills_root = roles_path.parent / "skills"

    sources: list[SkillSource] = [
        LocalDirSkillSource(skills_root, source_name="local"),
    ]

    if include_plugins:
        from bernstein.core.skills.sources.plugin import load_plugin_sources

        sources.extend(load_plugin_sources())

    return SkillLoader(sources=sources)


def _resolve_reader(source: SkillSource, attr: str) -> _ReaderFn:
    """Duck-type a ``read_reference`` / ``read_script`` attribute off a source.

    We attach these methods by duck-typing (rather than adding new abstract
    methods on :class:`SkillSource`) so custom sources that cannot serve
    bucketed files — a remote MCP source, for instance — can simply omit the
    attribute. This helper centralises the lookup so the caller always sees
    a strictly typed ``Callable``.

    Args:
        source: The owning :class:`SkillSource`.
        attr:   ``"read_reference"`` or ``"read_script"``.

    Returns:
        The bound reader function, narrowed to :data:`_ReaderFn`.

    Raises:
        RuntimeError: When the source omits the attribute or exposes a
            non-callable value under that name.
    """
    raw = getattr(source, attr, None)
    if raw is None:
        bucket = "reference" if attr == "read_reference" else "script"
        raise RuntimeError(f"source {source.name!r} does not support {bucket} reads")
    if not callable(raw):
        raise RuntimeError(f"{attr} attribute on {source.name!r} is not callable: {raw!r}")
    return cast("_ReaderFn", raw)


def _call_reader(reader: _ReaderFn, name: str, path: str) -> str:
    """Invoke a bucket reader and validate the return type.

    The call is unconditional here — :func:`_resolve_reader` has already
    guaranteed ``reader`` is callable. We still check the return type so a
    misbehaving plugin cannot smuggle a non-string back to the caller.
    """
    result = reader(name, path)
    if not isinstance(result, str):
        raise RuntimeError(f"reader returned {type(result).__name__}, expected str")
    return result
