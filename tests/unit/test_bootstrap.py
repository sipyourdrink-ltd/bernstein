"""Focused tests for bootstrap entry points and startup orchestration."""

from __future__ import annotations

import sys
import types
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.bootstrap import bootstrap_from_goal, bootstrap_from_seed
from bernstein.core.seed import SeedConfig
from bernstein.core.server_launch import BootstrapResult


class _CompletedFuture:
    """Small future-like object for bootstrap indexing tests."""

    def result(self, timeout: float | None = None) -> None:
        del timeout
        return None


class _Executor:
    """Small executor stub that avoids spawning real threads."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def submit(self, fn: object, *args: object, **kwargs: object) -> _CompletedFuture:
        del fn, args, kwargs
        return _CompletedFuture()

    def shutdown(self, wait: bool = False) -> None:
        del wait


def _seed(
    *,
    goal: str = "Ship the parser",
    cli: Literal["claude", "codex", "gemini", "qwen", "auto"] = "codex",
    model: str | None = "sonnet",
    max_agents: int = 3,
) -> SeedConfig:
    """Build a SeedConfig with deterministic defaults for bootstrap tests."""
    return SeedConfig(goal=goal, cli=cli, model=model, max_agents=max_agents)


def _verify_invariants(workdir: Path) -> tuple[bool, list[str]]:
    """Return a passing invariants result for bootstrap tests."""
    del workdir
    return True, []


def _write_lockfile(workdir: Path) -> None:
    """No-op lockfile writer used by bootstrap tests."""
    del workdir


def _invariants_module() -> types.ModuleType:
    """Build a fake invariants module for lazy imports in bootstrap flows."""
    module = cast(Any, types.ModuleType("bernstein.evolution.invariants"))
    module.verify_invariants = _verify_invariants
    module.write_lockfile = _write_lockfile
    return cast(types.ModuleType, module)


@pytest.fixture()
def invariants_module() -> types.ModuleType:
    """Provide the fake invariants module shared by bootstrap tests."""
    return _invariants_module()


def test_bootstrap_from_seed_returns_bootstrap_result(tmp_path: Path, invariants_module: types.ModuleType) -> None:
    """bootstrap_from_seed wires together server startup, planning, and spawner launch."""
    sync_result = SimpleNamespace(created=[], skipped=[])
    fake_console = MagicMock()

    with ExitStack() as stack:
        stack.enter_context(patch.dict(sys.modules, {"bernstein.evolution.invariants": invariants_module}))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.console", fake_console))
        stack.enter_context(
            patch("bernstein.core.orchestration.bootstrap.concurrent.futures.ThreadPoolExecutor", _Executor)
        )
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.parse_seed", return_value=_seed()))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.preflight_checks"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.ensure_sdd"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._clean_stale_runtime"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._discover_catalog"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._build_codebase_index"))
        stack.enter_context(
            patch("bernstein.core.orchestration.bootstrap._resolve_bind_host", return_value="127.0.0.1")
        )
        stack.enter_context(
            patch("bernstein.core.orchestration.bootstrap._resolve_auth_token", return_value="secret-token")
        )
        stack.enter_context(
            patch("bernstein.core.orchestration.bootstrap._resolve_server_url", return_value="http://server")
        )
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.supervised_server", return_value=111))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._wait_for_server", return_value=True))
        stack.enter_context(patch("bernstein.core.session.check_resume_session", return_value=None))
        stack.enter_context(patch("bernstein.core.sync.sync_backlog_to_server", return_value=sync_result))
        mock_inject = stack.enter_context(
            patch("bernstein.core.orchestration.bootstrap._inject_manager_task", return_value="mgr-1")
        )
        stack.enter_context(patch("bernstein.core.cost.cost.estimate_run_cost", return_value=(1.0, 2.0)))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._start_spawner", return_value=222))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._start_watchdog", return_value=333))
        result = bootstrap_from_seed(tmp_path / "bernstein.yaml", tmp_path)

    assert isinstance(result, BootstrapResult)
    assert result.server_pid == 111
    assert result.spawner_pid == 222
    assert result.manager_task_id == "mgr-1"
    mock_inject.assert_called_once()


def test_bootstrap_from_seed_skips_manager_when_backlog_tasks_exist(
    tmp_path: Path,
    invariants_module: types.ModuleType,
) -> None:
    """bootstrap_from_seed does not create a manager task when backlog sync found work."""
    sync_result = SimpleNamespace(created=["A"], skipped=["B"])

    with ExitStack() as stack:
        stack.enter_context(patch.dict(sys.modules, {"bernstein.evolution.invariants": invariants_module}))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.console", MagicMock()))
        stack.enter_context(
            patch("bernstein.core.orchestration.bootstrap.concurrent.futures.ThreadPoolExecutor", _Executor)
        )
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.parse_seed", return_value=_seed()))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.preflight_checks"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.ensure_sdd"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._clean_stale_runtime"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._discover_catalog"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._build_codebase_index"))
        stack.enter_context(
            patch("bernstein.core.orchestration.bootstrap._resolve_bind_host", return_value="127.0.0.1")
        )
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._resolve_auth_token", return_value=None))
        stack.enter_context(
            patch("bernstein.core.orchestration.bootstrap._resolve_server_url", return_value="http://server")
        )
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.supervised_server", return_value=111))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._wait_for_server", return_value=True))
        stack.enter_context(patch("bernstein.core.session.check_resume_session", return_value=None))
        stack.enter_context(patch("bernstein.core.sync.sync_backlog_to_server", return_value=sync_result))
        mock_inject = stack.enter_context(patch("bernstein.core.orchestration.bootstrap._inject_manager_task"))
        stack.enter_context(patch("bernstein.core.cost.cost.estimate_run_cost", return_value=(1.0, 2.0)))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._start_spawner", return_value=222))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._start_watchdog", return_value=333))
        result = bootstrap_from_seed(tmp_path / "bernstein.yaml", tmp_path)

    assert result.manager_task_id == ""
    mock_inject.assert_not_called()


def test_bootstrap_from_seed_exits_when_server_never_becomes_ready(
    tmp_path: Path,
    invariants_module: types.ModuleType,
) -> None:
    """bootstrap_from_seed aborts with SystemExit when the task server stays unavailable."""
    with ExitStack() as stack:
        stack.enter_context(patch.dict(sys.modules, {"bernstein.evolution.invariants": invariants_module}))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.console", MagicMock()))
        stack.enter_context(
            patch("bernstein.core.orchestration.bootstrap.concurrent.futures.ThreadPoolExecutor", _Executor)
        )
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.parse_seed", return_value=_seed()))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.preflight_checks"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.ensure_sdd"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._clean_stale_runtime"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._discover_catalog"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._build_codebase_index"))
        stack.enter_context(
            patch("bernstein.core.orchestration.bootstrap._resolve_bind_host", return_value="127.0.0.1")
        )
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._resolve_auth_token", return_value=None))
        stack.enter_context(
            patch("bernstein.core.orchestration.bootstrap._resolve_server_url", return_value="http://server")
        )
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.supervised_server", return_value=111))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._wait_for_server", return_value=False))
        with pytest.raises(SystemExit):
            bootstrap_from_seed(tmp_path / "bernstein.yaml", tmp_path)


def test_bootstrap_from_goal_autowrites_seed_on_first_run(
    tmp_path: Path,
    invariants_module: types.ModuleType,
) -> None:
    """bootstrap_from_goal auto-writes bernstein.yaml on a first run with cli=auto."""
    fake_console = MagicMock()
    discovery = SimpleNamespace(agents=[SimpleNamespace(name="codex", logged_in=True)])
    sync_result = SimpleNamespace(created=[], skipped=[])

    with ExitStack() as stack:
        stack.enter_context(patch.dict(sys.modules, {"bernstein.evolution.invariants": invariants_module}))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.console", fake_console))
        stack.enter_context(
            patch("bernstein.core.orchestration.bootstrap.concurrent.futures.ThreadPoolExecutor", _Executor)
        )
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._acquire_pid_lock"))
        stack.enter_context(patch("bernstein.core.agent_discovery.discover_agents_cached", return_value=discovery))
        stack.enter_context(patch("bernstein.core.server_launch._detect_project_type", return_value="python"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.preflight_checks"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.ensure_sdd", return_value=True))
        mock_autowrite = stack.enter_context(patch("bernstein.core.orchestration.bootstrap.auto_write_bernstein_yaml"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._clean_stale_runtime"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._discover_catalog"))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._build_codebase_index"))
        stack.enter_context(
            patch("bernstein.core.orchestration.bootstrap._resolve_bind_host", return_value="127.0.0.1")
        )
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._resolve_auth_token", return_value=None))
        stack.enter_context(
            patch("bernstein.core.orchestration.bootstrap._resolve_server_url", return_value="http://server")
        )
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap.supervised_server", return_value=111))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._wait_for_server", return_value=True))
        stack.enter_context(patch("bernstein.core.session.check_resume_session", return_value=None))
        stack.enter_context(patch("bernstein.core.sync.sync_backlog_to_server", return_value=sync_result))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._inject_manager_task", return_value="mgr-1"))
        stack.enter_context(patch("bernstein.core.cost.cost.estimate_run_cost", return_value=(1.0, 2.0)))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._start_spawner", return_value=222))
        stack.enter_context(patch("bernstein.core.orchestration.bootstrap._start_watchdog", return_value=333))
        result = bootstrap_from_goal("Ship the parser", tmp_path, cli="auto")

    assert result.manager_task_id == "mgr-1"
    mock_autowrite.assert_called_once_with(tmp_path)
