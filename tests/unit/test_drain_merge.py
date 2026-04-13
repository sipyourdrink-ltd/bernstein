"""Unit tests for drain merge-agent runner."""

from __future__ import annotations

import asyncio
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
    original_wait_for = module.asyncio.wait_for

    async def _create(*args: object, **kwargs: object) -> _Proc:
        await asyncio.sleep(0)  # Async interface requirement
        return proc

    async def _wait_for(coro: object, timeout: float) -> tuple[bytes, bytes]:
        return await coro  # type: ignore[misc]

    module.asyncio.create_subprocess_exec = _create  # type: ignore[assignment]
    module.asyncio.wait_for = _wait_for  # type: ignore[assignment]
    try:
        results = await run_merge_agent(["agent/a"], tmp_path, timeout_s=5)
    finally:
        module.asyncio.create_subprocess_exec = original_create
        module.asyncio.wait_for = original_wait_for

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
            await asyncio.sleep(0)  # Async interface requirement
            return (b"", b"")

        def kill(self) -> None:
            self.killed = True

    proc = _Proc()

    from bernstein.core import drain_merge as module

    original_create = module.asyncio.create_subprocess_exec
    original_wait_for = module.asyncio.wait_for

    async def _create(*args: object, **kwargs: object) -> _Proc:
        await asyncio.sleep(0)  # Async interface requirement
        return proc

    async def _wait_for(coro: object, timeout: float) -> tuple[bytes, bytes]:
        await asyncio.sleep(0)  # Async interface requirement
        if hasattr(coro, "close"):
            coro.close()  # type: ignore[reportUnknownMemberType]
        raise TimeoutError()

    module.asyncio.create_subprocess_exec = _create  # type: ignore[assignment]
    module.asyncio.wait_for = _wait_for  # type: ignore[assignment]
    try:
        results = await run_merge_agent(["agent/backend-a"], tmp_path, timeout_s=1)
    finally:
        module.asyncio.create_subprocess_exec = original_create
        module.asyncio.wait_for = original_wait_for

    assert results == []
    assert proc.killed is True


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
    original_wait_for = module.asyncio.wait_for

    async def _create(*args: object, **kwargs: object) -> _Proc:
        await asyncio.sleep(0)  # Async interface requirement
        return proc

    async def _wait_for(coro: object, timeout: float) -> tuple[bytes, bytes]:
        return await coro  # type: ignore[misc]

    module.asyncio.create_subprocess_exec = _create  # type: ignore[assignment]
    module.asyncio.wait_for = _wait_for  # type: ignore[assignment]
    try:
        results = await run_merge_agent(["agent/b"], tmp_path, timeout_s=5)
    finally:
        module.asyncio.create_subprocess_exec = original_create
        module.asyncio.wait_for = original_wait_for

    assert results == []
