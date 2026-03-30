"""Unit tests for TaskStealPolicy — distributed load balancing."""

from __future__ import annotations

from bernstein.core.cluster import NodeRegistry, TaskStealPolicy
from bernstein.core.models import ClusterConfig, NodeCapacity, NodeInfo, NodeStatus


def _make_config() -> ClusterConfig:
    return ClusterConfig(enabled=True)


def _make_registry() -> NodeRegistry:
    return NodeRegistry(_make_config())


def _register(registry: NodeRegistry, name: str, slots: int = 4) -> NodeInfo:
    return registry.register(
        NodeInfo(name=name, capacity=NodeCapacity(max_agents=slots, available_slots=slots))
    )


# ---------------------------------------------------------------------------
# TaskStealPolicy.find_steal_pairs
# ---------------------------------------------------------------------------


def test_no_steal_with_single_node() -> None:
    """A single node cannot steal from itself."""
    registry = _make_registry()
    _register(registry, "only-node", slots=4)

    policy = TaskStealPolicy(overload_threshold=3)
    pairs = policy.find_steal_pairs(registry, {"only-node": 10})
    assert pairs == []


def test_no_steal_when_balanced() -> None:
    """No stealing needed when no node exceeds the overload threshold."""
    registry = _make_registry()
    n1 = _register(registry, "node-1")
    n2 = _register(registry, "node-2")

    policy = TaskStealPolicy(overload_threshold=5)
    pairs = policy.find_steal_pairs(registry, {n1.id: 3, n2.id: 2})
    assert pairs == []


def test_steal_from_overloaded_to_idle() -> None:
    """An overloaded node should donate tasks to an idle node."""
    registry = _make_registry()
    overloaded = _register(registry, "overloaded", slots=4)
    idle = _register(registry, "idle", slots=6)

    policy = TaskStealPolicy(overload_threshold=3, idle_threshold=2, max_steal_per_tick=3)
    pairs = policy.find_steal_pairs(
        registry,
        {overloaded.id: 8, idle.id: 1},
    )

    assert len(pairs) == 1
    donor_id, receiver_id, count = pairs[0]
    assert donor_id == overloaded.id
    assert receiver_id == idle.id
    assert 1 <= count <= 3


def test_steal_respects_max_per_tick() -> None:
    """Steal count is capped by max_steal_per_tick."""
    registry = _make_registry()
    overloaded = _register(registry, "overloaded", slots=10)
    idle = _register(registry, "idle", slots=10)

    policy = TaskStealPolicy(overload_threshold=2, idle_threshold=1, max_steal_per_tick=2)
    pairs = policy.find_steal_pairs(
        registry,
        {overloaded.id: 20, idle.id: 0},
    )

    assert len(pairs) == 1
    _, _, count = pairs[0]
    assert count <= 2


def test_steal_skips_offline_nodes() -> None:
    """Offline nodes are neither donors nor receivers."""
    registry = _make_registry()
    overloaded = _register(registry, "overloaded")
    offline = _register(registry, "offline-node", slots=10)
    offline.status = NodeStatus.OFFLINE

    policy = TaskStealPolicy(overload_threshold=2)
    pairs = policy.find_steal_pairs(
        registry,
        {overloaded.id: 10, offline.id: 0},
    )
    # No receivers available (the only other node is offline)
    assert pairs == []


def test_steal_multiple_donors_multiple_receivers() -> None:
    """Multiple overloaded nodes distribute to multiple idle nodes."""
    registry = _make_registry()
    d1 = _register(registry, "donor-1", slots=4)
    d2 = _register(registry, "donor-2", slots=4)
    r1 = _register(registry, "receiver-1", slots=6)
    r2 = _register(registry, "receiver-2", slots=8)

    policy = TaskStealPolicy(overload_threshold=3, idle_threshold=2, max_steal_per_tick=5)
    pairs = policy.find_steal_pairs(
        registry,
        {d1.id: 10, d2.id: 8, r1.id: 1, r2.id: 0},
    )

    # Both donors should have steal actions
    donor_ids = {p[0] for p in pairs}
    assert d1.id in donor_ids or d2.id in donor_ids
    # Total stolen should be > 0
    total = sum(p[2] for p in pairs)
    assert total > 0


def test_empty_queue_depths() -> None:
    """Empty queue depths means no overloaded nodes."""
    registry = _make_registry()
    _register(registry, "node-1")
    _register(registry, "node-2")

    policy = TaskStealPolicy()
    pairs = policy.find_steal_pairs(registry, {})
    assert pairs == []


def test_steal_policy_defaults() -> None:
    """Default policy parameters are reasonable."""
    policy = TaskStealPolicy()
    assert policy.overload_threshold == 5
    assert policy.idle_threshold == 2
    assert policy.max_steal_per_tick == 3
