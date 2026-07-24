"""Regression tests for worker-side terminal-state reporting (#2808).

Before this fix, ``WorkerLoop._complete_task`` / ``_fail_task`` had zero call
sites: a worker reaped a finished agent's PID (freeing the slot) but never told
the server, so a remotely executed task stayed CLAIMED forever regardless of
whether the in-agent completion curl fired. ``_reap_finished`` now records a
terminal outcome per reaped agent (success/failure from the exit code) and
``_report_finished`` drains those to ``/complete`` / ``/fail``.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

import pytest

from bernstein.cli.commands import worker_cmd
from bernstein.cli.commands.worker_cmd import WorkerLoop

if TYPE_CHECKING:
    from pathlib import Path


def _make_loop(tmp_path: Path) -> WorkerLoop:
    return WorkerLoop(
        server_url="http://central:8052",
        name="test-node",
        auth_token="secret-token",
        adapter="claude",
        workdir=tmp_path,
    )


class TestReapEnqueuesTerminalOutcome:
    def test_clean_exit_enqueues_completion(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = _make_loop(tmp_path)
        loop._active_tasks = {"task-1": 111}
        monkeypatch.setattr(worker_cmd.os, "waitpid", lambda pid, flags: (pid, 0))
        monkeypatch.setattr(worker_cmd.os, "waitstatus_to_exitcode", lambda status: 0)

        assert loop._reap_finished() is True
        assert loop._active_tasks == {}
        assert loop._pending_reports == [("task-1", True, "agent exited with code 0")]

    def test_nonzero_exit_enqueues_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = _make_loop(tmp_path)
        loop._active_tasks = {"task-2": 222}
        monkeypatch.setattr(worker_cmd.os, "waitpid", lambda pid, flags: (pid, 3 << 8))
        monkeypatch.setattr(worker_cmd.os, "waitstatus_to_exitcode", lambda status: 3)

        assert loop._reap_finished() is True
        assert loop._pending_reports == [("task-2", False, "agent exited with code 3")]

    def test_signal_kill_enqueues_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = _make_loop(tmp_path)
        loop._active_tasks = {"task-3": 333}
        monkeypatch.setattr(worker_cmd.os, "waitpid", lambda pid, flags: (pid, 9))
        monkeypatch.setattr(worker_cmd.os, "waitstatus_to_exitcode", lambda status: -9)

        assert loop._reap_finished() is True
        assert loop._pending_reports == [("task-3", False, "agent exited with code -9")]

    def test_still_running_agent_is_not_reaped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = _make_loop(tmp_path)
        loop._active_tasks = {"task-4": 444}
        monkeypatch.setattr(worker_cmd.os, "waitpid", lambda pid, flags: (0, 0))
        monkeypatch.setattr(
            "bernstein.core.platform_compat.process_alive",
            lambda pid: True,
        )

        assert loop._reap_finished() is False
        assert loop._active_tasks == {"task-4": 444}
        assert loop._pending_reports == []

    def test_already_reaped_child_enqueues_completion(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = _make_loop(tmp_path)
        loop._active_tasks = {"task-5": 555}

        def _raise(pid: int, flags: int) -> tuple[int, int]:
            raise ChildProcessError

        monkeypatch.setattr(worker_cmd.os, "waitpid", _raise)

        assert loop._reap_finished() is True
        assert loop._pending_reports == [("task-5", True, "worker reaped agent process (exit status unavailable)")]


class TestReportFinished:
    def test_drains_queue_to_complete_and_fail(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        loop._pending_reports = [("t-ok", True, "done"), ("t-bad", False, "boom")]
        client = mock.MagicMock()

        loop._report_finished(client)

        posted = {call.args[0]: call.kwargs.get("json") for call in client.post.call_args_list}
        assert posted["http://central:8052/tasks/t-ok/complete"] == {"result_summary": "done"}
        assert posted["http://central:8052/tasks/t-bad/fail"] == {"reason": "boom"}
        # The queue is drained so a later cycle does not double-report.
        assert loop._pending_reports == []

    def test_noop_when_queue_empty(self, tmp_path: Path) -> None:
        loop = _make_loop(tmp_path)
        client = mock.MagicMock()
        loop._report_finished(client)
        client.post.assert_not_called()

    def test_reap_then_report_drives_completion(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end: a reaped clean exit reaches the server's /complete route."""
        loop = _make_loop(tmp_path)
        loop._active_tasks = {"task-9": 999}
        monkeypatch.setattr(worker_cmd.os, "waitpid", lambda pid, flags: (pid, 0))
        monkeypatch.setattr(worker_cmd.os, "waitstatus_to_exitcode", lambda status: 0)
        client = mock.MagicMock()

        loop._reap_finished()
        loop._report_finished(client)

        (call,) = client.post.call_args_list
        assert call.args[0] == "http://central:8052/tasks/task-9/complete"
