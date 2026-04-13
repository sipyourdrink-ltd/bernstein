"""Tests for weighted fair scheduler (issue #652)."""

from __future__ import annotations

import pytest

from bernstein.core.tasks.fair_scheduler import (
    FairScheduler,
    SchedulingDecision,
    TenantQuota,
)

# ---- TenantQuota dataclass ----


class TestTenantQuota:
    def test_defaults(self) -> None:
        q = TenantQuota(tenant_id="t1")
        assert q.tenant_id == "t1"
        assert q.weight == pytest.approx(1.0)
        assert q.max_concurrent == 0
        assert q.current_active == 0

    def test_frozen(self) -> None:
        q = TenantQuota(tenant_id="t1")
        with pytest.raises(AttributeError):
            q.weight = 2.0  # type: ignore[misc]

    def test_custom_values(self) -> None:
        q = TenantQuota(tenant_id="x", weight=3.5, max_concurrent=10, current_active=2)
        assert q.weight == pytest.approx(3.5)
        assert q.max_concurrent == 10
        assert q.current_active == 2


# ---- SchedulingDecision dataclass ----


class TestSchedulingDecision:
    def test_frozen(self) -> None:
        d = SchedulingDecision(task_id="T-1", tenant_id="t1", priority=3, wait_time_s=1.5, reason="test")
        with pytest.raises(AttributeError):
            d.priority = 1  # type: ignore[misc]

    def test_fields(self) -> None:
        d = SchedulingDecision(task_id="T-1", tenant_id="t1", priority=3, wait_time_s=0.0, reason="r")
        assert d.task_id == "T-1"
        assert d.tenant_id == "t1"
        assert d.priority == 3
        assert d.wait_time_s == pytest.approx(0.0)
        assert d.reason == "r"


# ---- Single tenant ----


class TestSingleTenant:
    def test_enqueue_dequeue_single_task(self) -> None:
        s = FairScheduler(quotas=[TenantQuota(tenant_id="A")])
        s.enqueue("T-1", "A", priority=5)
        dec = s.dequeue()
        assert dec is not None
        assert dec.task_id == "T-1"
        assert dec.tenant_id == "A"
        assert dec.priority == 5

    def test_dequeue_empty_returns_none(self) -> None:
        s = FairScheduler(quotas=[TenantQuota(tenant_id="A")])
        assert s.dequeue() is None

    def test_dequeue_no_tenants_returns_none(self) -> None:
        s = FairScheduler()
        assert s.dequeue() is None

    def test_priority_ordering_within_tenant(self) -> None:
        s = FairScheduler(quotas=[TenantQuota(tenant_id="A")])
        s.enqueue("T-low", "A", priority=10)
        s.enqueue("T-high", "A", priority=1)
        s.enqueue("T-mid", "A", priority=5)

        dec = s.dequeue()
        assert dec is not None
        assert dec.task_id == "T-high"

        dec = s.dequeue()
        assert dec is not None
        assert dec.task_id == "T-mid"

        dec = s.dequeue()
        assert dec is not None
        assert dec.task_id == "T-low"

    def test_duplicate_task_id_raises(self) -> None:
        s = FairScheduler(quotas=[TenantQuota(tenant_id="A")])
        s.enqueue("T-1", "A")
        with pytest.raises(ValueError, match="already tracked"):
            s.enqueue("T-1", "A")


# ---- Multi-tenant fairness ----


