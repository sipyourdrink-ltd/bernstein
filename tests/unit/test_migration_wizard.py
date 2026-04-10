"""Tests for migration wizard (road-016)."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.migration_wizard import (
    MigrationResult,
    MigrationSource,
    convert_crewai_config,
    convert_langgraph_config,
    detect_framework,
)


def test_detect_crewai(tmp_path: Path) -> None:
    """detect_framework finds CrewAI imports."""
    py = tmp_path / "app.py"
    py.write_text("from crewai import Agent, Task\n")
    assert detect_framework(tmp_path) == MigrationSource.CREWAI


def test_detect_langgraph(tmp_path: Path) -> None:
    """detect_framework finds LangGraph imports."""
    py = tmp_path / "graph.py"
    py.write_text("from langgraph.graph import StateGraph\n")
    assert detect_framework(tmp_path) == MigrationSource.LANGGRAPH


def test_detect_crewai_preferred_over_langgraph(tmp_path: Path) -> None:
    """When both frameworks are present, CrewAI is preferred."""
    (tmp_path / "a.py").write_text("import crewai\n")
    (tmp_path / "b.py").write_text("import langgraph\n")
    assert detect_framework(tmp_path) == MigrationSource.CREWAI


def test_detect_unknown(tmp_path: Path) -> None:
    """detect_framework returns None for unknown projects."""
    (tmp_path / "main.py").write_text("import flask\n")
    assert detect_framework(tmp_path) is None


def test_detect_nonexistent_dir(tmp_path: Path) -> None:
    """detect_framework returns None for nonexistent directory."""
    assert detect_framework(tmp_path / "nope") is None


def test_convert_crewai_basic(tmp_path: Path) -> None:
    """convert_crewai_config maps agents/tasks to Bernstein plan."""
    config = tmp_path / "crewai.yaml"
    config.write_text(
        """\
agents:
  - name: alice
    role: researcher
  - name: bob
    role: developer
tasks:
  - description: Research the topic
    agent: alice
  - description: Implement the feature
    agent: bob
"""
    )
    result = convert_crewai_config(config)
    assert isinstance(result, MigrationResult)
    assert result.source == MigrationSource.CREWAI
    assert result.tasks_converted == 2
    assert "Research the topic" in result.bernstein_yaml
    assert "Implement the feature" in result.bernstein_yaml


def test_convert_crewai_empty_tasks(tmp_path: Path) -> None:
    """convert_crewai_config warns when no tasks are found."""
    config = tmp_path / "crewai.yaml"
    config.write_text("agents:\n  - name: alice\n    role: qa\n")
    result = convert_crewai_config(config)
    assert result.tasks_converted == 0
    assert any("No tasks" in w for w in result.warnings)


def test_convert_crewai_expected_output_warning(tmp_path: Path) -> None:
    """convert_crewai_config warns about expected_output mapping."""
    config = tmp_path / "crewai.yaml"
    config.write_text(
        """\
agents:
  - name: writer
    role: writer
tasks:
  - description: Write docs
    agent: writer
    expected_output: A markdown document
"""
    )
    result = convert_crewai_config(config)
    assert result.tasks_converted == 1
    assert any("quality gate" in w for w in result.warnings)


def test_convert_crewai_invalid_yaml(tmp_path: Path) -> None:
    """convert_crewai_config handles non-mapping YAML."""
    config = tmp_path / "bad.yaml"
    config.write_text("- just\n- a\n- list\n")
    result = convert_crewai_config(config)
    assert result.tasks_converted == 0
    assert any("not a valid" in w for w in result.warnings)


def test_convert_langgraph_basic(tmp_path: Path) -> None:
    """convert_langgraph_config extracts nodes from add_node calls."""
    config = tmp_path / "graph.py"
    config.write_text(
        """\
from langgraph.graph import StateGraph

graph = StateGraph()
graph.add_node("research", research_fn)
graph.add_node("draft", draft_fn)
graph.add_node("review", review_fn)
graph.add_edge("research", "draft")
graph.add_edge("draft", "review")
"""
    )
    result = convert_langgraph_config(config)
    assert isinstance(result, MigrationResult)
    assert result.source == MigrationSource.LANGGRAPH
    assert result.tasks_converted == 3
    assert "research" in result.bernstein_yaml
    assert "review" in result.bernstein_yaml


def test_convert_langgraph_no_nodes(tmp_path: Path) -> None:
    """convert_langgraph_config warns when no nodes are found."""
    config = tmp_path / "empty.py"
    config.write_text("# no graph here\n")
    result = convert_langgraph_config(config)
    assert result.tasks_converted == 0
    assert any("No graph nodes" in w for w in result.warnings)
