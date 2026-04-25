"""Unit tests for the unmanaged-plan backfill on startup."""

from __future__ import annotations

from pathlib import Path

import yaml

from bernstein.core.planning.lifecycle import PlanLifecycle, PlanState


def _seed_loose(plans: Path, name: str) -> Path:
    plans.mkdir(parents=True, exist_ok=True)
    plan = plans / name
    plan.write_text(yaml.dump({"name": name, "stages": []}))
    return plan


def test_backfill_migrates_loose_plans(tmp_path: Path) -> None:
    plans = tmp_path / "plans"
    p1 = _seed_loose(plans, "alpha.yaml")
    p2 = _seed_loose(plans, "beta.yaml")

    lifecycle = PlanLifecycle(plans)
    moved = lifecycle.backfill_unmanaged()

    assert {m.name for m in moved} == {"alpha.yaml", "beta.yaml"}
    for src in (p1, p2):
        assert not src.exists()
    listed = {a.plan_id for a in lifecycle.list_plans(PlanState.ACTIVE)}
    assert listed == {"alpha", "beta"}


def test_backfill_is_idempotent(tmp_path: Path) -> None:
    plans = tmp_path / "plans"
    _seed_loose(plans, "alpha.yaml")
    lifecycle = PlanLifecycle(plans)
    first = lifecycle.backfill_unmanaged()
    second = lifecycle.backfill_unmanaged()

    assert len(first) == 1
    assert second == []  # no work the second time


def test_backfill_skips_managed_buckets(tmp_path: Path) -> None:
    plans = tmp_path / "plans"
    plans.mkdir()
    # File already in active/: should not move.
    (plans / "active").mkdir()
    (plans / "active" / "already.yaml").write_text("name: x\nstages: []\n")
    # And one loose file at the top level.
    _seed_loose(plans, "loose.yaml")

    lifecycle = PlanLifecycle(plans)
    moved = lifecycle.backfill_unmanaged()

    assert [m.name for m in moved] == ["loose.yaml"]
    assert (plans / "active" / "already.yaml").exists()
    assert (plans / "active" / "loose.yaml").exists()


def test_backfill_skips_when_target_already_exists(tmp_path: Path) -> None:
    plans = tmp_path / "plans"
    _seed_loose(plans, "dup.yaml")
    (plans / "active").mkdir(exist_ok=True)
    (plans / "active" / "dup.yaml").write_text("name: dup\nstages: []\n")

    lifecycle = PlanLifecycle(plans)
    moved = lifecycle.backfill_unmanaged()
    # The loose file is left alone - operator must resolve manually.
    assert moved == []
    assert (plans / "dup.yaml").exists()
