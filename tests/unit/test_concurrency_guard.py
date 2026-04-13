"""Tests for the ConcurrencyGuard generation-counted guard (T802)."""

from __future__ import annotations

import asyncio

import pytest
from bernstein.core.concurrency_guard import ConcurrencyGuard, GuardState, wrap_async


class TestConcurrencyGuard:
    def test_initial_state_is_idle(self) -> None:
        guard = ConcurrencyGuard()
        assert guard.state == GuardState.IDLE
        assert guard.generation == 0

    def test_start_increments_generation(self) -> None:
        guard = ConcurrencyGuard()
        gen = guard.start()
        assert gen == 1
        assert guard.state == GuardState.RUNNING
        assert guard.generation == 1

    def test_finish_returns_to_idle(self) -> None:
        guard = ConcurrencyGuard()
        guard.start()
        guard.finish()
        assert guard.state == GuardState.IDLE

    def test_start_while_running_raises(self) -> None:
        guard = ConcurrencyGuard()
        guard.start()
        with pytest.raises(RuntimeError, match="already running"):
            guard.start()

    def test_is_stale_detects_old_generation(self) -> None:
        guard = ConcurrencyGuard()
        gen1 = guard.start()
        guard.finish()
        gen2 = guard.start()
        assert guard.is_stale(gen1) is True
        assert guard.is_stale(gen2) is False

    def test_wrap_discards_stale_callback(self, caplog) -> None:  # type: ignore[no-untyped-def]
        guard = ConcurrencyGuard()
        gen1 = guard.start()
        called: list[int] = []

        def callback(value: int) -> None:
            called.append(value)

        guarded = guard.wrap(callback, gen1)
        guard.finish()
        guard.start()  # Advance generation

        with caplog.at_level("DEBUG"):
            guarded(42)
        assert called == []  # Should have been discarded
        guard.finish()

    def test_wrap_executes_fresh_callback(self) -> None:
        guard = ConcurrencyGuard()
        gen = guard.start()
        called: list[int] = []

        def callback(value: int) -> None:
            called.append(value)

        guarded = guard.wrap(callback, gen)
        guarded(42)
        assert called == [42]
        guard.finish()


@pytest.mark.asyncio
async def test_wrap_async_discards_stale() -> None:
    guard = ConcurrencyGuard()
    gen1 = guard.start()
    executed = False

    async def callback() -> str:
        await asyncio.sleep(0)  # Async interface requirement
        nonlocal executed
        executed = True
        return "done"

    guarded = wrap_async(guard, callback, gen1)
    guard.finish()
    guard.start()  # Advance generation

    result = await guarded()
    assert result is None
    assert executed is False
    guard.finish()


@pytest.mark.asyncio
async def test_wrap_async_runs_fresh() -> None:
    guard = ConcurrencyGuard()
    gen = guard.start()

    async def callback() -> str:
        await asyncio.sleep(0)  # Async interface requirement
        return "ok"

    guarded = wrap_async(guard, callback, gen)
    result = await guarded()
    assert result == "ok"
    guard.finish()
