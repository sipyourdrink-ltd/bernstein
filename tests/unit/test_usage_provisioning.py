"""Tests for usage budget provisioning (usage_provisioning.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from bernstein.cli.usage_provisioning import (
    UsageBudget,
    _budget_color,
    _make_progress_bar,
    _next_midnight_ts,
    _read_api_usage_metric_points,
    _read_cost_metric_points,
    _read_jsonl,
    _today_prefix,
    check_usage_budget,
    format_usage_report,
    is_over_budget,
    load_usage_budget_config,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _write_yaml(workdir: Path, content: str) -> None:
    rules_dir = workdir / ".bernstein"
    rules_dir.mkdir(exist_ok=True)
    (rules_dir / "usage_budget.yaml").write_text(content, encoding="utf-8")


def _write_metric_point(
    workdir: Path,
    filename: str,
    metric_type: str,
    value: float,
    labels: dict[str, str] | None = None,
) -> None:
    """Write a single JSONL metric point to the workdir's .sdd/metrics/."""
    metrics_dir = workdir / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    point = {
        "timestamp": 1700000000.0,
        "metric_type": metric_type,
        "value": value,
        "labels": labels or {},
    }
    filepath = metrics_dir / filename
    with filepath.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(point) + "\n")


# ---------------------------------------------------------------------------
# load_usage_budget_config
# ---------------------------------------------------------------------------


