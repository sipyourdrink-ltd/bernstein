"""Plugin skill source — loads third-party packs via setuptools entry points.

A plugin ships skills by registering a factory under the
``bernstein.skill_sources`` entry-point group. The factory returns a
:class:`~bernstein.core.skills.source.SkillSource` instance (usually a
:class:`~bernstein.core.skills.sources.local_dir.LocalDirSkillSource`
pointing at a directory bundled with the plugin wheel).

Example plugin ``pyproject.toml``::

    [project.entry-points."bernstein.skill_sources"]
    my-data-pack = "my_pack.skills:source"

Where ``my_pack/skills.py`` exposes::

    from pathlib import Path

    from bernstein.core.skills import SkillSource
    from bernstein.core.skills.sources import LocalDirSkillSource

    def source() -> SkillSource:
        return LocalDirSkillSource(
            Path(__file__).parent / "skills",
            source_name="plugin:my-data-pack",
        )

The helper :func:`load_plugin_sources` scans installed distributions and
returns every registered source, wrapping broken entry points in a clear
``ImportError`` so the operator sees which plugin failed.
"""

from __future__ import annotations

import logging
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING

from bernstein.core.skills.source import SkillArtifact, SkillSource

if TYPE_CHECKING:
    from collections.abc import Callable

#: Canonical entry-point group name (documented in docs/architecture/skills.md).
PLUGIN_ENTRY_POINT_GROUP: str = "bernstein.skill_sources"

logger = logging.getLogger(__name__)


class PluginSkillSource(SkillSource):
    """Wrap a factory-returned source with a plugin-qualified name.

    This exists because a plugin factory may return any
    :class:`SkillSource` — a :class:`LocalDirSkillSource`, a custom in-memory
    impl, whatever. Rather than require every plugin to name itself
    ``plugin:X``, we wrap the returned source and rename it so
    :class:`~bernstein.core.skills.loader.SkillLoader` sees ``plugin:<ep_name>``
    in conflict messages without coupling the plugin author to a
    naming convention.
    """

    def __init__(self, ep_name: str, inner: SkillSource) -> None:
        self._ep_name = ep_name
        self._inner = inner

    @property
    def name(self) -> str:
        return f"plugin:{self._ep_name}"

    @property
    def inner(self) -> SkillSource:
        """Expose the wrapped source so tests/tools can introspect it."""
        return self._inner

    def iter_skills(self) -> list[SkillArtifact]:
        return self._inner.iter_skills()


def load_plugin_sources(
    *,
    entry_point_group: str = PLUGIN_ENTRY_POINT_GROUP,
) -> list[SkillSource]:
    """Enumerate ``bernstein.skill_sources`` entry points and load them.

    Args:
        entry_point_group: Override the default group (used by tests).

    Returns:
        One :class:`PluginSkillSource` per successfully loaded plugin.
        Plugins that fail to import are logged but do not abort startup —
        a noisy third-party bug should not take down the orchestrator.
    """
    try:
        eps: tuple[EntryPoint, ...] = tuple(
            entry_points(group=entry_point_group)  # type: ignore[arg-type]
        )
    except TypeError:
        # Python 3.9/3.10 returned a SelectableGroups object — we target 3.12+,
        # but keep the fallback defensive for tooling on older interpreters.
        eps = tuple(entry_points().get(entry_point_group, ()))  # type: ignore[union-attr]

    sources: list[SkillSource] = []
    for ep in eps:
        try:
            factory = ep.load()
        except Exception as exc:
            logger.warning("Failed to load skill-source entry point %s: %s", ep.name, exc)
            continue

        source = _invoke_factory(ep.name, factory)
        if source is None:
            continue
        sources.append(PluginSkillSource(ep_name=ep.name, inner=source))
    return sources


def _invoke_factory(ep_name: str, factory: object) -> SkillSource | None:
    """Call a plugin factory and validate the returned object.

    Plugins may register either a zero-arg callable or a pre-built
    :class:`SkillSource` instance. Both are supported.

    Args:
        ep_name: Entry-point name (for logging).
        factory: Loaded attribute from the entry point.

    Returns:
        A :class:`SkillSource`, or ``None`` when the factory misbehaves
        (a warning is logged in that case).
    """
    if isinstance(factory, SkillSource):
        return factory

    if not callable(factory):
        logger.warning(
            "Skill-source entry point %s is neither callable nor SkillSource",
            ep_name,
        )
        return None

    try:
        # The cast is safe: we checked callability above.
        result = _call_factory(factory)
    except Exception as exc:
        logger.warning(
            "Skill-source factory for %s raised %s: %s",
            ep_name,
            type(exc).__name__,
            exc,
        )
        return None

    if not isinstance(result, SkillSource):
        logger.warning(
            "Skill-source factory for %s returned %s (expected SkillSource)",
            ep_name,
            type(result).__name__,
        )
        return None

    return result


def _call_factory(factory: object) -> object:
    """Call a callable factory with no arguments.

    Separated from :func:`_invoke_factory` to keep typing narrow.
    """
    # Pyright cannot narrow ``object`` to ``Callable`` across the isinstance
    # check performed by the caller, so cast here.
    call = cast_callable(factory)
    return call()


def cast_callable(obj: object) -> Callable[[], object]:
    """Narrow ``object`` to a zero-arg callable for pyright.

    Kept as a tiny helper so the cast is single-origin — we don't sprinkle
    ``cast`` calls across the module.
    """
    if not callable(obj):
        raise TypeError(f"expected callable, got {type(obj).__name__}")
    return obj  # pyright: ignore[reportReturnType]
