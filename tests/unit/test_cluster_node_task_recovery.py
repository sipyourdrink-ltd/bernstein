"""Regression tests for dead-worker detection and claim recovery (#2801).

When a cluster worker leaves -- by heartbeat timeout (crash) or graceful
unregister -- the tasks it had claimed must return to OPEN so a surviving
worker can pick them up, and the node reaper must run in the joinable
(cluster-auth-off) configuration, not only when cluster mode is enabled.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from bernstein.core.cluster import NodeRegistry
from bernstein.core.models import ClusterConfig, NodeInfo, NodeStatus

from bernstein.cli.commands.worker_cmd import WorkerLoop
from bernstein.core.routes.task_cluster import unregister_node
from bernstein.core.server.server_app import _node_reaper_loop
from bernstein.core.tasks.models import TaskStatus
from bernstein.core.tasks.task_store_core import TaskStore


def _req(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "title": "T",
        "description": "D",
        "role": "backend",
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
        "estimated_minutes": 30,
        "depends_on": [],
        "owned_files": [],
        "cell_id": None,
        "task_type": "standard",
        "upgrade_details": None,
        "model": None,
        "effort": None,
        "batch_eligible": False,
        "completion_signals": [],
        "slack_context": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "runtime" / "tasks.jsonl", archive_path=tmp_path / "archive" / "tasks.jsonl")


# ---------------------------------------------------------------------------
# TaskStore.reopen_tasks_for_node
# ---------------------------------------------------------------------------


class TestReopenTasksForNode:
    @pytest.mark.anyio
    async def test_releases_only_the_departed_nodes_claims(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await store.create(_req(title="one"))
        await store.create(_req(title="two"))
        # Claim order is by (priority, task_id), so key off the returned tasks
        # rather than assuming which named task each node received.
        claimed_a = await store.claim_next("backend", claimed_by_session="node-A")
        claimed_b = await store.claim_next("backend", claimed_by_session="node-B")
        assert claimed_a is not None
        assert claimed_b is not None

        released = store.reopen_tasks_for_node("node-A")

        assert released == 1
        assert store.get_task(claimed_a.id).status is TaskStatus.OPEN
        assert store.get_task(claimed_a.id).claimed_by_session is None
        # Node B's task is untouched.
        assert store.get_task(claimed_b.id).status is TaskStatus.CLAIMED
        assert store.get_task(claimed_b.id).claimed_by_session == "node-B"

    @pytest.mark.anyio
    async def test_noop_for_unknown_or_empty_node(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        await store.create(_req())
        await store.claim_next("backend", claimed_by_session="node-A")
        assert store.reopen_tasks_for_node("node-Z") == 0
        assert store.reopen_tasks_for_node("") == 0

    @pytest.mark.anyio
    async def test_release_survives_jsonl_replay(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        task = await store.create(_req())
        await store.claim_next("backend", claimed_by_session="node-A")
        await store.flush_buffer()

        assert store.reopen_tasks_for_node("node-A") == 1

        fresh = _store(tmp_path)
        fresh.replay_jsonl()
        replayed = fresh.get_task(task.id)
        assert replayed is not None
        assert replayed.status is TaskStatus.OPEN


# ---------------------------------------------------------------------------
# Node reaper loop: detect stale node AND release its claims
# ---------------------------------------------------------------------------


class TestNodeReaperLoop:
    @pytest.mark.anyio
    async def test_stale_node_marked_offline_and_claims_released(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        task = await store.create(_req())

        registry = NodeRegistry(ClusterConfig(enabled=False, node_timeout_s=1))
        node = registry.register(NodeInfo(name="worker-1", url="http://w1:8052"))
        await store.claim_next("backend", claimed_by_session=node.id)
        node.last_heartbeat = time.time() - 999  # definitely stale

        # Run exactly one reaper iteration, then cancel.
        calls = 0

        async def _fake_sleep(_seconds: float) -> None:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise asyncio.CancelledError

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            await _node_reaper_loop(registry, store, interval_s=0.0)

        assert registry.get(node.id) is not None
        assert registry.get(node.id).status is NodeStatus.OFFLINE
        assert store.get_task(task.id).status is TaskStatus.OPEN


# ---------------------------------------------------------------------------
# Unregister route: graceful leave releases claims
# ---------------------------------------------------------------------------


class TestUnregisterReleasesClaims:
    @pytest.mark.anyio
    async def test_graceful_unregister_reopens_node_tasks(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        task = await store.create(_req())
        registry = NodeRegistry(ClusterConfig(enabled=False))
        node = registry.register(NodeInfo(name="worker-1", url="http://w1:8052"))
        await store.claim_next("backend", claimed_by_session=node.id)

        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    cluster_authenticator=None,
                    node_registry=registry,
                    store=store,
                )
            ),
            headers={},
        )

        response = unregister_node(node.id, request)  # type: ignore[arg-type]

        assert response.status_code == 204
        assert registry.get(node.id) is None
        assert store.get_task(task.id).status is TaskStatus.OPEN


# ---------------------------------------------------------------------------
# Worker records its node id as the claim owner
# ---------------------------------------------------------------------------


class TestWorkerClaimStampsNode:
    def test_claim_task_sends_node_id_as_claim_owner(self, tmp_path: Path) -> None:
        loop = WorkerLoop(server_url="http://central:8052", name="w", adapter="claude", workdir=tmp_path)
        client = mock.MagicMock()
        client.get.return_value = mock.MagicMock(status_code=200, json=lambda: {"id": "t-1"})

        loop._claim_task(client, "backend", "node-42")

        _args, kwargs = client.get.call_args
        assert kwargs["params"] == {"claimed_by_session": "node-42"}

    def test_claim_task_without_node_id_sends_no_params(self, tmp_path: Path) -> None:
        loop = WorkerLoop(server_url="http://central:8052", name="w", adapter="claude", workdir=tmp_path)
        client = mock.MagicMock()
        client.get.return_value = mock.MagicMock(status_code=200, json=lambda: {"id": "t-1"})

        loop._claim_task(client, "backend")

        _args, kwargs = client.get.call_args
        assert kwargs["params"] is None
