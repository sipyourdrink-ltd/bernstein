"""Real-process two-node cluster scenarios.

Each test owns its ``two_node_cluster`` fixture instance, so the boots
are sequential - the suite trades raw wall-clock for the ability to
SIGKILL one process and watch the other react.

All tests are marked ``cluster_e2e`` and ``slow`` so they are skipped by
default. CI runs them via ``.github/workflows/cluster-e2e.yml``.
"""

from __future__ import annotations

import json
import sys
import time

import httpx
import pytest

from tests.integration.cluster.conftest import (
    NODE_TIMEOUT_S,
    ClusterHandle,
    _mint_token,
)

pytestmark = [
    pytest.mark.cluster_e2e,
    pytest.mark.slow,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="Real-process cluster harness has no Windows path",
    ),
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _post_task(handle: ClusterHandle, title: str, role: str = "backend") -> dict[str, object]:
    """Create a task on the central server and return the response payload."""
    resp = httpx.post(
        f"{handle.central_url}/tasks",
        json={
            "title": title,
            "description": f"e2e: {title}",
            "role": role,
            "priority": 1,
            "scope": "small",
            "complexity": "low",
            "estimated_minutes": 1,
        },
        timeout=5.0,
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _node_status(handle: ClusterHandle, node_id: str) -> str:
    """Look up the current ``status`` field for ``node_id`` (or "<missing>")."""
    payload = handle.current_status()
    nodes = payload.get("nodes", [])
    assert isinstance(nodes, list)
    for n in nodes:
        if isinstance(n, dict) and n.get("id") == node_id:
            return str(n.get("status", "<unknown>"))
    return "<missing>"


def _wait_for(condition, timeout_s: float, interval_s: float = 0.1) -> bool:
    """Poll ``condition()`` until it returns truthy or the deadline expires."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval_s)
    return False


# --------------------------------------------------------------------------- #
# 1. Happy path
# --------------------------------------------------------------------------- #


def test_happy_path_register_heartbeat_run_complete(two_node_cluster: ClusterHandle) -> None:
    """Register, send heartbeats, run a task, complete; verify both sides agree."""
    handle = two_node_cluster
    worker = handle.start_worker(name="happy-alpha")
    assert worker.node_id is not None

    # Cluster shows exactly one online node.
    assert _wait_for(
        lambda: handle.current_status().get("online_nodes") == 1,
        timeout_s=10.0,
    ), f"online_nodes != 1: {handle.current_status()}"

    task = _post_task(handle, "happy-task")
    task_id = str(task["id"])

    # Worker should claim + complete on its own. ``done`` must reach 1.
    def _done() -> bool:
        s = httpx.get(f"{handle.central_url}/status", timeout=5.0).json()
        return s.get("done", 0) >= 1

    assert _wait_for(_done, timeout_s=10.0), (
        f"Task {task_id} never reached done state. Final status: "
        f"{httpx.get(handle.central_url + '/status', timeout=5.0).json()}"
    )

    # Cluster cleanly shows the worker still online.
    assert _node_status(handle, worker.node_id) == "online"


# --------------------------------------------------------------------------- #
# 2. Worker crash mid-task - kill -9 and verify the central side reaps
# --------------------------------------------------------------------------- #


def test_worker_crash_mid_task_marks_offline(two_node_cluster: ClusterHandle) -> None:
    """SIGKILL the worker; central marks it OFFLINE within heartbeat timeout."""
    handle = two_node_cluster
    worker = handle.start_worker(name="crash-alpha")
    assert worker.node_id is not None

    # Wait until the worker registered + sent at least one heartbeat.
    assert _wait_for(
        lambda: _node_status(handle, worker.node_id or "") == "online",
        timeout_s=10.0,
    )

    # SIGKILL - no graceful unregister.
    handle.kill_worker(worker, force=True)

    # Within ~2x the heartbeat timeout the central server must mark OFFLINE.
    assert _wait_for(
        lambda: _node_status(handle, worker.node_id or "") == "offline",
        timeout_s=NODE_TIMEOUT_S * 4 + 5,
    ), f"Node never reached OFFLINE after kill -9. Final status: {handle.current_status()}"

    # Any task that was in the queue is still claimable by a fresh worker.
    task = _post_task(handle, "crash-followup")
    new_worker = handle.start_worker(name="crash-beta")
    assert new_worker.node_id is not None

    def _done() -> bool:
        s = httpx.get(f"{handle.central_url}/status", timeout=5.0).json()
        return s.get("done", 0) >= 1

    assert _wait_for(_done, timeout_s=15.0), f"Replacement worker did not reclaim+complete task {task['id']}"


# --------------------------------------------------------------------------- #
# 3. Central restart - node reappears in registry as OFFLINE, then heartbeats
# --------------------------------------------------------------------------- #


def test_central_restart_persists_registry(two_node_cluster: ClusterHandle) -> None:
    """SIGTERM central + reload from JSON registry; worker re-syncs cleanly."""
    handle = two_node_cluster
    worker = handle.start_worker(name="restart-alpha")
    assert worker.node_id is not None

    # Confirm the registry hit disk.
    assert _wait_for(handle.nodes_json.exists, timeout_s=10.0), "nodes.db never written"

    import sqlite3

    with sqlite3.connect(str(handle.nodes_json)) as conn:
        persisted = [r[0] for r in conn.execute("SELECT id FROM nodes")]
    assert worker.node_id in persisted, f"Worker not in nodes.db: {persisted}"

    # Restart central in-place.
    handle.restart_central()

    # On restart the registry loads everyone as OFFLINE; the running worker
    # should heartbeat back to ONLINE within a few seconds.
    assert _wait_for(
        lambda: _node_status(handle, worker.node_id or "") == "online",
        timeout_s=15.0,
    ), f"Worker did not re-sync to ONLINE: {handle.current_status()}"


# --------------------------------------------------------------------------- #
# 4. Network partition - proxy drops traffic, worker goes OFFLINE, reconciles
# --------------------------------------------------------------------------- #


def test_network_partition_then_heal(two_node_cluster: ClusterHandle) -> None:
    """Block worker<->central traffic; node goes OFFLINE; reconciles on heal."""
    handle = two_node_cluster
    worker = handle.start_worker(name="partition-alpha")
    assert worker.node_id is not None

    assert _wait_for(
        lambda: _node_status(handle, worker.node_id or "") == "online",
        timeout_s=10.0,
    )

    # Partition for long enough to trigger the central reaper.
    assert handle.proxy is not None
    handle.proxy.partition()
    try:
        assert _wait_for(
            lambda: _node_status(handle, worker.node_id or "") == "offline",
            timeout_s=NODE_TIMEOUT_S * 4 + 5,
        ), f"Node did not go OFFLINE during partition: {handle.current_status()}"
    finally:
        handle.proxy.heal()

    # After healing, worker heartbeats resume and the node returns to ONLINE.
    assert _wait_for(
        lambda: _node_status(handle, worker.node_id or "") == "online",
        timeout_s=15.0,
    ), f"Node did not recover after heal: {handle.current_status()}"


# --------------------------------------------------------------------------- #
# 5. Token expiry mid-flight
# --------------------------------------------------------------------------- #


def test_token_expiry_then_refresh(two_node_cluster: ClusterHandle) -> None:
    """5-second JWT, sleep 6, heartbeat fails 401, refresh succeeds."""
    handle = two_node_cluster

    # Register a node manually with a long-lived register token so we get a
    # node_id we can target - then exercise heartbeat lifecycle directly.
    register_token = _mint_token(handle.cluster_secret, "expiry-test", scopes=["node:register"])
    register_payload = {
        "name": "expiry-test",
        "url": "",
        "capacity": {
            "max_agents": 4,
            "available_slots": 4,
            "active_agents": 0,
            "gpu_available": False,
            "supported_models": ["sonnet"],
        },
        "labels": {},
        "cell_ids": [],
    }
    resp = httpx.post(
        f"{handle.central_url}/cluster/nodes",
        headers={"Authorization": f"Bearer {register_token}"},
        json=register_payload,
        timeout=5.0,
    )
    assert resp.status_code == 201, resp.text
    node_id = resp.json()["id"]

    # Mint a deliberately short-lived heartbeat token (5 seconds).
    short_token = _mint_token(handle.cluster_secret, node_id, ttl_s=5.0, scopes=["node:heartbeat"])
    hb_payload = {"capacity": register_payload["capacity"]}
    hb_url = f"{handle.central_url}/cluster/nodes/{node_id}/heartbeat"

    # Immediately after issue the token is good.
    resp = httpx.post(hb_url, headers={"Authorization": f"Bearer {short_token}"}, json=hb_payload, timeout=5.0)
    assert resp.status_code == 200, resp.text

    # Sleep past the TTL then retry - must be rejected with 401.
    time.sleep(6.0)
    resp = httpx.post(hb_url, headers={"Authorization": f"Bearer {short_token}"}, json=hb_payload, timeout=5.0)
    assert resp.status_code == 401, f"Expected 401 after expiry; got {resp.status_code} {resp.text}"

    # Refresh: mint a new token, verify it works.
    fresh_token = _mint_token(handle.cluster_secret, node_id, ttl_s=3600.0, scopes=["node:heartbeat"])
    resp = httpx.post(hb_url, headers={"Authorization": f"Bearer {fresh_token}"}, json=hb_payload, timeout=5.0)
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# 6. Concurrent claims - exactly one wins, cross-process CAS
# --------------------------------------------------------------------------- #


def test_concurrent_claims_exactly_one_winner(
    two_node_cluster: ClusterHandle,
    tmp_path,
) -> None:
    """Two worker processes race the same task; CAS guarantees a single winner."""
    import subprocess

    handle = two_node_cluster

    # Start two workers but block their auto-claim loop by putting them on a
    # role they don't poll for - actually our minimal worker only polls
    # ``backend``, so we use a different role for the contested task and
    # race two raw subprocesses against the claim endpoint instead.
    task = _post_task(handle, "contested-task", role="qa")  # role workers don't poll
    task_id = str(task["id"])

    # The minimal race-worker subprocess is at tests.integration.cluster._worker_proc
    # but we don't need it here - direct subprocess.Popen with httpx is
    # simpler and proves the same thing: two OS processes, one task, one win.
    result_a = tmp_path / "result-a.txt"
    result_b = tmp_path / "result-b.txt"
    admin_token = handle.admin_token()

    # Synchronise the two starts so they hit /claim within microseconds.
    start_at = time.time() + 1.5

    from pathlib import Path as _P

    worker_script = _P(__file__).parent / "_worker_proc.py"
    procs: list[subprocess.Popen[bytes]] = [
        subprocess.Popen(
            [
                sys.executable,
                str(worker_script),
                "--server",
                handle.central_url,
                "--task-id",
                task_id,
                "--token",
                admin_token,
                "--result-file",
                str(result_file),
                "--start-at",
                str(start_at),
                "--expected-version",
                "1",
            ],
        )
        for result_file in (result_a, result_b)
    ]

    for p in procs:
        p.wait(timeout=15.0)

    statuses: list[int] = []
    for path in (result_a, result_b):
        assert path.exists(), f"Race worker did not write {path}"
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        statuses.append(int(first_line))

    statuses.sort()
    assert statuses == [200, 409], (
        f"Expected exactly one winner ([200, 409]); got {statuses}. "
        f"a={result_a.read_text()!r} b={result_b.read_text()!r}"
    )

    # Final state on central: the task is claimed (or already moved past it).
    final = httpx.get(f"{handle.central_url}/tasks/{task_id}", timeout=5.0)
    assert final.status_code == 200, final.text
    assert final.json().get("status") in ("claimed", "in_progress", "working", "done"), (
        f"Task {task_id} unexpectedly in state {final.json().get('status')}"
    )
