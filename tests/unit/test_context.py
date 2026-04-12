"""Tests for bernstein.core.context."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from bernstein.core.context import (
    ApiUsageTracker,
    available_roles,
    clear_caches,
    file_tree,
    gather_project_context,
    get_usage_tracker,
)
from bernstein.core.file_discovery import _read_if_exists, _should_skip
from bernstein.core.knowledge_base import _git_cochanged_files

# ---------------------------------------------------------------------------
# _should_skip
# ---------------------------------------------------------------------------


class TestShouldSkip:
    """Tests for the path-exclusion heuristic."""

    def test_skips_git(self) -> None:
        assert _should_skip(Path(".git/objects/abc"))

    def test_skips_pycache(self) -> None:
        assert _should_skip(Path("src/__pycache__/mod.pyc"))

    def test_skips_node_modules(self) -> None:
        assert _should_skip(Path("node_modules/pkg/index.js"))

    def test_skips_pyc_suffix(self) -> None:
        assert _should_skip(Path("foo.pyc"))

    def test_keeps_normal_python(self) -> None:
        assert not _should_skip(Path("src/main.py"))

    def test_keeps_nested_normal(self) -> None:
        assert not _should_skip(Path("src/core/models.py"))


# ---------------------------------------------------------------------------
# file_tree
# ---------------------------------------------------------------------------


class TestFileTree:
    """Tests for file tree generation."""

    def test_lists_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").touch()
        (tmp_path / "b.py").touch()
        tree = file_tree(tmp_path, max_lines=50)
        assert "a.py" in tree
        assert "b.py" in tree

    def test_truncates_at_max_lines(self, tmp_path: Path) -> None:
        for i in range(20):
            (tmp_path / f"file_{i:02d}.txt").touch()
        tree = file_tree(tmp_path, max_lines=5)
        assert "more files" in tree

    def test_empty_dir(self, tmp_path: Path) -> None:
        tree = file_tree(tmp_path, max_lines=50)
        assert tree == ""

    def test_ttl_cache_avoids_repeated_subprocess(self, tmp_path: Path) -> None:
        clear_caches()
        mock_result = MagicMock()
        mock_result.returncode = 1  # force fallback so subprocess.run is the only call we track
        mock_result.stdout = ""
        (tmp_path / "x.py").touch()

        with patch("bernstein.core.git.git_context.subprocess.run", return_value=mock_result) as mock_run:
            file_tree(tmp_path, max_lines=50)
            file_tree(tmp_path, max_lines=50)
            assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# _read_if_exists
# ---------------------------------------------------------------------------


class TestReadIfExists:
    """Tests for the safe file reader."""

    def test_reads_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "hello.txt"
        f.write_text("hello world")
        assert _read_if_exists(f) == "hello world"

    def test_returns_none_for_missing(self, tmp_path: Path) -> None:
        assert _read_if_exists(tmp_path / "nope.txt") is None

    def test_truncates_long_content(self, tmp_path: Path) -> None:
        f = tmp_path / "big.txt"
        f.write_text("x" * 5000)
        result = _read_if_exists(f, max_chars=100)
        assert result is not None
        assert "truncated" in result
        assert len(result) < 5000


# ---------------------------------------------------------------------------
# available_roles
# ---------------------------------------------------------------------------


class TestAvailableRoles:
    """Tests for role discovery."""

    def test_discovers_roles(self, tmp_path: Path) -> None:
        for name in ("backend", "frontend", "qa"):
            d = tmp_path / name
            d.mkdir()
            (d / "system_prompt.md").write_text(f"You are {name}.")
        roles = available_roles(tmp_path)
        assert roles == ["backend", "frontend", "qa"]

    def test_ignores_dirs_without_prompt(self, tmp_path: Path) -> None:
        (tmp_path / "valid").mkdir()
        (tmp_path / "valid" / "system_prompt.md").write_text("ok")
        (tmp_path / "empty_dir").mkdir()
        roles = available_roles(tmp_path)
        assert roles == ["valid"]

    def test_returns_empty_for_missing_dir(self, tmp_path: Path) -> None:
        assert available_roles(tmp_path / "nonexistent") == []

    def test_sorted_order(self, tmp_path: Path) -> None:
        for name in ("zulu", "alpha", "mike"):
            d = tmp_path / name
            d.mkdir()
            (d / "system_prompt.md").write_text("ok")
        roles = available_roles(tmp_path)
        assert roles == ["alpha", "mike", "zulu"]


# ---------------------------------------------------------------------------
# gather_project_context
# ---------------------------------------------------------------------------


class TestGatherProjectContext:
    """Tests for the full context gatherer."""

    def test_includes_file_tree(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("print('hi')")
        ctx = gather_project_context(tmp_path)
        assert "File tree" in ctx
        assert "main.py" in ctx

    def test_includes_readme(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# My Project\nA cool project.")
        ctx = gather_project_context(tmp_path)
        assert "README" in ctx
        assert "cool project" in ctx

    def test_includes_project_md(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        (sdd / "project.md").write_text("Project: test orchestrator")
        ctx = gather_project_context(tmp_path)
        assert "project.md" in ctx
        assert "test orchestrator" in ctx

    def test_empty_project(self, tmp_path: Path) -> None:
        ctx = gather_project_context(tmp_path)
        assert ctx == ""

    def test_prefers_readme_md(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("markdown")
        (tmp_path / "README.txt").write_text("plain text")
        ctx = gather_project_context(tmp_path)
        assert "markdown" in ctx


# ---------------------------------------------------------------------------
# ApiUsageTracker
# ---------------------------------------------------------------------------


class TestApiUsageTracker:
    """Tests for API usage tracking."""

    def test_record_call(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        tracker.record_call(
            provider="openrouter",
            model="claude-sonnet",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.002,
            agent_id="agent-001",
        )
        assert len(tracker.calls) == 1
        assert tracker.calls[0].provider == "openrouter"
        assert tracker.calls[0].input_tokens + tracker.calls[0].output_tokens == 150

    def test_record_multiple_calls(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        tracker.record_call(
            provider="openrouter",
            model="claude-sonnet",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            agent_id="agent-001",
        )
        assert len(tracker.calls) == 1

    def test_provider_summary(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        tracker.record_call("openrouter", "claude-sonnet", 100, 50, 0.001, agent_id="agent-001")
        tracker.record_call("openrouter", "claude-opus", 200, 100, 0.003, agent_id="agent-001")
        tracker.record_call("gemini", "gemini-pro", 50, 25, 0.0005, agent_id="agent-002")

        summaries = tracker.provider_summary()
        assert "openrouter" in summaries
        assert summaries["openrouter"].calls == 2
        assert summaries["openrouter"].total_input_tokens + summaries["openrouter"].total_output_tokens == 450

    def test_agent_summary(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        tracker.record_call("openrouter", "claude-sonnet", 100, 50, 0.0, agent_id="agent-001")
        tracker.record_call("openrouter", "claude-sonnet", 200, 100, 0.0, agent_id="agent-001")
        tracker.record_call("gemini", "gemini-pro", 50, 25, 0.0, agent_id="agent-002")

        session = tracker.session_summary("agent-001")
        assert session["calls"] == 2
        assert session["total_tokens"] == 450

    def test_total_cost(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        tracker.record_call("openrouter", "claude-sonnet", 100, 50, 0.001, agent_id="agent-001")
        tracker.record_call("openrouter", "claude-sonnet", 200, 100, 0.002, agent_id="agent-001")

        assert abs(tracker.total_cost() - 0.003) < 1e-9

    def test_get_usage_tracker_singleton(self, tmp_path: Path) -> None:
        # First call creates the tracker
        tracker1 = get_usage_tracker(tmp_path)
        # Second call returns the same instance
        tracker2 = get_usage_tracker(tmp_path)
        assert tracker1 is tracker2


# ---------------------------------------------------------------------------
# _git_cochanged_files — batched subprocess + lru_cache
# ---------------------------------------------------------------------------


class TestGitCochangedFiles:
    """Tests for the batched git co-changed files lookup."""

    def _make_log_output(self, entries: list[tuple[str, list[str]]]) -> str:
        """Build a fake ``git log --name-only`` output string."""
        blocks = []
        for commit_hash, files in entries:
            block = commit_hash + "\n" + "\n".join(files)
            blocks.append(block)
        return "\n\n".join(blocks)

    def test_returns_list(self, tmp_path: Path) -> None:
        """_git_cochanged_files returns a list (possibly empty)."""
        result = _git_cochanged_files("nonexistent.py", tmp_path, 5)
        assert isinstance(result, list)

    def test_returns_empty_for_missing_file(self, tmp_path: Path) -> None:
        """Returns empty list for files not in the repo."""
        result = _git_cochanged_files("src/missing.py", tmp_path, 5)
        assert result == []
