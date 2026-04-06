"""Tests for worktree_claude_md — CLAUDE.md auto-injection per worktree."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.models import Scope, Task
from bernstein.core.worktree_claude_md import (
    _get_role_rules,
    generate_claude_md,
    write_claude_md,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    id: str = "T-001",
    title: str = "Implement feature",
    description: str = "Write the code.",
    role: str = "backend",
    scope: Scope = Scope.MEDIUM,
    priority: int = 2,
    owned_files: list[str] | None = None,
) -> Task:
    return Task(
        id=id,
        title=title,
        description=description,
        role=role,
        scope=scope,
        priority=priority,
        owned_files=owned_files or [],
    )


# ---------------------------------------------------------------------------
# generate_claude_md
# ---------------------------------------------------------------------------


class TestGenerateClaudeMd:
    def test_contains_header_with_role_and_session(self) -> None:
        task = _make_task()
        content = generate_claude_md([task], session_id="sess-1", role="backend", workdir=Path("/tmp"))
        assert "# Bernstein Agent: backend (sess-1)" in content

    def test_contains_task_instructions(self) -> None:
        task = _make_task(title="Fix auth parser", description="Parse JWT tokens correctly.")
        content = generate_claude_md([task], session_id="sess-1", role="backend", workdir=Path("/tmp"))
        assert "Fix auth parser" in content
        assert "Parse JWT tokens correctly." in content
        assert "T-001" in content

    def test_multiple_tasks(self) -> None:
        tasks = [
            _make_task(id="T-001", title="Task one"),
            _make_task(id="T-002", title="Task two"),
        ]
        content = generate_claude_md(tasks, session_id="sess-1", role="backend", workdir=Path("/tmp"))
        assert "Task 1: Task one" in content
        assert "Task 2: Task two" in content

    def test_contains_role_constraints(self) -> None:
        task = _make_task(role="qa")
        content = generate_claude_md([task], session_id="sess-1", role="qa", workdir=Path("/tmp"))
        assert "## Role constraints" in content
        assert "**qa** agent" in content

    def test_contains_allowed_file_paths(self) -> None:
        task = _make_task(owned_files=["src/foo.py", "src/bar.py"])
        content = generate_claude_md([task], session_id="sess-1", role="backend", workdir=Path("/tmp"))
        assert "## Allowed file paths" in content
        assert "`src/bar.py`" in content
        assert "`src/foo.py`" in content

    def test_no_allowed_files_section_when_empty(self) -> None:
        task = _make_task(owned_files=[])
        content = generate_claude_md([task], session_id="sess-1", role="backend", workdir=Path("/tmp"))
        assert "## Allowed file paths" not in content

    def test_contains_context_files(self) -> None:
        task = _make_task()
        content = generate_claude_md(
            [task],
            session_id="sess-1",
            role="backend",
            workdir=Path("/tmp"),
            context_files=["docs/architecture.md"],
        )
        assert "## Context files" in content
        assert "`docs/architecture.md`" in content

    def test_auto_includes_project_md_when_exists(self, tmp_path: Path) -> None:
        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        (sdd_dir / "project.md").write_text("project info", encoding="utf-8")

        task = _make_task()
        content = generate_claude_md([task], session_id="sess-1", role="backend", workdir=tmp_path)
        assert "`.sdd/project.md`" in content

    def test_contains_git_rules(self) -> None:
        task = _make_task()
        content = generate_claude_md([task], session_id="sess-1", role="backend", workdir=Path("/tmp"))
        assert "## Git rules" in content
        assert "agent/sess-1" in content

    def test_extra_instructions_appended(self) -> None:
        task = _make_task()
        content = generate_claude_md(
            [task],
            session_id="sess-1",
            role="backend",
            workdir=Path("/tmp"),
            extra_instructions="Always run tests before committing.",
        )
        assert "## Additional instructions" in content
        assert "Always run tests before committing." in content

    def test_no_extra_instructions_when_empty(self) -> None:
        task = _make_task()
        content = generate_claude_md([task], session_id="sess-1", role="backend", workdir=Path("/tmp"))
        assert "## Additional instructions" not in content


# ---------------------------------------------------------------------------
# write_claude_md
# ---------------------------------------------------------------------------


class TestWriteClaudeMd:
    def test_writes_file_to_worktree(self, tmp_path: Path) -> None:
        task = _make_task()
        result = write_claude_md(
            tmp_path,
            [task],
            session_id="sess-1",
            role="backend",
            workdir=Path("/tmp"),
        )
        assert result == tmp_path / "CLAUDE.md"
        assert result.exists()
        content = result.read_text(encoding="utf-8")
        assert "Bernstein Agent" in content

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("old content", encoding="utf-8")
        task = _make_task()
        write_claude_md(
            tmp_path,
            [task],
            session_id="sess-1",
            role="backend",
            workdir=Path("/tmp"),
        )
        content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        assert "old content" not in content
        assert "Bernstein Agent" in content


# ---------------------------------------------------------------------------
# _get_role_rules
# ---------------------------------------------------------------------------


class TestGetRoleRules:
    def test_known_roles_return_rules(self) -> None:
        for role in ("backend", "qa", "security", "docs", "frontend", "manager", "reviewer"):
            rules = _get_role_rules(role)
            assert len(rules) > 0, f"No rules for role: {role}"

    def test_unknown_role_returns_empty(self) -> None:
        assert _get_role_rules("unknown_custom_role") == []

    def test_case_insensitive(self) -> None:
        assert _get_role_rules("Backend") == _get_role_rules("backend")

    def test_qa_rules_include_read_only(self) -> None:
        rules = _get_role_rules("qa")
        joined = " ".join(rules).lower()
        assert "read" in joined or "not create" in joined.lower()
