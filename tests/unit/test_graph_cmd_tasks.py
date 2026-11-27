"""Tests for ``bernstein graph tasks`` output modes."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from bernstein.cli.graph_cmd import graph_group
from click.testing import CliRunner

_SHIP_TITLE = "Ship [bold] release notes for all supported platforms and list the architectures available on each one"


def _response_payload() -> dict[str, object]:
    return {
        "nodes": [
            {"id": "taska111", "title": "Bootstrap", "status": "done"},
            {"id": "taskb222", "title": "Build API", "status": "blocked"},
            {"id": "taskc333", "title": _SHIP_TITLE, "status": "open"},
        ],
        "edges": [
            {"from": "taska111", "to": "taskb222"},
            {"from": "taskb222", "to": "taskc333"},
        ],
        "critical_path": ["taska111", "taskb222", "taskc333"],
        "critical_path_minutes": 55,
        "bottlenecks": ["taskb222"],
    }


def _mock_response() -> Mock:
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = _response_payload()
    return response


def test_graph_tasks_ascii_shows_critical_path_and_blocked_nodes() -> None:
    runner = CliRunner()

    with patch("httpx.get", return_value=_mock_response()):
        result = runner.invoke(graph_group, ["tasks"])

    assert result.exit_code == 0
    assert "Task Dependency Graph" in result.stdout
    assert "Critical path" in result.output
    assert "Estimated duration: 55 min" in result.output
    assert "[BLOCKED]" in result.output
    assert "taska111 Bootstrap --> taskb222 Build API" in result.output


@pytest.mark.parametrize("columns", ["32", "160"])
def test_graph_tasks_mermaid_outputs_flowchart(columns: str) -> None:
    runner = CliRunner()

    with patch("httpx.get", return_value=_mock_response()):
        result = runner.invoke(graph_group, ["tasks", "--format", "mermaid"], env={"COLUMNS": columns})

    assert result.exit_code == 0
    assert result.stdout == (
        "flowchart TD\n"
        '    taska111["[DONE] Bootstrap"]\n'
        '    taskb222["[BLOCKED] Build API"]\n'
        f'    taskc333["[OPEN] {_SHIP_TITLE}"]\n'
        "    taska111 --> taskb222\n"
        "    taskb222 --> taskc333\n"
        "\n"
        "    classDef critical stroke:#d97706,stroke-width:3px;\n"
        "    class taska111,taskb222,taskc333 critical;\n"
    )
