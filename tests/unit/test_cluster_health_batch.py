"""Tests for health evaluation, cluster seed thresholds, and batch task assignment."""

from __future__ import annotations

import random

import pytest
from bernstein.core.models import (
    ClusterConfig,
    NodeCapacity,
    NodeInfo,
)

from bernstein.core.config.seed_parser import SeedError, _parse_cluster
from bernstein.core.protocols.cluster.cluster import (
    HealthThresholds,
    NodeRegistry,
    batch_assign,
    health_verdict,
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


def test_health_thresholds_parse_from_seed() -> None:
    config = _parse_cluster({"min_disk_free_mb": 2048, "max_mem_used_pct": 80, "max_mesh_rtt_ms": 200})
    assert config is not None
    assert (config.min_disk_free_mb, config.max_mem_used_pct, config.max_mesh_rtt_ms) == (2048, 80.0, 200.0)


@pytest.mark.parametrize(
    "key,value",
    [
        ("min_disk_free_mb", -1),
        ("min_disk_free_mb", True),
        ("max_mem_used_pct", 101),
        ("max_mesh_rtt_ms", float("nan")),
        ("max_mem_used_pct", "bad"),
        ("max_mesh_rtt_ms", float("inf")),
    ],
)
def test_invalid_health_thresholds_are_rejected(key: str, value: object) -> None:
    with pytest.raises(SeedError, match="cluster"):
        _parse_cluster({key: value})


def test_default_server_uses_selected_seed_health_limits(tmp_path, monkeypatch) -> None:
    from bernstein.core.server import server_app

    seed = tmp_path / "cluster.yaml"
    seed.write_text(
        "goal: cluster test\ncluster:\n  min_disk_free_mb: 2000\n  max_mem_used_pct: 72\n  max_mesh_rtt_ms: 120\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BERNSTEIN_SEED_PATH", str(seed))
    config = server_app.get_app().state.node_registry.config
    assert (config.min_disk_free_mb, config.max_mem_used_pct, config.max_mesh_rtt_ms) == (2000, 72, 120)


class TestBatchAssign:
    def _make_registry(self, nodes: list[NodeInfo]) -> NodeRegistry:
        reg = NodeRegistry(ClusterConfig(enabled=True))
        for node in nodes:
            reg.register(node)
        return reg

    def test_assigns_to_available(self) -> None:
        nodes = [
            NodeInfo(id="a", name="a", capacity=NodeCapacity(available_slots=2)),
            NodeInfo(id="b", name="b", capacity=NodeCapacity(available_slots=3)),
        ]
        reg = self._make_registry(nodes)
        tasks = [{"title": f"t{i}"} for i in range(3)]
        results = batch_assign(reg, tasks, rng=random.Random(1))
        assigned = [n for _, n in results if n is not None]
        assert len(assigned) == 3
        # In-memory planning must not mutate registry capacity records directly.
        node_a = reg.get("a")
        node_b = reg.get("b")
        assert node_a is not None
        assert node_a.capacity.available_slots == 2
        assert node_b is not None
        assert node_b.capacity.available_slots == 3

    def test_respects_model_and_gpu_filters(self) -> None:
        nodes = [
            NodeInfo(
                id="cpu-sonnet",
                capacity=NodeCapacity(available_slots=5, gpu_available=False, supported_models=["sonnet"]),
            ),
            NodeInfo(
                id="cpu-opus",
                capacity=NodeCapacity(available_slots=5, gpu_available=False, supported_models=["opus"]),
            ),
            NodeInfo(
                id="gpu-opus",
                capacity=NodeCapacity(available_slots=5, gpu_available=True, supported_models=["opus"]),
            ),
        ]
        reg = self._make_registry(nodes)
        tasks: list[dict[str, object]] = [
            {"required_model": "opus", "require_gpu": True},
            {"required_model": "haiku"},
        ]
        results = batch_assign(reg, tasks, rng=random.Random(2))
        first_node = results[0][1]
        assert first_node is not None
        assert first_node.id == "gpu-opus"
        assert results[1][1] is None

    def test_no_overassign_within_batch(self) -> None:
        nodes = [NodeInfo(id="a", capacity=NodeCapacity(available_slots=2))]
        reg = self._make_registry(nodes)
        tasks = [{"title": f"t{i}"} for i in range(5)]
        results = batch_assign(reg, tasks)
        assigned = [n for _, n in results if n is not None]
        unassigned = [t for t, n in results if n is None]
        assert len(assigned) == 2
        assert len(unassigned) == 3

    def test_empty_pool_and_empty_tasks(self) -> None:
        reg = NodeRegistry(ClusterConfig(enabled=True))
        assert batch_assign(reg, []) == []
        results = batch_assign(reg, [{"title": "t1"}])
        assert results == [({"title": "t1"}, None)]

    def test_skips_unhealthy_cordoned_and_draining_nodes(self) -> None:
        sick = NodeInfo(id="sick", capacity=NodeCapacity(available_slots=5, disk_free_mb=10))
        cordoned = NodeInfo(id="cordoned", capacity=NodeCapacity(available_slots=5))
        draining = NodeInfo(id="draining", capacity=NodeCapacity(available_slots=5))
        reg = self._make_registry([sick, cordoned, draining])
        sick_node = reg.get("sick")
        assert sick_node is not None
        assert sick_node.health == "unhealthy:disk"
        reg.cordon("cordoned")
        reg.start_drain("draining")
        results = batch_assign(reg, [{"title": "t1"}])
        assert results[0][1] is None

    def test_preferred_labels_prioritize_matching_tier_then_spill_over(self) -> None:
        nodes = [
            NodeInfo(id="eu-1", capacity=NodeCapacity(available_slots=1), labels={"region": "eu", "tier": "gold"}),
            NodeInfo(id="eu-2", capacity=NodeCapacity(available_slots=1), labels={"region": "eu"}),
            NodeInfo(id="us-1", capacity=NodeCapacity(available_slots=2), labels={"region": "us"}),
        ]
        reg = self._make_registry(nodes)
        tasks = [{"title": "t0"}, {"title": "t1"}, {"title": "t2"}]
        results = batch_assign(reg, tasks, preferred_labels={"region": "eu", "tier": "gold"}, rng=random.Random(3))
        picked_ids = [n.id for _, n in results if n is not None]
        assert picked_ids == ["eu-1", "eu-2", "us-1"]

    def test_power_of_two_choices_spreads_load_even_when_one_node_fits_all(self) -> None:
        nodes = [NodeInfo(id=f"n{i}", capacity=NodeCapacity(available_slots=25)) for i in range(10)]
        reg = self._make_registry(nodes)
        tasks = [{"title": f"t{i}"} for i in range(20)]
        results = batch_assign(reg, tasks, rng=random.Random(7))
        assigned_ids = [n.id for _, n in results if n is not None]
        assert len(assigned_ids) == 20
        assert len(set(assigned_ids)) >= 6
        # Power-of-two-choices picks the less-loaded of two candidates; verify higher-slot bias on 2 nodes.
        two_reg = self._make_registry(
            [
                NodeInfo(id="busy", capacity=NodeCapacity(available_slots=1)),
                NodeInfo(id="free", capacity=NodeCapacity(available_slots=10)),
            ]
        )
        first_pick = batch_assign(two_reg, [{"title": "single"}], rng=random.Random(11))[0][1]
        assert first_pick is not None
        assert first_pick.id == "free"
