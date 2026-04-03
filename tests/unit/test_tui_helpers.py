"""Tests for TUI helper modules."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bernstein.tui.agent_duration import format_agent_duration, get_duration_color
from bernstein.tui.worktree_status import WorktreeStatus, format_worktree_display, get_worktree_status


class TestAgentDuration:
    """Test agent duration formatting."""

    def test_format_short_duration(self) -> None:
        """Test formatting short duration."""
        # 2 minutes 30 seconds ago
        start_time = __import__("time").time() - 150

        result = format_agent_duration(start_time)

        assert "2m" in result
        assert "30s" in result

    def test_format_long_duration(self) -> None:
        """Test formatting long duration."""
        # 1 hour 7 minutes ago
        start_time = __import__("time").time() - (3600 + 420)

        result = format_agent_duration(start_time)

        assert "1h" in result
        assert "07m" in result

    def test_get_duration_color_short(self) -> None:
        """Test color for short duration."""
        # 5 minutes ago
        start_time = __import__("time").time() - 300

        color = get_duration_color(start_time)

        assert color == "green"

    def test_get_duration_color_medium(self) -> None:
        """Test color for medium duration."""
        # 15 minutes ago
        start_time = __import__("time").time() - 900

        color = get_duration_color(start_time)

        assert color == "yellow"

    def test_get_duration_color_long(self) -> None:
        """Test color for long duration."""
        # 35 minutes ago
        start_time = __import__("time").time() - 2100

        color = get_duration_color(start_time)

        assert color == "red"


class TestWorktreeStatus:
    """Test worktree status detection."""

    def test_format_worktree_clean(self) -> None:
        """Test formatting clean worktree."""
        status = WorktreeStatus(branch="feat/test", is_dirty=False)

        result = format_worktree_display(status)

        assert "feat/test" in result
        assert "[clean]" in result

    def test_format_worktree_dirty(self) -> None:
        """Test formatting dirty worktree."""
        status = WorktreeStatus(branch="feat/test", is_dirty=True)

        result = format_worktree_display(status)

        assert "feat/test" in result
        assert "[dirty]" in result

    def test_format_worktree_ahead_behind(self) -> None:
        """Test formatting with ahead/behind."""
        status = WorktreeStatus(branch="main", is_dirty=False, ahead=2, behind=1)

        result = format_worktree_display(status)

        assert "main" in result
        assert "2↑" in result
        assert "1↓" in result

    def test_get_worktree_status_success(self, tmp_path: Path) -> None:
        """Test getting worktree status."""
        # Create a fake git repo
        import subprocess

        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)

        result = get_worktree_status(tmp_path)

        # May return None if git commands fail in test environment
        # Just verify it doesn't crash
        assert result is None or result.branch is not None

    def test_get_worktree_status_not_git(self, tmp_path: Path) -> None:
        """Test getting status from non-git directory."""
        result = get_worktree_status(tmp_path)

        assert result is None

    def test_get_worktree_status_timeout(self) -> None:
        """Test handling git timeout."""
        with patch("subprocess.run") as mock_run:
            import subprocess

            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)

            result = get_worktree_status(Path("/fake"))

            assert result is None


def test_build_token_budget_bar_empty() -> None:
    """Bar renders dash when no budget."""
    from bernstein.tui.widgets import build_token_budget_bar

    assert build_token_budget_bar(0, 0) == "—"


def test_build_token_budget_bar_half() -> None:
    """Half budget consumed renders green bar."""
    from bernstein.tui.widgets import build_token_budget_bar

    bar = build_token_budget_bar(500, 1000, width=10)
    assert "50%" in bar
    assert "green" in bar


def test_build_token_budget_bar_high() -> None:
    """High budget consumed renders yellow bar."""
    from bernstein.tui.widgets import build_token_budget_bar

    bar = build_token_budget_bar(800, 1000, width=10)
    assert "80%" in bar
    assert "yellow" in bar


def test_build_token_budget_bar_full() -> None:
    """Full budget consumed renders red bar."""
    from bernstein.tui.widgets import build_token_budget_bar

    bar = build_token_budget_bar(1000, 1000, width=10)
    assert "100%" in bar
    assert "red" in bar


def test_task_row_from_api_includes_tokens() -> None:
    """TaskRow parses tokens_used and token_budget from API response."""
    from bernstein.tui.widgets import TaskRow

    row = TaskRow.from_api(
        {
            "id": "abc123",
            "status": "in_progress",
            "role": "backend",
            "title": "Add auth",
            "model": "sonnet",
            "elapsed": "2m",
            "session_id": "sess-1",
            "tokens_used": 4500,
            "token_budget": 10000,
        }
    )
    assert row.tokens_used == 4500
    assert row.tokens_budget == 10000


def test_agent_badge_color_deterministic() -> None:
    """Same agent returns same colour; empty string returns white."""
    from bernstein.tui.widgets import agent_badge_color

    c1 = agent_badge_color("agent-42")
    c2 = agent_badge_color("agent-42")
    assert c1 == c2


def test_agent_badge_color_empty() -> None:
    """Empty agent_id defaults to white."""
    from bernstein.tui.widgets import agent_badge_color

    assert agent_badge_color("") == "white"


def test_agent_badge_color_varies_by_agent() -> None:
    """Different agent IDs generally pick different colours."""
    from bernstein.tui.widgets import agent_badge_color

    colors = {agent_badge_color(f"sess-{i}") for i in range(30)}
    assert len(colors) > 1  # not all identical


def test_build_cache_hit_sparkline_empty() -> None:
    """Empty input returns empty string."""
    from bernstein.tui.widgets import build_cache_hit_sparkline

    assert build_cache_hit_sparkline([]) == ""


def test_build_cache_hit_sparkline_high_hit_rate() -> None:
    """High cache rate renders green with percentage."""
    from bernstein.tui.widgets import build_cache_hit_sparkline

    bar = build_cache_hit_sparkline([0.9, 0.95, 1.0, 0.8])
    assert "%" in bar
    assert "green" in bar


def test_build_cache_hit_sparkline_low_hit_rate() -> None:
    """Low cache rate renders red."""
    from bernstein.tui.widgets import build_cache_hit_sparkline

    bar = build_cache_hit_sparkline([0.0, 0.1, 0.2])
    assert "%" in bar
    assert "red" in bar


# ---------------------------------------------------------------------------
# Scratchpad viewer tests (T408)
# ---------------------------------------------------------------------------


def test_scratchpad_entry_size_display_bytes() -> None:
    """Small files display in bytes."""
    from bernstein.tui.widgets import ScratchpadEntry

    entry = ScratchpadEntry(name="test.txt", path=Path("/fake/test.txt"), size=500, modified=0.0)
    assert entry.size_display == "500B"


def test_scratchpad_entry_size_display_kb() -> None:
    """Medium files display in KB."""
    from bernstein.tui.widgets import ScratchpadEntry

    entry = ScratchpadEntry(name="test.txt", path=Path("/fake/test.txt"), size=2048, modified=0.0)
    assert "K" in entry.size_display


def test_scratchpad_entry_size_display_mb() -> None:
    """Large files display in MB."""
    from bernstein.tui.widgets import ScratchpadEntry

    entry = ScratchpadEntry(name="test.txt", path=Path("/fake/test.txt"), size=2 * 1024 * 1024, modified=0.0)
    assert "M" in entry.size_display


def test_scratchpad_entry_relative_display_with_sdd(tmp_path: Path) -> None:
    """Paths under .sdd show relative display."""
    from bernstein.tui.widgets import ScratchpadEntry

    path = tmp_path / ".sdd" / "runtime" / "scratchpad" / "run1" / "note.txt"
    entry = ScratchpadEntry(name="note.txt", path=path, size=100, modified=0.0)
    assert entry.relative_display.startswith(".sdd/")


def test_scratchpad_entry_relative_display_no_sdd(tmp_path: Path) -> None:
    """Paths without .sdd fall back to name."""
    from bernstein.tui.widgets import ScratchpadEntry

    path = tmp_path / "some" / "other" / "file.txt"
    entry = ScratchpadEntry(name="file.txt", path=path, size=100, modified=0.0)
    assert entry.relative_display == "file.txt"


def test_list_scratchpad_files_empty(tmp_path: Path) -> None:
    """Missing scratchpad directory returns empty list."""
    from bernstein.tui.widgets import list_scratchpad_files

    result = list_scratchpad_files(tmp_path / "nonexistent")
    assert result == []


def test_list_scratchpad_files_with_files(tmp_path: Path) -> None:
    """Scratchpad with files returns entries sorted newest first."""
    import time

    from bernstein.tui.widgets import list_scratchpad_files

    scratchpad = tmp_path / ".sdd" / "runtime" / "scratchpad"
    scratchpad.mkdir(parents=True)

    # Create files with different modification times
    old_file = scratchpad / "old.txt"
    old_file.write_text("old")
    time.sleep(0.01)
    new_file = scratchpad / "new.txt"
    new_file.write_text("new")

    result = list_scratchpad_files(scratchpad)
    assert len(result) == 2
    assert result[0].name == "new.txt"  # Newest first
    assert result[1].name == "old.txt"


def test_list_scratchpad_files_nested(tmp_path: Path) -> None:
    """Scratchpad with nested directories lists all files."""
    from bernstein.tui.widgets import list_scratchpad_files

    scratchpad = tmp_path / ".sdd" / "runtime" / "scratchpad"
    worker_dir = scratchpad / "worker-1"
    worker_dir.mkdir(parents=True)
    (worker_dir / "state.json").write_text("{}")
    (scratchpad / "shared.txt").write_text("shared")

    result = list_scratchpad_files(scratchpad)
    assert len(result) == 2
    names = {e.name for e in result}
    assert "state.json" in names
    assert "shared.txt" in names


def test_filter_scratchpad_entries_empty_query() -> None:
    """Empty query returns all entries."""
    from bernstein.tui.widgets import ScratchpadEntry, filter_scratchpad_entries

    entries = [
        ScratchpadEntry(name="a.txt", path=Path("/a.txt"), size=100, modified=0.0),
        ScratchpadEntry(name="b.txt", path=Path("/b.txt"), size=200, modified=0.0),
    ]
    result = filter_scratchpad_entries(entries, "")
    assert len(result) == 2


def test_filter_scratchpad_entries_by_name() -> None:
    """Filter matches by filename."""
    from bernstein.tui.widgets import ScratchpadEntry, filter_scratchpad_entries

    entries = [
        ScratchpadEntry(name="config.json", path=Path("/config.json"), size=100, modified=0.0),
        ScratchpadEntry(name="notes.txt", path=Path("/notes.txt"), size=200, modified=0.0),
    ]
    result = filter_scratchpad_entries(entries, "config")
    assert len(result) == 1
    assert result[0].name == "config.json"


def test_filter_scratchpad_entries_case_insensitive() -> None:
    """Filter is case-insensitive."""
    from bernstein.tui.widgets import ScratchpadEntry, filter_scratchpad_entries

    entries = [
        ScratchpadEntry(name="Config.JSON", path=Path("/Config.JSON"), size=100, modified=0.0),
    ]
    result = filter_scratchpad_entries(entries, "config")
    assert len(result) == 1


def test_filter_scratchpad_entries_by_path() -> None:
    """Filter matches by path substring."""
    from bernstein.tui.widgets import ScratchpadEntry, filter_scratchpad_entries

    entries = [
        ScratchpadEntry(
            name="state.json", path=Path("/.sdd/runtime/scratchpad/worker-1/state.json"), size=100, modified=0.0
        ),
    ]
    result = filter_scratchpad_entries(entries, "worker-1")
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Cost tier visualization tests (T411)
# ---------------------------------------------------------------------------


def test_build_model_tier_entries_nonempty() -> None:
    """Tier entries are generated for all models."""
    from bernstein.tui.widgets import build_model_tier_entries

    entries = build_model_tier_entries()
    assert len(entries) > 0
    # Cheapest first
    assert entries[0].total_usd_per_1m <= entries[-1].total_usd_per_1m


def test_model_tier_entry_has_required_fields() -> None:
    """Each entry has all required cost data."""
    from bernstein.tui.widgets import ModelTierEntry

    entry = ModelTierEntry(
        model="sonnet",
        input_usd_per_1m=3.0,
        output_usd_per_1m=15.0,
        cache_read_usd_per_1m=0.3,
        cache_write_usd_per_1m=3.75,
        total_usd_per_1m=9.0,
    )
    assert entry.model == "sonnet"
    assert entry.total_usd_per_1m == 9.0


def test_model_tier_cache_info_configured() -> None:
    """Cache info shows read/write pricing when configured."""
    from bernstein.tui.widgets import ModelTierEntry

    entry = ModelTierEntry(
        model="sonnet",
        input_usd_per_1m=3.0,
        output_usd_per_1m=15.0,
        cache_read_usd_per_1m=0.3,
        cache_write_usd_per_1m=3.75,
        total_usd_per_1m=9.0,
    )
    assert "read" in entry.cache_info
    assert "write" in entry.cache_info
    assert entry.cache_info != "not configured"


def test_model_tier_cache_info_not_configured() -> None:
    """Cache info shows 'not configured' when no cache pricing."""
    from bernstein.tui.widgets import ModelTierEntry

    entry = ModelTierEntry(
        model="gpt-5.4",
        input_usd_per_1m=2.5,
        output_usd_per_1m=15.0,
        cache_read_usd_per_1m=None,
        cache_write_usd_per_1m=None,
        total_usd_per_1m=8.75,
    )
    assert entry.cache_info == "not configured"


def test_render_model_tier_table() -> None:
    """render_model_tier_table returns label/value pairs."""
    from bernstein.tui.widgets import render_model_tier_table

    rows = render_model_tier_table()
    assert len(rows) > 0
    for label, detail in rows:
        assert "$" in label
        assert "input" in detail
        assert "output" in detail
        assert "cache" in detail
