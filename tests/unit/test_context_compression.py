"""Tests for context compression engine."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.compression_models import CompressionMetrics, CompressionResult
from bernstein.core.context_compression import BM25Ranker, ContextCompressor, DependencyGraph
from bernstein.core.knowledge_base import TaskContextBuilder
from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType

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
# CompressionResult / CompressionMetrics
# ---------------------------------------------------------------------------


class TestCompressionModels:
    """Tests for data models."""

    def test_compression_result_initialization(self) -> None:
        """CompressionResult initializes with all required fields."""
        result = CompressionResult(
            original_tokens=10000,
            compressed_tokens=6000,
            compression_ratio=0.60,
            selected_files=["src/foo.py", "src/bar.py"],
            dropped_files=["src/baz.py"],
            metrics=CompressionMetrics(
                bm25_matches=2,
                dependency_matches=3,
                semantic_matches=1,
                total_files_analyzed=5,
            ),
        )
        assert result.original_tokens == 10000
        assert result.compressed_tokens == 6000
        assert result.compression_ratio == 0.60
        assert len(result.selected_files) == 2
        assert len(result.dropped_files) == 1

    def test_compression_metrics_initialization(self) -> None:
        """CompressionMetrics initializes with all required fields."""
        m = CompressionMetrics(
            bm25_matches=5,
            dependency_matches=3,
            semantic_matches=1,
            total_files_analyzed=100,
        )
        assert m.bm25_matches == 5
        assert m.total_files_analyzed == 100


# ---------------------------------------------------------------------------
# DependencyGraph
# ---------------------------------------------------------------------------


class TestDependencyGraph:
    """Tests for AST-based file dependency graph."""

    def test_initialization(self, tmp_path: Path) -> None:
        """DependencyGraph initializes with workdir and empty graph."""
        graph = DependencyGraph(tmp_path)
        assert graph.workdir == tmp_path
        assert graph.graph == {}

    def test_build_simple_imports(self, tmp_path: Path) -> None:
        """DependencyGraph.build() records import relationships."""
        _write_py(tmp_path / "a.py", "import b\n")
        _write_py(tmp_path / "b.py", "# no imports\n")
        _write_py(tmp_path / "c.py", "from a import foo\n")

        graph = DependencyGraph(tmp_path)
        graph.build()

        # a.py depends on b.py
        assert "b.py" in graph.graph.get("a.py", [])
        # c.py depends on a.py
        assert "a.py" in graph.graph.get("c.py", [])
        # b.py has no local dependencies
        assert graph.graph.get("b.py", []) == []

    def test_dependents_of(self, tmp_path: Path) -> None:
        """DependencyGraph.dependents_of() returns files that import the target."""
        _write_py(tmp_path / "a.py", "# no imports\n")
        _write_py(tmp_path / "b.py", "import a\n")
        _write_py(tmp_path / "c.py", "from a import foo\n")

        graph = DependencyGraph(tmp_path)
        graph.build()

        dependents = graph.dependents_of("a.py")
        assert set(dependents) == {"b.py", "c.py"}

    def test_dependents_of_unknown_file(self, tmp_path: Path) -> None:
        """DependencyGraph.dependents_of() returns empty list for unknown file."""
        _write_py(tmp_path / "a.py", "# nothing\n")
        graph = DependencyGraph(tmp_path)
        graph.build()
        assert graph.dependents_of("nonexistent.py") == []

    def test_reachable_from(self, tmp_path: Path) -> None:
        """DependencyGraph.reachable_from() follows import chains."""
        _write_py(tmp_path / "a.py", "import b\n")
        _write_py(tmp_path / "b.py", "import c\n")
        _write_py(tmp_path / "c.py", "# leaf\n")
        _write_py(tmp_path / "d.py", "# unrelated\n")

        graph = DependencyGraph(tmp_path)
        graph.build()

        reachable = graph.reachable_from("a.py", max_depth=2)
        assert "a.py" in reachable
        assert "b.py" in reachable
        assert "c.py" in reachable
        assert "d.py" not in reachable

    def test_reachable_from_depth_limit(self, tmp_path: Path) -> None:
        """DependencyGraph.reachable_from() respects max_depth."""
        _write_py(tmp_path / "a.py", "import b\n")
        _write_py(tmp_path / "b.py", "import c\n")
        _write_py(tmp_path / "c.py", "# leaf\n")

        graph = DependencyGraph(tmp_path)
        graph.build()

        reachable = graph.reachable_from("a.py", max_depth=1)
        assert "a.py" in reachable
        assert "b.py" in reachable
        # c.py is 2 hops away, so should NOT be in depth=1 traversal
        assert "c.py" not in reachable

    def test_handles_syntax_errors(self, tmp_path: Path) -> None:
        """DependencyGraph.build() skips files with syntax errors."""
        _write_py(tmp_path / "good.py", "import os\n")
        _write_py(tmp_path / "bad.py", "def broken(\n")  # syntax error

        graph = DependencyGraph(tmp_path)
        graph.build()  # should not raise

        assert "good.py" in graph.graph

    def test_skips_venv_directories(self, tmp_path: Path) -> None:
        """DependencyGraph.build() skips venv directories."""
        _write_py(tmp_path / "src" / "app.py", "# real code\n")
        _write_py(tmp_path / ".venv" / "lib" / "site.py", "# venv code\n")

        graph = DependencyGraph(tmp_path)
        graph.build()

        assert ".venv/lib/site.py" not in graph.graph


# ---------------------------------------------------------------------------
# BM25Ranker
# ---------------------------------------------------------------------------


class TestBM25Ranker:
    """Tests for TF-IDF/BM25 keyword file ranker."""

    def test_initialization(self) -> None:
        """BM25Ranker initializes with documents."""
        docs = {
            "a.py": "spawner agent prompt task context",
            "b.py": "metrics collector export prometheus",
            "c.py": "tests unit integration pytest",
        }
        ranker = BM25Ranker(docs)
        assert len(ranker.documents) == 3

    def test_rank_returns_results(self) -> None:
        """BM25Ranker.rank() returns non-empty list for matching query."""
        docs = {
            "spawner.py": "spawn agent context prompt",
            "metrics.py": "collect metrics prometheus",
            "storage.py": "store database redis",
        }
        ranker = BM25Ranker(docs)
        ranked = ranker.rank("agent spawner context")

        assert len(ranked) > 0
        # Each item is (filename, score)
        assert all(isinstance(fname, str) and isinstance(score, float) for fname, score in ranked)

    def test_rank_orders_by_score_descending(self) -> None:
        """BM25Ranker.rank() returns results sorted by score descending."""
        docs = {
            "spawner.py": "spawn agent context prompt task agent spawn",
            "metrics.py": "collect metrics prometheus entirely different",
            "storage.py": "store database redis completely unrelated",
        }
        ranker = BM25Ranker(docs)
        ranked = ranker.rank("agent spawner context")

        scores = [score for _, score in ranked]
        assert scores == sorted(scores, reverse=True)

    def test_rank_with_threshold(self) -> None:
        """BM25Ranker.rank() with threshold filters low-scoring results."""
        docs = {
            "spawner.py": "spawn agent context prompt",
            "metrics.py": "completely unrelated content here",
        }
        ranker = BM25Ranker(docs)

        # Large threshold: only results above it
        ranked = ranker.rank("spawner agent", threshold=100.0)
        assert all(score >= 100.0 for _, score in ranked)

    def test_rank_with_top_k(self) -> None:
        """BM25Ranker.rank() with top_k limits returned results."""
        docs = {f"file{i}.py": f"content {i} something" for i in range(10)}
        ranker = BM25Ranker(docs)
        ranked = ranker.rank("content something", top_k=3)
        assert len(ranked) <= 3

    def test_rank_empty_documents(self) -> None:
        """BM25Ranker.rank() returns empty list when no documents."""
        ranker = BM25Ranker({})
        ranked = ranker.rank("anything")
        assert ranked == []

    def test_spawner_ranks_first_for_spawner_query(self) -> None:
        """spawner.py should rank highest for 'agent spawner context' query."""
        docs = {
            "spawner.py": "spawn agent context prompt task spawner agent spawn",
            "metrics.py": "collect metrics prometheus export gauge",
            "storage.py": "store database redis key value",
        }
        ranker = BM25Ranker(docs)
        ranked = ranker.rank("agent spawner context")

        if ranked:
            assert ranked[0][0] == "spawner.py"


# ---------------------------------------------------------------------------
# ContextCompressor
# ---------------------------------------------------------------------------


class TestContextCompressor:
    """Tests for the ContextCompressor orchestrator."""

    def test_select_relevant_files_by_keyword(self, tmp_path: Path) -> None:
        """ContextCompressor.select_relevant_files() returns task-relevant files."""
        (tmp_path / "src").mkdir()
        _write_py(
            tmp_path / "src" / "spawner.py",
            "def spawn_agent(task):\n    return task\n",
        )
        _write_py(
            tmp_path / "src" / "models.py",
            "class Task:\n    pass\n",
        )
        _write_py(
            tmp_path / "src" / "metrics.py",
            "def collect_metrics():\n    pass\n",
        )

        compressor = ContextCompressor(tmp_path)
        task = _make_task(title="Implement agent spawner", description="Modify the spawner")

        selected, bm25_count, dep_count = compressor.select_relevant_files([task], max_files=5)

        assert isinstance(selected, list)
        assert isinstance(bm25_count, int)
        assert isinstance(dep_count, int)
        assert any("spawner" in f for f in selected)

    def test_owned_files_always_included(self, tmp_path: Path) -> None:
        """Owned files are always included regardless of BM25 score."""
        _write_py(tmp_path / "owned.py", "# owned file\n")
        _write_py(tmp_path / "other.py", "# unrelated\n")

        compressor = ContextCompressor(tmp_path)
        task = _make_task(
            title="Random unrelated task",
            description="Nothing here",
            owned_files=["owned.py"],
        )

        selected, _bm25_count, _dep_count = compressor.select_relevant_files([task], max_files=5)
        assert "owned.py" in selected

    def test_estimate_tokens_empty(self, tmp_path: Path) -> None:
        """estimate_tokens returns 1 minimum for empty file list."""
        compressor = ContextCompressor(tmp_path)
        assert compressor.estimate_tokens([]) == 1

    def test_estimate_tokens_proportional(self, tmp_path: Path) -> None:
        """estimate_tokens returns proportional estimate based on file size."""
        content = "x" * 4000  # ~1000 tokens
        _write_py(tmp_path / "big.py", content)
        _write_py(tmp_path / "small.py", "x\n")

        compressor = ContextCompressor(tmp_path)
        big_tokens = compressor.estimate_tokens(["big.py"])
        small_tokens = compressor.estimate_tokens(["small.py"])

        assert big_tokens > small_tokens

    def test_compress_returns_compression_result(self, tmp_path: Path) -> None:
        """compress() returns a valid CompressionResult."""
        _write_py(tmp_path / "a.py", "def foo(): pass\n" * 50)
        _write_py(tmp_path / "b.py", "def bar(): pass\n" * 50)
        _write_py(tmp_path / "c.py", "def baz(): pass\n" * 50)

        compressor = ContextCompressor(tmp_path)
        task = _make_task(title="Fix foo function", description="Update foo in a.py")
        result = compressor.compress([task], max_files=2)

        assert isinstance(result, CompressionResult)
        assert result.original_tokens >= result.compressed_tokens
        assert 0.0 <= result.compression_ratio <= 1.0
        assert isinstance(result.selected_files, list)
        assert isinstance(result.dropped_files, list)

    def test_compress_with_max_files_limit(self, tmp_path: Path) -> None:
        """compress() respects max_files limit."""
        for i in range(10):
            _write_py(tmp_path / f"file{i}.py", f"def func{i}(): pass\n")

        compressor = ContextCompressor(tmp_path)
        task = _make_task(title="Fix something", description="General fix")
        result = compressor.compress([task], max_files=3)

        assert len(result.selected_files) <= 3

    def test_compress_empty_project(self, tmp_path: Path) -> None:
        """compress() handles empty project gracefully."""
        compressor = ContextCompressor(tmp_path)
        task = _make_task()
        result = compressor.compress([task])

        assert isinstance(result, CompressionResult)
        assert result.selected_files == []
        assert result.original_tokens >= 1


# ---------------------------------------------------------------------------
# TaskContextBuilder.build_context integration
# ---------------------------------------------------------------------------


class TestTaskContextBuilderBuildContext:
    """Tests for TaskContextBuilder.build_context() with compression."""

    def test_build_context_returns_string(self, tmp_path: Path) -> None:
        """build_context() returns a non-empty string."""
        _write_py(
            tmp_path / "src" / "spawner.py",
            '"""Agent spawning module."""\ndef spawn_agent(task): pass\n',
        )

        builder = TaskContextBuilder(tmp_path)
        task = _make_task(
            title="Fix spawner bug",
            description="Agent spawn context is too large",
            owned_files=["src/spawner.py"],
        )
        context = builder.build_context([task])

        assert isinstance(context, str)
        assert len(context) > 0

    def test_build_context_includes_file_info(self, tmp_path: Path) -> None:
        """build_context() mentions the spawner file for spawner-related task."""
        _write_py(
            tmp_path / "src" / "spawner.py",
            '"""Spawner module."""\ndef spawn_agent(task): pass\n',
        )
        _write_py(
            tmp_path / "src" / "models.py",
            '"""Data models."""\nclass Task: pass\n',
        )

        builder = TaskContextBuilder(tmp_path)
        task = _make_task(
            title="Fix spawner performance",
            description="Improve agent spawning",
            owned_files=["src/spawner.py"],
        )
        context = builder.build_context([task])

        assert "spawner" in context.lower()

    def test_build_context_falls_back_on_empty_project(self, tmp_path: Path) -> None:
        """build_context() handles empty project gracefully."""
        builder = TaskContextBuilder(tmp_path)
        task = _make_task()
        context = builder.build_context([task])

        # May be empty or have fallback content — should not raise
        assert isinstance(context, str)

    def test_build_context_multiple_tasks(self, tmp_path: Path) -> None:
        """build_context() handles multiple tasks."""
        _write_py(tmp_path / "a.py", "def alpha(): pass\n")
        _write_py(tmp_path / "b.py", "def beta(): pass\n")

        builder = TaskContextBuilder(tmp_path)
        tasks = [
            _make_task(id="T-001", title="Fix alpha", description="Update alpha func"),
            _make_task(id="T-002", title="Fix beta", description="Update beta func"),
        ]
        context = builder.build_context(tasks)

        assert isinstance(context, str)


# ---------------------------------------------------------------------------
# End-to-end test: spawner prompt includes compressed context
# ---------------------------------------------------------------------------


class TestEndToEndContextCompression:
    """End-to-end test verifying spawner prompt includes compressed context."""

    def test_spawner_prompt_includes_compressed_context(self, tmp_path: Path) -> None:
        """_render_prompt builds context via build_context() when builder given."""
        from bernstein.core.spawner import _render_prompt

        (tmp_path / "src" / "bernstein" / "core").mkdir(parents=True)
        _write_py(
            tmp_path / "src" / "bernstein" / "core" / "spawner.py",
            '"""Spawn agents."""\nimport models\ndef spawn_agent(task):\n    pass\n' + "    # work\n" * 30,
        )
        _write_py(
            tmp_path / "src" / "bernstein" / "core" / "models.py",
            '"""Data models."""\nclass Task:\n    pass\n' + "    # data\n" * 30,
        )
        _write_py(
            tmp_path / "src" / "bernstein" / "core" / "unrelated.py",
            '"""Completely unrelated."""\ndef store():\n    pass\n' + "    # store\n" * 30,
        )

        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)

        task = _make_task(
            title="Fix spawner performance",
            description="Improve agent spawning logic",
            owned_files=["src/bernstein/core/spawner.py"],
        )

        builder = TaskContextBuilder(tmp_path)
        prompt = _render_prompt([task], templates_dir, tmp_path, context_builder=builder)

        # spawner should be mentioned in context
        assert "spawner" in prompt.lower()

    def test_compression_reduces_context_size(self, tmp_path: Path) -> None:
        """Compressed context is smaller than full file listing."""
        # Create enough files that compression should select fewer than all
        for i in range(8):
            _write_py(
                tmp_path / f"module{i}.py",
                f'"""Module {i}."""\ndef func{i}():\n    pass\n' + "    # code\n" * 40,
            )

        compressor = ContextCompressor(tmp_path)
        task = _make_task(title="Fix module0", description="Update module0 function")

        result = compressor.compress([task], max_files=3)

        # With max_files=3 on 8 files, compression_ratio should be < 1.0
        assert result.compression_ratio < 1.0
        assert result.original_tokens > result.compressed_tokens
