"""Tests for MCP-009: A2A protocol federation."""

from __future__ import annotations

import pytest
from bernstein.core.a2a_federation import (
    A2AFederation,
    FederatedTask,
    FederatedTaskStatus,
    FederationPeer,
    PeerState,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def federation() -> A2AFederation:
    return A2AFederation(local_endpoint="http://localhost:8052")


@pytest.fixture()
def fed_with_peer(federation: A2AFederation) -> A2AFederation:
    federation.register_peer("design-team", "http://design.local:8052", capabilities=["ui", "css"])
    return federation


# ---------------------------------------------------------------------------
# Tests — Peer management
# ---------------------------------------------------------------------------


class TestPeerManagement:
    def test_register_peer(self, federation: A2AFederation) -> None:
        peer = federation.register_peer("backend", "http://backend.local:8052")
        assert peer.name == "backend"
        assert peer.state == PeerState.ACTIVE
        assert peer.endpoint == "http://backend.local:8052"

    def test_register_peer_strips_trailing_slash(self, federation: A2AFederation) -> None:
        peer = federation.register_peer("test", "http://example.com/")
        assert peer.endpoint == "http://example.com"

    def test_register_with_capabilities(self, federation: A2AFederation) -> None:
        peer = federation.register_peer("test", "http://test.local", capabilities=["a", "b"])
        assert peer.capabilities == ["a", "b"]

    def test_get_peer(self, fed_with_peer: A2AFederation) -> None:
        peer = fed_with_peer.get_peer("design-team")
        assert peer is not None
        assert peer.name == "design-team"

    def test_get_unknown_peer(self, federation: A2AFederation) -> None:
        assert federation.get_peer("nonexistent") is None

    def test_deregister_peer(self, fed_with_peer: A2AFederation) -> None:
        assert fed_with_peer.deregister_peer("design-team") is True
        peer = fed_with_peer.get_peer("design-team")
        assert peer is not None
        assert peer.state == PeerState.DEREGISTERED

    def test_deregister_unknown(self, federation: A2AFederation) -> None:
        assert federation.deregister_peer("nonexistent") is False

    def test_list_peers(self, fed_with_peer: A2AFederation) -> None:
        peers = fed_with_peer.list_peers()
        assert len(peers) == 1
        assert peers[0].name == "design-team"


# ---------------------------------------------------------------------------
# Tests — Task delegation (outbound)
# ---------------------------------------------------------------------------


class TestTaskDelegation:
    def test_delegate_task(self, fed_with_peer: A2AFederation) -> None:
        task = fed_with_peer.delegate_task("design-team", "Create wireframes")
        assert task is not None
        assert task.direction == "outbound"
        assert task.status == FederatedTaskStatus.PENDING
        assert task.peer_name == "design-team"

    def test_delegate_to_unknown_peer(self, federation: A2AFederation) -> None:
        task = federation.delegate_task("nonexistent", "Test")
        assert task is None

    def test_delegate_to_deregistered_peer(self, fed_with_peer: A2AFederation) -> None:
        fed_with_peer.deregister_peer("design-team")
        task = fed_with_peer.delegate_task("design-team", "Test")
        assert task is None

    def test_mark_sent(self, fed_with_peer: A2AFederation) -> None:
        task = fed_with_peer.delegate_task("design-team", "Test")
        assert task is not None
        result = fed_with_peer.mark_sent(task.id, "remote-123")
        assert result is True
        updated = fed_with_peer.get_task(task.id)
        assert updated is not None
        assert updated.remote_task_id == "remote-123"
        assert updated.status == FederatedTaskStatus.SENT

    def test_mark_sent_unknown(self, federation: A2AFederation) -> None:
        assert federation.mark_sent("nonexistent", "r") is False

    def test_delegate_increments_peer_count(self, fed_with_peer: A2AFederation) -> None:
        fed_with_peer.delegate_task("design-team", "T1")
        fed_with_peer.delegate_task("design-team", "T2")
        peer = fed_with_peer.get_peer("design-team")
        assert peer is not None
        assert peer.task_count == 2


# ---------------------------------------------------------------------------
# Tests — Inbound tasks
# ---------------------------------------------------------------------------


class TestInboundTasks:
    def test_accept_inbound(self, federation: A2AFederation) -> None:
        task = federation.accept_inbound_task("ext-peer", "remote-456", "Fix bug")
        assert task.direction == "inbound"
        assert task.status == FederatedTaskStatus.ACCEPTED
        assert task.remote_task_id == "remote-456"

    def test_link_local_task(self, federation: A2AFederation) -> None:
        task = federation.accept_inbound_task("ext-peer", "r1", "Test")
        assert federation.link_local_task(task.id, "local-789") is True
        updated = federation.get_task(task.id)
        assert updated is not None
        assert updated.local_task_id == "local-789"

    def test_link_unknown_task(self, federation: A2AFederation) -> None:
        assert federation.link_local_task("nonexistent", "local-1") is False


# ---------------------------------------------------------------------------
# Tests — Status updates
# ---------------------------------------------------------------------------


class TestStatusUpdates:
    def test_update_status(self, fed_with_peer: A2AFederation) -> None:
        task = fed_with_peer.delegate_task("design-team", "Test")
        assert task is not None
        fed_with_peer.update_status(task.id, FederatedTaskStatus.COMPLETED, result="Done")
        updated = fed_with_peer.get_task(task.id)
        assert updated is not None
        assert updated.status == FederatedTaskStatus.COMPLETED
        assert updated.result == "Done"

    def test_update_unknown(self, federation: A2AFederation) -> None:
        assert federation.update_status("nonexistent", FederatedTaskStatus.FAILED) is False


# ---------------------------------------------------------------------------
# Tests — Listing and filtering
# ---------------------------------------------------------------------------


class TestListAndFilter:
    def test_list_all(self, fed_with_peer: A2AFederation) -> None:
        fed_with_peer.delegate_task("design-team", "T1")
        fed_with_peer.accept_inbound_task("ext", "r1", "T2")
        assert len(fed_with_peer.list_tasks()) == 2

    def test_list_by_peer(self, fed_with_peer: A2AFederation) -> None:
        fed_with_peer.delegate_task("design-team", "T1")
        fed_with_peer.accept_inbound_task("ext", "r1", "T2")
        result = fed_with_peer.list_tasks(peer_name="design-team")
        assert len(result) == 1

    def test_list_by_direction(self, fed_with_peer: A2AFederation) -> None:
        fed_with_peer.delegate_task("design-team", "T1")
        fed_with_peer.accept_inbound_task("ext", "r1", "T2")
        outbound = fed_with_peer.list_tasks(direction="outbound")
        inbound = fed_with_peer.list_tasks(direction="inbound")
        assert len(outbound) == 1
        assert len(inbound) == 1


# ---------------------------------------------------------------------------
# Tests — Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_peer_to_dict(self) -> None:
        peer = FederationPeer(name="test", endpoint="http://test.local", capabilities=["a"])
        d = peer.to_dict()
        assert d["name"] == "test"
        assert d["state"] == "active"

    def test_task_to_dict(self) -> None:
        task = FederatedTask(id="t1", peer_name="peer1", message="test")
        d = task.to_dict()
        assert d["id"] == "t1"
        assert d["direction"] == "outbound"

    def test_federation_to_dict(self, fed_with_peer: A2AFederation) -> None:
        fed_with_peer.delegate_task("design-team", "T1")
        d = fed_with_peer.to_dict()
        assert "peers" in d
        assert d["task_count"] == 1
