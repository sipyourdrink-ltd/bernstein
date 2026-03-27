"""Tests for agent progress checkpoints and stall detection."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.agent_lifecycle import check_stalled_tasks
from bernstein.core.models import AgentSession, ModelConfig, ProgressSnapshot
from bernstein.core.server import TaskStore, create_app

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# ProgressSnapshot model
# ---------------------------------------------------------------------------


class TestProgressSnapshot:
    def test_default_fields(self) -> None:
        snap = ProgressSnapshot(timestamp=1000.0)
        assert snap.timestamp == 1000.0
        assert snap.files_changed == 0
        assert snap.tests_passing == -1
        assert snap.errors == 0
        assert snap.last_file == ""

    def test_is_same_progress_identical(self) -> None:
        a = ProgressSnapshot(timestamp=1.0, files_changed=3, tests_passing=10, errors=0)
        b = ProgressSnapshot(timestamp=2.0, files_changed=3, tests_passing=10, errors=0)
        assert a.is_same_progress(b)

    def test_is_same_progress_different_files(self) -> None:
        a = ProgressSnapshot(timestamp=1.0, files_changed=2, tests_passing=10, errors=0)
        b = ProgressSnapshot(timestamp=2.0, files_changed=3, tests_passing=10, errors=0)
        assert not a.is_same_progress(b)

    def test_is_same_progress_different_tests(self) -> None:
        a = ProgressSnapshot(timestamp=1.0, files_changed=2, tests_passing=8, errors=0)
        b = ProgressSnapshot(timestamp=2.0, files_changed=2, tests_passing=10, errors=0)
        assert not a.is_same_progress(b)

    def test_is_same_progress_different_errors(self) -> None:
        a = ProgressSnapshot(timestamp=1.0, files_changed=2, tests_passing=10, errors=1)
        b = ProgressSnapshot(timestamp=2.0, files_changed=2, tests_passing=10, errors=0)
        assert not a.is_same_progress(b)

    def test_is_same_progress_ignores_last_file(self) -> None:
        """Changing only last_file does not count as progress."""
        a = ProgressSnapshot(timestamp=1.0, files_changed=2, tests_passing=10, errors=0, last_file="a.py")
        b = ProgressSnapshot(timestamp=2.0, files_changed=2, tests_passing=10, errors=0, last_file="b.py")
        assert a.is_same_progress(b)

    def test_is_same_progress_ignores_timestamp(self) -> None:
        a = ProgressSnapshot(timestamp=1.0, files_changed=2, tests_passing=10, errors=0)
        b = ProgressSnapshot(timestamp=999.0, files_changed=2, tests_passing=10, errors=0)
        assert a.is_same_progress(b)


# ---------------------------------------------------------------------------
# TaskStore snapshot methods
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "tasks.jsonl")


class TestTaskStoreSnapshots:
    def test_add_snapshot_returns_snapshot(self, store: TaskStore) -> None:
        snap = store.add_snapshot("task1", files_changed=3, tests_passing=10, errors=0, last_file="a.py")
        assert isinstance(snap, ProgressSnapshot)
        assert snap.files_changed == 3
        assert snap.tests_passing == 10
        assert snap.errors == 0
        assert snap.last_file == "a.py"
        assert snap.timestamp > 0

    def test_get_snapshots_empty(self, store: TaskStore) -> None:
        assert store.get_snapshots("unknown") == []

    def test_get_snapshots_returns_oldest_first(self, store: TaskStore) -> None:
        store.add_snapshot("t1", files_changed=1, tests_passing=-1, errors=0, last_file="")
        store.add_snapshot("t1", files_changed=2, tests_passing=-1, errors=0, last_file="")
        snaps = store.get_snapshots("t1")
        assert len(snaps) == 2
        assert snaps[0].files_changed == 1
        assert snaps[1].files_changed == 2

    def test_get_snapshots_capped_at_10(self, store: TaskStore) -> None:
        for i in range(15):
            store.add_snapshot("t1", files_changed=i, tests_passing=-1, errors=0, last_file="")
        snaps = store.get_snapshots("t1")
        assert len(snaps) == 10
        # Should keep the 10 most recent (files_changed = 5..14)
        assert snaps[0].files_changed == 5
        assert snaps[-1].files_changed == 14

    def test_snapshots_isolated_per_task(self, store: TaskStore) -> None:
        store.add_snapshot("t1", files_changed=1, tests_passing=-1, errors=0, last_file="")
        store.add_snapshot("t2", files_changed=99, tests_passing=-1, errors=0, last_file="")
        assert store.get_snapshots("t1")[0].files_changed == 1
        assert store.get_snapshots("t2")[0].files_changed == 99


# ---------------------------------------------------------------------------
# HTTP API — POST /tasks/{id}/progress with snapshot fields
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path: Path):  # type: ignore[no-untyped-def]
    return create_app(jsonl_path=tmp_path / "tasks.jsonl")


@pytest.fixture()
async def client(app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _create_task(client: AsyncClient) -> str:
    resp = await client.post(
        "/tasks",
        json={"title": "Test task", "description": "desc", "role": "backend"},
    )
    assert resp.status_code == 201
    return str(resp.json()["id"])


@pytest.mark.anyio
async def test_progress_with_snapshot_fields_stored(client: AsyncClient) -> None:
    """POST /tasks/{id}/progress with snapshot fields creates a retrievable snapshot."""
    task_id = await _create_task(client)

    resp = await client.post(
        f"/tasks/{task_id}/progress",
        json={"files_changed": 3, "tests_passing": 10, "errors": 0},
    )
    assert resp.status_code == 200

    snaps_resp = await client.get(f"/tasks/{task_id}/snapshots")
    assert snaps_resp.status_code == 200
    snaps = snaps_resp.json()
    assert len(snaps) == 1
    assert snaps[0]["files_changed"] == 3
    assert snaps[0]["tests_passing"] == 10
    assert snaps[0]["errors"] == 0


@pytest.mark.anyio
async def test_progress_without_snapshot_fields_no_snapshot(client: AsyncClient) -> None:
    """POST /tasks/{id}/progress with only message does not store a snapshot."""
    task_id = await _create_task(client)

    resp = await client.post(
        f"/tasks/{task_id}/progress",
        json={"message": "doing work", "percent": 20},
    )
    assert resp.status_code == 200

    snaps_resp = await client.get(f"/tasks/{task_id}/snapshots")
    assert snaps_resp.status_code == 200
    assert snaps_resp.json() == []


@pytest.mark.anyio
async def test_get_snapshots_multiple(client: AsyncClient) -> None:
    """GET /tasks/{id}/snapshots returns multiple snapshots in order."""
    task_id = await _create_task(client)

    for files in [1, 2, 3]:
        await client.post(
            f"/tasks/{task_id}/progress",
            json={"files_changed": files, "tests_passing": -1, "errors": 0},
        )

    snaps = (await client.get(f"/tasks/{task_id}/snapshots")).json()
    assert len(snaps) == 3
    assert [s["files_changed"] for s in snaps] == [1, 2, 3]


@pytest.mark.anyio
async def test_get_snapshots_empty(client: AsyncClient) -> None:
    """GET /tasks/{id}/snapshots returns [] for task with no snapshots."""
    task_id = await _create_task(client)
    resp = await client.get(f"/tasks/{task_id}/snapshots")
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# check_stalled_tasks — stall detection logic
# ---------------------------------------------------------------------------


def _make_orch(
    tmp_path: Path,
    snapshots_by_task: dict[str, list[dict[str, Any]]],
) -> Any:
    """Build a minimal mock orchestrator with the stall detection state."""
    orch = MagicMock()
    orch._config.server_url = "http://mock"
    orch._stall_counts = {}
    orch._last_snapshot = {}
    orch._last_snapshot_ts = {}

    session = AgentSession(
        id="sess-1",
        role="backend",
        task_ids=list(snapshots_by_task.keys()),
        model_config=ModelConfig("sonnet", "high"),
        spawn_ts=time.time() - 200,
    )
    orch._agents = {"sess-1": session}

    def _get(url: str, **kwargs: Any) -> MagicMock:
        for task_id, snaps in snapshots_by_task.items():
            if url.endswith(f"/tasks/{task_id}/snapshots"):
                resp = MagicMock()
                resp.json.return_value = snaps
                return resp
        resp = MagicMock()
        resp.json.return_value = []
        return resp

    orch._client.get.side_effect = _get
    return orch


def _make_snap(ts: float, files: int = 1, tests: int = 5, errors: int = 0) -> dict[str, Any]:
    return {
        "timestamp": ts,
        "files_changed": files,
        "tests_passing": tests,
        "errors": errors,
        "last_file": "",
    }


class TestCheckStalledTasks:
    def test_no_snapshots_no_action(self, tmp_path: Path) -> None:
        orch = _make_orch(tmp_path, {"task-1": []})
        check_stalled_tasks(orch)
        orch._signal_mgr.write_wakeup.assert_not_called()
        orch._signal_mgr.write_shutdown.assert_not_called()
        orch._spawner.kill.assert_not_called()

    def test_single_snapshot_no_stall(self, tmp_path: Path) -> None:
        orch = _make_orch(tmp_path, {"task-1": [_make_snap(100.0)]})
        check_stalled_tasks(orch)
        orch._signal_mgr.write_wakeup.assert_not_called()

    def test_two_different_snapshots_no_stall(self, tmp_path: Path) -> None:
        orch = _make_orch(tmp_path, {"task-1": [_make_snap(100.0, files=1), _make_snap(160.0, files=2)]})
        # First call: processes snap at 100s
        check_stalled_tasks(orch)
        # Second call: processes snap at 160s (different files_changed → no stall)
        # Simulate returning latest snapshot on next check
        check_stalled_tasks(orch)
        orch._signal_mgr.write_wakeup.assert_not_called()

    def test_three_identical_snapshots_triggers_wakeup(self, tmp_path: Path) -> None:
        base_ts = 1000.0
        # First tick: last snapshot at base_ts
        orch = _make_orch(tmp_path, {"task-1": [_make_snap(base_ts)]})
        check_stalled_tasks(orch)
        assert orch._stall_counts.get("task-1", 0) == 0  # first snapshot seen, no prev

        # Second tick: new identical snapshot
        orch._client.get.side_effect = None
        resp2 = MagicMock()
        resp2.json.return_value = [_make_snap(base_ts + 60)]
        orch._client.get.return_value = resp2
        check_stalled_tasks(orch)
        assert orch._stall_counts["task-1"] == 1

        # Third tick: another identical snapshot → count=2
        resp3 = MagicMock()
        resp3.json.return_value = [_make_snap(base_ts + 120)]
        orch._client.get.return_value = resp3
        check_stalled_tasks(orch)
        assert orch._stall_counts["task-1"] == 2

        # Fourth tick: another identical snapshot → count=3 → WAKEUP
        resp4 = MagicMock()
        resp4.json.return_value = [_make_snap(base_ts + 180)]
        orch._client.get.return_value = resp4
        check_stalled_tasks(orch)
        assert orch._stall_counts["task-1"] == 3
        orch._signal_mgr.write_wakeup.assert_called_once()
        orch._signal_mgr.write_shutdown.assert_not_called()

    def test_five_identical_snapshots_triggers_shutdown(self, tmp_path: Path) -> None:
        base_ts = 2000.0
        orch = _make_orch(tmp_path, {"task-1": [_make_snap(base_ts)]})

        # First call: processes initial snapshot (no stall yet)
        check_stalled_tasks(orch)
        # Clear side_effect so return_value takes over for subsequent calls
        orch._client.get.side_effect = None

        # Simulate 5 consecutive identical snapshots
        for i in range(1, 6):
            resp = MagicMock()
            resp.json.return_value = [_make_snap(base_ts + i * 60)]
            orch._client.get.return_value = resp
            check_stalled_tasks(orch)

        assert orch._stall_counts["task-1"] == 5
        orch._signal_mgr.write_shutdown.assert_called()

    def test_seven_identical_snapshots_triggers_kill(self, tmp_path: Path) -> None:
        base_ts = 3000.0
        orch = _make_orch(tmp_path, {"task-1": [_make_snap(base_ts)]})

        # First call: processes initial snapshot (no stall yet)
        check_stalled_tasks(orch)
        # Clear side_effect so return_value takes over for subsequent calls
        orch._client.get.side_effect = None

        # Simulate 7 consecutive identical snapshots
        for i in range(1, 8):
            resp = MagicMock()
            resp.json.return_value = [_make_snap(base_ts + i * 60)]
            orch._client.get.return_value = resp
            check_stalled_tasks(orch)

        # Kill should have been called and count reset
        orch._spawner.kill.assert_called()
        assert orch._stall_counts["task-1"] == 0  # reset after kill

    def test_progress_resets_stall_count(self, tmp_path: Path) -> None:
        base_ts = 4000.0
        orch = _make_orch(tmp_path, {"task-1": [_make_snap(base_ts)]})

        # First call: processes initial snapshot
        check_stalled_tasks(orch)
        # Clear side_effect so return_value takes over
        orch._client.get.side_effect = None

        # 2 identical snapshots → stall_count = 2
        for i in range(1, 3):
            resp = MagicMock()
            resp.json.return_value = [_make_snap(base_ts + i * 60)]
            orch._client.get.return_value = resp
            check_stalled_tasks(orch)
        assert orch._stall_counts["task-1"] == 2

        # New snapshot with different files_changed → count reset to 0
        resp_new = MagicMock()
        resp_new.json.return_value = [_make_snap(base_ts + 180, files=5)]
        orch._client.get.return_value = resp_new
        check_stalled_tasks(orch)
        assert orch._stall_counts["task-1"] == 0

    def test_dead_agents_skipped(self, tmp_path: Path) -> None:
        orch = _make_orch(tmp_path, {"task-1": [_make_snap(100.0)]})
        orch._agents["sess-1"].status = "dead"
        check_stalled_tasks(orch)
        orch._client.get.assert_not_called()

    def test_http_error_skips_task(self, tmp_path: Path) -> None:
        orch = _make_orch(tmp_path, {"task-1": []})
        orch._client.get.side_effect = Exception("connection refused")
        # Should not raise
        check_stalled_tasks(orch)
        orch._signal_mgr.write_wakeup.assert_not_called()

    def test_already_processed_snapshot_not_double_counted(self, tmp_path: Path) -> None:
        base_ts = 5000.0
        orch = _make_orch(tmp_path, {"task-1": [_make_snap(base_ts)]})
        # First call
        check_stalled_tasks(orch)
        count_after_first = orch._stall_counts.get("task-1", 0)
        # Second call with same snapshot (same timestamp) → no increment
        check_stalled_tasks(orch)
        assert orch._stall_counts.get("task-1", 0) == count_after_first
