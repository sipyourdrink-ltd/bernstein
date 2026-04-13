"""Tests for orchestrator tick lifecycle plugin extension points."""

from __future__ import annotations

import time

import pytest
from bernstein.core.tick_hooks import (
    TickContext,
    TickHookManager,
    TickHookResult,
)

from bernstein.plugins import hookimpl

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(
    tick_number: int = 1,
    active_agents: int = 2,
    pending_tasks: int = 5,
    completed_tasks: int = 3,
    cost_usd: float = 0.42,
) -> TickContext:
    """Build a TickContext with sensible defaults."""
    return TickContext(
        tick_number=tick_number,
        timestamp=time.time(),
        active_agents=active_agents,
        pending_tasks=pending_tasks,
        completed_tasks=completed_tasks,
        cost_usd=cost_usd,
    )


# ---------------------------------------------------------------------------
# TickContext tests
# ---------------------------------------------------------------------------


class TestTickContext:
    """TickContext dataclass behaviour."""

    def test_frozen(self) -> None:
        ctx = _make_context()
        with pytest.raises(AttributeError):
            ctx.tick_number = 99  # type: ignore[misc]

    def test_fields_accessible(self) -> None:
        ctx = _make_context(
            tick_number=7,
            active_agents=4,
            pending_tasks=10,
            completed_tasks=8,
            cost_usd=1.23,
        )
        assert ctx.tick_number == 7
        assert ctx.active_agents == 4
        assert ctx.pending_tasks == 10
        assert ctx.completed_tasks == 8
        assert ctx.cost_usd == pytest.approx(1.23)

    def test_equality(self) -> None:
        ts = time.time()
        a = TickContext(1, ts, 2, 3, 4, 0.5)
        b = TickContext(1, ts, 2, 3, 4, 0.5)
        assert a == b

    def test_hashable(self) -> None:
        ctx = _make_context()
        # frozen + slots dataclasses are hashable
        assert isinstance(hash(ctx), int)


# ---------------------------------------------------------------------------
# TickHookResult tests
# ---------------------------------------------------------------------------


class TestTickHookResult:
    """TickHookResult dataclass behaviour."""

    def test_frozen(self) -> None:
        result = TickHookResult(
            hook_name="my_plugin",
            phase="pre_tick",
            success=True,
            duration_ms=1.5,
            message="ok",
        )
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore[misc]

    def test_fields(self) -> None:
        result = TickHookResult(
            hook_name="p",
            phase="post_spawn",
            success=False,
            duration_ms=42.0,
            message="boom",
        )
        assert result.hook_name == "p"
        assert result.phase == "post_spawn"
        assert result.success is False
        assert result.duration_ms == pytest.approx(42.0)
        assert result.message == "boom"


# ---------------------------------------------------------------------------
# Plugin fixtures
# ---------------------------------------------------------------------------


class PreTickPlugin:
    """Plugin that implements pre_tick."""

    @hookimpl
    def pre_tick(self, context: TickContext) -> TickHookResult:
        return TickHookResult(
            hook_name="pre_tick_plugin",
            phase="pre_tick",
            success=True,
            duration_ms=0.1,
            message=f"tick {context.tick_number}",
        )


class PostTickPlugin:
    """Plugin that implements post_tick."""

    @hookimpl
    def post_tick(self, context: TickContext) -> TickHookResult:
        return TickHookResult(
            hook_name="post_tick_plugin",
            phase="post_tick",
            success=True,
            duration_ms=0.2,
            message="done",
        )


class PreSpawnPlugin:
    """Plugin that implements pre_spawn."""

    @hookimpl
    def pre_spawn(self, context: TickContext, task_id: str, role: str) -> TickHookResult:
        return TickHookResult(
            hook_name="pre_spawn_plugin",
            phase="pre_spawn",
            success=True,
            duration_ms=0.3,
            message=f"spawning {role} for {task_id}",
        )


class PostSpawnPlugin:
    """Plugin that implements post_spawn."""

    @hookimpl
    def post_spawn(self, context: TickContext, task_id: str, agent_id: str) -> TickHookResult:
        return TickHookResult(
            hook_name="post_spawn_plugin",
            phase="post_spawn",
            success=True,
            duration_ms=0.4,
            message=f"spawned {agent_id} for {task_id}",
        )


class MultiHookPlugin:
    """Plugin implementing multiple hooks."""

    @hookimpl
    def pre_tick(self, context: TickContext) -> TickHookResult:
        return TickHookResult(
            hook_name="multi",
            phase="pre_tick",
            success=True,
            duration_ms=0.0,
            message="multi-pre",
        )

    @hookimpl
    def post_tick(self, context: TickContext) -> TickHookResult:
        return TickHookResult(
            hook_name="multi",
            phase="post_tick",
            success=True,
            duration_ms=0.0,
            message="multi-post",
        )


