"""Tests for TaskContextBuilder and knowledge base utilities."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.context import (
    TaskContextBuilder,
    append_decision,
    build_file_index,
    refresh_knowledge_base,
)
from bernstein.core.knowledge_base import _parse_python_file, _subsystem_context
from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    id: str = "T-001",
    role: str = "backend",
    title: str = "Implement feature",
    description: str = "Write the code.",
    owned_files: list[str] | None = None,
) -> Task:
    return Task(
        id=id,
        title=title,
        description=description,
        role=role,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus.OPEN,
        task_type=TaskType.STANDARD,
        priority=2,
        owned_files=owned_files or [],
    )


def _write_py(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# _parse_python_file
# ---------------------------------------------------------------------------


class TestParsePythonFile:
    """Tests for AST-based Python file parsing."""

    def test_extracts_docstring(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        _write_py(f, '"""Module docstring."""\n\ndef foo(): pass\n')
        summary = _parse_python_file(f)
        assert summary is not None
        assert summary.docstring == "Module docstring."

    def test_extracts_classes_and_methods(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        _write_py(f, ("class Foo:\n    def bar(self): pass\n    def baz(self): pass\n    def _private(self): pass\n"))
        summary = _parse_python_file(f)
        assert summary is not None
        assert len(summary.classes) == 1
        cls_name, methods = summary.classes[0]
        assert cls_name == "Foo"
        assert "bar" in methods
        assert "baz" in methods
        assert "_private" not in methods  # private methods excluded

    def test_extracts_functions(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        _write_py(f, "def alpha(): pass\ndef beta(): pass\n")
        summary = _parse_python_file(f)
        assert summary is not None
        assert "alpha" in summary.functions
        assert "beta" in summary.functions

    def test_extracts_imports(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        _write_py(f, "import os\nimport json\nfrom pathlib import Path\n")
        summary = _parse_python_file(f)
        assert summary is not None
        assert "os" in summary.imports
        assert "json" in summary.imports
        assert "pathlib" in summary.imports

    def test_returns_none_for_syntax_error(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.py"
        _write_py(f, "def foo(\n")
        assert _parse_python_file(f) is None

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert _parse_python_file(tmp_path / "nope.py") is None

    def test_no_docstring(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        _write_py(f, "x = 1\n")
        summary = _parse_python_file(f)
        assert summary is not None
        assert summary.docstring == ""

    def test_async_functions(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        _write_py(f, "async def fetch(): pass\n")
        summary = _parse_python_file(f)
        assert summary is not None
        assert "fetch" in summary.functions

    def test_truncates_long_docstring(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        _write_py(f, f'"""{"x" * 200}"""\n')
        summary = _parse_python_file(f)
        assert summary is not None
        assert len(summary.docstring) <= 120


# ---------------------------------------------------------------------------
# TaskContextBuilder
# ---------------------------------------------------------------------------


class TestTaskContextBuilder:
    """Tests for the TaskContextBuilder."""

    def test_returns_empty_for_no_owned_files(self, tmp_path: Path) -> None:
        builder = TaskContextBuilder(tmp_path)
        assert builder.task_context([]) == ""

    def test_includes_file_summary_for_python_files(self, tmp_path: Path) -> None:
        _write_py(tmp_path / "src" / "mod.py", '"""My module."""\nclass Foo:\n    def bar(self): pass\n')
        builder = TaskContextBuilder(tmp_path)
        ctx = builder.task_context(["src/mod.py"])
        assert "src/mod.py" in ctx
        assert "My module." in ctx
        assert "Foo" in ctx

    def test_handles_missing_files(self, tmp_path: Path) -> None:
        builder = TaskContextBuilder(tmp_path)
        ctx = builder.task_context(["nonexistent.py"])
        # task_context still includes the file path as a header
        assert "nonexistent.py" in ctx

    def test_includes_file_path_header(self, tmp_path: Path) -> None:
        _write_py(tmp_path / "a.py", "x = 1\n")
        builder = TaskContextBuilder(tmp_path)
        ctx = builder.task_context(["a.py"])
        assert "a.py" in ctx

    def test_handles_multiple_files(self, tmp_path: Path) -> None:
        _write_py(tmp_path / "a.py", "x = 1\n")
        _write_py(tmp_path / "b.py", "y = 2\n")
        builder = TaskContextBuilder(tmp_path)
        ctx = builder.task_context(["a.py", "b.py"])
        assert "a.py" in ctx
        assert "b.py" in ctx

    def test_deduplicates_owned_files(self, tmp_path: Path) -> None:
        _write_py(tmp_path / "a.py", "x = 1\n")
        builder = TaskContextBuilder(tmp_path)
        ctx = builder.task_context(["a.py", "a.py"])
        # Only processes first 5 unique files, so a.py appears once in output
        assert "a.py" in ctx

    def test_file_context_for_single_file(self, tmp_path: Path) -> None:
        _write_py(tmp_path / "a.py", '"""My helper."""\ndef greet(): pass\n')
        builder = TaskContextBuilder(tmp_path)
        ctx = builder.file_context("a.py")
        assert "a.py" in ctx
        assert "My helper." in ctx


# ---------------------------------------------------------------------------
# _subsystem_context
# ---------------------------------------------------------------------------


class TestSubsystemContext:
    """Tests for file-level subsystem context extraction."""

    def test_returns_module_docstring(self, tmp_path: Path) -> None:
        pkg = tmp_path / "src" / "core"
        pkg.mkdir(parents=True)
        _write_py(pkg / "mod.py", '"""Core orchestration module."""\nclass Engine:\n    pass\n')
        ctx = _subsystem_context("src/core/mod.py", tmp_path)
        assert "Core orchestration module" in ctx

    def test_returns_empty_when_nothing_found(self, tmp_path: Path) -> None:
        pkg = tmp_path / "src" / "core"
        pkg.mkdir(parents=True)
        ctx = _subsystem_context("src/core/mod.py", tmp_path)
        assert ctx == ""


# ---------------------------------------------------------------------------
# Knowledge base — build_file_index, build_architecture_md, refresh
# ---------------------------------------------------------------------------


class TestBuildFileIndex:
    """Tests for file index generation."""

    def test_indexes_python_files_in_git_repo(self, tmp_path: Path) -> None:
        # Set up a git repo
        import subprocess

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        _write_py(tmp_path / "mod.py", '"""My module."""\nclass Foo:\n    def bar(self): pass\n')
        subprocess.run(["git", "add", "mod.py"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init", "--no-gpg-sign"],
            cwd=tmp_path,
            capture_output=True,
            env={
                **__import__("os").environ,
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )

        index = build_file_index(tmp_path)
        assert "mod.py" in index
        entry = index["mod.py"]
        assert entry.docstring == "My module."
        assert any("Foo" in str(c) for c in entry.classes)

    def test_returns_empty_for_non_git_dir(self, tmp_path: Path) -> None:
        _write_py(tmp_path / "mod.py", "x = 1\n")
        index = build_file_index(tmp_path)
        assert index == {}


class TestRefreshKnowledgeBase:
    """Tests for the full knowledge base refresh."""

    def test_creates_knowledge_directory_and_files(self, tmp_path: Path) -> None:
        # Set up a git repo
        import subprocess

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        _write_py(tmp_path / "mod.py", '"""Hello."""\n')
        subprocess.run(["git", "add", "mod.py"], cwd=tmp_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init", "--no-gpg-sign"],
            cwd=tmp_path,
            capture_output=True,
            env={
                **__import__("os").environ,
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )

        refresh_knowledge_base(tmp_path)

        kb = tmp_path / ".sdd" / "knowledge"
        assert kb.is_dir()
        assert (kb / "file_index.json").is_file()
        assert (kb / "architecture.md").is_file()

        # Verify file_index.json is valid JSON
        data = json.loads((kb / "file_index.json").read_text())
        assert isinstance(data, dict)

    def test_creates_architecture_md(self, tmp_path: Path) -> None:
        import subprocess

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        refresh_knowledge_base(tmp_path)
        assert (tmp_path / ".sdd" / "knowledge" / "architecture.md").is_file()


# ---------------------------------------------------------------------------
# append_decision
# ---------------------------------------------------------------------------


class TestAppendDecision:
    """Tests for decision capture from completed tasks."""

    def test_creates_file_if_missing(self, tmp_path: Path) -> None:
        append_decision(tmp_path, "T-001", "Fixed routing", "Switched to EWMA scoring.")
        path = tmp_path / ".sdd" / "knowledge" / "recent_decisions.md"
        assert path.is_file()
        text = path.read_text()
        assert "Fixed routing" in text
        assert "T-001" in text
        assert "EWMA" in text

    def test_appends_to_existing(self, tmp_path: Path) -> None:
        kb = tmp_path / ".sdd" / "knowledge"
        kb.mkdir(parents=True)
        (kb / "recent_decisions.md").write_text("# Recent Decisions\n")
        append_decision(tmp_path, "T-001", "First", "Summary 1.")
        append_decision(tmp_path, "T-002", "Second", "Summary 2.")
        text = (kb / "recent_decisions.md").read_text()
        assert "First" in text
        assert "Second" in text

    def test_caps_at_15_entries(self, tmp_path: Path) -> None:
        kb = tmp_path / ".sdd" / "knowledge"
        kb.mkdir(parents=True)
        (kb / "recent_decisions.md").write_text("# Recent Decisions\n")
        for i in range(20):
            append_decision(tmp_path, f"T-{i:03d}", f"Task {i}", f"Summary {i}.")
        text = (kb / "recent_decisions.md").read_text()
        # Should have 15 entries max — the first 5 should be pruned
        assert "Task 0" not in text
        assert "Task 4" not in text
        assert "Task 5" in text
        assert "Task 19" in text


# ---------------------------------------------------------------------------
# Integration: spawner prompt includes rich context
# ---------------------------------------------------------------------------


class TestSpawnerContextIntegration:
    """Verify _render_prompt injects rich context when builder is provided."""

    def test_prompt_includes_context_block(self, tmp_path: Path) -> None:
        from bernstein.core.spawner import _render_prompt

        _write_py(tmp_path / "src" / "mod.py", '"""Module doc."""\nclass Foo:\n    def bar(self): pass\n')
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        task = _make_task(owned_files=["src/mod.py"])
        builder = TaskContextBuilder(tmp_path)
        prompt = _render_prompt([task], templates_dir, tmp_path, context_builder=builder)

        # Prompt should include file context from the context builder
        assert "mod.py" in prompt

    def test_prompt_works_without_context_builder(self, tmp_path: Path) -> None:
        from bernstein.core.spawner import _render_prompt

        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        task = _make_task(owned_files=["src/mod.py"])
        prompt = _render_prompt([task], templates_dir, tmp_path, context_builder=None)

        assert "Context (auto-generated)" not in prompt
        assert "backend specialist" in prompt
