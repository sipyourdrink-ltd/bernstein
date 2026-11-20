"""Tests for the ``bernstein adapters`` CLI group."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click
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

    monkeypatch.setattr(registry, "_ADAPTERS", {"claude": object(), "codex": object()})
    monkeypatch.setattr(registry, "entry_points", lambda **kwargs: [])
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "2 selectable adapter names" in result.output


def test_help_reads_plugin_metadata_without_importing_the_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.adapters import registry

    class BrokenPlugin:
        name = "broken-plugin"

        def load(self) -> None:
            pytest.fail("root help imported a plugin implementation")

    monkeypatch.setattr(registry, "entry_points", lambda **kwargs: [BrokenPlugin()])
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    assert "1 additional plugin declaration" in result.output


@pytest.mark.parametrize("args", [("--help",), ("--version",), ("config-path", "--json")])
def test_lightweight_cli_defers_unrelated_imports(tmp_path: Path, args: tuple[str, ...]) -> None:
    """Check imports in a fresh process so test collection cannot preload them."""
    source = Path(__file__).resolve().parents[3] / "src"
    script = """
import sys

from bernstein.cli.main import cli

cli.main(args=sys.argv[1:], standalone_mode=False)
assert "bernstein.adapters.qwen" not in sys.modules
assert "openai" not in sys.modules
assert "bernstein.cli.run_bootstrap" not in sys.modules
assert "bernstein.cli.commands.task_cmd" not in sys.modules
assert "bernstein.cli.commands.adapter_cmd" not in sys.modules
assert "bernstein.cli.commands.resume_cmd" not in sys.modules
assert "bernstein.eval.bench.bench_cli" not in sys.modules
assert "bernstein.core.server.server_app" not in sys.modules
assert "bernstein.cli.commands.approval_cmd" not in sys.modules
assert "bernstein.cli.commands.compliance_cmd" not in sys.modules
assert "bernstein.cli.commands.governance_cmd" not in sys.modules
"""
    env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "PYTHONPATH": str(source),
    }
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    result = subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_root_completion_matches_click_without_loading_command_implementations(tmp_path: Path) -> None:
    from bernstein.cli.main import _completion_metadata

    with click.Context(cli) as ctx:
        actual = {item.value: item.help for item in cli.shell_complete(ctx, "")}
    expected = {name: cmd.get_short_help_str() for name, cmd in cli.commands.items() if not cmd.hidden}
    assert actual == expected, "regenerate with python scripts/gen_cli_completion.py"
    assert set(_completion_metadata()) == set(cli.commands)
    script = """
import click, sys
from bernstein.cli.main import cli
with click.Context(cli) as ctx:
    assert cli.shell_complete(ctx, "")
assert "bernstein.cli.run_bootstrap" not in sys.modules
assert "openai" not in sys.modules
"""
    env = dict(os.environ, HOME=str(tmp_path), PYTHONPATH=str(Path(__file__).resolve().parents[3] / "src"))
    subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, timeout=30, check=True)


def test_cli_value_completion_does_not_load_a_declared_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.adapters import registry
    from bernstein.cli.helpers import adapter_cli_choice

    loads: list[str] = []

    class BrokenPlugin:
        name = "fixture-plugin"

        def load(self) -> None:
            loads.append(self.name)
            raise ImportError("fixture plugin dependency is missing")

    monkeypatch.setattr(registry, "entry_points", lambda **kwargs: [BrokenPlugin()])
    monkeypatch.setattr(registry, "_entrypoints_loaded", False)
    monkeypatch.setattr(registry, "_loaded_plugin_names", set())
    choice = adapter_cli_choice()
    param = click.Option(["--cli"], type=choice)
    with click.Context(cli) as ctx:
        assert [item.value for item in choice.shell_complete(ctx, param, "fixt")] == ["fixture-plugin"]
        assert "fixture-plugin" in (choice.get_metavar(param, ctx) or "")
        assert choice.convert("codex", param, ctx) == "codex"
        assert registry.get_adapter("codex") is not None
        assert loads == []
        with pytest.raises(click.BadParameter):
            choice.convert("fixture-plugin", param, ctx)
    assert loads == ["fixture-plugin"]


def test_real_selection_keeps_last_valid_matching_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.adapters import registry
    from bernstein.adapters.generic import GenericAdapter
    from bernstein.core.config.seed_parser import _parse_cli

    class Plugin:
        def __init__(self, name: str, label: str) -> None:
            self.name = name
            self.label = label
            self.adapter = GenericAdapter(cli_command=label)

        def load(self) -> GenericAdapter:
            loads.append(self.label)
            return self.adapter

    first = Plugin("codex", "first")
    unrelated = Plugin("other", "other")
    last = Plugin("codex", "last")
    loads: list[str] = []
    monkeypatch.setattr(
        registry, "_ADAPTERS", registry._AdapterRegistry({"codex": "bernstein.adapters.codex:CodexAdapter"})
    )
    monkeypatch.setattr(registry, "entry_points", lambda **kwargs: [first, unrelated, last])
    monkeypatch.setattr(registry, "_entrypoints_loaded", False)
    monkeypatch.setattr(registry, "_loaded_plugin_names", set())

    assert _parse_cli({}) == "auto"
    assert loads == []
    assert _parse_cli({"cli": "codex"}) == "codex"
    assert registry.get_adapter("codex") is last.adapter
    assert loads == ["first", "last"]
    assert registry.selectable_adapter_names() == {"codex", "other"}
    assert loads == ["first", "last", "other"]
