"""Tests for the TUI runtime/worktree health panel."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.worktrees.classifier import ClassifiedWorktree, WorktreeState
from bernstein.tui.worktree_status import (
    RuntimeHealthPanel,
    WorktreeListPanel,
    WorktreeStatus,
    count_reapable,
    get_worktree_status,
    render_runtime_health,
    render_worktree_list,
)


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [
        (128, "", None),
        (0, "", WorktreeStatus(branch="main", is_dirty=False)),
        (0, "?? new.txt\n", WorktreeStatus(branch="main", is_dirty=True)),
    ],
    ids=["failed", "clean", "dirty"],
)
def test_failed_porcelain_query_returns_no_status(
    tmp_path: Path, returncode: int, stdout: str, expected: WorktreeStatus | None
) -> None:
    branch = subprocess.CompletedProcess(["git", "rev-parse", "--abbrev-ref", "HEAD"], 0, stdout="main\n")
    status = subprocess.CompletedProcess(["git", "status", "--porcelain"], returncode, stdout=stdout)
    with patch("bernstein.tui.worktree_status.subprocess.run", side_effect=[branch, status]):
        assert get_worktree_status(tmp_path) == expected


def test_render_runtime_health_empty() -> None:
    """Missing runtime data renders an intentional empty state."""
    text = render_runtime_health(None)
    assert "unavailable" in text.plain.lower()


def test_render_runtime_health_snapshot() -> None:
    """Runtime health text includes the high-signal runtime fields."""
    text = render_runtime_health(
        {
            "git_branch": "main",
            "active_worktrees": 3,
            "restart_count": 1,
            "memory_mb": 128.5,
            "disk_usage_mb": 42.0,
            "config_hash": "abcdef1234567890",
        }
    )
    plain = text.plain
    assert "Runtime Health" in plain
    assert "main" in plain
    assert "3 / 1" in plain
    assert "128.5 MB / 42.0 MB" in plain
    assert "abcdef123456" in plain


def test_runtime_health_panel_renders_snapshot() -> None:
    """Widget render delegates to the runtime-health formatter."""
    widget = RuntimeHealthPanel()
    widget.set_snapshot({"git_branch": "feature/runtime", "active_worktrees": 2})
    assert "feature/runtime" in widget.render().plain


def _row(state: WorktreeState, *, sid: str = "session", size: int = 0) -> ClassifiedWorktree:
    return ClassifiedWorktree(
        path=Path(f"/tmp/{sid}"),
        session_id=sid,
        task_id=None,
        state=state,
        age_seconds=0,
        size_bytes=size,
        pid=None,
        pid_alive=False,
        last_trace_mtime=None,
    )


def test_count_reapable_excludes_active() -> None:
    """Only orphan / stale / corrupt rows are counted as reapable."""
    rows = [
        _row(WorktreeState.ACTIVE, sid="a"),
        _row(WorktreeState.ORPHAN, sid="o"),
        _row(WorktreeState.STALE, sid="s"),
        _row(WorktreeState.CORRUPT, sid="c"),
    ]
    assert count_reapable(rows) == 3


def test_render_worktree_list_empty() -> None:
    """The empty pane is rendered with a clear sentinel."""
    text = render_worktree_list([])
    assert "Worktrees" in text.plain
    assert "(none)" in text.plain


def test_render_worktree_list_has_reapable_summary() -> None:
    """The pane footer surfaces the reapable count."""
    rows = [_row(WorktreeState.ORPHAN, sid="orph", size=2048)]
    text = render_worktree_list(rows)
    assert "orph" in text.plain
    assert "orphan" in text.plain
    assert "1 reapable" in text.plain


def test_render_worktree_list_clean() -> None:
    """All-active panes render the ``clean`` footer."""
    rows = [_row(WorktreeState.ACTIVE)]
    text = render_worktree_list(rows)
    assert "clean" in text.plain


def test_worktree_list_panel_set_rows() -> None:
    """The panel renders rows passed via :meth:`set_rows`."""
    panel = WorktreeListPanel()
    panel.set_rows([_row(WorktreeState.STALE, sid="dead")])
    assert "dead" in panel.render().plain
    assert panel.reapable_count() == 1


def test_worktree_list_panel_refresh_from_repo(tmp_path: Path) -> None:
    """``refresh_from_repo`` loads classifier output for the configured root.

    The orphan must be a *real* clean git worktree: the classifier probes
    git state to refuse reaping unsaved work, so an unprobeable fake ``.git``
    stub would be preserved (count 0) rather than counted as reapable.
    """
    git_id = ["-c", "user.email=test@bernstein", "-c", "user.name=test", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "seed.txt").write_text("seed")
    subprocess.run(["git", "-C", str(tmp_path), *git_id, "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), *git_id, "commit", "-q", "-m", "seed"], check=True, capture_output=True)

    base = tmp_path / ".sdd" / "runtime" / "worktrees"
    base.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            *git_id,
            "worktree",
            "add",
            "-q",
            "-b",
            "agent/stranded",
            str(base / "stranded"),
            "main",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    panel = WorktreeListPanel(repo_root=tmp_path)
    panel.refresh_from_repo()
    assert panel.reapable_count() == 1
    assert "stranded" in panel.render().plain


def test_worktree_list_panel_no_repo_root_is_noop() -> None:
    """Refresh without a configured root must not raise."""
    panel = WorktreeListPanel()
    panel.refresh_from_repo()
    assert panel.reapable_count() == 0


def test_worktree_list_panel_picks_up_legacy_layout(tmp_path: Path) -> None:
    """Older layouts under ``.sdd/worktrees`` are still classified."""
    legacy = tmp_path / ".sdd" / "worktrees" / "legacy"
    legacy.mkdir(parents=True)
    (legacy / ".git").write_text("gitdir: /fake")

    panel = WorktreeListPanel(repo_root=tmp_path)
    panel.refresh_from_repo()
    assert "legacy" in panel.render().plain
    # No need to read os here – the import is exercised by the other tests.
    assert os.path.isdir(legacy)
