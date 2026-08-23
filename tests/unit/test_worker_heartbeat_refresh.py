"""The worker keeps the heartbeat honest for every adapter it wraps.

``bernstein-worker`` wraps every adapter that is not ``claude`` -- qwen,
codex, opencode, cursor and the rest. For that whole population nothing
advanced ``<root>/.sdd/runtime/heartbeats/<session>.json`` past the
spawner's pre-spawn write: the refresh loop lives in a shell snippet
pasted into the agent's prompt, and a model that never runs it leaves the
file frozen. Heartbeat age then measured uptime, so the idle recycler and
the escalation ladder killed working agents on a fixed clock (issue #4330).

The worker's own attempt to cover that gap missed in three ways at once:
it aimed at ``<worktree>/.sdd/runtime/heartbeats/`` while the orchestrator
reads the project root, it dropped the ``.json`` suffix the readers require,
and ``Path.touch()`` left a zero-byte file with no timestamp to parse.

The tests below pin the fix and, just as importantly, pin its limit: the
heartbeat advances only when the runner log has grown, so an agent that has
genuinely stopped emitting still ages out and is still recycled.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from bernstein.adapters.base import HEARTBEAT_DIR_ENV, build_worker_cmd
from bernstein.core.orchestration.worker import (
    _heartbeat_refresh_loop,
    _write_heartbeat,
)


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


# ---------------------------------------------------------------------------
# Plumbing the orchestrator root down to the worker
# ---------------------------------------------------------------------------


class TestHeartbeatDirPlumbing:
    """The worker must be told where the readers actually look.

    Every adapter derives its runtime paths from the ``workdir`` it is
    spawned into, which under worktree isolation is the agent's worktree --
    not the project root the orchestrator polls. The root travels through
    the process environment, the same channel ``BERNSTEIN_RUN_ID`` already
    uses to reach agent subprocesses, so none of the ~50 adapter call sites
    have to learn about it.
    """

    def test_flag_carries_the_exported_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root_hb = tmp_path / "root" / ".sdd" / "runtime" / "heartbeats"
        monkeypatch.setenv(HEARTBEAT_DIR_ENV, str(root_hb))

        argv = build_worker_cmd(
            ["qwen"],
            role="backend",
            session_id="backend-abc",
            pid_dir=tmp_path / "wt" / "pids",
            workdir=tmp_path / "wt",
            log_path=tmp_path / "wt" / "backend-abc.log",
        )

        assert _flag(argv, "--heartbeat-dir") == str(root_hb)

    def test_no_flag_without_an_exported_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A standalone worker keeps working off ``--workdir`` as before."""
        monkeypatch.delenv(HEARTBEAT_DIR_ENV, raising=False)

        argv = build_worker_cmd(
            ["qwen"],
            role="backend",
            session_id="backend-abc",
            pid_dir=tmp_path / "pids",
            workdir=tmp_path,
            log_path=tmp_path / "backend-abc.log",
        )

        assert "--heartbeat-dir" not in argv

    def test_explicit_argument_wins_over_the_environment(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(HEARTBEAT_DIR_ENV, str(tmp_path / "from-env"))

        argv = build_worker_cmd(
            ["qwen"],
            role="backend",
            session_id="backend-abc",
            pid_dir=tmp_path / "pids",
            workdir=tmp_path,
            log_path=tmp_path / "backend-abc.log",
            heartbeat_dir=tmp_path / "explicit",
        )

        assert _flag(argv, "--heartbeat-dir") == str(tmp_path / "explicit")


# ---------------------------------------------------------------------------
# The heartbeat body
# ---------------------------------------------------------------------------


class TestWriteHeartbeat:
    def test_creates_a_body_the_readers_can_parse(self, tmp_path: Path) -> None:
        """``touch()`` left nothing to read; every consumer parses JSON."""
        path = tmp_path / "backend-abc.json"

        _write_heartbeat(path, now=1234.5)

        assert json.loads(path.read_text()) == {
            "timestamp": 1234.5,
            "status": "running",
            "phase": "running",
        }

    def test_preserves_a_richer_body_written_by_another_writer(self, tmp_path: Path) -> None:
        """Adapters that report real phases must not be flattened.

        The claude wrapper writes a phase per parsed stream-json event. The
        worker only has bytes on a pipe, so it corrects the timestamp and
        leaves every field it cannot honestly speak to alone.
        """
        path = tmp_path / "backend-abc.json"
        path.write_text(json.dumps({"timestamp": 1.0, "status": "working", "phase": "implementing", "task": "t-9"}))

        _write_heartbeat(path, now=99.0)

        assert json.loads(path.read_text()) == {
            "timestamp": 99.0,
            "status": "working",
            "phase": "implementing",
            "task": "t-9",
        }

    def test_survives_a_corrupt_body(self, tmp_path: Path) -> None:
        path = tmp_path / "backend-abc.json"
        path.write_text("{not json")

        _write_heartbeat(path, now=7.0)

        assert json.loads(path.read_text())["timestamp"] == 7.0


# ---------------------------------------------------------------------------
# What the refresh is allowed to mean
# ---------------------------------------------------------------------------


def _runs(times: int) -> Callable[[], bool]:
    state = {"n": 0}

    def _is_running() -> bool:
        state["n"] += 1
        return state["n"] <= times

    return _is_running


class TestRefreshLoop:
    def test_growing_log_advances_the_heartbeat(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("")
        hb = tmp_path / "agent.json"
        _write_heartbeat(hb, now=0.0)

        def _is_running() -> bool:
            log.write_text(log.read_text() + "tool call\n")
            return log.stat().st_size < 40

        _heartbeat_refresh_loop(hb, log, _is_running, interval_s=0.0)

        assert json.loads(hb.read_text())["timestamp"] > 0.0

    def test_silent_agent_still_ages_out(self, tmp_path: Path) -> None:
        """The point of the fix is a truthful signal, not an immortal agent.

        A worker that bumped the heartbeat on a timer would report uptime --
        exactly the defect being fixed, only harder to see. With no new bytes
        in the log the heartbeat must stay where it was so the recycler and
        the escalation ladder can still do their job.
        """
        log = tmp_path / "agent.log"
        log.write_text("frozen")
        hb = tmp_path / "agent.json"
        _write_heartbeat(hb, now=0.0)

        _heartbeat_refresh_loop(hb, log, _runs(25), interval_s=0.0)

        assert json.loads(hb.read_text())["timestamp"] == 0.0

    def test_no_log_path_means_no_refresh(self, tmp_path: Path) -> None:
        hb = tmp_path / "agent.json"
        _write_heartbeat(hb, now=0.0)

        _heartbeat_refresh_loop(hb, None, _runs(25), interval_s=0.0)

        assert json.loads(hb.read_text())["timestamp"] == 0.0

    def test_a_truncated_log_is_not_progress(self, tmp_path: Path) -> None:
        log = tmp_path / "agent.log"
        log.write_text("aaaaaaaaaa")
        hb = tmp_path / "agent.json"
        _write_heartbeat(hb, now=0.0)

        def _is_running() -> bool:
            log.write_text("a")
            return True

        _heartbeat_refresh_loop(hb, log, _runs_then(_is_running, 5), interval_s=0.0)

        assert json.loads(hb.read_text())["timestamp"] == 0.0


def _runs_then(side_effect: Callable[[], bool], times: int) -> Callable[[], bool]:
    state = {"n": 0}

    def _is_running() -> bool:
        state["n"] += 1
        side_effect()
        return state["n"] <= times

    return _is_running


# ---------------------------------------------------------------------------
# End to end through a real worker process
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group signals")
class TestWorkerProcessHeartbeat:
    def test_worker_writes_the_heartbeat_where_the_orchestrator_reads_it(self, tmp_path: Path) -> None:
        """The whole defect, end to end: right directory, right name, real body."""
        root_hb = tmp_path / "root" / ".sdd" / "runtime" / "heartbeats"
        worktree = tmp_path / "wt"
        worktree.mkdir()
        log_path = worktree / "backend-e2e.log"

        argv = build_worker_cmd(
            ["sleep", "10"],
            role="backend",
            session_id="backend-e2e",
            pid_dir=tmp_path / "pids",
            workdir=worktree,
            log_path=log_path,
            heartbeat_dir=root_hb,
        )
        proc = subprocess.Popen(argv, start_new_session=True)
        try:
            hb_file = root_hb / "backend-e2e.json"
            deadline = time.monotonic() + 15
            while not hb_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)

            assert hb_file.exists(), f"no heartbeat at {hb_file}; wrote {list(root_hb.parent.rglob('*'))}"
            body = json.loads(hb_file.read_text())
            assert isinstance(body["timestamp"], (int, float))
            assert not (worktree / ".sdd" / "runtime" / "heartbeats" / "backend-e2e").exists()
        finally:
            os.killpg(os.getpgid(proc.pid), 15)
            proc.wait(timeout=10)


def test_worker_help_documents_the_flag() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "bernstein.core.orchestration.worker", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "--heartbeat-dir" in out.stdout
