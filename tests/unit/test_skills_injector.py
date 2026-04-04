"""Tests for per-task Claude Code skill injection."""

from __future__ import annotations

from pathlib import Path

from bernstein.adapters.skills_injector import (
    ROLE_SKILL_MAP,
    inject_skills,
    render_skill_template,
)
from bernstein.core.models import Task


def _make_task(id: str = "T-001", title: str = "Test task") -> Task:
    return Task(id=id, title=title, description="A test task", role="backend")


class TestRenderSkillTemplate:
    def test_replaces_session_id(self) -> None:
        content = "Check signals at .sdd/runtime/signals/{{SESSION_ID}}/WAKEUP"
        result = render_skill_template(content, session_id="backend-abc123")
        assert "backend-abc123" in result
        assert "{{SESSION_ID}}" not in result

    def test_replaces_complete_cmds_with_task_curl(self) -> None:
        tasks = [_make_task(id="T-001", title="Fix bug")]
        result = render_skill_template("{{COMPLETE_CMDS}}", tasks=tasks)
        assert "T-001" in result
        assert "curl" in result
        assert "/complete" in result

    def test_replaces_complete_cmds_for_multiple_tasks(self) -> None:
        tasks = [
            _make_task(id="T-001", title="First task"),
            _make_task(id="T-002", title="Second task"),
        ]
        result = render_skill_template("{{COMPLETE_CMDS}}", tasks=tasks)
        assert "T-001" in result
        assert "T-002" in result

    def test_replaces_task_ids(self) -> None:
        tasks = [_make_task(id="T-001"), _make_task(id="T-002")]
        result = render_skill_template("Tasks: {{TASK_IDS}}", tasks=tasks)
        assert "T-001" in result
        assert "T-002" in result

    def test_no_tasks_produces_placeholder_comment(self) -> None:
        result = render_skill_template("{{COMPLETE_CMDS}}", tasks=[])
        assert "No task IDs available" in result

    def test_empty_template_unchanged(self) -> None:
        result = render_skill_template("", session_id="s-1")
        assert result == ""

    def test_unknown_placeholders_left_intact(self) -> None:
        result = render_skill_template("{{UNKNOWN_TOKEN}}", session_id="s-1")
        assert "{{UNKNOWN_TOKEN}}" in result


