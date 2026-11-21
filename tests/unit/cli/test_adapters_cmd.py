"""Tests for the ``bernstein adapters`` CLI group."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli


def test_adapters_list_outputs_table_with_min_count() -> None:
    """``bernstein adapters list`` lists at least 40 adapters from the registry."""
    runner = CliRunner()
    result = runner.invoke(cli, ["adapters", "list"])

    assert result.exit_code == 0, result.output
    # Title line includes the count; e.g. "Bernstein adapters (44)".
    assert "Bernstein adapters" in result.output
    # Sanity-check a handful of well-known adapter names appear in the table.
    for expected in ("claude", "codex", "gemini", "aider", "cursor", "generic"):
        assert expected in result.output, f"adapter {expected!r} missing from output"


def test_adapters_list_json_is_valid_and_has_min_40_entries() -> None:
    """``--json`` emits a parseable object with >=40 adapters."""
    runner = CliRunner()
    result = runner.invoke(cli, ["adapters", "list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, dict)
    assert payload["count"] == len(payload["adapters"])
    assert payload["count"] >= 40, f"expected >=40 adapters, got {payload['count']}"

    # Every row has the documented schema.
    expected_keys = {"name", "source", "binary", "status"}
    for row in payload["adapters"]:
        assert expected_keys.issubset(row.keys()), row
        assert row["status"] in {"installed", "missing", "n/a"}

    names = {row["name"] for row in payload["adapters"]}
    # Spot-check core adapters.
    assert {"claude", "codex", "gemini", "aider", "generic"}.issubset(names)


def test_adapters_group_registered_on_main_cli() -> None:
    """The ``adapters`` group is reachable from the top-level CLI."""
    runner = CliRunner()
    result = runner.invoke(cli, ["adapters", "--help"])

    assert result.exit_code == 0
    assert "list" in result.output


def test_adapters_list_counts_selectable_names_including_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Count selectable names, including generic and aliases, but not mock."""
    from bernstein.adapters import registry

    names = ("agy", "antigravity", "generic", "mock")
    monkeypatch.setattr(registry, "_ADAPTERS", {name: registry._ADAPTERS[name] for name in names})
    monkeypatch.setattr(registry, "_entrypoints_loaded", True)
    result = CliRunner().invoke(cli, ["adapters", "list"])

    assert result.exit_code == 0, result.output
    assert "Selectable CLI agent adapter names: 3" in result.output


def test_rich_help_counts_selectable_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Help uses the same live selection set as adapters list."""
    from bernstein.adapters import registry

    monkeypatch.setattr(registry, "selectable_adapter_names", lambda: frozenset({"claude", "codex"}))
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "2 selectable adapter names" in result.output
