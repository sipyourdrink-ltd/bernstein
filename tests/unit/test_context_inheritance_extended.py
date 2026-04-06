"""Tests for extended context inheritance (AGENT-012)."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.context_inheritance import (
    InheritedContext,
    build_subagent_context,
    inject_subagent_config,
)


class TestInheritedContext:
    def test_to_dict_roundtrip(self) -> None:
        ctx = InheritedContext(
            parent_session_id="sess-1",
            parent_role="backend",
            task_ids=["t-1", "t-2"],
            owned_files=["src/main.py"],
            role_constraints={"read_only": True},
            environment={"API_KEY": "test"},
            max_depth=5,
            current_depth=1,
        )
        data = ctx.to_dict()
        restored = InheritedContext.from_dict(data)
        assert restored.parent_session_id == "sess-1"
        assert restored.parent_role == "backend"
        assert restored.task_ids == ["t-1", "t-2"]
        assert restored.owned_files == ["src/main.py"]
        assert restored.role_constraints == {"read_only": True}
        assert restored.environment == {"API_KEY": "test"}
        assert restored.max_depth == 5
        assert restored.current_depth == 1

    def test_can_delegate_within_depth(self) -> None:
        ctx = InheritedContext(
            parent_session_id="s",
            parent_role="r",
            max_depth=3,
            current_depth=2,
        )
        assert ctx.can_delegate()

    def test_cannot_delegate_at_max_depth(self) -> None:
        ctx = InheritedContext(
            parent_session_id="s",
            parent_role="r",
            max_depth=3,
            current_depth=3,
        )
        assert not ctx.can_delegate()

    def test_child_context_increments_depth(self) -> None:
        parent = InheritedContext(
            parent_session_id="parent",
            parent_role="backend",
            task_ids=["t-1"],
            owned_files=["src/foo.py"],
            max_depth=5,
            current_depth=1,
        )
        child = parent.child_context("child-sess", "qa")
        assert child.parent_session_id == "child-sess"
        assert child.parent_role == "qa"
        assert child.current_depth == 2
        assert child.task_ids == ["t-1"]
        assert child.owned_files == ["src/foo.py"]

    def test_child_context_inherits_constraints(self) -> None:
        parent = InheritedContext(
            parent_session_id="p",
            parent_role="backend",
            role_constraints={"no_delete": True},
            environment={"TOKEN": "abc"},
        )
        child = parent.child_context("c", "qa")
        assert child.role_constraints == {"no_delete": True}
        assert child.environment == {"TOKEN": "abc"}

    def test_from_dict_with_defaults(self) -> None:
        ctx = InheritedContext.from_dict({})
        assert ctx.parent_session_id == ""
        assert ctx.max_depth == 3
        assert ctx.current_depth == 0


class TestBuildSubagentContext:
    def test_includes_session_and_role(self) -> None:
        text = build_subagent_context(
            session_id="sess-1",
            role="backend",
            task_ids=[],
            owned_files=[],
        )
        assert "backend" in text
        assert "sess-1" in text

    def test_includes_task_ids(self) -> None:
        text = build_subagent_context(
            session_id="sess-1",
            role="qa",
            task_ids=["task-1", "task-2"],
            owned_files=[],
        )
        assert "task-1" in text
        assert "task-2" in text

    def test_includes_owned_files(self) -> None:
        text = build_subagent_context(
            session_id="sess-1",
            role="backend",
            task_ids=[],
            owned_files=["src/main.py", "src/lib.py"],
        )
        assert "src/main.py" in text
        assert "src/lib.py" in text


class TestInjectSubagentConfig:
    def test_writes_claude_md(self, tmp_path: Path) -> None:
        inject_subagent_config(
            tmp_path,
            session_id="sess-1",
            role="backend",
            task_ids=["t-1"],
            owned_files=["src/main.py"],
        )
        claude_md = tmp_path / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text()
        assert "Bernstein orchestration context" in content

    def test_writes_settings_json(self, tmp_path: Path) -> None:
        inject_subagent_config(
            tmp_path,
            session_id="sess-1",
            role="qa",
            task_ids=["t-2"],
            owned_files=[],
        )
        settings = tmp_path / ".claude" / "settings.local.json"
        assert settings.exists()
        data = json.loads(settings.read_text())
        assert data["bernstein_context"]["parent_session_id"] == "sess-1"
        assert data["bernstein_context"]["parent_role"] == "qa"

    def test_idempotent_injection(self, tmp_path: Path) -> None:
        inject_subagent_config(
            tmp_path,
            session_id="sess-1",
            role="backend",
            task_ids=[],
            owned_files=[],
        )
        inject_subagent_config(
            tmp_path,
            session_id="sess-1",
            role="backend",
            task_ids=[],
            owned_files=[],
        )
        content = (tmp_path / "CLAUDE.md").read_text()
        assert content.count("Bernstein orchestration context") == 1
