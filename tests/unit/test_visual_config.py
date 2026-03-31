"""Tests for bernstein.core.visual_config."""

from __future__ import annotations

import pytest

from bernstein.core.visual_config import VisualConfig, parse_visual_config, resolve_visual_config


def test_parse_visual_config_defaults() -> None:
    cfg = parse_visual_config(None)

    assert cfg == VisualConfig()


def test_parse_visual_config_mapping() -> None:
    cfg = parse_visual_config({"splash": False, "crt_effects": False, "scanlines": True, "splash_tier": "tier2"})

    assert cfg.splash is False
    assert cfg.crt_effects is False
    assert cfg.scanlines is True
    assert cfg.splash_tier == "tier2"


def test_parse_visual_config_invalid_tier_raises() -> None:
    with pytest.raises(ValueError, match="visual.splash_tier"):
        parse_visual_config({"splash_tier": "cinema"})


def test_resolve_visual_config_applies_env_and_flag_overrides() -> None:
    cfg = resolve_visual_config(
        {"scanlines": False},
        no_splash=True,
        environ={"BERNSTEIN_SCANLINES": "1"},
    )

    assert cfg.splash is False
    assert cfg.scanlines is True


def test_resolve_visual_config_accepts_existing_dataclass() -> None:
    cfg = resolve_visual_config(VisualConfig(splash=False, splash_tier="tier3"))

    assert cfg.splash is False
    assert cfg.splash_tier == "tier3"
