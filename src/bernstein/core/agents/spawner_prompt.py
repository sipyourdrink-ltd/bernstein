"""Apply :class:`ModeProfile` to spawn-time prompts and tool allowlists.

This module is the integration glue between the routing-layer mode profiles
(:mod:`bernstein.core.routing.mode_profile`) and the agent spawner. It:
- Resolves the profile for a (model_id, task) pair.
- Loads YAML-defined profiles from ``templates/mode_profiles/`` once.
- Returns the prompt+tools+turn-budget bundle the spawner should hand to the
  CLI adapter.
- Records a Prometheus counter labelled by ``mode_profile``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein import _BUNDLED_TEMPLATES_DIR  # type: ignore[reportPrivateUsage]
from bernstein.core.defaults import MODE_PROFILES_ENABLED
from bernstein.core.observability.prometheus import agent_spawns_by_mode_total
from bernstein.core.routing.mode_profile import (
    AppliedMode,
    ModeProfile,
    apply_mode,
    install_loaded_profiles,
    load_profiles_from_dir,
    select_mode,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.tasks.models import Task

logger = logging.getLogger(__name__)

_LOAD_LOCK = threading.Lock()
_LOADED = False


def _profiles_dir(workdir: Path | None) -> Path:
    """Return the first existing profile directory in priority order."""
    candidates: list[Path] = []
    if workdir is not None:
        candidates.append(workdir / "templates" / "mode_profiles")
    candidates.append(_BUNDLED_TEMPLATES_DIR / "mode_profiles")
    for path in candidates:
        if path.is_dir():
            return path
    return candidates[-1]


def ensure_profiles_loaded(workdir: Path | None = None, *, force: bool = False) -> None:
    """Load YAML-defined profiles once (idempotent, thread-safe)."""
    global _LOADED
    if _LOADED and not force:
        return
    with _LOAD_LOCK:
        if _LOADED and not force:
            return
        path = _profiles_dir(workdir)
        loaded = load_profiles_from_dir(path)
        if loaded:
            install_loaded_profiles(loaded)
            logger.info("Loaded %d mode profiles from %s", len(loaded), path)
        _LOADED = True


@dataclass(frozen=True)
class SpawnModeBundle:
    """Outcome of applying a mode profile at spawn time.

    Attributes:
        profile: The :class:`ModeProfile` that was applied.
        prompt: System prompt with the profile preamble prepended.
        tools: Tool list filtered through the profile's allowlist.
        max_turns: Per-spawn turn budget from the profile.
        temperature: Sampling temperature target from the profile.
        top_p: Optional nucleus-sampling target (``None`` = provider default).
        top_k: Optional top-k target (``None`` = provider default).
        max_tokens: Optional output-token cap (``None`` = adapter default).
    """

    profile: ModeProfile
    prompt: str
    tools: list[str]
    max_turns: int
    temperature: float
    top_p: float | None = None
    top_k: int | None = None
    max_tokens: int | None = None


def apply_mode_to_spawn(
    *,
    model_id: str,
    prompt: str,
    tools: list[str] | None = None,
    task: Task | None = None,
    workdir: Path | None = None,
) -> SpawnModeBundle:
    """Resolve and apply the mode profile for a spawn.

    When :data:`MODE_PROFILES_ENABLED` is ``False`` the call is a no-op: the
    prompt and tool list are returned unchanged with the ``smart`` profile
    selected for accounting purposes.

    Args:
        model_id: Selected model identifier.
        prompt: Base system prompt before mode preamble.
        tools: Tool names available to the agent (allowlist input).
        task: Optional task; ``task.metadata['mode']`` overrides the default.
        workdir: Project root used to locate ``templates/mode_profiles/``.

    Returns:
        A :class:`SpawnModeBundle` ready to hand to the adapter.
    """
    available = list(tools or [])
    if not MODE_PROFILES_ENABLED:
        from bernstein.core.routing.mode_profile import MODE_REGISTRY

        smart = MODE_REGISTRY.get("smart")
        if smart is None:
            raise RuntimeError("smart profile missing from MODE_REGISTRY")
        return SpawnModeBundle(
            profile=smart,
            prompt=prompt,
            tools=available,
            max_turns=smart.max_turns,
            temperature=smart.temperature,
            top_p=smart.top_p,
            top_k=smart.top_k,
            max_tokens=smart.max_tokens,
        )

    ensure_profiles_loaded(workdir)

    profile = select_mode(model_id, task)
    applied: AppliedMode = apply_mode(profile, prompt=prompt, tools=available)

    try:
        agent_spawns_by_mode_total.labels(mode_profile=profile.name).inc()
    except Exception as exc:
        logger.debug("Failed to record mode_profile metric: %s", exc)

    return SpawnModeBundle(
        profile=applied.profile,
        prompt=applied.prompt,
        tools=applied.tools,
        max_turns=profile.max_turns,
        temperature=profile.temperature,
        top_p=profile.top_p,
        top_k=profile.top_k,
        max_tokens=profile.max_tokens,
    )


def resolve_profile(model_id: str, task: Task | None = None) -> ModeProfile:
    """Return the mode profile that would be applied for *model_id* and *task*."""
    return select_mode(model_id, task)


def reset_profile_cache() -> None:
    """Reset the one-shot YAML load flag (test helper)."""
    global _LOADED
    with _LOAD_LOCK:
        _LOADED = False


__all__ = [
    "SpawnModeBundle",
    "apply_mode_to_spawn",
    "ensure_profiles_loaded",
    "reset_profile_cache",
    "resolve_profile",
]
