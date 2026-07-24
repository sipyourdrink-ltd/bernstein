"""``bernstein stop --force`` reaps surviving process *groups* (issue #2874).

Individual-PID SIGKILLs miss a grandchild a worker re-parented into its own
process group (e.g. a disowned ``while true; curl`` heartbeat loop). The hard
stop now anchors on each repo-owned group *leader* and ``os.killpg``s the whole
group, while still counting only PIDs confirmed terminated.

The tests spawn tiny real process groups in a scratch dir - no orchestrator
processes, no PID files - so the pgid-reuse guard and the whole-group kill are
exercised against real kernel process groups.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from bernstein.cli.commands import stop_cmd

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")

# A leader that forks a grandchild into its own group, prints the grandchild
# PID, then blocks. ``start_new_session=True`` makes the leader a new session +
# group leader, so ``pid == pgid``.
_LEADER_SRC = (
    "import os,sys,subprocess,time;"
    "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
    "print(c.pid, flush=True);"
    "time.sleep(120)"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _spawn_group(extra_arg: str | None = None) -> tuple[subprocess.Popen[str], int, int]:
    """Return ``(popen, leader_pid, grandchild_pid)`` for a fresh group."""
    argv = [sys.executable, "-c", _LEADER_SRC]
    if extra_arg is not None:
        argv.append(extra_arg)
    proc = subprocess.Popen(argv, start_new_session=True, stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    grandchild = int(proc.stdout.readline().strip())
    # Leader is its own group leader.
    assert os.getpgid(proc.pid) == proc.pid
    return proc, proc.pid, grandchild


def _hard_cleanup(proc: subprocess.Popen[str], leader: int, grandchild: int) -> None:
    for target in (leader, grandchild):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(target, 9)
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(target, 9)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=3)


class TestReapProcessGroup:
    def test_kills_whole_group(self) -> None:
        proc, leader, grandchild = _spawn_group()
        try:
            killed: set[int] = set()
            stop_cmd._reap_process_group(leader, killed, member_pids=(leader, grandchild))

            # The leader is a direct child of the test, so reap the zombie via
            # wait(): a negative return code confirms it was signal-killed.
            assert proc.wait(timeout=6) < 0, "group leader survived killpg"
            assert _wait_dead(grandchild), "group grandchild survived killpg"
            # Members are recorded so ``_count_reaped`` can confirm them.
            assert {leader, grandchild} <= killed
        finally:
            _hard_cleanup(proc, leader, grandchild)

    def test_guards_skip_own_and_invalid_groups(self, monkeypatch: Any) -> None:
        """Never signal pgid <= 0 or the caller's own group (reuse safety)."""
        signalled: list[int] = []
        monkeypatch.setattr(os, "killpg", lambda pgid, _sig: signalled.append(pgid))

        killed: set[int] = set()
        stop_cmd._reap_process_group(os.getpgrp(), killed, member_pids=(123,))
        stop_cmd._reap_process_group(0, killed, member_pids=(123,))
        stop_cmd._reap_process_group(-1, killed, member_pids=(123,))

        assert signalled == []
        assert killed == set()


class TestReapRepoProcessGroups:
    def test_only_repo_owned_group_is_reaped(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.chdir(tmp_path)
        worktree_prefix = str(tmp_path / ".sdd" / "worktrees")

        ours_proc, ours_leader, ours_grandchild = _spawn_group()
        other_proc, other_leader, other_grandchild = _spawn_group()
        try:
            snapshots = [
                stop_cmd._ProcessSnapshot(
                    pid=ours_leader,
                    ppid=1,
                    pgid=ours_leader,
                    command=f"{sys.executable} worker --worktree {worktree_prefix}/task-1",
                ),
                stop_cmd._ProcessSnapshot(
                    pid=ours_grandchild,
                    ppid=ours_leader,
                    pgid=ours_leader,
                    command=f"{sys.executable} -c heartbeat-loop",
                ),
                stop_cmd._ProcessSnapshot(
                    pid=other_leader,
                    ppid=1,
                    pgid=other_leader,
                    command=f"{sys.executable} -c unrelated-sleeper",
                ),
                stop_cmd._ProcessSnapshot(
                    pid=other_grandchild,
                    ppid=other_leader,
                    pgid=other_leader,
                    command=f"{sys.executable} -c unrelated-child",
                ),
            ]
            monkeypatch.setattr(stop_cmd, "_list_process_snapshots", lambda: snapshots)

            killed: set[int] = set()
            stop_cmd._reap_repo_process_groups(killed)

            # ours_leader is a direct child of the test; wait() reaps the zombie
            # and its negative return code confirms the group kill.
            assert ours_proc.wait(timeout=6) < 0, "repo-owned leader survived"
            assert _wait_dead(ours_grandchild), "repo-owned grandchild survived"
            # The unrelated group must be left alone (no pgid-reuse collateral).
            assert _alive(other_leader), "unrelated group was wrongly reaped"
            assert {ours_leader, ours_grandchild} <= killed
            assert other_leader not in killed
        finally:
            _hard_cleanup(ours_proc, ours_leader, ours_grandchild)
            _hard_cleanup(other_proc, other_leader, other_grandchild)
