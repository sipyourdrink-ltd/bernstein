"""Focused tests for server_launch.py."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from bernstein.core.seed import SeedConfig
from bernstein.core.server_launch import (
    _clean_stale_runtime,
    _inject_manager_task,
    _start_server,
    _wait_for_server,
    ensure_sdd,
)


def test_ensure_sdd_creates_workspace_and_session_gitignore(tmp_path: Path) -> None:
    """ensure_sdd creates the standard workspace structure and ensures session.json is ignored."""
    created = ensure_sdd(tmp_path)

    assert created is True
    assert (tmp_path / ".sdd" / "runtime" / ".gitignore").read_text(encoding="utf-8").find("session.json") >= 0
    assert (tmp_path / ".sdd" / "config.yaml").exists()


def test_clean_stale_runtime_removes_dead_pids_tasks_and_index_locks(tmp_path: Path) -> None:
    """_clean_stale_runtime removes stale pid files, task logs, and SQLite lock sidecars."""
    runtime = tmp_path / ".sdd" / "runtime"
    index_dir = tmp_path / ".sdd" / "index"
    runtime.mkdir(parents=True)
    index_dir.mkdir(parents=True)
    (runtime / "server.pid").write_text("999", encoding="utf-8")
    (runtime / "tasks.jsonl").write_text("{}", encoding="utf-8")
    (index_dir / "code.db-wal").write_text("", encoding="utf-8")
    (index_dir / "code.db-shm").write_text("", encoding="utf-8")

    with patch("bernstein.core.server.server_launch._is_alive", return_value=False):
        _clean_stale_runtime(tmp_path)

    assert not (runtime / "server.pid").exists()
    assert not (runtime / "tasks.jsonl").exists()
    assert not (index_dir / "code.db-wal").exists()
    assert not (index_dir / "code.db-shm").exists()


def test_wait_for_server_polls_until_health_endpoint_succeeds() -> None:
    """_wait_for_server retries through connection errors until a 200 /health response arrives."""
    healthy = SimpleNamespace(status_code=200)

    with (
        patch("bernstein.core.server.server_launch.httpx.get", side_effect=[httpx.ConnectError("down"), healthy]),
        patch("bernstein.core.server.server_launch.time.sleep"),
        patch("bernstein.core.server.server_launch.time.monotonic", side_effect=[0.0, 1.0, 2.0]),
    ):
        assert _wait_for_server(8052) is True


def test_inject_manager_task_posts_seed_payload_with_auth_header(tmp_path: Path) -> None:
    """_inject_manager_task posts the initial manager task and includes bearer auth when configured."""
    seed = SeedConfig(goal="Ship planner")
    response = MagicMock()
    response.status_code = 201
    response.json.return_value = {"id": "mgr-1"}

    with (
        patch("bernstein.core.server.server_launch.seed_to_initial_task", return_value=SimpleNamespace(description="Plan it")),
        patch("bernstein.core.server.server_launch.httpx.post", return_value=response) as mock_post,
    ):
        task_id = _inject_manager_task(seed, tmp_path, 8052, auth_token="secret")

    assert task_id == "mgr-1"
    assert mock_post.call_args.kwargs["headers"] == {"Authorization": "Bearer secret"}
    assert mock_post.call_args.kwargs["json"]["title"] == "Plan and decompose goal into tasks"


def test_start_server_rejects_existing_live_pid(tmp_path: Path) -> None:
    """_start_server raises RuntimeError when the pid file points to an already-live server."""
    (tmp_path / ".sdd" / "runtime").mkdir(parents=True)

    with (
        patch("bernstein.core.server.server_launch._read_pid", return_value=1234),
        patch("bernstein.core.server.server_launch._is_alive", return_value=True),
        pytest.raises(RuntimeError),
    ):
        _start_server(tmp_path, 8052)
