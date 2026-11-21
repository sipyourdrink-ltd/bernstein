"""Worktree GC reporting tests with no filesystem or worker operations."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from bernstein.cli.commands import worktrees_cmd
from bernstein.core.lifecycle.hooks import LifecycleEvent
from bernstein.core.worktrees.classifier import ClassifiedWorktree, WorktreeFingerprint, WorktreeState

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def row(tmp_path: Path) -> ClassifiedWorktree:
    return ClassifiedWorktree(
        path=tmp_path / "synthetic-worktree",
        session_id="synthetic-session",
        task_id="synthetic-task",
        state=WorktreeState.ORPHAN,
        age_seconds=3600,
        size_bytes=128,
        pid=None,
        pid_alive=False,
        last_trace_mtime=None,
    )


@pytest.fixture
def gc_calls(monkeypatch: pytest.MonkeyPatch) -> Mock:
    calls = Mock()
    monkeypatch.setattr(worktrees_cmd, "lock_gc", Mock(return_value=nullcontext()))
    monkeypatch.setattr(worktrees_cmd, "worktree_fingerprint", Mock(return_value=WorktreeFingerprint(None, None)))
    monkeypatch.setattr(worktrees_cmd, "_open_audit_log", Mock(side_effect=AssertionError("use the injected log")))
    monkeypatch.setattr(worktrees_cmd, "reap_worktree", calls.remove)
    monkeypatch.setattr(worktrees_cmd, "_shared_registry", lambda: calls.hooks)
    return calls


@pytest.mark.parametrize(
    ("dry_run", "remover_result", "count", "reaped_flag", "dry_run_flag"),
    [(False, False, 0, "0", "0"), (False, True, 1, "1", "0"), (True, True, 0, "0", "1")],
    ids=["apply-failure", "apply-success", "dry-run-simulated-success"],
)
def test_run_gc_reports_remover_outcome(
    tmp_path: Path,
    row: ClassifiedWorktree,
    gc_calls: Mock,
    dry_run: bool,
    remover_result: bool,
    count: int,
    reaped_flag: str,
    dry_run_flag: str,
) -> None:
    gc_calls.remove.return_value = remover_result

    removed_count = worktrees_cmd.run_gc(
        tmp_path, [row], dry_run=dry_run, on_progress=gc_calls.progress, audit_log=gc_calls.audit
    )

    assert removed_count == count
    assert [entry[0] for entry in gc_calls.mock_calls] == ["audit.log", "remove", "progress", "hooks.run"]
    audit = gc_calls.audit.log.call_args.kwargs
    assert audit["event_type"] == "worktree.reap"
    assert audit["details"]["reaped"] is True
    assert audit["details"]["dry_run"] is dry_run
    gc_calls.remove.assert_called_once_with(tmp_path, row, dry_run=dry_run)
    gc_calls.progress.assert_called_once_with(row, remover_result)
    event, context = gc_calls.hooks.run.call_args.args
    assert event is context.event is LifecycleEvent.POST_ARCHIVE
    assert context.env == {
        "BERNSTEIN_WORKTREE_GC_EVENT": "worktree.gc",
        "BERNSTEIN_WORKTREE_GC_STATE": "orphan",
        "BERNSTEIN_WORKTREE_GC_PATH": str(row.path),
        "BERNSTEIN_WORKTREE_GC_DRY_RUN": dry_run_flag,
        "BERNSTEIN_WORKTREE_GC_REAPED": reaped_flag,
        "BERNSTEIN_WORKTREE_GC_UNSAVED": "0",
    }


def test_run_gc_reports_unsaved_skip(tmp_path: Path, row: ClassifiedWorktree, gc_calls: Mock) -> None:
    row = replace(row, has_unsaved_work=True)

    removed_count = worktrees_cmd.run_gc(
        tmp_path, [], dry_run=False, skipped=[row], on_progress=gc_calls.progress, audit_log=gc_calls.audit
    )

    assert removed_count == 0
    assert [entry[0] for entry in gc_calls.mock_calls] == ["audit.log", "hooks.run"]
    audit = gc_calls.audit.log.call_args.kwargs
    assert audit["event_type"] == "worktree.reap"
    assert audit["details"]["reaped"] is False
    assert audit["details"]["has_unsaved_work"] is True
    event, context = gc_calls.hooks.run.call_args.args
    assert event is context.event is LifecycleEvent.POST_ARCHIVE
    assert context.env["BERNSTEIN_WORKTREE_GC_REAPED"] == "0"
    assert context.env["BERNSTEIN_WORKTREE_GC_UNSAVED"] == "1"


def test_run_gc_stops_on_append_error(tmp_path: Path, row: ClassifiedWorktree, gc_calls: Mock) -> None:
    error = OSError("synthetic append failure")
    gc_calls.audit.log.side_effect = error

    with pytest.raises(OSError) as raised:
        worktrees_cmd.run_gc(tmp_path, [row], dry_run=False, on_progress=gc_calls.progress, audit_log=gc_calls.audit)

    assert raised.value is error
    assert [entry[0] for entry in gc_calls.mock_calls] == ["audit.log"]


@pytest.mark.parametrize("flags", [(), ("--apply", "--yes", "--dry")], ids=["default", "dry-override"])
def test_gc_preview_bypasses_apply(
    tmp_path: Path, row: ClassifiedWorktree, monkeypatch: pytest.MonkeyPatch, flags: tuple[str, ...]
) -> None:
    monkeypatch.setattr(worktrees_cmd, "classify_worktrees", Mock(return_value=[row]))
    apply = Mock(side_effect=AssertionError("CLI preview reached run_gc"))
    monkeypatch.setattr(worktrees_cmd, "run_gc", apply)

    result = CliRunner().invoke(worktrees_cmd.worktrees_group, ["gc", "--workdir", str(tmp_path), *flags])

    assert result.exit_code == 0, result.output
    assert "Would remove" in result.output
    apply.assert_not_called()
