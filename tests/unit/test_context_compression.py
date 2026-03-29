"""Tests for context compression engine — DependencyGraph, BM25Ranker, ContextCompressor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


# --- Fixtures ---


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """Create a minimal Python project for compression tests."""
    src = tmp_path / "src" / "myapp"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "models.py").write_text(
        "class User:\n    name: str\n\nclass Order:\n    user: User\n"
    )
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
        selected, bm25, _ = compressor.select_relevant_files([task], max_files=10)

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
