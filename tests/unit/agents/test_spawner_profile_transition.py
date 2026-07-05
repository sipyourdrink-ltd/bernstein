"""Spawn-path profile_transition events (issue #2245).

A task re-spawned under a different response-style profile (for
example after a role-policy change between attempts) must leave a
``profile_transition`` record so per-profile cost attribution can
exclude the task instead of guessing which tokens belong to which
profile.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.cost.profile_attribution import (
    default_transitions_path,
    load_transitions,
)

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import MagicMock


def _build_spawner(
    workdir: Path,
    adapter: MagicMock,
    *,
    role_model_policy: dict[str, dict[str, Any]] | None = None,
) -> AgentSpawner:
    templates_dir = workdir / "templates" / "roles"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return AgentSpawner(
        adapter,
        templates_dir,
        workdir,
        use_worktrees=False,
        default_model="mock-model",
        role_model_policy=role_model_policy,
    )


class TestProfileTransitionOnRespawn:
    def test_respawn_under_different_profile_records_transition(
        self, tmp_path, make_task, mock_adapter_factory
    ) -> None:
        task = make_task()
        spawner_terse = _build_spawner(
            tmp_path,
            mock_adapter_factory(),
            role_model_policy={"backend": {"model": "mock-model", "response_style": "terse"}},
        )
        spawner_terse.spawn_for_tasks([task])
        assert task.metadata["response_profile"] == "terse"

        spawner_verbose = _build_spawner(
            tmp_path,
            mock_adapter_factory(),
            role_model_policy={"backend": {"model": "mock-model", "response_style": "verbose"}},
        )
        spawner_verbose.spawn_for_tasks([task])
        assert task.metadata["response_profile"] == "verbose"

        transitions = load_transitions(default_transitions_path(tmp_path / ".sdd"))
        assert len(transitions) == 1
        rec = transitions[0]
        assert rec.task_id == task.id
        assert rec.from_profile == "terse"
        assert rec.to_profile == "verbose"
        assert rec.from_sha256 != rec.to_sha256

    def test_respawn_under_same_profile_records_nothing(self, tmp_path, make_task, mock_adapter_factory) -> None:
        task = make_task()
        policy = {"backend": {"model": "mock-model", "response_style": "terse"}}
        for _ in range(2):
            spawner = _build_spawner(tmp_path, mock_adapter_factory(), role_model_policy=policy)
            spawner.spawn_for_tasks([task])
        assert load_transitions(default_transitions_path(tmp_path / ".sdd")) == []

    def test_first_spawn_records_nothing(self, tmp_path, make_task, mock_adapter_factory) -> None:
        spawner = _build_spawner(
            tmp_path,
            mock_adapter_factory(),
            role_model_policy={"backend": {"model": "mock-model", "response_style": "terse"}},
        )
        spawner.spawn_for_tasks([make_task()])
        assert load_transitions(default_transitions_path(tmp_path / ".sdd")) == []
