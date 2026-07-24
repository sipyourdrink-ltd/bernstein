"""Regression tests for worker model/adapter propagation (#2804).

A worker started with a non-Claude adapter (e.g. --adapter qwen) used to:
- discard the claimed task's model / cli / effort / scope / metadata, rebuilding
  a stripped Task(id, title, description, role);
- construct its spawner with no default_model, so an unpinned Claude tier name
  from the role config raised ModelNotConfiguredError on every task;
- advertise a hardcoded ["sonnet", "opus", "haiku"] capability list unrelated to
  the adapter it actually runs.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

import pytest
from bernstein.core.models import AgentSession

from bernstein.cli.commands import worker_cmd
from bernstein.cli.commands.worker_cmd import WorkerLoop

if TYPE_CHECKING:
    from pathlib import Path

RICH_TASK = {
    "id": "task-77",
    "title": "Do the thing",
    "description": "Body.",
    "role": "backend",
    "model": "qwen-max",
    "cli": "qwen",
    "effort": "high",
    "scope": "large",
    "complexity": "high",
    "priority": 1,
    "metadata": {"foo": "bar"},
}

_QWEN_PROFILE = ("qwen-max", ["qwen-max", "qwen-plus", "qwen-turbo"])


def _patch_qwen_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_cmd, "_adapter_model_profile", lambda name: _QWEN_PROFILE)


def _spawn_patches() -> tuple[mock._patch, mock._patch]:
    return (
        mock.patch("bernstein.adapters.registry.get_adapter", return_value=mock.MagicMock()),
        mock.patch("bernstein.core.agents.spawner.AgentSpawner", autospec=True),
    )


class TestSpawnCarriesTaskFields:
    def test_spawn_builds_task_from_full_dict(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_qwen_profile(monkeypatch)
        loop = WorkerLoop(server_url="http://central:8052", adapter="qwen", workdir=tmp_path)
        session = AgentSession(id="qwen-1", role="backend", pid=1234)
        get_adapter_patch, spawner_patch = _spawn_patches()
        with get_adapter_patch, spawner_patch as spawner_cls:
            spawner_cls.return_value.spawn_for_tasks.return_value = session
            loop._spawn_agent(RICH_TASK)

        (tasks,) = spawner_cls.return_value.spawn_for_tasks.call_args.args
        task = tasks[0]
        assert task.id == "task-77"
        assert task.model == "qwen-max"
        assert task.cli == "qwen"
        assert task.effort == "high"
        assert task.scope.value == "large"
        assert task.complexity.value == "high"
        assert task.priority == 1
        assert task.metadata == {"foo": "bar"}


class TestSpawnDefaultModel:
    def test_non_claude_default_from_discovery(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_qwen_profile(monkeypatch)
        loop = WorkerLoop(server_url="http://central:8052", adapter="qwen", workdir=tmp_path)
        assert loop._spawn_default_model == "qwen-max"

    def test_model_flag_overrides_discovery(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_qwen_profile(monkeypatch)
        loop = WorkerLoop(server_url="http://central:8052", adapter="qwen", model="MiniMax-M3", workdir=tmp_path)
        assert loop._spawn_default_model == "MiniMax-M3"

    def test_claude_default_is_left_unset(self, tmp_path: Path) -> None:
        loop = WorkerLoop(server_url="http://central:8052", adapter="claude", workdir=tmp_path)
        assert loop._spawn_default_model is None

    def test_spawn_passes_default_model_to_spawner(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_qwen_profile(monkeypatch)
        loop = WorkerLoop(server_url="http://central:8052", adapter="qwen", workdir=tmp_path)
        session = AgentSession(id="qwen-1", role="backend", pid=1234)
        get_adapter_patch, spawner_patch = _spawn_patches()
        with get_adapter_patch, spawner_patch as spawner_cls:
            spawner_cls.return_value.spawn_for_tasks.return_value = session
            loop._spawn_agent(dict(RICH_TASK, model=None))

        assert spawner_cls.call_args.kwargs["default_model"] == "qwen-max"


class TestSupportedModelsAdvertised:
    def test_claude_advertises_tier_names(self, tmp_path: Path) -> None:
        loop = WorkerLoop(server_url="http://central:8052", adapter="claude", workdir=tmp_path)
        assert loop._supported_models == ["sonnet", "opus", "haiku"]

    def test_non_claude_advertises_discovered_models(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_qwen_profile(monkeypatch)
        loop = WorkerLoop(server_url="http://central:8052", adapter="qwen", workdir=tmp_path)
        assert loop._supported_models == ["qwen-max", "qwen-plus", "qwen-turbo"]
        assert "sonnet" not in loop._supported_models

    def test_register_payload_uses_supported_models(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_qwen_profile(monkeypatch)
        loop = WorkerLoop(server_url="http://central:8052", adapter="qwen", workdir=tmp_path)
        client = mock.MagicMock()
        client.post.return_value = mock.MagicMock(status_code=201, json=lambda: {"id": "n1"})

        node_id = loop._register(client)

        assert node_id == "n1"
        payload = client.post.call_args.kwargs["json"]
        assert payload["capacity"]["supported_models"] == ["qwen-max", "qwen-plus", "qwen-turbo"]
