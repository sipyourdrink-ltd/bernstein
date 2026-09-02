"""Tests for the operator opt-in to harness-local discovery (#3969 P5).

``AgentDiscovery.discover_harness_local`` is off unless a caller passes
``enabled=True``, but until this change no operator-reachable surface could
pass it: ``bernstein agents discover`` only ever called ``full_sync`` with
the network flag. These tests drive the real CLI so the opt-in is asserted
where an operator hits it, not at the Python boundary underneath.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from click.testing import CliRunner

from bernstein.agents.agency_provider import compute_catalog_digest
from bernstein.cli.commands.agents_cmd import agents_group


def _registry_entries(fs_root: Path) -> list[dict[str, Any]]:
    """Read the directory entries the ``discover`` command wrote."""
    registry_path = fs_root / ".sdd" / "agents" / "registry.json"
    raw: dict[str, Any] = json.loads(registry_path.read_text(encoding="utf-8"))
    return list(raw["directories"])


def _write_harness_agent(root: Path) -> Path:
    """Create ``<root>/.claude/agents`` holding one parseable agent file."""
    claude_agents = root / ".claude" / "agents"
    claude_agents.mkdir(parents=True)
    (claude_agents / "reviewer.md").write_text(
        "---\nname: Code Reviewer\ndescription: Reviews code\n---\nPrompt",
        encoding="utf-8",
    )
    return claude_agents


def test_discover_without_the_flag_ingests_no_harness_resource(tmp_path: Path) -> None:
    """Default ``agents discover`` leaves harness-local resources untouched.

    The opt-in is the whole trust boundary: a third-party prompt sitting in
    the operator's own harness directory must not become a bernstein agent
    source because a routine discovery scan happened to run.
    """
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path) as fs:
        _write_harness_agent(Path(fs))
        with patch("pathlib.Path.home", return_value=Path(fs)):
            result = runner.invoke(agents_group, ["discover"])

    assert result.exit_code == 0, result.output
    assert "harness" not in result.output


def test_discover_with_the_flag_lists_source_path_and_digest(tmp_path: Path) -> None:
    """With the explicit flag, a discovered directory reports where and at what digest.

    The rendered listing is width-elided, so the full source path is asserted
    against the registry the command writes; the digest prefix is asserted in
    the operator-visible output.
    """
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path) as fs:
        claude_agents = _write_harness_agent(Path(fs))
        digest = compute_catalog_digest(claude_agents)
        with patch("pathlib.Path.home", return_value=Path(fs)):
            result = runner.invoke(agents_group, ["discover", "--harness-local"])
        assert result.exit_code == 0, result.output
        registry = _registry_entries(Path(fs))

    harness = [e for e in registry if str(e.get("name", "")).startswith("harness:")]
    assert harness, registry
    assert all(e["path"] == str(claude_agents) for e in harness)
    assert all(e["content_digest"] == digest for e in harness)
    assert digest[:12] in result.output


def test_discover_lists_a_refused_directory_as_refused(tmp_path: Path) -> None:
    """A directory failing digest verification is listed as refused, not dropped."""
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path) as fs:
        claude_agents = _write_harness_agent(Path(fs))
        (claude_agents / "agents.lock").write_text('{"content_digest": "invalid_digest_0000"}', encoding="utf-8")
        with patch("pathlib.Path.home", return_value=Path(fs)):
            result = runner.invoke(agents_group, ["discover", "--harness-local"])
        assert result.exit_code == 0, result.output
        registry = _registry_entries(Path(fs))

    harness = [e for e in registry if str(e.get("name", "")).startswith("harness:")]
    assert harness, registry
    assert all(e["path"] == str(claude_agents) for e in harness)
    assert all(e["enabled"] is False for e in harness)
    assert "refused" in result.output.lower()
