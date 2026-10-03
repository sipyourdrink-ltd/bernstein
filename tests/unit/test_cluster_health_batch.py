"""Tests for health_verdict() and batch_assign()."""

from __future__ import annotations

from bernstein.core.protocols.cluster.cluster import (
    HealthThresholds,
    NodeRegistry,
    batch_assign,
    health_verdict,
)
from bernstein.core.tasks.models import (
    ClusterConfig,
    NodeCapacity,
    NodeInfo,
)


class TestHealthVerdict:
    def test_all_ok(self) -> None:
        assert health_verdict(disk_free_mb=2048, mem_used_pct=50.0, mesh_rtt_ms=10.0) == "ok"

    def test_low_disk(self) -> None:
        v = health_verdict(disk_free_mb=100)
        assert v == "unhealthy:disk"

    def test_high_memory(self) -> None:
        v = health_verdict(mem_used_pct=98.0)
        assert v == "unhealthy:memory"

    def test_high_rtt(self) -> None:
        v = health_verdict(mesh_rtt_ms=10000.0)
        assert v == "unhealthy:mesh-rtt"

    def test_multiple_reasons(self) -> None:
        v = health_verdict(disk_free_mb=10, mem_used_pct=99.0, mesh_rtt_ms=60000.0)
        assert "disk" in v
        assert "memory" in v
        assert "mesh-rtt" in v

    def test_none_values_skipped(self) -> None:
        assert health_verdict() == "ok"

    def test_custom_thresholds(self) -> None:
        t = HealthThresholds(min_disk_free_mb=2048)
        assert health_verdict(disk_free_mb=1024, thresholds=t) == "unhealthy:disk"
        assert health_verdict(disk_free_mb=4096, thresholds=t) == "ok"


class TestBatchAssign:
    def _make_registry(self, nodes: list[NodeInfo]) -> NodeRegistry:
        reg = NodeRegistry(ClusterConfig(enabled=True))
        for n in nodes:
            reg.register(n)
        return reg

    def test_assigns_to_available(self) -> None:
        nodes = [
            NodeInfo(id="a", name="a", capacity=NodeCapacity(available_slots=2)),
            NodeInfo(id="b", name="b", capacity=NodeCapacity(available_slots=3)),
        ]
        reg = self._make_registry(nodes)
        tasks = [{"title": f"t{i}"} for i in range(3)]
        results = batch_assign(reg, tasks)
        assigned = [n for _, n in results if n is not None]
        assert len(assigned) == 3

    def test_respects_model_filter(self) -> None:
        nodes = [
            NodeInfo(id="a", capacity=NodeCapacity(available_slots=5, supported_models=["sonnet"])),
            NodeInfo(id="b", capacity=NodeCapacity(available_slots=5, supported_models=["opus"])),
        ]
        reg = self._make_registry(nodes)
        tasks = [{"required_model": "opus"}]
        results = batch_assign(reg, tasks)
        assert results[0][1] is not None
        assert results[0][1].id == "b"

    def test_no_overassign(self) -> None:
        nodes = [NodeInfo(id="a", capacity=NodeCapacity(available_slots=2))]
        reg = self._make_registry(nodes)
        tasks = [{"title": f"t{i}"} for i in range(5)]
        results = batch_assign(reg, tasks)
        assigned = [n for _, n in results if n is not None]
        unassigned = [t for t, n in results if n is None]
        assert len(assigned) == 2
        assert len(unassigned) == 3

    def test_empty_pool(self) -> None:
        reg = NodeRegistry(ClusterConfig(enabled=True))
        tasks = [{"title": "t1"}]
        results = batch_assign(reg, tasks)
        assert results[0][1] is None

    def test_skips_unhealthy_nodes(self) -> None:
        node = NodeInfo(id="sick", capacity=NodeCapacity(available_slots=5), health="unhealthy:disk")
        reg = self._make_registry([node])
        tasks = [{"title": "t1"}]
        results = batch_assign(reg, tasks)
        assert results[0][1] is None

    def test_distributes_across_nodes(self) -> None:
        nodes = [NodeInfo(id=f"n{i}", capacity=NodeCapacity(available_slots=10)) for i in range(10)]
        reg = self._make_registry(nodes)
        tasks = [{"title": f"t{i}"} for i in range(100)]
        results = batch_assign(reg, tasks)
        assigned_ids = [n.id for _, n in results if n is not None]
        assert len(set(assigned_ids)) > 1
