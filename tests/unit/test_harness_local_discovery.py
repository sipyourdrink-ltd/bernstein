"""Unit tests for opt-in harness-local agent and skill discovery (#3975)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.agents.agency_provider import compute_catalog_digest
from bernstein.agents.discovery import AgentDiscovery


def _harness_dir_with_agent(tmp_path: Path) -> Path:
    """Create a harness-local agent directory holding one parseable agent file."""
    claude_agents = tmp_path / ".claude" / "agents"
    claude_agents.mkdir(parents=True)
    (claude_agents / "reviewer.md").write_text(
        "---\nname: Code Reviewer\ndescription: Reviews code\n---\nPrompt",
        encoding="utf-8",
    )
    return claude_agents


def _discover(tmp_path: Path) -> list:
    """Run harness-local discovery with home and project both anchored at *tmp_path*."""
    discovery = AgentDiscovery(registry_path=tmp_path / "registry.json", project_dir=tmp_path)
    with patch("pathlib.Path.home", return_value=tmp_path):
        return discovery.discover_harness_local(enabled=True)


def test_discovery_off_by_default_touches_nothing_outside_repo(tmp_path: Path) -> None:
    """Issue #3975: Harness-local discovery is OFF by default and returns [] when enabled=False without touching Path.home."""
    discovery = AgentDiscovery(registry_path=tmp_path / "registry.json")
    with patch("pathlib.Path.home", side_effect=RuntimeError("Path.home called when disabled")):
        entries = discovery.discover_harness_local(enabled=False)
    assert entries == []


def test_discovery_on_lists_harness_resources(tmp_path: Path) -> None:
    """Issue #3975: Explicit opt-in discovers harness-local agents and records entry."""
    claude_agents = tmp_path / ".claude" / "agents"
    claude_agents.mkdir(parents=True)
    (claude_agents / "reviewer.md").write_text(
        "---\nname: Code Reviewer\ndescription: Reviews code\n---\nPrompt",
        encoding="utf-8",
    )

    discovery = AgentDiscovery(registry_path=tmp_path / "registry.json", project_dir=tmp_path)

    with patch("pathlib.Path.home", return_value=tmp_path):
        entries = discovery.discover_harness_local(enabled=True)

    assert len(entries) >= 1
    harness_entry = next((e for e in entries if "harness:agents" in e.name), None)
    assert harness_entry is not None
    assert harness_entry.agents == 1
    assert harness_entry.enabled is True


def test_verification_failure_is_listed_as_refused(tmp_path: Path) -> None:
    """Issue #3975: A discovered resource failing lockfile verification is marked as refused (enabled=False, agents=0)."""
    claude_agents = tmp_path / ".claude" / "agents"
    claude_agents.mkdir(parents=True)
    (claude_agents / "reviewer.md").write_text(
        "---\nname: Code Reviewer\ndescription: Reviews code\n---\nPrompt",
        encoding="utf-8",
    )

    # Write mismatched lockfile
    lock_file = claude_agents / "agents.lock"
    lock_file.write_text(json.dumps({"content_digest": "invalid_digest_0000"}), encoding="utf-8")

    discovery = AgentDiscovery(registry_path=tmp_path / "registry.json", project_dir=tmp_path)

    with patch("pathlib.Path.home", return_value=tmp_path):
        entries = discovery.discover_harness_local(enabled=True)

    harness_entry = next((e for e in entries if "harness:agents" in e.name), None)
    assert harness_entry is not None
    assert harness_entry.enabled is False  # Refused due to digest mismatch
    assert harness_entry.agents == 0  # Directory walk was skipped on refusal


def test_matching_lockfile_admits_the_directory(tmp_path: Path) -> None:
    """A directory whose lockfile digest matches is admitted, not refused.

    Pins that discovery verifies with the same digest function the catalog
    lockfile is written from, so the two halves of the chain agree on what a
    clean directory looks like.
    """
    claude_agents = _harness_dir_with_agent(tmp_path)
    digest = compute_catalog_digest(claude_agents)
    (claude_agents / "agents.lock").write_text(
        json.dumps({"content_digest": digest}), encoding="utf-8"
    )

    entries = _discover(tmp_path)

    harness_entry = next((e for e in entries if "harness:agents" in e.name), None)
    assert harness_entry is not None
    assert harness_entry.enabled is True
    assert harness_entry.agents == 1


@pytest.mark.parametrize(
    ("case", "payload"),
    [
        ("malformed_json", b"{corrupt_json:"),
        ("no_content_digest", b'{"url": "https://example.com"}'),
        ("undecodable_bytes", b'{"content_digest": "\xff\xfe"}'),
    ],
)
def test_unusable_lockfile_refuses_the_directory(tmp_path: Path, case: str, payload: bytes) -> None:
    """A lockfile that is present but unusable refuses the directory instead of raising.

    Covers malformed JSON, a lockfile recording no digest, and bytes that are not
    valid UTF-8 - each must land on the refusal path rather than escaping and
    aborting the whole discovery sweep.
    """
    claude_agents = _harness_dir_with_agent(tmp_path)
    (claude_agents / "agents.lock").write_bytes(payload)

    entries = _discover(tmp_path)

    harness_entry = next((e for e in entries if "harness:agents" in e.name), None)
    assert harness_entry is not None, case
    assert harness_entry.enabled is False, case
    assert harness_entry.agents == 0, case
