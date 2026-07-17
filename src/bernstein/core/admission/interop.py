"""Express the pre-existing per-dimension limiters as named admission limits.

Phase 3 of #2544 converges five scattered per-dimension limiters onto one
audited vocabulary. This module maps the provider spawn throttle
(:class:`bernstein.core.agents.spawn_rate_limiter.SpawnRateLimitConfig`) into
predefined named :class:`~bernstein.core.admission.models.RateLimitSpec`
entries *with no behavior regression*: the named limit's ceiling reproduces the
spawn throttle's per-provider ``max_spawns`` exactly, so an operator can reason
about spawn throttling through the same ``bernstein limits`` surface without
changing what the throttle admits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.admission.models import Posture, RateLimitSpec

if TYPE_CHECKING:
    from bernstein.core.agents.spawn_rate_limiter import SpawnRateLimitConfig

__all__ = ["SPAWN_LIMIT_PREFIX", "spawn_limit_name", "spawn_throttles_as_named_limits"]

#: Named-limit prefix for spawn throttles, so the converged vocabulary keeps a
#: single obvious namespace (``spawn-<provider>``).
SPAWN_LIMIT_PREFIX = "spawn-"


def spawn_limit_name(provider: str) -> str:
    """Return the named-limit name for a provider's spawn throttle."""
    return f"{SPAWN_LIMIT_PREFIX}{provider.strip().lower()}"


def spawn_throttles_as_named_limits(
    config: SpawnRateLimitConfig,
    *,
    providers: tuple[str, ...] = (),
    posture: Posture = Posture.ENFORCE,
) -> list[RateLimitSpec]:
    """Return named rate-limit specs equivalent to a spawn throttle config.

    Each spec's ``base_limit`` equals the spawn config's effective
    ``max_spawns`` for that provider (honouring ``per_provider_overrides``), so
    replacing the throttle with the named limit admits the identical count --
    no behavior regression. A ``default`` spec captures the base ceiling.

    Args:
        config: The spawn rate-limit configuration to express.
        providers: Provider names to emit explicit per-provider specs for. The
            per-provider overrides in the config are always included.
        posture: Enforcement posture for the emitted specs.
    """
    specs: list[RateLimitSpec] = [
        RateLimitSpec(name=spawn_limit_name("default"), base_limit=config.max_spawns, floor=1, posture=posture)
    ]
    seen: set[str] = {"default"}
    names = tuple(config.per_provider_overrides) + providers
    for provider in names:
        key = provider.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        limit = config.per_provider_overrides.get(provider, config.max_spawns)
        specs.append(RateLimitSpec(name=spawn_limit_name(provider), base_limit=limit, floor=1, posture=posture))
    return specs