class TestInjectSkills:
    def _make_skills_dir(self, tmp_path: Path) -> Path:
        """Create a minimal templates/skills/ directory."""
        skills_dir = tmp_path / "templates" / "skills"
        skills_dir.mkdir(parents=True)

        (skills_dir / "bernstein-completion-protocol.md").write_text(
            "---\nname: bernstein-completion-protocol\n"
            "description: Report task completion\n"
            "whenToUse: When finished\n---\n"
            "Complete tasks: {{COMPLETE_CMDS}}\n",
            encoding="utf-8",
        )
        (skills_dir / "bernstein-signal-check.md").write_text(
            "---\nname: bernstein-signal-check\n"
            "description: Check signals\n"
            "whenToUse: Periodically\n---\n"
            "Signals at {{SESSION_ID}}\n",
            encoding="utf-8",
        )
        (skills_dir / "bernstein-test-runner.md").write_text(
            "---\nname: bernstein-test-runner\n"
            "description: Run tests\n"
            "whenToUse: When testing\n---\n"
            "Run tests with uv.\n",
            encoding="utf-8",
        )
        (skills_dir / "bernstein-commit-protocol.md").write_text(
            "---\nname: bernstein-commit-protocol\n"
            "description: Commit conventions\n"
            "whenToUse: When committing\n---\n"
            "Use main branch.\n",
            encoding="utf-8",
        )
        return tmp_path / "templates" / "roles"  # templates_dir (roles subdirectory)

    def test_creates_skills_directory(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(
            workdir=workdir,
            role="backend",
            tasks=[_make_task()],
            session_id="backend-abc",
            templates_dir=templates_dir,
        )

        assert (workdir / ".claude" / "skills").is_dir()

    def test_always_injects_completion_protocol(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="docs", tasks=[], session_id="s-1", templates_dir=templates_dir)

        assert (workdir / ".claude" / "skills" / "bernstein-completion-protocol.md").exists()

    def test_always_injects_signal_check(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="qa", tasks=[], session_id="s-2", templates_dir=templates_dir)

        assert (workdir / ".claude" / "skills" / "bernstein-signal-check.md").exists()

    def test_backend_role_gets_test_runner(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="backend", tasks=[], session_id="s-3", templates_dir=templates_dir)

        assert (workdir / ".claude" / "skills" / "bernstein-test-runner.md").exists()

    def test_backend_role_gets_commit_protocol(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="backend", tasks=[], session_id="s-4", templates_dir=templates_dir)

        assert (workdir / ".claude" / "skills" / "bernstein-commit-protocol.md").exists()

    def test_qa_role_gets_test_runner_only(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="qa", tasks=[], session_id="s-5", templates_dir=templates_dir)

        skills_dir = workdir / ".claude" / "skills"
        assert (skills_dir / "bernstein-test-runner.md").exists()
        assert not (skills_dir / "bernstein-commit-protocol.md").exists()

    def test_session_id_rendered_in_signal_check(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="qa", tasks=[], session_id="qa-deadbeef", templates_dir=templates_dir)

        content = (workdir / ".claude" / "skills" / "bernstein-signal-check.md").read_text()
        assert "qa-deadbeef" in content

    def test_task_ids_rendered_in_completion_protocol(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        tasks = [_make_task(id="T-99", title="My task")]

        inject_skills(workdir=workdir, role="backend", tasks=tasks, session_id="s-6", templates_dir=templates_dir)

        content = (workdir / ".claude" / "skills" / "bernstein-completion-protocol.md").read_text()
        assert "T-99" in content

    def test_missing_templates_dir_skips_gracefully(self, tmp_path: Path) -> None:
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        # templates_dir points to a directory with no sibling skills/
        templates_dir = tmp_path / "templates" / "roles"

        # Should not raise
        inject_skills(workdir=workdir, role="backend", tasks=[], session_id="s-7", templates_dir=templates_dir)

        assert not (workdir / ".claude" / "skills").exists()

    def test_unknown_role_gets_only_always_inject_skills(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="unknown_role", tasks=[], session_id="s-8", templates_dir=templates_dir)

        skills_dir = workdir / ".claude" / "skills"
        assert (skills_dir / "bernstein-completion-protocol.md").exists()
        assert (skills_dir / "bernstein-signal-check.md").exists()
        assert not (skills_dir / "bernstein-test-runner.md").exists()

    def test_skills_have_valid_frontmatter(self, tmp_path: Path) -> None:
        """Injected skills must have name, description, and whenToUse frontmatter."""
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="backend", tasks=[], session_id="s-9", templates_dir=templates_dir)

        skills_dir = workdir / ".claude" / "skills"
        for skill_file in skills_dir.iterdir():
            content = skill_file.read_text()
            assert content.startswith("---"), f"{skill_file.name} missing frontmatter"
            assert "name:" in content, f"{skill_file.name} missing 'name' field"
            assert "description:" in content, f"{skill_file.name} missing 'description' field"
            assert "whenToUse:" in content, f"{skill_file.name} missing 'whenToUse' field"


class TestRoleSkillMap:
    def test_backend_has_test_runner_and_commit(self) -> None:
        assert "bernstein-test-runner.md" in ROLE_SKILL_MAP["backend"]
        assert "bernstein-commit-protocol.md" in ROLE_SKILL_MAP["backend"]

    def test_qa_has_test_runner(self) -> None:
        assert "bernstein-test-runner.md" in ROLE_SKILL_MAP["qa"]

    def test_docs_has_commit_protocol(self) -> None:
        assert "bernstein-commit-protocol.md" in ROLE_SKILL_MAP["docs"]
