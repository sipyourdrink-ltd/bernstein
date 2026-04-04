"""Unit tests for MCP auto-discovery registration/unregistration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from bernstein.cli.stop_cmd import _unregister_mcp_discovery
from bernstein.core.bootstrap import _register_mcp_discovery


def test_register_creates_mcp_json(tmp_path: Path) -> None:
    """register writes .claude/mcp.json with bernstein entry."""
    _register_mcp_discovery(tmp_path)

    mcp_path = tmp_path / ".claude" / "mcp.json"
    assert mcp_path.exists()
    data = json.loads(mcp_path.read_text())
    assert "bernstein" in data["mcpServers"]
    entry = data["mcpServers"]["bernstein"]
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "bernstein.mcp.server"]


def test_register_merges_with_existing(tmp_path: Path) -> None:
    """register preserves pre-existing MCP server entries."""
    mcp_path = tmp_path / ".claude" / "mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(json.dumps({"mcpServers": {"other": {"command": "other-cmd", "args": []}}}))

    _register_mcp_discovery(tmp_path)

    data = json.loads(mcp_path.read_text())
    assert "other" in data["mcpServers"]
    assert "bernstein" in data["mcpServers"]


def test_register_overwrites_stale_bernstein_entry(tmp_path: Path) -> None:
    """register replaces an existing (stale) bernstein entry with fresh config."""
    mcp_path = tmp_path / ".claude" / "mcp.json"
    mcp_path.parent.mkdir(parents=True)
    stale = {"mcpServers": {"bernstein": {"command": "/old/python", "args": ["-m", "old.module"]}}}
    mcp_path.write_text(json.dumps(stale))

    _register_mcp_discovery(tmp_path)

    data = json.loads(mcp_path.read_text())
    assert data["mcpServers"]["bernstein"]["command"] == sys.executable


def test_register_tolerates_corrupt_existing_file(tmp_path: Path) -> None:
    """register handles corrupt JSON in existing mcp.json gracefully."""
    mcp_path = tmp_path / ".claude" / "mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text("NOT JSON {{{")

    _register_mcp_discovery(tmp_path)  # must not raise

    data = json.loads(mcp_path.read_text())
    assert "bernstein" in data["mcpServers"]


def test_unregister_removes_bernstein_entry(tmp_path: Path) -> None:
    """unregister removes only the bernstein entry, leaving others intact."""
    mcp_path = tmp_path / ".claude" / "mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(
        json.dumps({"mcpServers": {"bernstein": {"command": "x"}, "other": {"command": "y"}}})
    )

    _unregister_mcp_discovery(tmp_path)

    data = json.loads(mcp_path.read_text())
    assert "bernstein" not in data["mcpServers"]
    assert "other" in data["mcpServers"]


def test_unregister_noop_when_no_mcp_json(tmp_path: Path) -> None:
    """unregister does nothing when .claude/mcp.json does not exist."""
    _unregister_mcp_discovery(tmp_path)  # must not raise


def test_unregister_noop_when_bernstein_not_present(tmp_path: Path) -> None:
    """unregister is idempotent when bernstein entry is already absent."""
    mcp_path = tmp_path / ".claude" / "mcp.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(json.dumps({"mcpServers": {"other": {"command": "y"}}}))

    _unregister_mcp_discovery(tmp_path)

    data = json.loads(mcp_path.read_text())
    assert "other" in data["mcpServers"]


def test_register_then_unregister_roundtrip(tmp_path: Path) -> None:
    """register followed by unregister leaves file with no bernstein entry."""
    _register_mcp_discovery(tmp_path)
    _unregister_mcp_discovery(tmp_path)

    mcp_path = tmp_path / ".claude" / "mcp.json"
    data = json.loads(mcp_path.read_text())
    assert "bernstein" not in data.get("mcpServers", {})
