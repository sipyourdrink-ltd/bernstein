"""Abstract skill-source interface.

Every place skills can come from (bundled ``templates/skills/``, a pluggy
entry-point, a future MCP-mounted directory, …) implements
:class:`SkillSource`. Sources are merged by :class:`~bernstein.core.skills.loader.SkillLoader`
with explicit conflict detection.

Two concrete shapes:

- :class:`SkillSource`      — materialised upfront; returns manifests + bodies.
- :class:`LazySkillSource`  — returns manifests upfront but defers body
  reads until :meth:`load_body` is called.

The separation lets us index hundreds of plugin skills without paying for
their bodies unless the agent actually requests one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.skills.manifest import SkillManifest


@dataclass(frozen=True)
class SkillArtifact:
    """A fully materialised skill pack returned by a source.

    Attributes:
        manifest: Parsed ``SKILL.md`` frontmatter.
        body: Markdown body (the text after the closing ``---`` marker).
        origin: Human-readable location (filesystem path, plugin name, …).
            Shown in ``bernstein skills list`` and error messages.
    """

    manifest: SkillManifest
    body: str
    origin: str


class SkillSource(ABC):
    """A source of skill packs.

    Subclasses must implement :meth:`iter_skills`. The ``name`` property is
    used for error messages and conflict reporting.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable source identifier (e.g. ``local``, ``plugin:my-pack``)."""

    @abstractmethod
    def iter_skills(self) -> list[SkillArtifact]:
        """Return every skill this source provides.

        Returns:
            List of :class:`SkillArtifact` — sources return an empty list
            when they have no skills, never ``None``.

        Raises:
            bernstein.core.skills.manifest.SkillManifestError: When a skill
                directory cannot be parsed. The loader surfaces this as a
                startup-time failure so broken plugins cannot silently drop
                from the index.
        """


class LazySkillSource(SkillSource):
    """A skill source that defers body reads until requested.

    Useful for sources that can list manifests cheaply (e.g. reading only
    frontmatter from disk) but whose bodies are expensive — plugin packs
    fetched from URLs, for instance. :class:`SkillLoader` falls through to
    :meth:`load_body` when an agent calls ``load_skill``.
    """

    @abstractmethod
    def iter_manifests(self) -> list[tuple[SkillManifest, str]]:
        """Return ``(manifest, origin)`` pairs for every skill.

        Returns:
            One tuple per skill; ``origin`` is used for error reporting.
        """

    @abstractmethod
    def load_body(self, name: str) -> str:
        """Load the body for a skill by name.

        Args:
            name: Skill name (matches ``SkillManifest.name``).

        Returns:
            Markdown body text.

        Raises:
            KeyError: When the source does not own a skill with that name.
        """

    def iter_skills(self) -> list[SkillArtifact]:
        """Default :meth:`iter_skills` implementation — eagerly loads bodies.

        Subclasses that want true lazy behaviour should override this and
        return an empty list (the loader will fall back to
        :meth:`iter_manifests` + :meth:`load_body`).

        Returns:
            Fully materialised artifacts, one per skill.
        """
        artifacts: list[SkillArtifact] = []
        for manifest, origin in self.iter_manifests():
            body = self.load_body(manifest.name)
            artifacts.append(SkillArtifact(manifest=manifest, body=body, origin=origin))
        return artifacts
