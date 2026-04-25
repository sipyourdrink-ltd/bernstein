"""Tests for the fleet config loader."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.fleet.config import (
    FleetConfig,
    load_projects_config,
    parse_projects_config,
)


def test_parse_minimal(tmp_path: Path) -> None:
    """A single ``[[project]]`` block parses with safe defaults."""
    text = f"""
[[project]]
path = "{tmp_path}"
"""
    config = parse_projects_config(text)
    assert len(config.projects) == 1
    assert config.errors == []
    project = config.projects[0]
    assert project.name == tmp_path.name
    assert project.task_server_url.startswith("http://127.0.0.1:")
    assert project.sdd_dir == project.path / ".sdd"


def test_parse_explicit_name_and_url(tmp_path: Path) -> None:
    text = f"""
[[project]]
name = "alpha"
path = "{tmp_path}"
task_server_url = "http://127.0.0.1:8080"
"""
    config = parse_projects_config(text)
    assert len(config.projects) == 1
    assert config.projects[0].name == "alpha"
    assert config.projects[0].task_server_url == "http://127.0.0.1:8080"


def test_missing_path_yields_validation_error(tmp_path: Path) -> None:
    """Missing ``path`` records a non-fatal error rather than crashing."""
    text = """
[[project]]
name = "broken"
"""
    config = parse_projects_config(text)
    assert config.projects == []
    assert any("path" in err.message for err in config.errors)


def test_duplicate_names_flagged(tmp_path: Path) -> None:
    text = f"""
[[project]]
name = "shared"
path = "{tmp_path}"

[[project]]
name = "shared"
path = "{tmp_path}"
"""
    config = parse_projects_config(text)
    assert len(config.projects) == 1
    assert any("duplicate" in err.message for err in config.errors)


def test_invalid_toml_records_global_error() -> None:
    config = parse_projects_config("[[project")
    assert config.projects == []
    assert any(err.index == -1 for err in config.errors)


def test_load_missing_file_returns_error(tmp_path: Path) -> None:
    cfg = load_projects_config(tmp_path / "does-not-exist.toml")
    assert isinstance(cfg, FleetConfig)
    assert cfg.projects == []
    assert any("not found" in err.message for err in cfg.errors)
