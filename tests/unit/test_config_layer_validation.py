"""Unit tests for per-layer schema validation before merge (issue #5110 Slice 3)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bernstein.core.config.config_schema import (
    LayerValidationError,
    validate_layer_partial,
)
from bernstein.core.config.run_overlay import resolve_effective_mapping
from bernstein.core.config.seed_config import SeedError
from bernstein.core.config.seed_parser import parse_seed

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A minimal project with git structure and .sdd/config.yaml."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir(parents=True)
    sdd = tmp_path / ".sdd"
    sdd.mkdir(parents=True)
    (sdd / "config.yaml").write_text("cli: codex\nmax_agents: 3\n", encoding="utf-8")
    seed_file = tmp_path / "bernstein.yaml"
    seed_file.write_text("goal: Test goal\n", encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate tests from real ~/.bernstein and leaked environment variables."""
    monkeypatch.setenv("BERNSTEIN_HOME", str(tmp_path / "home"))
    for leaked in (
        "BERNSTEIN_CLI",
        "BERNSTEIN_EFFORT",
        "BERNSTEIN_MAX_AGENTS",
        "BERNSTEIN_MODEL",
        "BERNSTEIN_BUDGET",
        "BERNSTEIN_CONFIG_OVERLAY",
        "BERNSTEIN_CONFIG_OVERRIDE",
    ):
        monkeypatch.delenv(leaked, raising=False)
    yield


# -- validate_layer_partial direct tests --


def test_validate_layer_partial_valid_section() -> None:
    """A valid section dict passes without error."""
    validate_layer_partial({"quality_gates": {"enabled": True}}, layer_name="test")


def test_validate_layer_partial_invalid_section() -> None:
    """An invalid section value raises LayerValidationError."""
    with pytest.raises(LayerValidationError) as exc_info:
        validate_layer_partial({"quality_gates": {"enabled": "not_a_bool"}}, layer_name="test")
    assert "quality_gates.enabled" in str(exc_info.value)


def test_validate_layer_partial_invalid_scalar() -> None:
    """An invalid top-level scalar raises LayerValidationError."""
    with pytest.raises(LayerValidationError) as exc_info:
        validate_layer_partial({"max_agents": "bad_int"}, layer_name="test")
    assert "max_agents" in str(exc_info.value)


def test_validate_layer_partial_missing_required_fields_ignored() -> None:
    """Partial overlays with missing required fields pass (fields come from base)."""
    # RemoteSchema requires 'host', but a partial overlay may only set 'port'.
    validate_layer_partial({"remote": {"port": 2222}}, layer_name="test")
    # SmtpSchema requires 'host' and 'port', but a partial overlay may set only 'username'.
    validate_layer_partial({"smtp": {"username": "bot"}}, layer_name="test")


# -- resolve_effective_mapping integration tests --


def test_invalid_overlay_section_names_the_overlay_file(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed section in a run overlay fails pre-merge validation naming the file."""
    overlay_path = project / "overlay.yaml"
    overlay_path.write_text("quality_gates:\n  enabled: nope\n", encoding="utf-8")
    monkeypatch.setenv("BERNSTEIN_CONFIG_OVERLAY", str(overlay_path))

    seed_path = project / "bernstein.yaml"
    base = {"goal": "Test goal"}

    with pytest.raises(LayerValidationError) as exc_info:
        resolve_effective_mapping(base, config_path=seed_path)

    err_msg = str(exc_info.value)
    assert "run-overlay" in err_msg
    assert str(overlay_path) in err_msg
    assert "quality_gates.enabled" in err_msg


def test_invalid_inline_override_names_the_env_var(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed setting in $BERNSTEIN_CONFIG_OVERRIDE fails pre-merge validation."""
    monkeypatch.setenv("BERNSTEIN_CONFIG_OVERRIDE", '{"notify": {"webhook": 42}}')

    seed_path = project / "bernstein.yaml"
    base = {"goal": "Test goal"}

    with pytest.raises(LayerValidationError) as exc_info:
        resolve_effective_mapping(base, config_path=seed_path)

    err_msg = str(exc_info.value)
    assert "inline-override" in err_msg
    assert "$BERNSTEIN_CONFIG_OVERRIDE" in err_msg
    assert "notify.webhook" in err_msg


def test_valid_partial_layer_passes(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid partial overlay (e.g. only setting max_agents) succeeds."""
    overlay_path = project / "valid_overlay.yaml"
    overlay_path.write_text("max_agents: 4\n", encoding="utf-8")
    monkeypatch.setenv("BERNSTEIN_CONFIG_OVERLAY", str(overlay_path))

    seed_path = project / "bernstein.yaml"
    base = {"goal": "Base goal", "max_agents": 2}

    merged = resolve_effective_mapping(base, config_path=seed_path)
    assert merged["goal"] == "Base goal"
    assert merged["max_agents"] == 4


# -- parse_seed integration test --


def test_parse_seed_surfaces_layer_validation_error(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`parse_seed()` converts a pre-merge LayerValidationError into a SeedError."""
    overlay_path = project / "overlay_bad.yaml"
    overlay_path.write_text("quality_gates:\n  enabled: invalid_bool\n", encoding="utf-8")
    monkeypatch.setenv("BERNSTEIN_CONFIG_OVERLAY", str(overlay_path))

    seed_path = project / "bernstein.yaml"

    with pytest.raises(SeedError) as exc_info:
        parse_seed(seed_path)

    err_msg = str(exc_info.value)
    assert "run-overlay" in err_msg
    assert str(overlay_path) in err_msg
    assert "quality_gates.enabled" in err_msg
