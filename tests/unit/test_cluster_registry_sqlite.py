"""Scheduling eligibility for worker-reported health."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.models import ClusterConfig, NodeCapacity, NodeInfo

from bernstein.core.protocols.cluster.cluster import NodeRegistry


def _node(i: int) -> NodeInfo:
    return NodeInfo(id=f"n{i}", name=f"w{i}", url=f"http://w{i}:8052")


def test_unhealthy_node_is_skipped(tmp_path: Path) -> None:
    reg = NodeRegistry(ClusterConfig(), persist_path=tmp_path / "nodes.json")
    sick, fine = _node(1), _node(2)
    sick.capacity = NodeCapacity(available_slots=6)
    fine.capacity = NodeCapacity(available_slots=1)
    reg.register(sick)
    reg.register(fine)
    reg.heartbeat("n1", disk_free_mb=1)
    assert reg.get("n1").health.startswith("unhealthy")  # type: ignore[union-attr]
    best = reg.best_node_for_task()
    assert best is not None
    assert best.id == "n2"


def test_configured_threshold_and_recovery() -> None:
    reg = NodeRegistry(ClusterConfig(min_disk_free_mb=2048))
    reg.register(_node(1))
    node = reg.heartbeat("n1", disk_free_mb=1024)
    assert node is not None and node.health == "unhealthy:disk"
    first = node.unhealthy_since
    assert first is not None
    assert reg.best_node_for_task() is None
    reg.heartbeat("n1", disk_free_mb=1024)
    assert node.unhealthy_since == first
    reg.heartbeat("n1", disk_free_mb=4096)
    assert node.health == "ok" and node.unhealthy_since is None
    assert reg.best_node_for_task() is node
    reg.cordon("n1")
    reg.heartbeat("n1", disk_free_mb=4096)
    assert not node.is_schedulable
    assert reg.best_node_for_task() is None


def test_task_stealing_excludes_unhealthy_receivers() -> None:
    from bernstein.core.protocols.cluster.cluster import TaskStealPolicy

    reg = NodeRegistry(ClusterConfig())
    reg.register(NodeInfo(id="donor", capacity=NodeCapacity(available_slots=0)))
    reg.register(NodeInfo(id="sick", capacity=NodeCapacity(available_slots=6)))
    reg.heartbeat("sick", disk_free_mb=1)
    policy = TaskStealPolicy()
    assert policy.find_steal_pairs(reg, {"donor": 10, "sick": 0}) == []
    reg.heartbeat("sick", disk_free_mb=4096)
    assert policy.find_steal_pairs(reg, {"donor": 10, "sick": 0}) == [("donor", "sick", 3)]