class TestMultiTenantFairness:
    def test_equal_weight_round_robin(self) -> None:
        """Two tenants with equal weight should alternate."""
        s = FairScheduler(
            quotas=[
                TenantQuota(tenant_id="A"),
                TenantQuota(tenant_id="B"),
            ]
        )
        for i in range(4):
            s.enqueue(f"A-{i}", "A", priority=5)
            s.enqueue(f"B-{i}", "B", priority=5)

        results: list[str] = []
        for _ in range(8):
            dec = s.dequeue()
            assert dec is not None
            results.append(dec.tenant_id)

        a_count = results.count("A")
        b_count = results.count("B")
        assert a_count == 4
        assert b_count == 4

    def test_weighted_proportionality(self) -> None:
        """Tenant with weight=2 should get roughly 2x the tasks of weight=1."""
        s = FairScheduler(
            quotas=[
                TenantQuota(tenant_id="heavy", weight=2.0),
                TenantQuota(tenant_id="light", weight=1.0),
            ]
        )
        # Enqueue plenty so we don't run out for either tenant.
        for i in range(30):
            s.enqueue(f"H-{i}", "heavy", priority=5)
            s.enqueue(f"L-{i}", "light", priority=5)

        results: list[str] = []
        for _ in range(30):
            dec = s.dequeue()
            if dec is None:
                break
            results.append(dec.tenant_id)

        heavy_count = results.count("heavy")
        light_count = results.count("light")
        # With DRR, heavy should get roughly 2x light.
        # Allow some tolerance for boundary effects.
        assert heavy_count > light_count, f"heavy={heavy_count}, light={light_count}"
        ratio = heavy_count / max(light_count, 1)
        assert 1.5 <= ratio <= 2.5, f"ratio={ratio:.2f}"

    def test_three_tenants_fairness(self) -> None:
        """Three tenants with weights 3:2:1 get proportional shares."""
        s = FairScheduler(
            quotas=[
                TenantQuota(tenant_id="A", weight=3.0),
                TenantQuota(tenant_id="B", weight=2.0),
                TenantQuota(tenant_id="C", weight=1.0),
            ]
        )
        for i in range(60):
            s.enqueue(f"A-{i}", "A")
            s.enqueue(f"B-{i}", "B")
            s.enqueue(f"C-{i}", "C")

        counts: dict[str, int] = {"A": 0, "B": 0, "C": 0}
        for _ in range(60):
            dec = s.dequeue()
            if dec is None:
                break
            counts[dec.tenant_id] += 1

        # A should get ~3x C, B should get ~2x C.
        assert counts["A"] > counts["B"] > counts["C"]

    def test_starved_tenant_eventually_served(self) -> None:
        """Even a low-weight tenant accumulates deficit and gets served."""
        s = FairScheduler(
            quotas=[
                TenantQuota(tenant_id="heavy", weight=10.0),
                TenantQuota(tenant_id="light", weight=1.0),
            ]
        )
        for i in range(20):
            s.enqueue(f"H-{i}", "heavy")
        s.enqueue("L-0", "light")

        seen_light = False
        for _ in range(25):
            dec = s.dequeue()
            if dec is None:
                break
            if dec.tenant_id == "light":
                seen_light = True
                break

        assert seen_light, "light tenant was never served"


# ---- max_concurrent enforcement ----


class TestMaxConcurrent:
    def test_concurrency_cap_blocks_dequeue(self) -> None:
        """When active count reaches max_concurrent, further dequeues skip the tenant."""
        s = FairScheduler(quotas=[TenantQuota(tenant_id="A", max_concurrent=2)])
        s.enqueue("T-1", "A")
        s.enqueue("T-2", "A")
        s.enqueue("T-3", "A")

        d1 = s.dequeue()
        assert d1 is not None
        s.mark_active(d1.task_id)

        d2 = s.dequeue()
        assert d2 is not None
        s.mark_active(d2.task_id)

        # Third dequeue should return None -- tenant A is at capacity.
        d3 = s.dequeue()
        assert d3 is None

    def test_slot_release_unblocks(self) -> None:
        """Completing a task frees a slot so the next dequeue succeeds."""
        s = FairScheduler(quotas=[TenantQuota(tenant_id="A", max_concurrent=1)])
        s.enqueue("T-1", "A")
        s.enqueue("T-2", "A")

        d1 = s.dequeue()
        assert d1 is not None
        s.mark_active(d1.task_id)

        # At capacity.
        assert s.dequeue() is None

        s.mark_done(d1.task_id)

        d2 = s.dequeue()
        assert d2 is not None
        assert d2.task_id == "T-2"

    def test_unlimited_concurrency(self) -> None:
        """max_concurrent=0 means no cap."""
        s = FairScheduler(quotas=[TenantQuota(tenant_id="A", max_concurrent=0)])
        for i in range(10):
            s.enqueue(f"T-{i}", "A")

        for _i in range(10):
            dec = s.dequeue()
            assert dec is not None
            s.mark_active(dec.task_id)

        # All 10 active, nothing left queued.
        st = s.stats()
        assert st[0].active_count == 10
        assert st[0].queue_depth == 0

    def test_cross_tenant_cap_independence(self) -> None:
        """One tenant hitting its cap should not block another tenant."""
        s = FairScheduler(
            quotas=[
                TenantQuota(tenant_id="A", max_concurrent=1),
                TenantQuota(tenant_id="B", max_concurrent=1),
            ]
        )
        s.enqueue("A-1", "A")
        s.enqueue("A-2", "A")
        s.enqueue("B-1", "B")

        d1 = s.dequeue()
        assert d1 is not None
        s.mark_active(d1.task_id)

        d2 = s.dequeue()
        assert d2 is not None
        s.mark_active(d2.task_id)

        # Both tenants now at cap -- we should have one from each.
        tids = {d1.tenant_id, d2.tenant_id}
        assert tids == {"A", "B"}

        # Further dequeue returns None (both capped).
        assert s.dequeue() is None


