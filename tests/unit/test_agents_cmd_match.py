"""Tests for `bernstein agents match` -- catalog-hit rendering (issue #3310).

`agents_match` renders a `rich.text.Text` summary when the catalog registry
finds a matching agent. Drive the command through its actual CLI surface
(not by importing the render logic directly) so a regression in the Text
API usage is caught the way an operator would hit it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bernstein.agents.catalog import CatalogAgent, CatalogRegistry
from bernstein.cli.commands.agents_cmd import agents_group


def _registry_with_agent(**overrides: object) -> CatalogRegistry:
    defaults: dict[str, object] = {
        "name": "BackendBot",
        "role": "backend",
        "description": "Backend engineer specializing in REST APIs.",
        "system_prompt": "You are a specialist.",
        "id": "test:backend-bot",
        "tools": ["pytest", "ruff"],
        "priority": 10,
        "source": "catalog",
    }
    defaults.update(overrides)
    registry = CatalogRegistry()
    registry.register_agent(CatalogAgent(**defaults))  # type: ignore[arg-type]
    return registry


def test_agents_match_renders_catalog_hit_without_error(tmp_path: Path) -> None:
    """A catalog hit must render successfully instead of raising AttributeError.

    Reproduces issue #3310: the catalog-hit rendering path called
    `Text.extend(...)`, which does not exist on `rich.text.Text`, so any
    `bernstein agents match` invocation that found a catalog agent crashed.
    """
    runner = CliRunner()
    registry = _registry_with_agent()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        with patch.object(CatalogRegistry, "default", classmethod(lambda cls: registry)):
            result = runner.invoke(agents_group, ["match", "--role", "backend"])

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "BackendBot" in result.output
    assert "pytest, ruff" in result.output


def test_agents_match_renders_catalog_hit_with_no_tools(tmp_path: Path) -> None:
    """The tools line falls back to '-' when the matched agent has none."""
    runner = CliRunner()
    registry = _registry_with_agent(name="NoToolsBot", tools=[])

    with runner.isolated_filesystem(temp_dir=tmp_path):
        with patch.object(CatalogRegistry, "default", classmethod(lambda cls: registry)):
            result = runner.invoke(agents_group, ["match", "--role", "backend"])

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "NoToolsBot" in result.output
