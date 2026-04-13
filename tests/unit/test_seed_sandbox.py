"""Unit tests for sandbox seed parsing."""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.seed import SeedError, parse_seed


@pytest.fixture()
def seed_file(tmp_path: Path) -> Path:
    """Return a temporary bernstein.yaml path."""

    return tmp_path / "bernstein.yaml"


def test_parse_seed_sandbox_mapping(seed_file: Path) -> None:
    """Seed parsing should load the typed sandbox section."""

    seed_file.write_text(
        "goal: ship sandboxing\n"
        "sandbox:\n"
        "  enabled: true\n"
        "  runtime: podman\n"
        "  image:\n"
        "    default: bernstein/base:latest\n"
        "    claude: bernstein/claude:latest\n"
        "  cpu_cores: 1.5\n"
        "  memory_mb: 2048\n"
        "  disk_mb: 1024\n"
        "  pids_limit: 128\n",
        encoding="utf-8",
    )

    seed = parse_seed(seed_file)

    assert seed.sandbox is not None
    assert seed.sandbox.runtime == "podman"
    assert seed.sandbox.image_for_adapter("claude") == "bernstein/claude:latest"
    assert seed.sandbox.image_for_adapter("codex") == "bernstein/base:latest"
    assert seed.sandbox.disk_mb == 1024


def test_parse_seed_sandbox_rejects_invalid_image_shape(seed_file: Path) -> None:
    """Non-string adapter image entries should fail parsing."""

    seed_file.write_text(
        "goal: ship sandboxing\nsandbox:\n  image:\n    default: bernstein/base:latest\n    claude: 123\n",
        encoding="utf-8",
    )

    with pytest.raises(SeedError, match="sandbox.image adapter entries must be strings"):
        parse_seed(seed_file)
