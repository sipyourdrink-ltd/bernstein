"""Focused tests for cost history persistence, trends, and alerts."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from bernstein.core.cost_history import (
    DailyCostSnapshot,
    append_daily_snapshot,
    compute_trends,
    get_active_alerts,
    load_history,
    upsert_daily_snapshot,
)


def _sdd_dir(tmp_path: Path) -> Path:
    """Create a minimal .sdd directory for cost-history tests."""
    sdd_dir = tmp_path / ".sdd"
    (sdd_dir / "metrics").mkdir(parents=True)
    return sdd_dir


def test_append_and_load_history_round_trip(tmp_path: Path) -> None:
    """append_daily_snapshot persists snapshots that load_history returns oldest-first."""
    sdd_dir = _sdd_dir(tmp_path)
    append_daily_snapshot(sdd_dir, spent_usd=3.5, budget_usd=10.0, snapshot_date=date(2026, 3, 29))
    append_daily_snapshot(sdd_dir, spent_usd=4.5, budget_usd=10.0, snapshot_date=date(2026, 3, 30))

    snapshots = load_history(sdd_dir)

    assert [snapshot.date_str for snapshot in snapshots] == ["2026-03-29", "2026-03-30"]
    assert [snapshot.spent_usd for snapshot in snapshots] == [3.5, 4.5]


def test_load_history_skips_malformed_lines(tmp_path: Path) -> None:
    """load_history ignores malformed JSON lines while keeping valid snapshots."""
    sdd_dir = _sdd_dir(tmp_path)
    history_file = sdd_dir / "metrics" / "cost_history.jsonl"
    history_file.write_text(
        "not-json\n" + json.dumps({"date_str": date.today().isoformat(), "spent_usd": 2.0, "timestamp": 1.0}) + "\n",
        encoding="utf-8",
    )

    snapshots = load_history(sdd_dir)

    assert len(snapshots) == 1
    assert snapshots[0].spent_usd == pytest.approx(2.0)


def test_upsert_replaces_same_date_and_prunes_old_history(tmp_path: Path) -> None:
    """upsert_daily_snapshot last-write-wins for a day and prunes entries older than the retention window."""
    sdd_dir = _sdd_dir(tmp_path)
    old_day = date.today() - timedelta(days=181)
    keep_day = date.today() - timedelta(days=1)
    append_daily_snapshot(sdd_dir, spent_usd=1.0, snapshot_date=old_day)
    append_daily_snapshot(sdd_dir, spent_usd=2.0, snapshot_date=keep_day)
    append_daily_snapshot(sdd_dir, spent_usd=3.0, snapshot_date=date.today())

    upsert_daily_snapshot(sdd_dir, spent_usd=5.5, budget_usd=9.0, run_count=4, snapshot_date=date.today())

    snapshots = load_history(sdd_dir)
    assert all(snapshot.date_str != old_day.isoformat() for snapshot in snapshots)
    today_snapshot = next(snapshot for snapshot in snapshots if snapshot.date_str == date.today().isoformat())
    assert today_snapshot.spent_usd == pytest.approx(5.5)
    assert today_snapshot.run_count == 4


def test_compute_trends_detects_upward_spend_shift() -> None:
    """compute_trends marks the trend as up when the current 30-day window is materially higher."""
    today = date.today()
    snapshots: list[DailyCostSnapshot] = [
        DailyCostSnapshot(
            date_str=(today - timedelta(days=days_ago)).isoformat(),
            spent_usd=10.0,
            budget_usd=0.0,
            run_count=1,
            timestamp=0.0,
        )
        for days_ago in range(60, 30, -1)
    ]
    for days_ago in range(30, 0, -1):
        snapshots.append(
            DailyCostSnapshot(
                date_str=(today - timedelta(days=days_ago)).isoformat(),
                spent_usd=20.0,
                budget_usd=0.0,
                run_count=1,
                timestamp=0.0,
            )
        )

    trend = compute_trends(snapshots)

    assert trend.trend_direction == "up"
    assert trend.avg_30d_usd > trend.avg_90d_usd
    assert trend.pct_change_30d > 0


def test_get_active_alerts_emits_expected_thresholds(tmp_path: Path) -> None:
    """get_active_alerts emits 80% and 95% alerts only when the budget is bounded."""
    sdd_dir = _sdd_dir(tmp_path)

    warn = get_active_alerts(sdd_dir, current_spent_usd=8.5, budget_usd=10.0)
    critical = get_active_alerts(sdd_dir, current_spent_usd=9.8, budget_usd=10.0)
    unlimited = get_active_alerts(sdd_dir, current_spent_usd=50.0, budget_usd=0.0)

    assert warn[0].alert_type == "budget_80pct"
    assert critical[0].alert_type == "budget_95pct"
    assert unlimited == []


# ---------------------------------------------------------------------------
# Tenant attribution (issue #3702)
# ---------------------------------------------------------------------------


def test_append_daily_snapshot_persists_tenant_id(tmp_path: Path) -> None:
    """A snapshot appended with a tenant id round-trips through load_history."""
    sdd_dir = _sdd_dir(tmp_path)

    append_daily_snapshot(sdd_dir, spent_usd=4.0, snapshot_date=date.today(), tenant_id="acme")

    snapshots = load_history(sdd_dir, tenant_id="acme")
    assert [s.tenant_id for s in snapshots] == ["acme"]
    assert snapshots[0].spent_usd == pytest.approx(4.0)


def test_from_dict_treats_missing_tenant_key_as_unattributed(tmp_path: Path) -> None:
    """A JSONL row written before the tenant field existed loads with tenant_id=None.

    This is the "records written before the change must still load" contract:
    the row is not rejected, and it is not silently attributed to any tenant.
    """
    sdd_dir = _sdd_dir(tmp_path)
    history_file = sdd_dir / "metrics" / "cost_history.jsonl"
    history_file.write_text(
        json.dumps({"date_str": date.today().isoformat(), "spent_usd": 1.0, "budget_usd": 0.0, "timestamp": 1.0})
        + "\n",
        encoding="utf-8",
    )

    snapshots = load_history(sdd_dir)

    assert len(snapshots) == 1
    assert snapshots[0].tenant_id is None
    assert snapshots[0].spent_usd == pytest.approx(1.0)


def test_from_dict_treats_non_string_tenant_as_unattributed(tmp_path: Path) -> None:
    """A stored tenant_id of the wrong type is read as unattributed, not coerced.

    str(True) == "True" and str(123) == "123" both pass the identifier rules,
    so coercing would attribute the row to a tenant that never wrote it.
    """
    sdd_dir = _sdd_dir(tmp_path)
    history_file = sdd_dir / "metrics" / "cost_history.jsonl"
    history_file.write_text(
        json.dumps(
            {
                "date_str": date.today().isoformat(),
                "spent_usd": 1.0,
                "budget_usd": 0.0,
                "timestamp": 1.0,
                "tenant_id": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    snapshots = load_history(sdd_dir)

    assert snapshots[0].tenant_id is None


def test_load_history_excludes_unattributed_records_from_every_scoped_query(tmp_path: Path) -> None:
    """An unattributed historical record never appears in a scoped result -

    not even the default tenant's. Defaulting it to "default" would silently
    credit that one tenant with spend that was never verified as its own.
    """
    sdd_dir = _sdd_dir(tmp_path)
    append_daily_snapshot(sdd_dir, spent_usd=9.0, snapshot_date=date.today())  # no tenant_id -> unattributed
    append_daily_snapshot(sdd_dir, spent_usd=2.0, snapshot_date=date.today() - timedelta(days=1), tenant_id="default")

    default_scoped = load_history(sdd_dir, tenant_id="default")
    acme_scoped = load_history(sdd_dir, tenant_id="acme")

    assert [s.spent_usd for s in default_scoped] == [2.0]
    assert acme_scoped == []


def test_load_history_narrows_to_requested_tenant_only(tmp_path: Path) -> None:
    """A tenant's scoped read returns its own snapshots, not another tenant's."""
    sdd_dir = _sdd_dir(tmp_path)
    append_daily_snapshot(sdd_dir, spent_usd=3.0, snapshot_date=date.today(), tenant_id="tenant-a")
    append_daily_snapshot(sdd_dir, spent_usd=99.0, snapshot_date=date.today() - timedelta(days=1), tenant_id="tenant-b")

    scoped = load_history(sdd_dir, tenant_id="tenant-a")

    assert [s.spent_usd for s in scoped] == [3.0]
    assert all(s.tenant_id == "tenant-a" for s in scoped)


def test_load_history_without_tenant_filter_returns_every_record(tmp_path: Path) -> None:
    """Omitting tenant_id keeps the unscoped, whole-install read available."""
    sdd_dir = _sdd_dir(tmp_path)
    append_daily_snapshot(sdd_dir, spent_usd=1.0, snapshot_date=date.today())
    append_daily_snapshot(sdd_dir, spent_usd=2.0, snapshot_date=date.today() - timedelta(days=1), tenant_id="acme")

    snapshots = load_history(sdd_dir)

    assert sorted(s.spent_usd for s in snapshots) == [1.0, 2.0]


def test_upsert_daily_snapshot_does_not_clobber_another_tenants_same_day_entry(tmp_path: Path) -> None:
    """A same-day upsert for one tenant must not overwrite another tenant's snapshot.

    Matching the replace target on date alone - the pre-tenant behaviour -
    would let tenant A's daily write erase tenant B's snapshot for that day.
    """
    sdd_dir = _sdd_dir(tmp_path)
    today = date.today()
    append_daily_snapshot(sdd_dir, spent_usd=5.0, snapshot_date=today, tenant_id="tenant-a")
    append_daily_snapshot(sdd_dir, spent_usd=7.0, snapshot_date=today, tenant_id="tenant-b")

    upsert_daily_snapshot(sdd_dir, spent_usd=50.0, snapshot_date=today, tenant_id="tenant-a")

    tenant_a_snapshots = load_history(sdd_dir, tenant_id="tenant-a")
    tenant_b_snapshots = load_history(sdd_dir, tenant_id="tenant-b")
    assert [s.spent_usd for s in tenant_a_snapshots] == [50.0]
    assert [s.spent_usd for s in tenant_b_snapshots] == [7.0]
