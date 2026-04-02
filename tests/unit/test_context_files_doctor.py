"""Tests for context_files_doctor — doctor context warnings generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.context_files_doctor import (
    DoctorWarning,
    check_context_files,
    check_mcp_servers,
    check_permission_rules,
)

# --- Fixtures ---


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    """Create a minimal project directory."""
    return tmp_path


# --- TestCheckContextFiles ---


class TestCheckContextFiles:
    def test_no_context_files_ok(self, project_dir: Path) -> None:
        results = check_context_files(project_dir)
        assert len(results) == 1
        assert results[0].ok is True

    def test_empty_context_file_warning(self, project_dir: Path) -> None:
        (project_dir / "CLAUDE.md").write_text("")
        results = check_context_files(project_dir)
        warn = _first_bad(results)
        assert "empty" in warn.detail.lower()

    def test_invalid_json_context_file(self, project_dir: Path) -> None:
        settings = project_dir / ".claude"
        settings.mkdir(parents=True)
        (settings / "settings.json").write_text("{ invalid json")
        results = check_context_files(project_dir)
        warn = _first_bad(results)
        assert "invalid JSON" in warn.detail

    def test_large_context_file_warning(self, project_dir: Path) -> None:
        (project_dir / "AGENTS.md").write_text("x" * 101_000)
        results = check_context_files(project_dir)
        warn = _first_bad(results)
        assert "large file" in warn.detail.lower()

    def test_valid_markdown_passes(self, project_dir: Path) -> None:
        (project_dir / "CLAUDE.md").write_text("# Project\nHello world")
        (project_dir / "AGENTS.md").write_text("# Agents\nAll good")
        results = check_context_files(project_dir)
        assert all(w.ok for w in results)

    def test_empty_template_roles_dir(self, project_dir: Path) -> None:
        roles_dir = project_dir / "templates" / "roles"
        roles_dir.mkdir(parents=True)
        results = check_context_files(project_dir)
        warn = _first_bad(results)
        assert "no .md files" in warn.detail


# --- TestCheckMcpServers ---


class TestCheckMcpServers:
    def test_no_mcp_servers(self, project_dir: Path) -> None:
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text("{}")
        results = check_mcp_servers(project_dir)
        assert len(results) == 1
        assert results[0].ok is True

    def test_mcp_server_command_found(self, project_dir: Path) -> None:
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        settings = {
            "mcpServers": {
                "example": {"command": "python3", "args": ["-m", "example"]}
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        results = check_mcp_servers(project_dir)
        warn = _first_ok(results, "MCP server: example")
        assert warn is not None
        assert warn.ok is True

    def test_mcp_server_command_not_found(self, project_dir: Path) -> None:
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        settings = {
            "mcpServers": {
                "bad-server": {"command": "nonexistent-binary-xyz"}
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        results = check_mcp_servers(project_dir)
        warn = _first_bad(results)
        assert "not found in PATH" in warn.detail

    def test_mcp_server_missing_command(self, project_dir: Path) -> None:
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        settings = {"mcpServers": {"no-cmd": {}}}
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        results = check_mcp_servers(project_dir)
        warn = _first_bad(results)
        assert "no command" in warn.detail.lower()


# --- TestCheckPermissionRules ---


class TestCheckPermissionRules:
    def test_no_permission_files(self, project_dir: Path) -> None:
        results = check_permission_rules(project_dir)
        assert len(results) == 1
        assert results[0].ok is True

    def test_wildcard_deny_blocks_all(self, project_dir: Path) -> None:
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        settings = {
            "env": {
                "allow": [],
                "deny": ["*"],
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        results = check_permission_rules(project_dir)
        warn = _first_bad(results)
        assert "blocks everything" in warn.detail

    def test_negative_allow_pattern(self, project_dir: Path) -> None:
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        settings = {
            "env": {
                "allow": ["!/etc/passwd", "src/*"],
                "deny": [],
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        results = check_permission_rules(project_dir)
        warn = _first_bad(results)
        assert "negative allow" in warn.detail.lower()

    def test_valid_rules_pass(self, project_dir: Path) -> None:
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        settings = {
            "env": {
                "allow": ["src/*", "tests/*"],
                "deny": ["/etc/*"],
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        results = check_permission_rules(project_dir)
        # Should find a "no issues detected" warning (which is ok=True)
        assert any(w.ok for w in results)

    def test_env_not_object_skipped(self, project_dir: Path) -> None:
        """env key is not a dict — should be silently skipped."""
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text('{"env": "not-a-dict"}')
        results = check_permission_rules(project_dir)
        assert all(w.ok for w in results)


# --- Helpers ---


def _first_bad(results: list[DoctorWarning]) -> DoctorWarning:
    bad = [r for r in results if not r.ok]
    assert bad, f"No warning found in {results}"
    return bad[0]


def _first_ok(results: list[DoctorWarning], name: str) -> DoctorWarning | None:
    for r in results:
        if r.name == name and r.ok:
            return r
    return None
