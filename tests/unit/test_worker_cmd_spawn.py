"""Tests for WorkerLoop._spawn_agent in the worker CLI command."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from bernstein.core.models import AgentSession, Task

from bernstein.cli.commands.worker_cmd import WorkerLoop

if TYPE_CHECKING:
    from pathlib import Path

TASK_DICT = {
    "id": "task-42",
    "title": "Fix the flaky heartbeat",
    "description": "Heartbeat loop drops the node after eviction.",
    "role": "backend",
}

_ENV_KEYS = ("BERNSTEIN_SERVER_URL", "BERNSTEIN_AUTH_TOKEN")


@pytest.fixture(autouse=True)
def _restore_server_env():
    """Snapshot and restore the env vars _spawn_agent mutates.

    _spawn_agent exports BERNSTEIN_SERVER_URL / BERNSTEIN_AUTH_TOKEN into
    os.environ as a side effect. Without an explicit restore those values
    leak into the rest of the pytest session and change the behaviour of
    later tests that read them (e.g. auth middleware and CLI helpers).
    """
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _make_loop(tmp_path: Path) -> WorkerLoop:
    return WorkerLoop(
        server_url="http://central:8052",
        name="test-node",
        auth_token="secret-token",
        adapter="claude",
        workdir=tmp_path,
    )


class TestSpawnAgent:
    """Tests for WorkerLoop._spawn_agent."""

    def test_spawn_returns_pid_and_uses_roles_templates_dir(self, tmp_path: Path, monkeypatch) -> None:
        """_spawn_agent resolves the adapter, builds a Task, and returns the session PID."""
        monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
        monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
        loop = _make_loop(tmp_path)
        fake_adapter = mock.MagicMock(name="adapter")
        session = AgentSession(id="backend-abc", role="backend", pid=4242)

        with (
            mock.patch("bernstein.adapters.registry.get_adapter", return_value=fake_adapter) as get_adapter,
            mock.patch("bernstein.core.agents.spawner.AgentSpawner", autospec=True) as spawner_cls,
        ):
            spawner_cls.return_value.spawn_for_tasks.return_value = session
            pid = loop._spawn_agent(TASK_DICT)

        assert pid == 4242
        get_adapter.assert_called_once_with("claude")
        # AgentSpawner must be constructed with the real contract:
        # an adapter instance and the templates/roles directory.
        kwargs = spawner_cls.call_args.kwargs
        assert kwargs["adapter"] is fake_adapter
        assert kwargs["templates_dir"].name == "roles"
        assert kwargs["workdir"] == tmp_path
        # spawn_for_tasks receives a single Task built from the claimed dict.
        (tasks,) = spawner_cls.return_value.spawn_for_tasks.call_args.args
        assert len(tasks) == 1
        assert isinstance(tasks[0], Task)
        assert tasks[0].id == "task-42"
        assert tasks[0].role == "backend"

    def test_spawn_exports_server_env_for_agents(self, tmp_path: Path, monkeypatch) -> None:
        """Spawned agents inherit server URL and auth token via env vars."""
        monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
        monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
        loop = _make_loop(tmp_path)
        session = AgentSession(id="backend-abc", role="backend", pid=4242)

        with (
            mock.patch("bernstein.adapters.registry.get_adapter", return_value=mock.MagicMock()),
            mock.patch("bernstein.core.agents.spawner.AgentSpawner", autospec=True) as spawner_cls,
        ):
            spawner_cls.return_value.spawn_for_tasks.return_value = session
            loop._spawn_agent(TASK_DICT)

        assert os.environ["BERNSTEIN_SERVER_URL"] == "http://central:8052"
        assert os.environ["BERNSTEIN_AUTH_TOKEN"] == "secret-token"

    def test_spawn_failure_returns_none_and_warns(self, tmp_path: Path, caplog) -> None:
        """A spawn error is logged as a warning and returns None instead of raising."""
        loop = _make_loop(tmp_path)

        with (
            mock.patch("bernstein.adapters.registry.get_adapter", return_value=mock.MagicMock()),
            mock.patch("bernstein.core.agents.spawner.AgentSpawner", autospec=True) as spawner_cls,
            caplog.at_level("WARNING", logger="bernstein.cli.commands.worker_cmd"),
        ):
            spawner_cls.return_value.spawn_for_tasks.side_effect = RuntimeError("boom")
            pid = loop._spawn_agent(TASK_DICT)

        assert pid is None
        assert any("Failed to spawn agent for task task-42" in rec.getMessage() for rec in caplog.records)

    def test_spawn_returns_none_when_session_has_no_pid(self, tmp_path: Path) -> None:
        """A session without a PID yields None so the slot is not consumed."""
        loop = _make_loop(tmp_path)
        session = AgentSession(id="backend-abc", role="backend", pid=None)

        with (
            mock.patch("bernstein.adapters.registry.get_adapter", return_value=mock.MagicMock()),
            mock.patch("bernstein.core.agents.spawner.AgentSpawner", autospec=True) as spawner_cls,
        ):
            spawner_cls.return_value.spawn_for_tasks.return_value = session
            pid = loop._spawn_agent(TASK_DICT)

        assert pid is None
