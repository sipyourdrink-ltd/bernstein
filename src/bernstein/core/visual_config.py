"""Typed visual configuration for premium Bernstein CLI features."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

SplashTier = Literal["auto", "tier1", "tier2", "tier3"]


@dataclass(frozen=True)
class VisualConfig:
    """Visual feature flags used by splash and shutdown effects."""

    splash: bool = True
    crt_effects: bool = True
    scanlines: bool = False
    splash_tier: SplashTier = "auto"


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_VALID_TIERS = {"auto", "tier1", "tier2", "tier3"}


def _parse_bool(value: object, field_name: str) -> bool:
    """Parse a boolean-like value from seed or env config.

    Args:
        value: Raw config value.
        field_name: Field name for error messages.

    Returns:
        Parsed boolean value.

    Raises:
        ValueError: If the value is not boolean-like.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_VALUES:
            return True
        if lowered in _FALSE_VALUES:
            return False
    raise ValueError(f"{field_name} must be a boolean, got {value!r}")


def parse_visual_config(raw: object | None) -> VisualConfig:
    """Parse the optional ``visual`` config section.

    Args:
        raw: Raw section from ``bernstein.yaml``.

    Returns:
        Parsed :class:`VisualConfig`, or defaults when the section is missing.

    Raises:
        ValueError: If the section shape or values are invalid.
    """
    if raw is None:
        return VisualConfig()
    if not isinstance(raw, Mapping):
        raise ValueError("visual must be a mapping")

    data = cast("Mapping[str, object]", raw)
    splash = _parse_bool(data.get("splash", True), "visual.splash")
    crt_effects = _parse_bool(data.get("crt_effects", True), "visual.crt_effects")
    scanlines = _parse_bool(data.get("scanlines", False), "visual.scanlines")

    splash_tier_raw = data.get("splash_tier", "auto")
    if not isinstance(splash_tier_raw, str) or splash_tier_raw not in _VALID_TIERS:
        raise ValueError("visual.splash_tier must be one of auto, tier1, tier2, tier3")

    return VisualConfig(
        splash=splash,
        crt_effects=crt_effects,
        scanlines=scanlines,
        splash_tier=cast("SplashTier", splash_tier_raw),
    )


def resolve_visual_config(
    raw: VisualConfig | Mapping[str, object] | None = None,
    *,
    no_splash: bool = False,
    environ: Mapping[str, str] | None = None,
) -> VisualConfig:
    """Resolve defaults, config values, env overrides, and CLI overrides.

    Precedence: ``no_splash`` > env vars > supplied config > defaults.

    Args:
        raw: Either an existing :class:`VisualConfig`, a raw mapping, or None.
        no_splash: CLI override disabling the splash.
        environ: Optional env mapping for testing. Defaults to ``os.environ``.

    Returns:
        Fully resolved :class:`VisualConfig`.
    """
    env = environ if environ is not None else os.environ
    config = raw if isinstance(raw, VisualConfig) else parse_visual_config(raw)

    splash = config.splash
    crt_effects = config.crt_effects
    scanlines = config.scanlines
    splash_tier = config.splash_tier

    if _env_truthy(env.get("BERNSTEIN_NO_SPLASH")):
        splash = False
    if _env_truthy(env.get("BERNSTEIN_SCANLINES")):
        scanlines = True

    if no_splash:
        splash = False

    return VisualConfig(
        splash=splash,
        crt_effects=crt_effects,
        scanlines=scanlines,
        splash_tier=splash_tier,
    )


def _env_truthy(value: str | None) -> bool:
    """Return True when an env var should be treated as enabled."""
    return value is not None and value.strip().lower() in _TRUE_VALUES
