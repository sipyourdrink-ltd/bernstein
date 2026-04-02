"""Tests for OpenClaw bridge seed parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.seed import SeedError, parse_seed


@pytest.fixture()
def seed_file(tmp_path: Path) -> Path:
    """Return a temporary bernstein.yaml path."""
    return tmp_path / "bernstein.yaml"


def test_parse_openclaw_bridge_valid(seed_file: Path) -> None:
    """A valid bridges.openclaw block should parse into typed config."""
    seed_file.write_text(
        "goal: T\n"
        "bridges:\n"
        "  openclaw:\n"
        "    enabled: true\n"
        "    url: ws://127.0.0.1:18789\n"
        "    api_key: secret-token\n"
        "    agent_id: ops\n"
    )
    cfg = parse_seed(seed_file)

    assert cfg.bridges is not None
    assert cfg.bridges.openclaw is not None
    assert cfg.bridges.openclaw.enabled is True
    assert cfg.bridges.openclaw.url == "ws://127.0.0.1:18789"
    assert cfg.bridges.openclaw.agent_id == "ops"


def test_parse_openclaw_bridge_env_substitution(seed_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """${VAR} references should resolve from the environment."""
    monkeypatch.setenv("OPENCLAW_API_KEY", "env-secret")
    seed_file.write_text(
        "goal: T\n"
        "bridges:\n"
        "  openclaw:\n"
        "    enabled: true\n"
        "    url: ws://127.0.0.1:18789\n"
        "    api_key: ${OPENCLAW_API_KEY}\n"
        "    agent_id: ops\n"
    )

    cfg = parse_seed(seed_file)

    assert cfg.bridges is not None
    assert cfg.bridges.openclaw is not None
    assert cfg.bridges.openclaw.api_key == "env-secret"


def test_parse_openclaw_bridge_disabled_is_allowed(seed_file: Path) -> None:
    """Disabled bridge configs should not require runtime credentials."""
    seed_file.write_text("goal: T\nbridges:\n  openclaw:\n    enabled: false\n")

    cfg = parse_seed(seed_file)

    assert cfg.bridges is not None
    assert cfg.bridges.openclaw is not None
    assert cfg.bridges.openclaw.enabled is False


def test_parse_openclaw_bridge_invalid_mode_raises(seed_file: Path) -> None:
    """Only shared_workspace mode is supported by D04a."""
    seed_file.write_text(
        "goal: T\n"
        "bridges:\n"
        "  openclaw:\n"
        "    enabled: true\n"
        "    url: ws://127.0.0.1:18789\n"
        "    api_key: secret-token\n"
        "    agent_id: ops\n"
        "    workspace_mode: remote_workspace\n"
    )

    with pytest.raises(SeedError, match="shared_workspace"):
        parse_seed(seed_file)


def test_parse_openclaw_bridge_missing_required_fields_raises(seed_file: Path) -> None:
    """Enabled bridge configs must declare URL, API key, and agent_id."""
    seed_file.write_text(
        "goal: T\nbridges:\n  openclaw:\n    enabled: true\n    url: ws://127.0.0.1:18789\n    api_key: secret-token\n"
    )

    with pytest.raises(SeedError, match="agent_id"):
        parse_seed(seed_file)


def test_parse_openclaw_bridge_invalid_shape_raises(seed_file: Path) -> None:
    """Malformed bridges sections should fail loudly."""
    seed_file.write_text("goal: T\nbridges: bad\n")

    with pytest.raises(SeedError, match="bridges must be a mapping"):
        parse_seed(seed_file)
