"""Unit tests for NodeRegistry and cluster coordination."""

from __future__ import annotations

import time

from bernstein.core.cluster import NodeRegistry, _node_to_dict, node_from_dict
from bernstein.core.models import (
    ClusterConfig,
    ClusterTopology,
    NodeCapacity,
    NodeInfo,
    NodeStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(timeout_s: int = 60) -> ClusterConfig:
    return ClusterConfig(enabled=True, node_timeout_s=timeout_s)


def _make_node(name: str = "worker-1", url: str = "http://host:8052") -> NodeInfo:
    return NodeInfo(
        name=name,
        url=url,
        capacity=NodeCapacity(max_agents=4, available_slots=4),
    )


# ---------------------------------------------------------------------------
# NodeRegistry.register
# ---------------------------------------------------------------------------


def test_register_new_node() -> None:
    registry = NodeRegistry(_make_config())
    node = _make_node()
    registered = registry.register(node)

    assert registered.status == NodeStatus.ONLINE
    assert registered.name == "worker-1"
    assert registry.online_count() == 1


def test_register_same_id_updates_info() -> None:
    registry = NodeRegistry(_make_config())
    node = _make_node()
    first = registry.register(node)
    original_registered_at = first.registered_at

    # Re-register with updated capacity
    updated = NodeInfo(
        id=first.id,
        name="worker-1-renamed",
        url="http://newhost:8052",
        capacity=NodeCapacity(max_agents=8, available_slots=8),
    )
    second = registry.register(updated)

    assert second.id == first.id
    assert second.name == "worker-1-renamed"
    assert second.capacity.max_agents == 8
    assert second.registered_at == original_registered_at  # preserved
    assert registry.online_count() == 1  # still one node


def test_register_two_nodes() -> None:
    registry = NodeRegistry(_make_config())
    registry.register(_make_node("a", "http://a:8052"))
    registry.register(_make_node("b", "http://b:8052"))
    assert registry.online_count() == 2
    assert len(registry.list_nodes()) == 2


# ---------------------------------------------------------------------------
# NodeRegistry.heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_updates_timestamp() -> None:
    registry = NodeRegistry(_make_config())
    node = registry.register(_make_node())
    old_ts = node.last_heartbeat
    time.sleep(0.01)

    result = registry.heartbeat(node.id)
    assert result is not None
    assert result.last_heartbeat > old_ts
    assert result.status == NodeStatus.ONLINE


def test_heartbeat_updates_capacity() -> None:
    registry = NodeRegistry(_make_config())
    node = registry.register(_make_node())
    new_cap = NodeCapacity(max_agents=4, available_slots=2, active_agents=2)

    result = registry.heartbeat(node.id, new_cap)
    assert result is not None
    assert result.capacity.available_slots == 2
    assert result.capacity.active_agents == 2


def test_heartbeat_unknown_node_returns_none() -> None:
    registry = NodeRegistry(_make_config())
    assert registry.heartbeat("nonexistent-id") is None


# ---------------------------------------------------------------------------
# NodeRegistry.unregister
# ---------------------------------------------------------------------------


def test_unregister_known_node() -> None:
    registry = NodeRegistry(_make_config())
    node = registry.register(_make_node())
    assert registry.unregister(node.id) is True
    assert registry.online_count() == 0
    assert registry.get(node.id) is None


def test_unregister_unknown_node_returns_false() -> None:
    registry = NodeRegistry(_make_config())
    assert registry.unregister("does-not-exist") is False


# ---------------------------------------------------------------------------
# NodeRegistry.mark_stale
# ---------------------------------------------------------------------------


def test_mark_stale_offline_after_timeout() -> None:
    registry = NodeRegistry(_make_config(timeout_s=0))  # instant timeout
    node = registry.register(_make_node())
    node.last_heartbeat = time.time() - 999  # definitely stale

    stale = registry.mark_stale()
    assert len(stale) == 1
    assert stale[0].id == node.id
    assert stale[0].status == NodeStatus.OFFLINE


def test_mark_stale_does_not_touch_fresh_nodes() -> None:
    registry = NodeRegistry(_make_config(timeout_s=60))
    registry.register(_make_node())

    stale = registry.mark_stale()
    assert stale == []
    assert registry.online_count() == 1


# ---------------------------------------------------------------------------
# NodeRegistry.total_capacity / online_count
# ---------------------------------------------------------------------------


def test_total_capacity_sums_online_nodes() -> None:
    registry = NodeRegistry(_make_config())
    n1 = registry.register(NodeInfo(name="n1", capacity=NodeCapacity(available_slots=3)))
    n2 = registry.register(NodeInfo(name="n2", capacity=NodeCapacity(available_slots=5)))
    assert registry.total_capacity() == 8

    # Offline node doesn't count
    n1.status = NodeStatus.OFFLINE
    assert registry.total_capacity() == 5


# ---------------------------------------------------------------------------
# NodeRegistry.best_node_for_task
# ---------------------------------------------------------------------------


def test_best_node_for_task_picks_most_available() -> None:
    registry = NodeRegistry(_make_config())
    n_small = registry.register(
        NodeInfo(
            name="small",
            capacity=NodeCapacity(available_slots=1),
        )
    )
    n_big = registry.register(
        NodeInfo(
            name="big",
            capacity=NodeCapacity(available_slots=10),
        )
    )
    best = registry.best_node_for_task()
    assert best is not None
    assert best.id == n_big.id


def test_best_node_filters_by_model() -> None:
    registry = NodeRegistry(_make_config())
    registry.register(
        NodeInfo(
            name="no-opus",
            capacity=NodeCapacity(available_slots=10, supported_models=["sonnet", "haiku"]),
        )
    )
    gpu_node = registry.register(
        NodeInfo(
            name="has-opus",
            capacity=NodeCapacity(available_slots=5, supported_models=["sonnet", "opus"]),
        )
    )
    best = registry.best_node_for_task(required_model="opus")
    assert best is not None
    assert best.id == gpu_node.id


def test_best_node_returns_none_when_all_full() -> None:
    registry = NodeRegistry(_make_config())
    registry.register(
        NodeInfo(
            name="full",
            capacity=NodeCapacity(available_slots=0),
        )
    )
    assert registry.best_node_for_task() is None


def test_best_node_label_affinity() -> None:
    registry = NodeRegistry(_make_config())
    generic = registry.register(
        NodeInfo(
            name="generic",
            capacity=NodeCapacity(available_slots=4),
            labels={},
        )
    )
    gpu = registry.register(
        NodeInfo(
            name="gpu-node",
            capacity=NodeCapacity(available_slots=2),
            labels={"gpu": "true"},
        )
    )
    best = registry.best_node_for_task(preferred_labels={"gpu": "true"})
    # gpu-node matches preferred label — it wins despite fewer slots
    assert best is not None
    assert best.id == gpu.id


# ---------------------------------------------------------------------------
# NodeRegistry.cluster_summary
# ---------------------------------------------------------------------------


def test_cluster_summary_structure() -> None:
    registry = NodeRegistry(ClusterConfig(enabled=True, topology=ClusterTopology.STAR))
    registry.register(
        NodeInfo(
            name="w1",
            capacity=NodeCapacity(max_agents=4, available_slots=2, active_agents=2),
        )
    )
    summary = registry.cluster_summary()

    assert summary["topology"] == "star"
    assert summary["total_nodes"] == 1
    assert summary["online_nodes"] == 1
    assert summary["offline_nodes"] == 0
    assert summary["total_capacity"] == 4
    assert summary["available_slots"] == 2
    assert summary["active_agents"] == 2
    assert len(summary["nodes"]) == 1


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def test_node_roundtrip() -> None:
    node = NodeInfo(
        id="abc123",
        name="worker",
        url="http://host:8052",
        capacity=NodeCapacity(max_agents=4, available_slots=3, active_agents=1, gpu_available=True),
        status=NodeStatus.ONLINE,
        labels={"region": "us-east"},
        cell_ids=["cell-1"],
    )
    d = _node_to_dict(node)
    restored = node_from_dict(d)

    assert restored.id == node.id
    assert restored.name == node.name
    assert restored.url == node.url
    assert restored.capacity.gpu_available is True
    assert restored.labels == {"region": "us-east"}
    assert restored.cell_ids == ["cell-1"]


def test_node_from_dict_defaults() -> None:
    restored = node_from_dict({})
    assert restored.status == NodeStatus.ONLINE
    assert restored.capacity.max_agents == 6
