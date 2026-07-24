"""Write boundary for the manager/planning spawn (issue #2793).

A planning agent is told (by prompt) not to write files, but prompt text is not
a security boundary: an ungated CLI adapter that ignores the rule writes
straight into whatever path it targets. Two layers defend the operator tree:

1. Preflight refusal -- the hard stop when there is no isolation at all
   (manager in the operator checkout, no worktree, no OS sandbox).
2. Reap-time stray-write sweep -- a per-session worktree confines only the
   agent's *relative* writes; an absolute/``..`` path still escapes into the
   operator checkout. The sweep snapshots the operator tree's untracked set at
   spawn and quarantines anything new once the agent is reaped, so the operator
   ``git status`` stays clean regardless of which adapter ran.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bernstein.core.models import AgentSession, ModelConfig
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.agy import AgyAdapter
from bernstein.adapters.base import CLIAdapter, SpawnError, SpawnResult
from bernstein.core.agents.spawner_core import (
    manager_stray_writes,
    manager_write_boundary_error,
    operator_tree_untracked,
    quarantine_manager_stray_writes,
)


def _write_manager_role_config(templates_dir: Path) -> None:
    """Mirror the shipped manager role template (default_model: opus)."""
    role_dir = templates_dir / "manager"
    role_dir.mkdir(parents=True, exist_ok=True)
    (role_dir / "config.yaml").write_text("default_model: opus\ndefault_effort: max\n")


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


# --- Layer 2: reap-time stray-write sweep (the worktree escape) ------------


def test_operator_tree_untracked_lists_new_files(tmp_path: Path) -> None:
    """The snapshot reports untracked files created in the operator checkout."""
    assert "hello.txt" not in operator_tree_untracked(tmp_path)
    (tmp_path / "hello.txt").write_text("x", encoding="utf-8")
    assert "hello.txt" in operator_tree_untracked(tmp_path)


def test_manager_stray_writes_diffs_baseline_and_ignores_runtime(tmp_path: Path) -> None:
    """Only untracked paths that appeared *after* the baseline count as stray.

    Paths present at baseline (a pre-existing operator scratch file) and the
    ``.sdd`` runtime subtree are never reported.
    """
    (tmp_path / "preexisting.txt").write_text("kept", encoding="utf-8")
    baseline = operator_tree_untracked(tmp_path)

    (tmp_path / "escaped.txt").write_text("stray", encoding="utf-8")
    runtime_marker = tmp_path / ".sdd" / "runtime"
    runtime_marker.mkdir(parents=True, exist_ok=True)
    (runtime_marker / "state.json").write_text("{}", encoding="utf-8")

    stray = manager_stray_writes(tmp_path, baseline)
    assert stray == ["escaped.txt"]


def test_quarantine_moves_stray_and_cleans_operator_tree(tmp_path: Path) -> None:
    """Quarantine relocates stray writes and leaves the operator tree clean."""
    (tmp_path / "escaped.txt").write_text("stray", encoding="utf-8")
    baseline: frozenset[str] = frozenset()

    stray = manager_stray_writes(tmp_path, baseline)
    dest = quarantine_manager_stray_writes(tmp_path, "manager-abc", stray)

    assert dest is not None
    assert not (tmp_path / "escaped.txt").exists()
    assert "escaped.txt" not in operator_tree_untracked(tmp_path)
    moved = tmp_path / ".sdd" / "runtime" / "manager-stray" / "manager-abc" / "escaped.txt"
    assert moved.read_text(encoding="utf-8") == "stray"


def test_planning_spawn_out_of_scope_write_is_contained(tmp_path: Path, make_task, monkeypatch) -> None:
    """A planning agent's out-of-scope write leaves the operator tree unaffected.

    The manager runs in a per-session worktree (the default), but the adapter
    writes an absolute path into the operator checkout root that the worktree
    cwd does not confine. The write happens; the reap-time sweep then contains
    it so the operator ``git status`` is clean (acceptance #1/#2 for #2793).
    Adapter-agnostic: the sweep acts on whatever landed in the operator tree.
    """
    templates_dir = tmp_path / "templates" / "roles"
    _write_manager_role_config(templates_dir)

    stray = tmp_path / "hello.txt"
    adapter = AgyAdapter()

    def _fake_spawn(*, model_config: ModelConfig, **_kwargs) -> SpawnResult:
        # Simulate an ungated CLI adapter writing straight into the operator
        # checkout root, past its worktree cwd.
        stray.write_text("stray planning output", encoding="utf-8")
        return SpawnResult(pid=4321, log_path=tmp_path / "agent.log")

    monkeypatch.setattr(adapter, "spawn", _fake_spawn)

    spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=True, default_model="default")

    session = spawner.spawn_for_tasks([make_task(role="manager")])

    # The out-of-scope write actually happened (defect reproduced).
    assert stray.exists()
    assert "hello.txt" in operator_tree_untracked(tmp_path)

    # The reap-time sweep contains it: operator tree clean, bytes preserved.
    spawner._sweep_manager_write_boundary(session)

    assert not stray.exists()
    assert "hello.txt" not in operator_tree_untracked(tmp_path)
    quarantined = tmp_path / ".sdd" / "runtime" / "manager-stray" / session.id / "hello.txt"
    assert quarantined.read_text(encoding="utf-8") == "stray planning output"


def test_reap_invokes_write_boundary_sweep(tmp_path: Path) -> None:
    """reap_completed_agent runs the sweep so a stray write is contained on reap."""
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)

    adapter = MagicMock(spec=CLIAdapter)
    adapter.is_rate_limited.return_value = False
    spawner = AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False)

    session = AgentSession(id="manager-xyz", role="manager", model_config=ModelConfig("sonnet", "high"))
    # Baseline captured at spawn: the operator tree was clean of this write.
    spawner._manager_write_baselines[session.id] = operator_tree_untracked(tmp_path)
    spawner._procs[session.id] = MagicMock()

    # A stray write lands in the operator checkout while the agent runs.
    stray = tmp_path / "escaped.txt"
    stray.write_text("oops", encoding="utf-8")
    assert "escaped.txt" in operator_tree_untracked(tmp_path)

    spawner.reap_completed_agent(session, skip_merge=True)

    assert not stray.exists()
    assert "escaped.txt" not in operator_tree_untracked(tmp_path)
    assert (tmp_path / ".sdd" / "runtime" / "manager-stray" / session.id / "escaped.txt").exists()
