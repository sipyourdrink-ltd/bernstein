"""Unit tests for drain merge-agent runner."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path

import pytest
from bernstein.core.drain_merge import run_merge_agent


@pytest.mark.asyncio
async def test_run_merge_agent_empty_branch_list_returns_empty(tmp_path: Path) -> None:
    assert await run_merge_agent([], tmp_path) == []


@pytest.mark.asyncio
async def test_run_merge_agent_parses_valid_report(tmp_path: Path) -> None:
    class _Proc:
        def __init__(self) -> None:
            self.returncode: int | None = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(0)  # Async interface requirement
            return (
                b"noise\nMERGE_REPORT_JSON:\n"
                b'[{"branch":"agent/a","action":"merged","files_changed":2,"reason":"clean"}]\n',
                b"",
            )

        def kill(self) -> None:
            return None

    proc = _Proc()
    from bernstein.core import drain_merge as module

    original_create = module.asyncio.create_subprocess_exec

    async def _create(*args: object, **kwargs: object) -> _Proc:
        await asyncio.sleep(0)  # Async interface requirement
        return proc

    module.asyncio.create_subprocess_exec = _create  # type: ignore[assignment]
    try:
        results = await run_merge_agent(["agent/a"], tmp_path, timeout_s=5)
    finally:
        module.asyncio.create_subprocess_exec = original_create

    assert len(results) == 1
    assert results[0].branch == "agent/a"
    assert results[0].action == "merged"


@pytest.mark.asyncio
async def test_run_merge_agent_timeout_returns_empty(tmp_path: Path) -> None:
    class _Proc:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.killed = False

        async def communicate(self) -> tuple[bytes, bytes]:
            # Hang long enough for the timeout to fire
            await asyncio.sleep(60)
            return (b"", b"")

        def kill(self) -> None:
            self.killed = True

    proc = _Proc()

    from bernstein.core import drain_merge as module

    original_create = module.asyncio.create_subprocess_exec

    async def _create(*args: object, **kwargs: object) -> _Proc:
        await asyncio.sleep(0)  # Async interface requirement
        return proc

    module.asyncio.create_subprocess_exec = _create  # type: ignore[assignment]
    try:
        results = await run_merge_agent(["agent/backend-a"], tmp_path, timeout_s=0.05)
    finally:
        module.asyncio.create_subprocess_exec = original_create

    assert results == []
    assert proc.killed is True


@pytest.mark.asyncio
async def test_run_merge_agent_killed_on_cancellation(tmp_path: Path) -> None:
    """Cancelling the coroutine mid-run must kill the merge-agent subprocess.

    The merge agent is a coding-agent CLI holding ``cwd`` write access to the
    repository.  If the orchestrator aborts the drain phase, the subprocess
    must not outlive the cancellation -- otherwise it keeps editing the working
    tree with nothing recording that it is still alive.  The kill therefore has
    to happen on the cancellation path, which propagates as ``CancelledError``
    and is not caught by the ``except TimeoutError`` branch.
    """
    from bernstein.core import drain_merge as module

    real_create = asyncio.create_subprocess_exec
    captured: dict[str, asyncio.subprocess.Process] = {}

    async def _create(*_args: object, **_kwargs: object) -> asyncio.subprocess.Process:
        # Ignore the hardcoded ``claude`` command; spawn a controllable,
        # long-lived child that never writes and never exits, so the parent's
        # ``communicate()`` blocks until we cancel the coroutine.
        proc = await real_create(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        captured["proc"] = proc
        return proc

    module.asyncio.create_subprocess_exec = _create  # type: ignore[assignment]
    try:
        task = asyncio.create_task(run_merge_agent(["agent/x"], tmp_path, timeout_s=30))

        # Wait until the subprocess is actually spawned and communicate() is
        # blocking on it.
        for _ in range(500):
            if "proc" in captured:
                break
            await asyncio.sleep(0.01)
        assert "proc" in captured, "merge-agent subprocess was never spawned"
        proc = captured["proc"]
        assert proc.returncode is None  # still running

        # Cancel the drain-merge coroutine while the subprocess runs.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The subprocess must be dead: reaped (returncode set) and gone from
        # the process table.  Poll briefly so a broken implementation that
        # never kills is caught (the child would still be sleeping) rather than
        # racing the reap of a correctly-killed child.
        for _ in range(200):
            if proc.returncode is not None:
                break
            await asyncio.sleep(0.01)
        assert proc.returncode is not None, "subprocess survived cancellation"
        with pytest.raises(ProcessLookupError):
            os.kill(proc.pid, 0)
    finally:
        module.asyncio.create_subprocess_exec = real_create
        # Defensive: never leak the child if the assertions above failed (this
        # runs during the revert-check when the fix is absent).
        leaked = captured.get("proc")
        if leaked is not None and leaked.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                leaked.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(leaked.wait(), timeout=5)


@pytest.mark.asyncio
async def test_run_merge_agent_nonzero_exit_returns_empty(tmp_path: Path) -> None:
    class _Proc:
        def __init__(self) -> None:
            self.returncode: int | None = 2

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(0)  # Async interface requirement
            return (b"no report", b"error")

        def kill(self) -> None:
            return None

    proc = _Proc()
    from bernstein.core import drain_merge as module

    original_create = module.asyncio.create_subprocess_exec

    async def _create(*args: object, **kwargs: object) -> _Proc:
        await asyncio.sleep(0)  # Async interface requirement
        return proc

    module.asyncio.create_subprocess_exec = _create  # type: ignore[assignment]
    try:
        results = await run_merge_agent(["agent/b"], tmp_path, timeout_s=5)
    finally:
        module.asyncio.create_subprocess_exec = original_create

    assert results == []
