"""Focused tests for bootstrap entry points and startup orchestration."""

from __future__ import annotations

import subprocess
import sys
import types
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.bootstrap import bootstrap_from_goal, bootstrap_from_seed
from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.seed import SeedConfig
from bernstein.core.server_launch import BootstrapResult

from bernstein.core.orchestration.bootstrap import _post_plan_tasks, _start_watchdog


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


def _plan_task() -> Task:
    """A minimal plan-file Task for _post_plan_tasks auth tests."""
    return Task(
        id="t1",
        title="Do the thing",
        description="details",
        role="backend",
        priority=1,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
    )


def test_post_plan_tasks_authenticates_to_its_own_server() -> None:
    """Plan-file task POSTs carry the spawned server's bearer token.

    Regression for the dashboard-auth self-lockout: ``bernstein run <plan.yaml>``
    spawns an auth-enabled task server, so the CLI's own ``/tasks`` writes must
    present the token or the server 401s the CLI against its own server.
    """
    captured: dict[str, str | None] = {}

    async def _fake_post(client: Any, server_url: str, task: Any, **_kw: Any) -> str:
        del server_url, task
        captured["auth"] = client.headers.get("authorization")
        return "srv-1"

    with patch("bernstein.core.planner._post_task_to_server", _fake_post):
        _post_plan_tasks(
            [_plan_task()],
            "http://server",
            SimpleNamespace(arrow_right=">"),
            auth_token="secret-token",
        )

    assert captured["auth"] == "Bearer secret-token"


def test_post_plan_tasks_sends_no_header_when_auth_disabled() -> None:
    """With no token configured the POST stays unauthenticated (loopback default)."""
    captured: dict[str, str | None] = {}

    async def _fake_post(client: Any, server_url: str, task: Any, **_kw: Any) -> str:
        del server_url, task
        captured["auth"] = client.headers.get("authorization")
        return "srv-1"

    with patch("bernstein.core.planner._post_task_to_server", _fake_post):
        _post_plan_tasks(
            [_plan_task()],
            "http://server",
            SimpleNamespace(arrow_right=">"),
            auth_token=None,
        )

    assert captured["auth"] is None


class _FakeWatchdogProc:
    """Minimal ``subprocess.Popen`` stand-in for ``_start_watchdog`` tests."""

    def __init__(self, pid: int = 4242, *, exit_code: int | None = None) -> None:
        self.pid = pid
        self._exit_code = exit_code

    def wait(self, timeout: float | None = None) -> int:
        """Mirror ``Popen.wait``: raise ``TimeoutExpired`` while still running."""
        if self._exit_code is None:
            raise subprocess.TimeoutExpired(cmd="watchdog", timeout=timeout or 0.0)
        return self._exit_code

    def poll(self) -> int | None:
        return self._exit_code


def test_start_watchdog_launches_a_runpy_runnable_module(tmp_path: Path) -> None:
    """The watchdog launcher must target a module ``runpy`` can execute.

    Regression guard for issue #2795: the launcher spawned
    ``python -m bernstein.core.bootstrap``, but that name is a compatibility
    redirect alias whose loader returns no code object, so ``runpy`` raised
    "No code object available for bernstein.core.bootstrap" and the watchdog
    died on arrival. Run the exact ``python -m <module>`` the launcher targets
    in a fresh interpreter (the alias is unimported there, as in a real wheel
    launch) and assert it loads. ``--watchdog`` is deliberately omitted so the
    module exits instead of entering the monitor loop; the "No code object"
    failure is raised at load time, before argument parsing, so its absence is
    what the guard checks -- a future rename or redirect cannot silently rebreak
    the launch.
    """
    (tmp_path / ".sdd" / "runtime").mkdir(parents=True)
    captured: dict[str, list[str]] = {}

    def _fake_popen(argv: list[str], **_kwargs: Any) -> _FakeWatchdogProc:
        captured["argv"] = argv
        return _FakeWatchdogProc()

    with (
        patch("bernstein.core.orchestration.bootstrap.subprocess.Popen", _fake_popen),
        patch("bernstein.core.orchestration.bootstrap.console") as mock_console,
        patch("bernstein.core.orchestration.bootstrap.logger") as mock_logger,
    ):
        _start_watchdog(tmp_path, 8052)

    argv = captured["argv"]
    module_name = argv[argv.index("-m") + 1]

    result = subprocess.run(
        [sys.executable, "-m", module_name],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
        check=False,
    )
    output = result.stdout + result.stderr
    assert "No code object available" not in output, (
        f"python -m {module_name} is not runpy-runnable (#2795): {output.strip()}"
    )
    assert result.returncode == 0, f"python -m {module_name} exited {result.returncode}: {output.strip()}"

    # A healthy launch stays quiet: no operator-facing failure is surfaced.
    mock_logger.error.assert_not_called()
    mock_console.print.assert_not_called()


def test_start_watchdog_surfaces_a_dead_on_arrival_watchdog(tmp_path: Path) -> None:
    """A watchdog that exits immediately is surfaced, not swallowed.

    Regression guard for issue #2795: a broken launch left one line in
    ``watchdog.log`` and a dead pid file while the run reported itself healthy.
    The launcher must detect the immediate exit and raise the alarm at an
    operator-visible level (error log plus console), not only inside the log
    file that nobody reads.
    """
    (tmp_path / ".sdd" / "runtime").mkdir(parents=True)

    def _fake_popen(argv: list[str], *, stdout: Any = None, **_kwargs: Any) -> _FakeWatchdogProc:
        del argv
        # Model the real child: it writes its failure to the log sink and exits.
        if stdout is not None:
            stdout.write("No code object available for bernstein.core.bootstrap\n")
        return _FakeWatchdogProc(exit_code=1)

    with (
        patch("bernstein.core.orchestration.bootstrap.subprocess.Popen", _fake_popen),
        patch("bernstein.core.orchestration.bootstrap.console") as mock_console,
        patch("bernstein.core.orchestration.bootstrap.logger") as mock_logger,
    ):
        _start_watchdog(tmp_path, 8052)

    assert mock_logger.error.called, "dead-on-arrival watchdog must log an operator-visible error"
    assert mock_console.print.called, "dead-on-arrival watchdog must be surfaced to the console"
