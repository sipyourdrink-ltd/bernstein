"""Tests for ``bernstein graph tasks`` output modes."""

from __future__ import annotations

from unittest.mock import Mock, patch

from click.testing import CliRunner

from bernstein.cli.graph_cmd import graph_group


def _response_payload() -> dict[str, object]:
    return {
        "nodes": [
            {"id": "taska111", "title": "Bootstrap", "status": "done"},
            {"id": "taskb222", "title": "Build API", "status": "blocked"},
            {"id": "taskc333", "title": "Ship", "status": "open"},
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
    assert "Critical path" in result.output
    assert "Estimated duration: 55 min" in result.output
    assert "[BLOCKED]" in result.output
    assert "taska111 Bootstrap --> taskb222 Build API" in result.output


def test_graph_tasks_mermaid_outputs_flowchart() -> None:
    runner = CliRunner()

    with patch("httpx.get", return_value=_mock_response()):
        result = runner.invoke(graph_group, ["tasks", "--format", "mermaid"])

    assert result.exit_code == 0
    assert "flowchart TD" in result.output
    assert 'taska111["[DONE] Bootstrap"]' in result.output
    assert "classDef critical" in result.output
    assert "taska111 --> taskb222" in result.output