class TestLoadUsageBudgetConfig:
    def test_returns_none_when_no_yaml(self, tmp_path: Path) -> None:
        assert load_usage_budget_config(tmp_path) is None

    def test_parses_both_limits(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path,
            "daily_limit_usd: 10.0\ndaily_limit_tokens: 100000\n",
        )
        cfg = load_usage_budget_config(tmp_path)
        assert cfg is not None
        assert cfg.daily_limit_usd == pytest.approx(10.0)
        assert cfg.daily_limit_tokens == 100000
        assert cfg.timezone == "UTC"

    def test_defaults_to_unlimited_when_limits_missing(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "timezone: UTC\n")
        cfg = load_usage_budget_config(tmp_path)
        assert cfg is not None
        assert cfg.daily_limit_usd is None
        assert cfg.daily_limit_tokens is None

    def test_custom_timezone_is_preserved(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "daily_limit_usd: 5.0\ntimezone: US/Eastern\n")
        cfg = load_usage_budget_config(tmp_path)
        assert cfg is not None
        assert cfg.timezone == "US/Eastern"

    def test_returns_none_on_malformed_yaml(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / ".bernstein"
        rules_dir.mkdir()
        (rules_dir / "usage_budget.yaml").write_text(":\n  - broken: [", encoding="utf-8")
        assert load_usage_budget_config(tmp_path) is None

    def test_empty_file_gives_default_config(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "")
        cfg = load_usage_budget_config(tmp_path)
        assert cfg is not None
        assert cfg.daily_limit_usd is None
        assert cfg.daily_limit_tokens is None

    def test_float_limit_tokens_is_int_cast(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "daily_limit_tokens: 50000.5\n")
        cfg = load_usage_budget_config(tmp_path)
        assert cfg is not None
        assert cfg.daily_limit_tokens == 50000


# ---------------------------------------------------------------------------
# _read_jsonl
# ---------------------------------------------------------------------------


class TestReadJsonl:
    def test_nonexistent_file_returns_empty(self, tmp_path: Path) -> None:
        assert _read_jsonl(tmp_path / "nope.jsonl") == []

    def test_reads_valid_records(self, tmp_path: Path) -> None:
        fp = tmp_path / "data.jsonl"
        fp.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
        records = _read_jsonl(fp)
        assert records == [{"a": 1}, {"a": 2}]

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        fp = tmp_path / "data.jsonl"
        fp.write_text('{"a": 1}\n\n{"a": 3}\n', encoding="utf-8")
        records = _read_jsonl(fp)
        assert len(records) == 2


# ---------------------------------------------------------------------------
# _today_prefix and _next_midnight_ts
# ---------------------------------------------------------------------------


class TestDateHelpers:
    def test_today_prefix_is_valid_format(self) -> None:
        prefix = _today_prefix()
        # Should parse as a date
        datetime.strptime(prefix, "%Y-%m-%d")

    def test_next_midnight_is_in_the_future(self) -> None:
        ts = _next_midnight_ts()
        assert ts > datetime.now(UTC).timestamp()

    def test_next_midnight_is_at_midnight_utc(self) -> None:
        ts = _next_midnight_ts()
        dt = datetime.fromtimestamp(ts, tz=UTC)
        assert dt.hour == 0
        assert dt.minute == 0
        assert dt.second == 0


# ---------------------------------------------------------------------------
# Metric point readers
# ---------------------------------------------------------------------------


class TestReadMetricPoints:
    def test_no_metrics_returns_empty(self, tmp_path: Path) -> None:
        pts = _read_cost_metric_points(tmp_path / ".sdd" / "metrics", "2026-01-01")
        assert pts == []

    def test_reads_cost_metric_points(self, tmp_path: Path) -> None:
        _write_metric_point(tmp_path, "cost_efficiency_2026-01-01.jsonl", "cost_efficiency", 0.009)
        _write_metric_point(tmp_path, "cost_efficiency_2026-01-01.jsonl", "cost_efficiency", 0.015)
        pts = _read_cost_metric_points(tmp_path / ".sdd" / "metrics", "2026-01-01")
        assert len(pts) == 2
        assert [p["value"] for p in pts] == [0.009, 0.015]

    def test_reads_api_usage_points(self, tmp_path: Path) -> None:
        _write_metric_point(tmp_path, "api_usage_2026-01-01.jsonl", "api_usage", 1500.0)
        _write_metric_point(tmp_path, "api_usage_2026-01-01.jsonl", "api_usage", 3000.0)
        pts = _read_api_usage_metric_points(tmp_path / ".sdd" / "metrics", "2026-01-01")
        assert len(pts) == 2


# ---------------------------------------------------------------------------
# check_usage_budget
# ---------------------------------------------------------------------------


class TestCheckUsageBudget:
    def test_aggregates_usage_from_metrics(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path,
            "daily_limit_usd: 10.0\ndaily_limit_tokens: 100000\n",
        )
        today = _today_prefix()
        _write_metric_point(tmp_path, f"cost_efficiency_{today}.jsonl", "cost_efficiency", 0.5)
        _write_metric_point(tmp_path, f"cost_efficiency_{today}.jsonl", "cost_efficiency", 1.5)
        _write_metric_point(tmp_path, f"api_usage_{today}.jsonl", "api_usage", 5000.0)
        _write_metric_point(tmp_path, f"api_usage_{today}.jsonl", "api_usage", 3000.0)

        budget = check_usage_budget(tmp_path)
        assert budget.daily_limit_usd == pytest.approx(10.0)
        assert budget.daily_limit_tokens == 100000
        assert budget.current_usage_usd == pytest.approx(2.0)
        assert budget.current_usage_tokens == 8000
        assert budget.reset_at_ts > 0

    def test_returns_unlimited_when_no_config(self, tmp_path: Path) -> None:
        budget = check_usage_budget(tmp_path)
        assert budget.daily_limit_usd is None
        assert budget.daily_limit_tokens is None
        assert budget.current_usage_usd == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# is_over_budget
# ---------------------------------------------------------------------------


class TestIsOverBudget:
    def test_no_limits_means_never_over(self) -> None:
        budget = UsageBudget()
        assert not is_over_budget(budget)

    def test_usd_over_budget(self) -> None:
        budget = UsageBudget(
            daily_limit_usd=5.0,
            daily_limit_tokens=None,
            current_usage_usd=6.0,
        )
        assert is_over_budget(budget)

    def test_usd_under_budget(self) -> None:
        budget = UsageBudget(
            daily_limit_usd=10.0,
            current_usage_usd=5.0,
        )
        assert not is_over_budget(budget)

    def test_usd_exactly_at_limit_is_over(self) -> None:
        budget = UsageBudget(daily_limit_usd=10.0, current_usage_usd=10.0)
        assert is_over_budget(budget)

    def test_tokens_over_budget(self) -> None:
        budget = UsageBudget(
            daily_limit_tokens=100,
            current_usage_tokens=150,
        )
        assert is_over_budget(budget)

    def test_usd_over_tokens_under_still_over(self) -> None:
        budget = UsageBudget(
            daily_limit_usd=5.0,
            daily_limit_tokens=10000,
            current_usage_usd=6.0,
            current_usage_tokens=100,
        )
        assert is_over_budget(budget)

    def test_usd_under_tokens_over_still_over(self) -> None:
        budget = UsageBudget(
            daily_limit_usd=10.0,
            daily_limit_tokens=500,
            current_usage_usd=2.0,
            current_usage_tokens=600,
        )
        assert is_over_budget(budget)


# ---------------------------------------------------------------------------
# _make_progress_bar and _budget_color
# ---------------------------------------------------------------------------


class TestProgressBar:
    def test_full_bar(self) -> None:
        assert _make_progress_bar(1.0, width=10) == "█" * 10

    def test_empty_bar(self) -> None:
        assert _make_progress_bar(0.0, width=10) == "░" * 10

    def test_half_bar(self) -> None:
        bar = _make_progress_bar(0.5, width=10)
        assert bar.count("█") == 5
        assert bar.count("░") == 5


class TestBudgetColor:
    @pytest.mark.parametrize("pct,expected", [(0.0, "green"), (0.5, "green"), (0.74, "green")])
    def test_green_below_75(self, pct: float, expected: str) -> None:
        assert _budget_color(pct) == expected

    @pytest.mark.parametrize("pct,expected", [(0.75, "yellow"), (0.9, "yellow"), (0.99, "yellow")])
    def test_yellow_at_75_to_99(self, pct: float, expected: str) -> None:
        assert _budget_color(pct) == expected

    @pytest.mark.parametrize("pct,expected", [(1.0, "bold red"), (1.5, "bold red")])
    def test_red_at_100_or_more(self, pct: float, expected: str) -> None:
        assert _budget_color(pct) == expected


# ---------------------------------------------------------------------------
# format_usage_report
# ---------------------------------------------------------------------------


class TestFormatUsageReport:
    def test_report_has_limits_and_progress(self) -> None:
        budget = UsageBudget(
            daily_limit_usd=10.0,
            daily_limit_tokens=10000,
            current_usage_usd=5.0,
            current_usage_tokens=5000,
            reset_at_ts=_next_midnight_ts(),
        )
        report = format_usage_report(budget)
        assert "USD:" in report
        assert "Tokens:" in report
        assert "Within budget" in report
        assert "Resets at:" in report

    def test_report_shows_over_budget_warning(self) -> None:
        budget = UsageBudget(
            daily_limit_usd=1.0,
            current_usage_usd=2.0,
        )
        report = format_usage_report(budget)
        assert "OVER BUDGET" in report

    def test_report_shows_unlimited_when_no_config(self) -> None:
        budget = UsageBudget()
        report = format_usage_report(budget)
        assert "USD:" in report
        assert "unlimited" in report
        assert "Tokens:" in report
        # Should say within budget
        assert "Within budget" in report

    def test_report_contains_config_path(self) -> None:
        budget = UsageBudget(
            config_path="/project/.bernstein/usage_budget.yaml",
        )
        report = format_usage_report(budget)
        assert "Config:" in report
        assert "/project/.bernstein/usage_budget.yaml" in report
