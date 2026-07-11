"""Regression: default-branch guard must not be bypassed when the real trunk is
``master``, ``origin/HEAD`` is unset, and a stray local ``main`` exists.

The prior ``resolve_default_branch`` probed local candidates in the fixed order
``("main", "master")`` before consulting ``init.defaultBranch`` or the
``origin/master`` remote-tracking ref. In a clone whose real protected default
is ``master`` but whose ``origin/HEAD`` was deleted (the state of many
``git worktree`` checkouts / partial clones), a stray local ``main`` branch made
the helper resolve ``main`` while the real trunk ``master`` was checked out. The
merge guard then compared ``master != main`` and let unreviewed commits land on
the protected ``master`` trunk.

These tests build a real clone in that exact state and assert:
- ``resolve_default_branch`` resolves ``master`` (not the stray ``main``);
- ``protected_default_branches`` fails closed by protecting BOTH ``main`` and
  ``master`` when the default is genuinely ambiguous;
- the merge guard refuses a merge onto ``master`` in that state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.git_ops import (
    current_branch,
    protected_default_branches,
    resolve_default_branch,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def master_default_clone(tmp_path: Path) -> Path:
    """A clone whose real default is ``master``, with origin/HEAD deleted and a
    stray local ``main`` branch, master checked out.
    """
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "master")
    _git(upstream, "config", "user.email", "test@example.com")
    _git(upstream, "config", "user.name", "Test")
    (upstream / "README.md").write_text("# upstream\n", encoding="utf-8")
    _git(upstream, "add", "README.md")
    _git(upstream, "commit", "-q", "-m", "initial")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(upstream), str(clone))
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "Test")

    # Delete origin/HEAD so the authoritative signal is unavailable, mirroring
    # a worktree checkout / partial clone where set-head was never run.
    _git(clone, "remote", "set-head", "origin", "-d")

    # Add a stray local ``main`` that is NOT the real trunk.
    _git(clone, "branch", "main")

    # The real trunk (master) is checked out.
    assert current_branch(clone) == "master"
    return clone


def test_resolve_default_branch_prefers_master_when_origin_head_unset(
    master_default_clone: Path,
) -> None:
    # Prior behaviour resolved "main" (the stray local branch); now we resolve
    # the real trunk via the origin/master remote-tracking ref.
    assert resolve_default_branch(master_default_clone) == "master"


def test_protected_branches_fail_closed_on_ambiguous_default(
    master_default_clone: Path,
) -> None:
    # origin/HEAD unset AND both local main and master exist -> protect both.
    assert protected_default_branches(master_default_clone) == frozenset({"main", "master"})


def _session(session_id: str = "s1", task_ids: list[str] | None = None) -> Any:
    return SimpleNamespace(id=session_id, task_ids=task_ids or ["t1"], task_title="")


def test_merge_guard_refuses_master_in_ambiguous_state(
    master_default_clone: Path,
) -> None:
    """End-to-end: with master checked out in the ambiguous clone, the guard
    refuses the merge (the reproduction the fix closes)."""
    from bernstein.core.agents import spawner_merge

    merge_fn = MagicMock()
    collector = MagicMock()
    with (
        patch(
            "bernstein.core.metric_collector.get_collector",
            return_value=collector,
        ),
        patch.object(spawner_merge, "_allow_merge_to_default_branch", return_value=False),
    ):
        result = spawner_merge._run_merge_and_push(_session(), master_default_clone, merge_fn)

    merge_fn.assert_not_called()
    assert result is not None
    assert result.success is False
    assert "default branch" in (result.error or "")
