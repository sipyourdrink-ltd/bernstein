"""Tests for context compression engine — DependencyGraph, BM25Ranker, ContextCompressor."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from pathlib import Path

# --- Fixtures ---


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """Create a minimal Python project for compression tests."""
    src = tmp_path / "src" / "myapp"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "models.py").write_text("class User:\n    name: str\n\nclass Order:\n    user: User\n")
    (src / "service.py").write_text(
        "from myapp.models import User\n\ndef create_user(name: str) -> User:\n    return User()\n"
    )
    (src / "api.py").write_text(
        "from myapp.service import create_user\nfrom myapp.models import Order\n\ndef handle():\n    pass\n"
    )
    return tmp_path


@pytest.fixture()
def task() -> MagicMock:
    """Create a mock Task with title, description, and owned_files."""
    t = MagicMock()
    t.title = "Add user validation"
    t.description = "Validate user name before creating"
    t.owned_files = []
    return t


# --- TestShouldSkip ---


class TestShouldSkip:
    def test_hidden_dirs_skipped(self) -> None:
        from bernstein.core.context_compression import _should_skip

        assert _should_skip((".git", "config")) is True
        assert _should_skip((".claude", "worktrees", "foo.py")) is True
        assert _should_skip((".venv", "lib", "site.py")) is True

    def test_explicit_skip_dirs(self) -> None:
        from bernstein.core.context_compression import _should_skip

        assert _should_skip(("node_modules", "foo.js")) is True
        assert _should_skip(("__pycache__", "mod.pyc")) is True
        assert _should_skip(("dist", "pkg.whl")) is True

    def test_normal_dirs_pass(self) -> None:
        from bernstein.core.context_compression import _should_skip

        assert _should_skip(("src", "myapp", "models.py")) is False
        assert _should_skip(("tests", "test_foo.py")) is False


# --- TestIterPythonFiles ---


class TestIterPythonFiles:
    def test_skips_hidden_directories(self, tmp_path: Path) -> None:
        from bernstein.core.context_compression import _iter_python_files

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "ok.py").write_text("x = 1")
        hidden = tmp_path / ".claude" / "worktrees" / "wt1" / "src"
        hidden.mkdir(parents=True)
        (hidden / "dup.py").write_text("x = 2")
        venv = tmp_path / ".venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "site.py").write_text("x = 3")

        files = _iter_python_files(tmp_path)
        rel_paths = {f.relative_to(tmp_path).as_posix() for f in files}

        assert "src/ok.py" in rel_paths
        assert ".claude/worktrees/wt1/src/dup.py" not in rel_paths
        assert ".venv/lib/site.py" not in rel_paths


# --- TestDependencyGraph ---


class TestDependencyGraph:
    def test_builds_graph(self, project: Path) -> None:
        from bernstein.core.context_compression import DependencyGraph

        graph = DependencyGraph(project)
        graph.build()

        assert len(graph.graph) >= 3

    def test_reachable_from(self, project: Path) -> None:
        from bernstein.core.context_compression import DependencyGraph

        graph = DependencyGraph(project)
        graph.build()

        api_path = "src/myapp/api.py"
        reachable = graph.reachable_from(api_path, max_depth=2)
        assert api_path in reachable

    def test_dependents_of(self, project: Path) -> None:
        from bernstein.core.context_compression import DependencyGraph

        graph = DependencyGraph(project)
        graph.build()

        models_path = "src/myapp/models.py"
        dependents = graph.dependents_of(models_path)
        assert len(dependents) >= 1


# --- TestBM25Ranker ---


class TestBM25Ranker:
    def test_rank_returns_results(self) -> None:
        from bernstein.core.context_compression import BM25Ranker

        docs = {
            "models.py": "class User name email password",
            "service.py": "create user validation check",
            "utils.py": "format date string helper",
        }
        ranker = BM25Ranker(docs)
        results = ranker.rank("user validation", top_k=2)

        assert len(results) <= 2
        assert all(isinstance(r, tuple) and len(r) == 2 for r in results)

    def test_rank_empty_corpus(self) -> None:
        from bernstein.core.context_compression import BM25Ranker

        ranker = BM25Ranker({})
        assert ranker.rank("anything") == []

    def test_relevant_files_ranked_higher(self) -> None:
        from bernstein.core.context_compression import BM25Ranker

        docs = {
            "auth.py": "authentication login password token jwt",
            "models.py": "database schema migration table column",
        }
        ranker = BM25Ranker(docs)
        results = ranker.rank("authentication login")

        assert results[0][0] == "auth.py"


# --- TestContextCompressor ---


class TestContextCompressor:
    def test_compress_returns_result(self, project: Path, task: MagicMock) -> None:
        from bernstein.core.context_compression import ContextCompressor

        compressor = ContextCompressor(project)
        result = compressor.compress([task], max_files=10)

        assert result.selected_files
        assert result.original_tokens >= 1
        assert result.compressed_tokens >= 1
        assert 0.0 <= result.compression_ratio <= 1.0

    def test_owned_files_always_included(self, project: Path, task: MagicMock) -> None:
        from bernstein.core.context_compression import ContextCompressor

        task.owned_files = ["src/myapp/models.py"]
        compressor = ContextCompressor(project)
        selected, _bm25, _ = compressor.select_relevant_files([task], max_files=10)

        assert "src/myapp/models.py" in selected

    def test_no_hidden_dir_files(self, tmp_path: Path, task: MagicMock) -> None:
        from bernstein.core.context_compression import ContextCompressor

        src = tmp_path / "src"
        src.mkdir()
        (src / "real.py").write_text("class Validator:\n    pass\n")
        hidden = tmp_path / ".claude" / "worktrees" / "wt" / "src"
        hidden.mkdir(parents=True)
        (hidden / "ghost.py").write_text("class Validator:\n    pass\n")

        compressor = ContextCompressor(tmp_path)
        result = compressor.compress([task], max_files=10)

        for f in result.selected_files:
            assert not f.startswith("."), f"Hidden file leaked: {f}"

    def test_estimate_tokens(self, project: Path) -> None:
        from bernstein.core.context_compression import ContextCompressor

        compressor = ContextCompressor(project)
        tokens = compressor.estimate_tokens(["src/myapp/models.py"])
        assert tokens >= 1


# --- TestSectionPriority ---


class TestSectionPriority:
    def test_essential_sections_have_max_priority(self) -> None:
        from bernstein.core.context_compression import _section_priority

        assert _section_priority("role") == 10
        assert _section_priority("tasks") == 10
        assert _section_priority("instructions") == 10
        assert _section_priority("signal files") == 10

    def test_low_value_sections_have_low_priority(self) -> None:
        from bernstein.core.context_compression import _section_priority

        assert _section_priority("specialists") < 5
        assert _section_priority("team awareness") <= 3

    def test_unknown_section_returns_default(self) -> None:
        from bernstein.core.context_compression import _section_priority

        assert _section_priority("unknown_xyz_section") == 5

    def test_case_insensitive(self) -> None:
        from bernstein.core.context_compression import _section_priority

        assert _section_priority("ROLE PROMPT") == _section_priority("role prompt")


# --- TestPromptCompressor ---


class TestPromptCompressor:
    def _make_section(self, name: str, char_count: int) -> tuple[str, str]:
        return (name, "x" * char_count)

    def test_no_compression_under_budget(self) -> None:
        from bernstein.core.context_compression import PromptCompressor

        compressor = PromptCompressor(token_budget=10_000)
        sections = [
            ("role", "You are a backend engineer. " * 10),
            ("tasks", "Fix the auth bug. " * 5),
            ("instructions", "Complete and mark done. " * 5),
        ]
        compressed, orig, compressed_tok, dropped = compressor.compress_sections(sections)

        assert dropped == []
        assert compressed_tok == orig
        assert compressed == "".join(c for _, c in sections)

    def test_drops_low_priority_sections_first(self) -> None:
        from bernstein.core.context_compression import PromptCompressor

        # Budget of 100 tokens (~400 chars).
        # specialists section is 300 chars (75 tokens) — should be dropped.
        # role + tasks are essential (priority 10) — must be kept.
        compressor = PromptCompressor(token_budget=100)
        sections = [
            ("role", "You are a specialist. " * 5),          # ~110 chars, 27 tokens
            ("specialists", "Available: agentA agentB " * 12),  # ~300 chars, 75 tokens
            ("tasks", "Implement feature X. " * 5),          # ~105 chars, 26 tokens
            ("instructions", "Mark complete when done. " * 3), # ~75 chars, 18 tokens
        ]
        _compressed, _orig, compressed_tok, dropped = compressor.compress_sections(sections)

        assert "specialists" in dropped
        assert compressed_tok <= 100 + 10  # small tolerance for char-boundary rounding

    def test_essential_sections_never_dropped(self) -> None:
        from bernstein.core.context_compression import PromptCompressor

        # Tiny budget: only essential sections survive.
        compressor = PromptCompressor(token_budget=10)
        sections = [
            ("role", "r" * 200),          # 50 tokens
            ("tasks", "t" * 200),         # 50 tokens
            ("instructions", "i" * 200),  # 50 tokens
            ("signal", "s" * 200),        # 50 tokens
            ("specialists", "a" * 400),   # 100 tokens — droppable
            ("lessons", "l" * 400),       # 100 tokens — droppable
        ]
        _compressed, _orig, _compressed_tok, dropped = compressor.compress_sections(sections)

        assert "role" not in dropped
        assert "tasks" not in dropped
        assert "instructions" not in dropped
        assert "signal" not in dropped

    def test_empty_sections_returns_empty(self) -> None:
        from bernstein.core.context_compression import PromptCompressor

        compressor = PromptCompressor(token_budget=50_000)
        compressed, orig, compressed_tok, dropped = compressor.compress_sections([])

        assert compressed == ""
        assert orig == 0
        assert compressed_tok == 0
        assert dropped == []

    def test_compress_returns_compression_result(self) -> None:
        from bernstein.core.context_compression import PromptCompressor

        compressor = PromptCompressor(token_budget=50_000)
        sections = [
            ("role", "You are a backend engineer."),
            ("tasks", "Implement task X."),
            ("instructions", "Complete and exit."),
        ]
        result = compressor.compress(sections)

        assert result.original_tokens >= 1
        assert result.compressed_tokens >= 1
        assert 0.0 <= result.compression_ratio <= 1.0
        assert isinstance(result.selected_files, list)
        assert isinstance(result.dropped_files, list)

    def test_achieves_30_percent_reduction_on_bloated_prompt(self) -> None:
        from bernstein.core.context_compression import PromptCompressor

        # Budget set to 50% of a large prompt — simulates small-task target.
        role_content = "You are a backend engineer." * 50        # ~1350 chars
        tasks_content = "Implement feature X." * 20              # ~400 chars
        instructions_content = "Mark complete when done." * 20   # ~480 chars
        specialists_content = "Available: " + "agentX " * 200    # ~1400 chars — droppable
        lessons_content = "Lesson: do X not Y. " * 100          # ~2000 chars — droppable
        team_content = "Team: agentA working on Y. " * 100      # ~2700 chars — droppable

        total_chars = sum(
            len(c)
            for c in [
                role_content,
                tasks_content,
                instructions_content,
                specialists_content,
                lessons_content,
                team_content,
            ]
        )
        total_tokens_est = total_chars // 4

        # Set budget to 50% of total
        budget = total_tokens_est // 2
        compressor = PromptCompressor(token_budget=budget)

        sections = [
            ("role", role_content),
            ("tasks", tasks_content),
            ("instructions", instructions_content),
            ("specialists", specialists_content),
            ("lessons", lessons_content),
            ("team awareness", team_content),
        ]
        _compressed, orig, compressed_tok, dropped = compressor.compress_sections(sections)

        reduction = 1.0 - compressed_tok / max(1, orig)
        assert reduction >= 0.30, f"Expected ≥30% reduction, got {reduction:.1%}"
        assert len(dropped) >= 1
