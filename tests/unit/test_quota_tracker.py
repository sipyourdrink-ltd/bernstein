"""Tests for bernstein.core.cost.quota_tracker."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bernstein.core.cost.quota_tracker import (
    QuotaAlert,
    QuotaStatus,
    QuotaTracker,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tracker() -> QuotaTracker:
    """QuotaTracker with persistence disabled."""
    return QuotaTracker(sdd_root=None)


@pytest.fixture
def tracker_with_persistence(tmp_path: Path) -> QuotaTracker:
    """QuotaTracker with persistence to a temp directory."""
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    return QuotaTracker(sdd_root=sdd)


# ---------------------------------------------------------------------------
# QuotaStatus / QuotaAlert dataclass tests
# ---------------------------------------------------------------------------


class TestQuotaStatusFrozen:
    """QuotaStatus is a frozen dataclass."""

    def test_frozen(self) -> None:
        status = QuotaStatus(
            provider="anthropic",
            requests_used=10,
            requests_limit=100,
            tokens_used=500,
            tokens_limit=10000,
            utilization_pct=10.0,
            resets_at=None,
            tier="pro",
        )
        with pytest.raises(AttributeError):
            status.provider = "openai"  # type: ignore[misc]

    def test_fields(self) -> None:
        now = datetime.now(tz=UTC)
        status = QuotaStatus(
            provider="openai",
            requests_used=50,
            requests_limit=200,
            tokens_used=8000,
            tokens_limit=10000,
            utilization_pct=80.0,
            resets_at=now,
            tier="max",
        )
        assert status.provider == "openai"
        assert status.utilization_pct == pytest.approx(80.0)
        assert status.resets_at == now
        assert status.tier == "max"


class TestQuotaAlertFrozen:
    """QuotaAlert is a frozen dataclass."""

    def test_frozen(self) -> None:
        alert = QuotaAlert(
            provider="anthropic",
            message="test",
            severity="warning",
            utilization_pct=85.0,
        )
        with pytest.raises(AttributeError):
            alert.severity = "info"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# record_request / get_status
# ---------------------------------------------------------------------------


class TestRecordAndStatus:
    """Recording requests updates status correctly."""

    def test_single_request(self, tracker: QuotaTracker) -> None:
        tracker.configure_provider("anthropic", requests_limit=100, tokens_limit=10000)
        tracker.record_request("anthropic", tokens_in=500, tokens_out=200)

        status = tracker.get_status("anthropic")
        assert status.requests_used == 1
        assert status.tokens_used == 700
        assert status.utilization_pct == pytest.approx(7.0)

    def test_multiple_requests(self, tracker: QuotaTracker) -> None:
        tracker.configure_provider("openai", requests_limit=50, tokens_limit=5000)
        tracker.record_request("openai", tokens_in=100, tokens_out=100)
        tracker.record_request("openai", tokens_in=200, tokens_out=300)

        status = tracker.get_status("openai")
        assert status.requests_used == 2
        assert status.tokens_used == 700
        assert status.utilization_pct == pytest.approx(14.0)

    def test_unconfigured_provider(self, tracker: QuotaTracker) -> None:
        tracker.record_request("unknown", tokens_in=100, tokens_out=50)
        status = tracker.get_status("unknown")
        assert status.requests_used == 1
        assert status.tokens_used == 150
        assert status.utilization_pct == pytest.approx(0.0)  # no limits set

    def test_request_utilization_dominates(self, tracker: QuotaTracker) -> None:
        tracker.configure_provider("a", requests_limit=10, tokens_limit=100000)
        for _ in range(9):
            tracker.record_request("a", tokens_in=1, tokens_out=1)
        status = tracker.get_status("a")
        assert status.utilization_pct == pytest.approx(90.0)

    def test_cache_tokens_tracked(self, tracker: QuotaTracker) -> None:
        tracker.configure_provider("anthropic", requests_limit=100, tokens_limit=10000)
        tracker.record_request("anthropic", tokens_in=100, tokens_out=50, cache_read=30, cache_write=10)
        # cache tokens don't add to tokens_used, only recorded for reporting
        status = tracker.get_status("anthropic")
        assert status.tokens_used == 150


# ---------------------------------------------------------------------------
# get_all_statuses
# ---------------------------------------------------------------------------


class TestGetAllStatuses:
    """get_all_statuses returns sorted list."""

    def test_empty(self, tracker: QuotaTracker) -> None:
        assert tracker.get_all_statuses() == []

    def test_multiple_providers(self, tracker: QuotaTracker) -> None:
        tracker.configure_provider("openai", requests_limit=100, tokens_limit=10000)
        tracker.configure_provider("anthropic", requests_limit=200, tokens_limit=20000)
        tracker.record_request("openai", tokens_in=100, tokens_out=0)

        statuses = tracker.get_all_statuses()
        assert len(statuses) == 2
        assert statuses[0].provider == "anthropic"
        assert statuses[1].provider == "openai"


# ---------------------------------------------------------------------------
# check_alerts
# ---------------------------------------------------------------------------


class TestCheckAlerts:
    """Alert thresholds at 70% (info), 85% (warning), 95% (critical)."""

    def test_no_alerts_below_threshold(self, tracker: QuotaTracker) -> None:
        tracker.configure_provider("a", requests_limit=100, tokens_limit=10000)
        tracker.record_request("a", tokens_in=300, tokens_out=200)
        assert tracker.check_alerts() == []

    def test_info_alert(self, tracker: QuotaTracker) -> None:
        tracker.configure_provider("a", requests_limit=100, tokens_limit=1000)
        tracker.record_request("a", tokens_in=400, tokens_out=350)
        alerts = tracker.check_alerts()
        assert len(alerts) == 1
        assert alerts[0].severity == "info"
        assert alerts[0].utilization_pct == pytest.approx(75.0)

    def test_warning_alert(self, tracker: QuotaTracker) -> None:
        tracker.configure_provider("a", requests_limit=100, tokens_limit=1000)
        tracker.record_request("a", tokens_in=500, tokens_out=380)
        alerts = tracker.check_alerts()
        assert len(alerts) == 1
        assert alerts[0].severity == "warning"

    def test_critical_alert(self, tracker: QuotaTracker) -> None:
        tracker.configure_provider("a", requests_limit=100, tokens_limit=1000)
        tracker.record_request("a", tokens_in=600, tokens_out=370)
        alerts = tracker.check_alerts()
        assert len(alerts) == 1
        assert alerts[0].severity == "critical"


# ---------------------------------------------------------------------------
# should_throttle
# ---------------------------------------------------------------------------


class TestShouldThrottle:
    """Throttle fires at >90% utilization."""

    def test_below_threshold(self, tracker: QuotaTracker) -> None:
        tracker.configure_provider("a", requests_limit=100, tokens_limit=1000)
        tracker.record_request("a", tokens_in=400, tokens_out=0)
        assert tracker.should_throttle("a") is False

    def test_above_threshold(self, tracker: QuotaTracker) -> None:
        tracker.configure_provider("a", requests_limit=100, tokens_limit=1000)
        tracker.record_request("a", tokens_in=500, tokens_out=420)
        assert tracker.should_throttle("a") is True

    def test_unknown_provider(self, tracker: QuotaTracker) -> None:
        assert tracker.should_throttle("nope") is False


# ---------------------------------------------------------------------------
# render_status_line
# ---------------------------------------------------------------------------


class TestRenderStatusLine:
    """TUI status line rendering."""

    def test_no_providers(self, tracker: QuotaTracker) -> None:
        assert tracker.render_status_line() == "no providers tracked"

    def test_single_provider(self, tracker: QuotaTracker) -> None:
        tracker.configure_provider("anthropic", requests_limit=100, tokens_limit=1000)
        tracker.record_request("anthropic", tokens_in=200, tokens_out=220, cache_read=100)
        line = tracker.render_status_line()
        assert "anthropic:" in line.lower()
        assert "cache:" in line.lower()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    """Records are appended to .sdd/metrics/quota.jsonl."""

    def test_writes_jsonl(self, tracker_with_persistence: QuotaTracker, tmp_path: Path) -> None:
        tracker_with_persistence.record_request("anthropic", tokens_in=100, tokens_out=50)
        tracker_with_persistence.record_request("anthropic", tokens_in=200, tokens_out=75)

        quota_file = tmp_path / ".sdd" / "metrics" / "quota.jsonl"
        assert quota_file.exists()
        lines = quota_file.read_text().strip().split("\n")
        assert len(lines) == 2

        record = json.loads(lines[0])
        assert record["provider"] == "anthropic"
        assert record["tokens_in"] == 100
        assert record["tokens_out"] == 50
        assert "ts" in record