class NoneReturningPlugin:
    """Plugin that returns None from hooks."""

    @hookimpl
    def pre_tick(self, context: TickContext) -> None:
        return None


class ExplodingPlugin:
    """Plugin that raises in its hook."""

    @hookimpl
    def pre_tick(self, context: TickContext) -> TickHookResult:
        msg = "kaboom"
        raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# TickHookManager tests
# ---------------------------------------------------------------------------


class TestTickHookManagerRegister:
    """Registration and basic wiring."""

    def test_register_single_plugin(self) -> None:
        mgr = TickHookManager()
        mgr.register(PreTickPlugin())
        results = mgr.run_pre_tick(_make_context(tick_number=5))
        assert len(results) == 1
        assert results[0].hook_name == "pre_tick_plugin"
        assert results[0].phase == "pre_tick"
        assert results[0].success is True
        assert "5" in results[0].message

    def test_register_multiple_plugins(self) -> None:
        mgr = TickHookManager()
        mgr.register(PreTickPlugin())
        mgr.register(MultiHookPlugin())
        results = mgr.run_pre_tick(_make_context())
        assert len(results) == 2
        names = {r.hook_name for r in results}
        assert names == {"pre_tick_plugin", "multi"}


class TestRunPreTick:
    """run_pre_tick behaviour."""

    def test_returns_results(self) -> None:
        mgr = TickHookManager()
        mgr.register(PreTickPlugin())
        results = mgr.run_pre_tick(_make_context())
        assert len(results) == 1
        assert results[0].success is True

    def test_empty_when_no_plugins(self) -> None:
        mgr = TickHookManager()
        results = mgr.run_pre_tick(_make_context())
        assert results == []

    def test_none_results_filtered_out(self) -> None:
        mgr = TickHookManager()
        mgr.register(NoneReturningPlugin())
        results = mgr.run_pre_tick(_make_context())
        assert results == []


class TestRunPostTick:
    """run_post_tick behaviour."""

    def test_returns_results(self) -> None:
        mgr = TickHookManager()
        mgr.register(PostTickPlugin())
        results = mgr.run_post_tick(_make_context())
        assert len(results) == 1
        assert results[0].hook_name == "post_tick_plugin"

    def test_empty_when_no_plugins(self) -> None:
        mgr = TickHookManager()
        assert mgr.run_post_tick(_make_context()) == []


class TestRunPreSpawn:
    """run_pre_spawn behaviour."""

    def test_returns_results(self) -> None:
        mgr = TickHookManager()
        mgr.register(PreSpawnPlugin())
        ctx = _make_context()
        results = mgr.run_pre_spawn(ctx, task_id="t-1", role="backend")
        assert len(results) == 1
        assert "backend" in results[0].message
        assert "t-1" in results[0].message

    def test_empty_when_no_plugins(self) -> None:
        mgr = TickHookManager()
        assert mgr.run_pre_spawn(_make_context(), task_id="t-1", role="qa") == []


class TestRunPostSpawn:
    """run_post_spawn behaviour."""

    def test_returns_results(self) -> None:
        mgr = TickHookManager()
        mgr.register(PostSpawnPlugin())
        ctx = _make_context()
        results = mgr.run_post_spawn(ctx, task_id="t-2", agent_id="a-99")
        assert len(results) == 1
        assert "a-99" in results[0].message
        assert "t-2" in results[0].message

    def test_empty_when_no_plugins(self) -> None:
        mgr = TickHookManager()
        assert mgr.run_post_spawn(_make_context(), task_id="t-1", agent_id="a-1") == []


class TestErrorHandling:
    """Hooks that raise should not crash the manager."""

    def test_exploding_plugin_returns_failure_result(self) -> None:
        mgr = TickHookManager()
        mgr.register(ExplodingPlugin())
        results = mgr.run_pre_tick(_make_context())
        # pluggy itself may propagate the error; our _run_hooks catches it
        assert len(results) == 1
        assert results[0].success is False
        assert results[0].phase == "pre_tick"

    def test_good_plugin_not_affected_by_bad_plugin_in_different_phase(self) -> None:
        mgr = TickHookManager()
        mgr.register(ExplodingPlugin())  # only implements pre_tick
        mgr.register(PostTickPlugin())
        # post_tick should work fine
        results = mgr.run_post_tick(_make_context())
        assert len(results) == 1
        assert results[0].success is True


class TestMultiHookPlugin:
    """Plugin implementing multiple hook phases."""

    def test_pre_and_post_tick(self) -> None:
        mgr = TickHookManager()
        mgr.register(MultiHookPlugin())
        ctx = _make_context()
        pre = mgr.run_pre_tick(ctx)
        post = mgr.run_post_tick(ctx)
        assert len(pre) == 1
        assert len(post) == 1
        assert pre[0].message == "multi-pre"
        assert post[0].message == "multi-post"
