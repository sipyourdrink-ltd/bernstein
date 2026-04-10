"""Tests for worker badge identity — no network or filesystem needed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bernstein.worker_badges import (
    STATUS_ICON_COLORS,
    STATUS_ICONS,
    TIER_COLORS,
    TierColor,
    WorkerBadge,
    WorkerStatus,
    format_worker_badge,
    get_badge_for_worker,
)

FIXED_TIME = datetime(2026, 4, 3, 10, 0, 0, tzinfo=UTC)


# --- WorkerBadge ---


class TestWorkerBadgeDefaults:
    """Tests for WorkerBadge dataclass defaults."""

    def test_default_status_is_running(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="backend",
            model="sonnet",
            tier="free",
            start_time=FIXED_TIME,
        )
        assert badge.status is WorkerStatus.RUNNING

    def test_status_icon_running(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="backend",
            model="sonnet",
            tier="free",
            start_time=FIXED_TIME,
            status=WorkerStatus.RUNNING,
        )
        assert badge.status_icon == "✓"

    def test_status_icon_paused(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="backend",
            model="sonnet",
            tier="free",
            start_time=FIXED_TIME,
            status=WorkerStatus.PAUSED,
        )
        assert badge.status_icon == "⏸"

    def test_status_icon_stopped(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="backend",
            model="sonnet",
            tier="free",
            start_time=FIXED_TIME,
            status=WorkerStatus.STOPPED,
        )
        assert badge.status_icon == "✗"

    def test_status_icon_error(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="backend",
            model="sonnet",
            tier="free",
            start_time=FIXED_TIME,
            status=WorkerStatus.ERROR,
        )
        assert badge.status_icon == "⚠"


class TestWorkerBadgeColorProperties:
    """Tests for color and tier display properties."""

    def test_tier_color_free_is_green(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="qa",
            model="haiku",
            tier="free",
            start_time=FIXED_TIME,
        )
        assert badge.tier_color == "green"

    def test_tier_color_paid_is_blue(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="qa",
            model="haiku",
            tier="paid",
            start_time=FIXED_TIME,
        )
        assert badge.tier_color == "blue"

    def test_tier_color_enterprise_is_gold(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="security",
            model="gpt-5.4",
            tier="enterprise",
            start_time=FIXED_TIME,
        )
        assert badge.tier_color == "gold"

    def test_tier_color_unknown_defaults_to_blue(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="qa",
            model="haiku",
            tier="unknown_tier",
            start_time=FIXED_TIME,
        )
        assert badge.tier_color == "blue"

    def test_tier_display_free(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="backend",
            model="sonnet",
            tier="free",
            start_time=FIXED_TIME,
        )
        assert badge.tier_display == "free-tier"

    def test_tier_display_enterprise(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="backend",
            model="sonnet",
            tier="enterprise",
            start_time=FIXED_TIME,
        )
        assert badge.tier_display == "enterprise-tier"


# --- format_worker_badge ---


class TestFormatWorkerBadge:
    """Tests for format_worker_badge markup output."""

    def test_running_produces_green_icon(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="backend",
            model="sonnet",
            tier="free",
            start_time=FIXED_TIME,
            status=WorkerStatus.RUNNING,
        )
        result = format_worker_badge(badge)
        assert "[green]✓[/]" in result

    def test_paused_produces_yellow_icon(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="backend",
            model="sonnet",
            tier="free",
            start_time=FIXED_TIME,
            status=WorkerStatus.PAUSED,
        )
        result = format_worker_badge(badge)
        assert "[yellow]⏸[/]" in result

    def test_stopped_produces_red_icon(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="backend",
            model="sonnet",
            tier="free",
            start_time=FIXED_TIME,
            status=WorkerStatus.STOPPED,
        )
        result = format_worker_badge(badge)
        assert "[red]✗[/]" in result

    def test_error_produces_red_icon(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="backend",
            model="sonnet",
            tier="free",
            start_time=FIXED_TIME,
            status=WorkerStatus.ERROR,
        )
        result = format_worker_badge(badge)
        assert "[red]⚠[/]" in result

    def test_tier_color_applies_to_tier_display(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="backend",
            model="sonnet",
            tier="paid",
            start_time=FIXED_TIME,
        )
        result = format_worker_badge(badge)
        assert "[blue]paid-tier[/]" in result
        assert "[gold]" not in result

    def test_role_and_model_present(self) -> None:
        badge = WorkerBadge(
            worker_id="a1b2c3d4e5f6",
            role="qa",
            model="gpt-5.4",
            tier="enterprise",
            start_time=FIXED_TIME,
        )
        result = format_worker_badge(badge)
        assert "qa" in result
        assert "gpt-5.4" in result


# --- get_badge_for_worker ---


class TestGetBadgeForWorker:
    """Tests for get_badge_for_worker builder."""

    def test_builds_correct_badge(self) -> None:
        badge = get_badge_for_worker(
            worker_id="abc123def456",
            role="devops",
            model="claude",
            tier="free",
            start_time=FIXED_TIME,
        )
        assert badge.worker_id == "abc123def456"
        assert badge.role == "devops"
        assert badge.model == "claude"
        assert badge.tier == "free"
        assert badge.start_time == FIXED_TIME
        assert badge.status is WorkerStatus.RUNNING

    def test_default_status_to_running(self) -> None:
        badge = get_badge_for_worker(
            worker_id="abc123def456",
            role="devops",
            model="claude",
            tier="paid",
            start_time=FIXED_TIME,
        )
        assert badge.status is WorkerStatus.RUNNING

    def test_custom_status(self) -> None:
        badge = get_badge_for_worker(
            worker_id="abc123def456",
            role="devops",
            model="claude",
            tier="paid",
            start_time=FIXED_TIME,
            status=WorkerStatus.PAUSED,
        )
        assert badge.status is WorkerStatus.PAUSED

    def test_default_start_time_is_recent(self) -> None:
        """When start_time is None, uses a time close to now."""
        before = datetime.now(tz=UTC)
        badge = get_badge_for_worker(
            worker_id="abc123def456",
            role="devops",
            model="claude",
            tier="paid",
        )
        after = datetime.now(tz=UTC)
        assert before - timedelta(seconds=1) <= badge.start_time <= after + timedelta(seconds=1)


# --- Constants ---


class TestConstants:
    """Tests for module-level constants."""

    def test_all_statuses_have_icons(self) -> None:
        for status in WorkerStatus:
            assert status in STATUS_ICONS
            assert STATUS_ICONS[status] is not None

    def test_all_statuses_have_colors(self) -> None:
        for status in WorkerStatus:
            assert status in STATUS_ICON_COLORS
            assert STATUS_ICON_COLORS[status] in ("green", "yellow", "red")

    def test_all_known_tiers_mapped(self) -> None:
        assert "free" in TIER_COLORS
        assert "paid" in TIER_COLORS
        assert "enterprise" in TIER_COLORS
        assert TIER_COLORS["free"] is TierColor.FREE
        assert TIER_COLORS["paid"] is TierColor.PAID
        assert TIER_COLORS["enterprise"] is TierColor.ENTERPRISE
