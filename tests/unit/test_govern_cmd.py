"""CLI tests for ``bernstein govern inventory --render`` (#5133).

Idiom copied from ``tests/unit/test_graph_cmd_tasks.py``: CliRunner against
the command group, assert flowchart text in stdout.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.govern_cmd import govern_group
from bernstein.cli.main import cli

FIXTURE_STORE = Path(__file__).resolve().parents[1] / "fixtures" / "govern" / "inventory-store.json"


def test_govern_help_lists_inventory_render() -> None:
    result = CliRunner().invoke(cli, ["govern", "inventory", "--help"])
    assert result.exit_code == 0, result.output
    assert "--render" in result.output
    assert "mermaid" in result.output
    assert "dot" in result.output


def test_govern_inventory_mermaid_outputs_flowchart() -> None:
    runner = CliRunner()
    result = runner.invoke(
        govern_group,
        ["inventory", "--render", "mermaid", "--store", str(FIXTURE_STORE)],
    )
    assert result.exit_code == 0, result.output
    assert "flowchart TD" in result.output
    assert 'n0["agent_claude: Claude Code"]' in result.output
    assert "n2 --> n0" in result.output
    assert "classDef" not in result.output


def test_govern_inventory_dot_outputs_digraph() -> None:
    runner = CliRunner()
    result = runner.invoke(
        govern_group,
        ["inventory", "--render", "dot", "--store", str(FIXTURE_STORE)],
    )
    assert result.exit_code == 0, result.output
    assert "digraph inventory" in result.output
    assert '"agent_claude" [label="Claude Code"];' in result.output
    assert '"host_dev" -> "agent_claude";' in result.output


def test_render_is_required() -> None:
    result = CliRunner().invoke(
        govern_group,
        ["inventory", "--store", str(FIXTURE_STORE)],
    )
    assert result.exit_code == 2
    assert "--render" in result.output


def test_malformed_store_exits_one(tmp_path: Path) -> None:
    bad = tmp_path / "store.json"
    bad.write_text("[]", encoding="utf-8")
    result = CliRunner().invoke(
        govern_group,
        ["inventory", "--render", "mermaid", "--store", str(bad)],
    )
    assert result.exit_code == 1
    assert "JSON object" in result.output


def test_non_list_nodes_exits_one(tmp_path: Path) -> None:
    bad = tmp_path / "store.json"
    bad.write_text('{"nodes": {}, "edges": []}', encoding="utf-8")
    result = CliRunner().invoke(
        govern_group,
        ["inventory", "--render", "mermaid", "--store", str(bad)],
    )
    assert result.exit_code == 1
    assert "nodes must be a list" in result.output
