"""Tests for context_inheritance — subagent context injection via CLAUDE.md and settings.local.json."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.context_inheritance import (
    _update_settings_json,
    build_subagent_context,
    inject_subagent_config,
)

# ---------------------------------------------------------------------------
# build_subagent_context
# ---------------------------------------------------------------------------


class TestBuildSubagentContext:
    def test_contains_session_and_role(self) -> None:
        ctx = build_subagent_context(
            session_id="backend-abc123",
            role="backend",
            task_ids=["T-001"],
            owned_files=["src/foo.py"],
        )
        assert "backend-abc123" in ctx
        assert "backend" in ctx

    def test_contains_task_ids(self) -> None:
        ctx = build_subagent_context(
            session_id="s1",
            role="backend",
            task_ids=["T-001", "T-002"],
            owned_files=[],
        )
        assert "`T-001`" in ctx
        assert "`T-002`" in ctx

    def test_contains_owned_files(self) -> None:
        ctx = build_subagent_context(
            session_id="s1",
            role="backend",
            task_ids=[],
            owned_files=["src/foo.py", "src/bar.py"],
        )
        assert "`src/bar.py`" in ctx
        assert "`src/foo.py`" in ctx

    def test_contains_coordination_rules(self) -> None:
        ctx = build_subagent_context(
            session_id="s1",
            role="backend",
            task_ids=[],
            owned_files=[],
        )
        assert "Coordination rules" in ctx
        assert "Do NOT call the task server" in ctx

    def test_deduplicates_owned_files(self) -> None:
        ctx = build_subagent_context(
            session_id="s1",
            role="backend",
            task_ids=[],
            owned_files=["src/foo.py", "src/foo.py", "src/bar.py"],
        )
        # Count occurrences — should only appear once each
        assert ctx.count("`src/foo.py`") == 1

    def test_no_task_ids_section_when_empty(self) -> None:
        ctx = build_subagent_context(
            session_id="s1",
            role="backend",
            task_ids=[],
            owned_files=[],
        )
        assert "Parent task IDs" not in ctx

    def test_no_file_ownership_section_when_empty(self) -> None:
        ctx = build_subagent_context(
            session_id="s1",
            role="backend",
            task_ids=[],
            owned_files=[],
        )
        assert "File ownership rules" not in ctx

    def test_custom_server_url(self) -> None:
        ctx = build_subagent_context(
            session_id="s1",
            role="backend",
            task_ids=[],
            owned_files=[],
            server_url="http://custom:9999",
        )
        assert "http://custom:9999" in ctx


# ---------------------------------------------------------------------------
# inject_subagent_config
# ---------------------------------------------------------------------------


class TestInjectSubagentConfig:
    def test_creates_claude_md_with_context(self, tmp_path: Path) -> None:
        inject_subagent_config(
            tmp_path,
            session_id="s1",
            role="backend",
            task_ids=["T-001"],
            owned_files=["src/foo.py"],
        )
        claude_md = tmp_path / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text(encoding="utf-8")
        assert "Bernstein orchestration context (inherited)" in content
        assert "`T-001`" in content

    def test_appends_to_existing_claude_md(self, tmp_path: Path) -> None:
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Existing CLAUDE.md\n\nSome instructions.\n", encoding="utf-8")

        inject_subagent_config(
            tmp_path,
            session_id="s1",
            role="qa",
            task_ids=["T-002"],
            owned_files=[],
        )
        content = claude_md.read_text(encoding="utf-8")
        assert "# Existing CLAUDE.md" in content
        assert "Bernstein orchestration context (inherited)" in content

    def test_idempotent_injection(self, tmp_path: Path) -> None:
        inject_subagent_config(tmp_path, session_id="s1", role="backend", task_ids=[], owned_files=[])
        inject_subagent_config(tmp_path, session_id="s1", role="backend", task_ids=[], owned_files=[])
        content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        # Marker should appear exactly once
        assert content.count("Bernstein orchestration context (inherited)") == 1

    def test_writes_settings_json(self, tmp_path: Path) -> None:
        inject_subagent_config(
            tmp_path,
            session_id="s1",
            role="backend",
            task_ids=["T-001"],
            owned_files=["src/foo.py"],
        )
        settings_path = tmp_path / ".claude" / "settings.local.json"
        assert settings_path.exists()
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert "bernstein_context" in data
        assert data["bernstein_context"]["parent_session_id"] == "s1"
        assert data["bernstein_context"]["parent_role"] == "backend"
        assert data["bernstein_context"]["task_ids"] == ["T-001"]
        assert data["bernstein_context"]["owned_files"] == ["src/foo.py"]


# ---------------------------------------------------------------------------
# _update_settings_json
# ---------------------------------------------------------------------------


class TestUpdateSettingsJson:
    def test_creates_settings_file(self, tmp_path: Path) -> None:
        _update_settings_json(
            tmp_path,
            session_id="s1",
            role="backend",
            task_ids=["T-001"],
            owned_files=["src/foo.py"],
        )
        settings_path = tmp_path / ".claude" / "settings.local.json"
        assert settings_path.exists()
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["bernstein_context"]["parent_session_id"] == "s1"

    def test_preserves_existing_settings(self, tmp_path: Path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir(parents=True)
        settings_path = settings_dir / "settings.local.json"
        existing = {"hooks": {"PostToolUse": [{"matcher": "", "hooks": []}]}, "custom_key": "value"}
        settings_path.write_text(json.dumps(existing), encoding="utf-8")

        _update_settings_json(
            tmp_path,
            session_id="s1",
            role="qa",
            task_ids=[],
            owned_files=[],
        )
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert data["custom_key"] == "value"
        assert "hooks" in data
        assert "bernstein_context" in data

    def test_handles_corrupt_settings(self, tmp_path: Path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir(parents=True)
        (settings_dir / "settings.local.json").write_text("not json", encoding="utf-8")

        _update_settings_json(
            tmp_path,
            session_id="s1",
            role="backend",
            task_ids=[],
            owned_files=[],
        )
        data = json.loads((settings_dir / "settings.local.json").read_text(encoding="utf-8"))
        assert "bernstein_context" in data
