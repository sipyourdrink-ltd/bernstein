"""Role resolver — picks between a skill-pack index and the legacy role template.

Called from :mod:`bernstein.core.agents.spawn_prompt` during prompt
rendering. The resolver tries three things in order:

1. **Skill pack** (new; oai-004) — if ``templates/skills/<role>/SKILL.md``
   exists the resolver returns the compact index built by
   :func:`~bernstein.core.skills.build_skill_index` *plus* the matched
   skill's body as a "primary" hint. Downstream adapters inject this into
   the system prompt.
2. **Legacy role template** — ``templates/roles/<role>/system_prompt.md``
   rendered via the existing Jinja-like template engine. This preserves
   backwards compatibility during migration.
3. **Fallback stub** — ``"You are a {role} specialist."``

The resolver is stateless: each call walks the filesystem fresh. For
high-traffic spawn paths, ``_cached_loader`` caches the :class:`SkillLoader`
keyed on the templates directory mtime so we are not re-parsing 17
SKILL.md files on every spawn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from bernstein.core.skills.index_builder import build_skill_index
from bernstein.core.skills.loader import (
    SkillNotFoundError,
    default_loader_from_templates,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.skills.loader import SkillLoader

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedRolePrompt:
    """Result of :func:`resolve_role_prompt`.

    Attributes:
        body: The text the adapter injects for the role section.
        source: ``"skill"`` | ``"legacy"`` | ``"fallback"`` — used for
            observability and the token-reduction regression test.
        skill_name: Name of the skill whose index was injected, when
            ``source == "skill"``. ``None`` otherwise.
    """

    body: str
    source: str
    skill_name: str | None = None


# ``(templates_dir, mtime) -> SkillLoader`` — the mtime lets us pick up
# edits during dev without restarting.
_loader_cache: dict[tuple[str, float], SkillLoader] = {}


def resolve_role_prompt(
    role: str,
    *,
    templates_dir: Path,
    legacy_renderer: object | None = None,
    legacy_context: dict[str, str] | None = None,
    include_plugins: bool = True,
) -> ResolvedRolePrompt:
    """Return the role-section body + a tag identifying its source.

    Args:
        role: Role name (e.g. ``"backend"``).
        templates_dir: Path to ``templates/roles/``. The sibling
            ``templates/skills/`` is auto-discovered.
        legacy_renderer: Callable matching ``(role, context, templates_dir) -> str``
            used when the role is not skill-backed. Injected so tests can
            stub out the template system without loading the whole
            dependency graph. When ``None``, falls back to the canonical
            ``bernstein.templates.renderer.render_role_prompt``.
        legacy_context: Context dict for the legacy renderer.
        include_plugins: Whether to include third-party skill sources.

    Returns:
        :class:`ResolvedRolePrompt` — callers append its ``body`` into
        the system prompt's ``role`` section.
    """
    loader = _get_loader(templates_dir, include_plugins=include_plugins)

    if loader.has(role):
        try:
            skill = loader.get(role)
        except SkillNotFoundError:
            skill = None
        if skill is not None:
            index = build_skill_index(loader, highlight=role)
            body = _compose_skill_body(index=index, skill=skill)
            return ResolvedRolePrompt(body=body, source="skill", skill_name=role)

    legacy = _try_legacy(role, templates_dir, legacy_renderer, legacy_context)
    if legacy is not None:
        return ResolvedRolePrompt(body=legacy, source="legacy", skill_name=None)

    return ResolvedRolePrompt(
        body=f"You are a {role} specialist.",
        source="fallback",
        skill_name=None,
    )


def build_index_only(
    *,
    templates_dir: Path,
    include_plugins: bool = True,
) -> str:
    """Return just the skill-index markdown (no role body).

    Used by the token-reduction regression test and by the CLI's
    ``bernstein skills list`` command.

    Args:
        templates_dir: Path to ``templates/roles/``.
        include_plugins: Whether to include third-party skill sources.

    Returns:
        Index string, empty when no skills exist.
    """
    loader = _get_loader(templates_dir, include_plugins=include_plugins)
    return build_skill_index(loader)


def get_loader(
    templates_dir: Path,
    *,
    include_plugins: bool = True,
) -> SkillLoader:
    """Expose the cached loader for CLI / MCP use."""
    return _get_loader(templates_dir, include_plugins=include_plugins)


def invalidate_cache() -> None:
    """Clear the loader cache. Tests call this between runs."""
    _loader_cache.clear()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _get_loader(templates_dir: Path, *, include_plugins: bool) -> SkillLoader:
    """Return a loader, rebuilding when the skills dir mtime changed."""
    skills_root = templates_dir.parent / "skills"
    try:
        mtime = skills_root.stat().st_mtime if skills_root.exists() else 0.0
    except OSError:
        mtime = 0.0
    cache_key = (str(templates_dir), mtime)
    cached = _loader_cache.get(cache_key)
    if cached is not None:
        return cached
    # Clear older mtime entries for this templates_dir so we don't leak memory
    # when an agent edits skill files repeatedly in a long-running dev loop.
    for stale_key in [k for k in _loader_cache if k[0] == str(templates_dir)]:
        _loader_cache.pop(stale_key, None)
    loader = default_loader_from_templates(
        templates_dir,
        include_plugins=include_plugins,
    )
    _loader_cache[cache_key] = loader
    return loader


def _try_legacy(
    role: str,
    templates_dir: Path,
    legacy_renderer: object | None,
    legacy_context: dict[str, str] | None,
) -> str | None:
    """Render the legacy role template; return ``None`` when unavailable."""
    # Resolve the renderer lazily so unit tests can exercise the resolver
    # without importing Jinja / the template renderer at all.
    if legacy_renderer is None:
        from bernstein.templates.renderer import TemplateError, render_role_prompt

        renderer = render_role_prompt
        err_types: tuple[type[Exception], ...] = (FileNotFoundError, TemplateError)
    else:
        renderer = legacy_renderer  # type: ignore[assignment]
        err_types = (FileNotFoundError, Exception)

    context = legacy_context if legacy_context is not None else {}
    try:
        result: object = renderer(role, context, templates_dir=templates_dir)  # type: ignore[misc]
    except err_types as exc:
        logger.debug("Legacy role template unavailable for %s: %s", role, exc)
        return None

    if not isinstance(result, str):
        logger.warning(
            "Legacy renderer for role %s returned %s (expected str)",
            role,
            type(result).__name__,  # pyright: ignore[reportUnknownArgumentType]
        )
        return None
    return result


def _compose_skill_body(*, index: str, skill: _LoadedSkillLike) -> str:
    """Render the role section as a compact index plus a one-line primary hint.

    This is the progressive-disclosure payload: no skill body is included,
    only the directory of available skills and a pointer to the matched
    primary skill. Agents call ``load_skill(name=...)`` to read the full
    SKILL.md body on demand.

    Args:
        index: Index from :func:`build_skill_index` (marks the primary).
        skill: The matched :class:`LoadedSkill` — we reference its ``name``
            in the header so the agent knows who it is at a glance.
    """
    # One-line pointer; keep it terse — every byte multiplies by spawn count.
    hint = f'Role: {skill.name}. load_skill(name="{skill.name}").\n'
    return hint + index.strip() + "\n"


# Minimal duck-typed contract used by :func:`_compose_skill_body`.
# ``LoadedSkill`` lives in :mod:`loader`; we declare a Protocol here so
# the resolver stays free of runtime coupling. Read-only properties match
# ``LoadedSkill``'s frozen dataclass fields.
class _LoadedSkillLike(Protocol):
    """Contract: a loaded skill exposes a ``name`` and ``description``."""

    @property
    def name(self) -> str: ...  # pragma: no cover — structural only

    @property
    def description(self) -> str: ...  # pragma: no cover — structural only
