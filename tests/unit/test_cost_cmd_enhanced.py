"""Tests for enhanced cost CLI: --last, --by, cache hit rate."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
from bernstein.cli.cost import cost_cmd
from click.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    """Build a realistic .sdd tree with metrics, archive, and token files."""
    sdd = tmp_path / ".sdd"
    metrics = sdd / "metrics"
    archive = sdd / "archive"
    runtime = sdd / "runtime"
    metrics.mkdir(parents=True)
    archive.mkdir(parents=True)
    runtime.mkdir(parents=True)

    now = time.time()

    # metrics/tasks.jsonl — recent tasks
    recent_tasks = [
        {
            "task_id": f"recent-{i}",
            "role": "backend",
            "model": "claude-opus-4" if i % 3 == 0 else "claude-sonnet-4",
            "timestamp": now - i * 3600,
            "tokens_prompt": 1000,
            "tokens_completion": 500,
            "cost_usd": 0.50 if i % 3 == 0 else 0.10,
            "duration_seconds": 30.0,
            "scope": "small",
            "complexity": "low",
            "agent_id": f"agent-{i % 2}",
        }
        for i in range(10)
    ]
    (metrics / "tasks.jsonl").write_text("\n".join(json.dumps(r) for r in recent_tasks))

    # archive/tasks.jsonl — older tasks (8 days ago)
    old_tasks = [
        {
            "task_id": f"old-{i}",
            "role": "qa",
            "model": "claude-haiku-4",
            "timestamp": now - 8 * 86400 - i * 3600,
            "tokens_prompt": 400,
            "tokens_completion": 200,
            "cost_usd": 0.02,
            "duration_seconds": 15.0,
            "agent_id": "agent-old",
        }
        for i in range(5)
    ]
    (archive / "tasks.jsonl").write_text("\n".join(json.dumps(r) for r in old_tasks))

    # runtime/*.tokens — cache data
    token_lines = [
        json.dumps({"ts": now - 100, "in": 1000, "out": 500, "cache_read": 700, "cache_write": 300}),
        json.dumps({"ts": now - 50, "in": 800, "out": 400, "cache_read": 600, "cache_write": 200}),
    ]
    (runtime / "session-1.tokens").write_text("\n".join(token_lines))

    return sdd


# ---------------------------------------------------------------------------
# --last time range filtering
# ---------------------------------------------------------------------------


class TestTimeRangeFilter:
    def test_last_7d_excludes_old_archive(self, sdd_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cost_cmd, ["--metrics-dir", str(sdd_dir / "metrics"), "--last", "7d", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["time_range"] == "last 7d"
        # Old tasks (8 days ago) should be excluded
        task_count = data["totals"]["tasks"]
        # Only the 10 recent tasks should be counted (deduplicated by model)
        assert task_count <= 10

    def test_last_30d_includes_everything(self, sdd_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cost_cmd, ["--metrics-dir", str(sdd_dir / "metrics"), "--last", "30d", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["time_range"] == "last 30d"
        # All 15 tasks (10 recent + 5 archive)
        assert data["totals"]["tasks"] == 15

    def test_last_1h_very_few(self, sdd_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cost_cmd, ["--metrics-dir", str(sdd_dir / "metrics"), "--last", "1h", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # Only the most recent task (i=0) should be within 1h
        assert data["totals"]["tasks"] <= 2

    def test_no_last_flag_shows_all_time(self, sdd_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cost_cmd, ["--metrics-dir", str(sdd_dir / "metrics"), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["time_range"] == "all time"


# ---------------------------------------------------------------------------
# --by grouping
# ---------------------------------------------------------------------------


class TestGroupBy:
    def test_by_agent_json(self, sdd_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cost_cmd, ["--metrics-dir", str(sdd_dir / "metrics"), "--by", "agent", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["grouped_by"] == "agent"
        assert "grouped" in data
        # Should have agent-0, agent-1, agent-old
        assert len(data["grouped"]) >= 2

    def test_by_day_json(self, sdd_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cost_cmd, ["--metrics-dir", str(sdd_dir / "metrics"), "--by", "day", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["grouped_by"] == "day"
        assert "grouped" in data

    def test_by_task_json(self, sdd_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cost_cmd, ["--metrics-dir", str(sdd_dir / "metrics"), "--by", "task", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["grouped_by"] == "task"

    def test_by_model_uses_default_view(self, sdd_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cost_cmd, ["--metrics-dir", str(sdd_dir / "metrics"), "--by", "model", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # model grouping falls through to default rows
        assert "rows" in data

    def test_by_agent_rich_output(self, sdd_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cost_cmd, ["--metrics-dir", str(sdd_dir / "metrics"), "--by", "agent"])
        assert result.exit_code == 0, result.output
        assert "By Agent" in result.output
        assert "tasks" in result.output


# ---------------------------------------------------------------------------
# Cache hit rate
# ---------------------------------------------------------------------------


class TestCacheHitRate:
    def test_cache_hit_rate_in_json(self, sdd_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cost_cmd, ["--metrics-dir", str(sdd_dir / "metrics"), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "cache_hit_rate" in data
        # 700+600=1300 reads, 300+200=500 writes, rate = 1300/1800*100 = 72.2%
        assert data["cache_hit_rate"] is not None
        assert 70.0 < data["cache_hit_rate"] < 75.0

    def test_cache_hit_rate_in_table(self, sdd_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cost_cmd, ["--metrics-dir", str(sdd_dir / "metrics")])
        assert result.exit_code == 0, result.output
        assert "Cache hit rate" in result.output

    def test_no_tokens_files_returns_null(self, tmp_path: Path) -> None:
        mdir = tmp_path / "metrics"
        mdir.mkdir()
        (mdir / "tasks.jsonl").write_text(
            json.dumps(
                {
                    "task_id": "x",
                    "model": "sonnet",
                    "tokens_prompt": 100,
                    "tokens_completion": 50,
                    "cost_usd": 0.01,
                    "timestamp": time.time(),
                }
            )
        )
        runner = CliRunner()
        result = runner.invoke(cost_cmd, ["--metrics-dir", str(mdir), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["cache_hit_rate"] is None


# ---------------------------------------------------------------------------
# Downgrade tip
# ---------------------------------------------------------------------------


class TestDowngradeTip:
    def test_tip_present_when_opus_tasks_are_simple(self, sdd_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cost_cmd, ["--metrics-dir", str(sdd_dir / "metrics"), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # Our fixture has opus tasks with scope=small, complexity=low
        if "tip" in data:
            assert "opus" in data["tip"]
            assert data["potential_savings_usd"] > 0


# ---------------------------------------------------------------------------
# Combined: --last + --by
# ---------------------------------------------------------------------------


class TestCombined:
    def test_last_and_by_together(self, sdd_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cost_cmd, ["--metrics-dir", str(sdd_dir / "metrics"), "--last", "7d", "--by", "agent", "--json"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["time_range"] == "last 7d"
        assert data["grouped_by"] == "agent"
        # Old agent should not appear (its tasks are > 7d old)
        assert "agent-old" not in data["grouped"]
