"""Write-boundary preflight for the manager/planning spawn (issue #2793).

A planning agent is told (by prompt) not to write files, but prompt text is not
a security boundary: an ungated CLI adapter that ignores the rule writes
straight into whatever cwd it runs in. When the manager would run directly in
the operator checkout with no OS sandbox, the spawn must fail loudly instead of
proceeding on prompt-only protection and letting a stray write land untracked
in the operator working tree. A per-session git worktree (a cwd distinct from
the operator root) or an OS sandbox both satisfy the boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.agy import AgyAdapter
from bernstein.adapters.base import SpawnError, SpawnResult
from bernstein.core.agents.spawner_core import manager_write_boundary_error


def test_manager_in_operator_tree_without_sandbox_is_refused(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    err = manager_write_boundary_error(
        role="manager",
        spawn_cwd=workdir,
        workdir=workdir,
        has_os_sandbox=False,
    )
    assert err is not None
    assert "manager" in err
    assert "worktree" in err.lower() or "sandbox" in err.lower()


def test_manager_in_worktree_is_allowed(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    worktree = tmp_path / "worktrees" / "manager-abc"
    workdir.mkdir()
    worktree.mkdir(parents=True)
    assert (
        manager_write_boundary_error(
            role="manager",
            spawn_cwd=worktree,
            workdir=workdir,
            has_os_sandbox=False,
        )
        is None
    )


def test_manager_with_os_sandbox_is_allowed(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    assert (
        manager_write_boundary_error(
            role="manager",
            spawn_cwd=workdir,
            workdir=workdir,
            has_os_sandbox=True,
        )
        is None
    )


def test_non_manager_role_is_not_gated(tmp_path: Path) -> None:
    """The preflight targets the planning role; workers are gated elsewhere."""
    workdir = tmp_path / "repo"
    workdir.mkdir()
    assert (
        manager_write_boundary_error(
            role="backend",
            spawn_cwd=workdir,
            workdir=workdir,
            has_os_sandbox=False,
        )
        is None
    )


def test_full_spawn_refuses_manager_without_boundary(tmp_path: Path, make_task, monkeypatch) -> None:
    """A manager spawn with no worktree and no sandbox fails loudly, not silently.

    ``use_worktrees=False`` leaves spawn_cwd at the operator root and no sandbox
    is configured, so there is no effective write boundary for the CLI adapter.
    """
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)

    adapter = AgyAdapter()
    # adapter.spawn must never be reached: the preflight refuses before launch.
    monkeypatch.setattr(
        adapter,
        "spawn",
        lambda **_kwargs: SpawnResult(pid=1, log_path=tmp_path / "agent.log"),
    )

    spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False, default_model="default")

    with pytest.raises(SpawnError, match="operator checkout"):
        spawner.spawn_for_tasks([make_task(role="manager")])
