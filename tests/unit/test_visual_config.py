"""Tests for bernstein.core.visual_config."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bernstein.core.visual_config import VisualConfig, parse_visual_config, resolve_visual_config

if TYPE_CHECKING:
    from pathlib import Path


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


def test_parse_seed_includes_visual_section(tmp_path: Path) -> None:
    from bernstein.core.seed import parse_seed

    seed_file = tmp_path / "bernstein.yaml"
    seed_file.write_text(
        'goal: "Ship premium splash"\n'
        "visual:\n"
        "  splash: false\n"
        "  crt_effects: false\n"
        "  scanlines: true\n"
        "  splash_tier: tier2\n"
    )

    cfg = parse_seed(seed_file)

    assert cfg.visual is not None
    assert cfg.visual.splash is False
    assert cfg.visual.scanlines is True
    assert cfg.visual.splash_tier == "tier2"


def test_auto_write_bernstein_yaml_includes_visual_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.server_launch import auto_write_bernstein_yaml

    monkeypatch.setattr("bernstein.core.agent_discovery.generate_auto_routing_yaml", lambda: "cli: auto")
    auto_write_bernstein_yaml(tmp_path)

    content = (tmp_path / "bernstein.yaml").read_text()
    assert "visual:" in content
    assert "splash_tier: auto" in content