# ---- mark_active / mark_done ----


class TestActiveTracking:
    def test_mark_active_unknown_raises(self) -> None:
        s = FairScheduler()
        with pytest.raises(KeyError, match="unknown task"):
            s.mark_active("nope")

    def test_mark_done_unknown_raises(self) -> None:
        s = FairScheduler()
        with pytest.raises(KeyError, match="unknown task"):
            s.mark_done("nope")

    def test_mark_done_removes_tracking(self) -> None:
        s = FairScheduler(quotas=[TenantQuota(tenant_id="A")])
        s.enqueue("T-1", "A")
        dec = s.dequeue()
        assert dec is not None
        s.mark_active("T-1")
        s.mark_done("T-1")

        # After mark_done the task is no longer tracked.
        with pytest.raises(KeyError):
            s.mark_done("T-1")


# ---- register_tenant ----


class TestRegisterTenant:
    def test_register_new_tenant(self) -> None:
        s = FairScheduler()
        s.register_tenant("X", weight=2.5, max_concurrent=3)
        st = s.stats()
        assert len(st) == 1
        assert st[0].tenant_id == "X"
        assert st[0].weight == pytest.approx(2.5)
        assert st[0].max_concurrent == 3

    def test_update_existing_tenant(self) -> None:
        s = FairScheduler(quotas=[TenantQuota(tenant_id="X", weight=1.0)])
        s.register_tenant("X", weight=5.0, max_concurrent=10)
        st = s.stats()
        assert st[0].weight == pytest.approx(5.0)
        assert st[0].max_concurrent == 10

    def test_invalid_weight_raises(self) -> None:
        s = FairScheduler()
        with pytest.raises(ValueError, match="positive"):
            s.register_tenant("X", weight=0.0)
        with pytest.raises(ValueError, match="positive"):
            s.register_tenant("X", weight=-1.0)

    def test_auto_register_on_enqueue(self) -> None:
        s = FairScheduler()
        s.enqueue("T-1", "auto-tenant")
        st = s.stats()
        assert len(st) == 1
        assert st[0].tenant_id == "auto-tenant"
        assert st[0].weight == pytest.approx(1.0)


# ---- stats ----


class TestStats:
    def test_empty_scheduler(self) -> None:
        s = FairScheduler()
        assert s.stats() == []

    def test_stats_reflect_state(self) -> None:
        s = FairScheduler(
            quotas=[
                TenantQuota(tenant_id="A", weight=2.0, max_concurrent=5),
                TenantQuota(tenant_id="B", weight=1.0, max_concurrent=3),
            ]
        )
        s.enqueue("A-1", "A")
        s.enqueue("A-2", "A")
        s.enqueue("B-1", "B")

        d = s.dequeue()
        assert d is not None
        s.mark_active(d.task_id)

        st = {t.tenant_id: t for t in s.stats()}
        total_queued = st["A"].queue_depth + st["B"].queue_depth
        total_active = st["A"].active_count + st["B"].active_count
        assert total_queued == 2
        assert total_active == 1

    def test_stats_sorted_by_tenant_id(self) -> None:
        s = FairScheduler(
            quotas=[
                TenantQuota(tenant_id="Z"),
                TenantQuota(tenant_id="A"),
                TenantQuota(tenant_id="M"),
            ]
        )
        ids = [t.tenant_id for t in s.stats()]
        assert ids == ["A", "M", "Z"]


# ---- SchedulingDecision fields ----


class TestSchedulingDecisionFields:
    def test_wait_time_non_negative(self) -> None:
        s = FairScheduler(quotas=[TenantQuota(tenant_id="A")])
        s.enqueue("T-1", "A")
        dec = s.dequeue()
        assert dec is not None
        assert dec.wait_time_s >= 0.0

    def test_reason_contains_drr(self) -> None:
        s = FairScheduler(quotas=[TenantQuota(tenant_id="A")])
        s.enqueue("T-1", "A")
        dec = s.dequeue()
        assert dec is not None
        assert "DRR" in dec.reason
