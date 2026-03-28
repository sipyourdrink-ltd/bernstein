"""Tests for bernstein.core.context."""
from __future__ import annotations

from pathlib import Path

import pytest

from unittest.mock import MagicMock, patch

from bernstein.core.context import (
    _git_cochanged_files,
    _read_if_exists,
    _should_skip,
    ApiUsageTracker,
    available_roles,
    clear_caches,
    file_tree,
    gather_project_context,
    get_usage_tracker,
)
from bernstein.core.models import ApiTier


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

        with patch("bernstein.core.context.subprocess.run", return_value=mock_result) as mock_run:
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
        assert "no project context" in ctx

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
        record = tracker.record_call(
            provider="openrouter",
            model="claude-sonnet",
            agent_session_id="agent-001",
            tokens_input=100,
            tokens_output=50,
            cost_usd=0.002,
            latency_ms=250.0,
            success=True,
        )
        assert record.provider == "openrouter"
        assert record.tokens_total == 150
        assert record.success is True

    def test_record_failed_call(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        record = tracker.record_call(
            provider="openrouter",
            model="claude-sonnet",
            agent_session_id="agent-001",
            tokens_input=0,
            tokens_output=0,
            cost_usd=0.0,
            latency_ms=5000.0,
            success=False,
            error="Rate limit exceeded",
        )
        assert record.success is False
        assert record.error == "Rate limit exceeded"

    def test_provider_summary(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        tracker.record_call("openrouter", "claude-sonnet", "agent-001", tokens_input=100, tokens_output=50, cost_usd=0.001)
        tracker.record_call("openrouter", "claude-opus", "agent-001", tokens_input=200, tokens_output=100, cost_usd=0.003)
        tracker.record_call("gemini", "gemini-pro", "agent-002", tokens_input=50, tokens_output=25, cost_usd=0.0005)

        summary = tracker.get_provider_summary("openrouter")
        assert summary is not None
        assert summary.total_calls == 2
        assert summary.total_tokens == 450
        assert summary.total_cost_usd == 0.004
        assert "claude-sonnet" in summary.models_used
        assert "claude-opus" in summary.models_used

    def test_agent_summary(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        tracker.record_call("openrouter", "claude-sonnet", "agent-001", tokens_input=100, tokens_output=50)
        tracker.record_call("openrouter", "claude-sonnet", "agent-001", tokens_input=200, tokens_output=100)
        tracker.record_call("gemini", "gemini-pro", "agent-002", tokens_input=50, tokens_output=25)

        agent_summary = tracker.get_agent_summary("agent-001")
        assert agent_summary is not None
        assert agent_summary.total_calls == 2
        assert agent_summary.total_tokens == 450
        assert "openrouter" in agent_summary.providers_used

    def test_global_summary(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        tracker.record_call("openrouter", "claude-sonnet", "agent-001", tokens_input=100, tokens_output=50, cost_usd=0.001)
        tracker.record_call("openrouter", "claude-sonnet", "agent-001", tokens_input=200, tokens_output=100, cost_usd=0.002, success=False)

        summary = tracker.get_global_summary()
        assert summary["total_api_calls"] == "2"
        assert summary["total_tokens_consumed"] == "450"
        assert summary["successful_calls"] == "1"
        assert summary["failed_calls"] == "1"
        assert summary["providers_active"] == "1"

    def test_tier_consumption(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        tracker.set_tier_consumption(
            provider="openrouter",
            tier=ApiTier.PRO,
            tokens_used=50000,
            tokens_limit=100000,
            requests_used=500,
            requests_limit=1000,
        )

        tiers = tracker.get_tier_consumption("openrouter")
        assert len(tiers) == 1
        assert tiers[0].tier == ApiTier.PRO
        assert tiers[0].percentage_used == 50.0

    def test_summary_for_agent(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        tracker.record_call("openrouter", "claude-sonnet", "agent-001", tokens_input=100, tokens_output=50, cost_usd=0.0015)

        summary = tracker.get_summary_for_agent("agent-001")
        assert summary["agent_session_id"] == "agent-001"
        assert summary["total_calls"] == "1"
        assert summary["total_tokens"] == "150"
        assert "openrouter" in summary["providers_used"]

    def test_summary_for_nonexistent_agent(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        summary = tracker.get_summary_for_agent("nonexistent")
        assert "error" in summary

    def test_get_all_summaries(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        tracker.record_call("openrouter", "claude-sonnet", "agent-001", tokens_input=100)
        tracker.record_call("gemini", "gemini-pro", "agent-002", tokens_input=50)

        provider_summaries = tracker.get_all_provider_summaries()
        agent_summaries = tracker.get_all_agent_summaries()

        assert len(provider_summaries) == 2
        assert len(agent_summaries) == 2
        assert "openrouter" in provider_summaries
        assert "gemini" in provider_summaries

    def test_persists_to_file(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        tracker.record_call("openrouter", "claude-sonnet", "agent-001", tokens_input=100, tokens_output=50)

        # Check file was created
        files = list(tmp_path.glob("api_calls_*.jsonl"))
        assert len(files) == 1

    def test_export_summary(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        tracker.record_call("openrouter", "claude-sonnet", "agent-001", tokens_input=100, tokens_output=50, cost_usd=0.001)
        tracker.set_tier_consumption("openrouter", ApiTier.PRO, tokens_used=50000, tokens_limit=100000)

        output_path = tmp_path / "export.json"
        tracker.export_summary(output_path)

        assert output_path.exists()
        import json
        data = json.loads(output_path.read_text())
        assert "global_summary" in data
        assert "provider_summaries" in data
        assert "agent_summaries" in data
        assert "tier_consumption" in data

    def test_latency_ema(self, tmp_path: Path) -> None:
        tracker = ApiUsageTracker(tmp_path)
        # First call sets the EMA
        tracker.record_call("openrouter", "claude-sonnet", "agent-001", latency_ms=100.0)
        # Second call should update EMA (alpha=0.3)
        tracker.record_call("openrouter", "claude-sonnet", "agent-001", latency_ms=200.0)

        summary = tracker.get_provider_summary("openrouter")
        assert summary is not None
        # EMA = 0.3 * 200 + 0.7 * 100 = 60 + 70 = 130
        assert 120 < summary.avg_latency_ms < 140

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

    def test_second_call_uses_cache(self, tmp_path: Path) -> None:
        """Repeated calls with identical args must not spawn a second subprocess."""
        _git_cochanged_files.cache_clear()
        fake_output = self._make_log_output([
            ("abc123", ["src/a.py", "src/b.py"]),
            ("def456", ["src/a.py", "src/c.py"]),
        ])
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = fake_output

        with patch("bernstein.core.context.subprocess.run", return_value=mock_result) as mock_run:
            result1 = _git_cochanged_files("src/a.py", tmp_path, 5)
            result2 = _git_cochanged_files("src/a.py", tmp_path, 5)

        assert mock_run.call_count == 1, "subprocess.run called more than once — cache miss"
        assert result1 == result2

    def test_single_subprocess_call(self, tmp_path: Path) -> None:
        """Verify only one subprocess is spawned regardless of commit count."""
        _git_cochanged_files.cache_clear()
        commits = [(f"{'a' * 40}", ["src/x.py", "src/y.py"]) for _ in range(20)]
        fake_output = self._make_log_output(commits)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = fake_output

        with patch("bernstein.core.context.subprocess.run", return_value=mock_result) as mock_run:
            _git_cochanged_files("src/x.py", tmp_path, 5)

        assert mock_run.call_count == 1

    def test_counts_cochanged_correctly(self, tmp_path: Path) -> None:
        """Most frequently co-changed file appears first."""
        _git_cochanged_files.cache_clear()
        fake_output = self._make_log_output([
            ("aaa", ["target.py", "src/frequent.py"]),
            ("bbb", ["target.py", "src/frequent.py"]),
            ("ccc", ["target.py", "src/rare.py"]),
        ])
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = fake_output

        with patch("bernstein.core.context.subprocess.run", return_value=mock_result):
            result = _git_cochanged_files("target.py", tmp_path, 5)

        assert result[0] == "src/frequent.py"
        assert "src/rare.py" in result

    def test_returns_empty_on_git_failure(self, tmp_path: Path) -> None:
        _git_cochanged_files.cache_clear()
        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stdout = ""

        with patch("bernstein.core.context.subprocess.run", return_value=mock_result):
            result = _git_cochanged_files("src/missing.py", tmp_path, 5)

        assert result == []
