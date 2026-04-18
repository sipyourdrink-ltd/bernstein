"""Tests for audit-035: CI autofix poll wiring in the orchestrator tick.

Validates that:
- The ``ci_autofix`` config defaults to disabled (no behaviour change).
- ``_maybe_poll_ci_autofix`` is a no-op when the flag is off.
- When enabled, it invokes ``CIMonitor.poll`` with the configured repo/token.
- It rate-limits subsequent calls via ``poll_interval_s``.
- The tick pipeline calls ``_maybe_poll_ci_autofix`` during normal cadence.
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from bernstein.core.models import CIAutofixConfig, OrchestratorConfig
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.orchestration.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mock_adapter() -> MagicMock:
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=42, log_path=Path("/tmp/t.log"))
    adapter.is_alive.return_value = True
    adapter.is_rate_limited.return_value = False
    adapter.kill.return_value = None
    adapter.name.return_value = "MockCLI"
    return adapter


def _build_orch(tmp_path: Path, ci_cfg: CIAutofixConfig) -> Orchestrator:
    """Build an orchestrator with a CI autofix config and a no-op httpx client."""
    cfg = OrchestratorConfig(
        max_agents=1,
        poll_interval_s=1,
        server_url="http://testserver",
        ci_autofix=ci_cfg,
    )
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True, exist_ok=True)
    spawner = AgentSpawner(_mock_adapter(), templates_dir, tmp_path)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tasks": [], "total": 0})

    client = httpx.Client(transport=httpx.MockTransport(_handler), base_url="http://testserver")
    return Orchestrator(cfg, spawner, tmp_path, client=client)


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


class TestCIAutofixConfigDefaults:
    def test_default_disabled(self) -> None:
        cfg = CIAutofixConfig()
        assert cfg.enabled is False
        assert cfg.poll_interval_s == 60
        assert cfg.repo == ""
        assert cfg.token == ""
        assert cfg.per_page == 10

    def test_orchestrator_config_has_ci_autofix(self) -> None:
        oc = OrchestratorConfig()
        assert isinstance(oc.ci_autofix, CIAutofixConfig)
        assert oc.ci_autofix.enabled is False


# ---------------------------------------------------------------------------
# _maybe_poll_ci_autofix
# ---------------------------------------------------------------------------


class TestMaybePollCIAutofix:
    def test_flag_disabled_skips_poll(self, tmp_path: Path) -> None:
        orch = _build_orch(tmp_path, CIAutofixConfig(enabled=False, repo="o/r"))
        with patch("bernstein.core.quality.ci_monitor.CIMonitor") as monitor_cls:
            created = orch._maybe_poll_ci_autofix()
        assert created == []
        monitor_cls.assert_not_called()

    def test_empty_repo_skips_poll(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        orch = _build_orch(tmp_path, CIAutofixConfig(enabled=True, repo=""))
        with patch("bernstein.core.quality.ci_monitor.CIMonitor") as monitor_cls:
            created = orch._maybe_poll_ci_autofix()
        assert created == []
        monitor_cls.assert_not_called()

    def test_missing_token_skips_poll(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        orch = _build_orch(tmp_path, CIAutofixConfig(enabled=True, repo="o/r"))
        with patch("bernstein.core.quality.ci_monitor.CIMonitor") as monitor_cls:
            created = orch._maybe_poll_ci_autofix()
        assert created == []
        monitor_cls.assert_not_called()

    def test_enabled_calls_poll(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "tok-env")
        orch = _build_orch(
            tmp_path,
            CIAutofixConfig(enabled=True, repo="owner/repo", poll_interval_s=1),
        )

        fake_monitor = MagicMock()
        fake_monitor.poll.return_value = ["task-123"]
        fake_pipeline = MagicMock()

        with (
            patch(
                "bernstein.core.quality.ci_monitor.CIMonitor",
                return_value=fake_monitor,
            ),
            patch(
                "bernstein.core.quality.ci_fix.CIAutofixPipeline",
                return_value=fake_pipeline,
            ),
        ):
            created = orch._maybe_poll_ci_autofix()

        assert created == ["task-123"]
        fake_monitor.poll.assert_called_once()
        call_args = fake_monitor.poll.call_args
        assert call_args.args[0] == "owner/repo"
        assert call_args.args[1] == "tok-env"
        assert call_args.args[2] is fake_pipeline
        assert call_args.kwargs["per_page"] == 10

    def test_rate_limited_between_calls(self, tmp_path: Path) -> None:
        orch = _build_orch(
            tmp_path,
            CIAutofixConfig(
                enabled=True,
                repo="owner/repo",
                token="tok",
                poll_interval_s=60,
            ),
        )

        fake_monitor = MagicMock()
        fake_monitor.poll.return_value = []
        fake_pipeline = MagicMock()

        with (
            patch(
                "bernstein.core.quality.ci_monitor.CIMonitor",
                return_value=fake_monitor,
            ),
            patch(
                "bernstein.core.quality.ci_fix.CIAutofixPipeline",
                return_value=fake_pipeline,
            ),
        ):
            orch._maybe_poll_ci_autofix()
            orch._maybe_poll_ci_autofix()  # second call within interval

        assert fake_monitor.poll.call_count == 1

    def test_respects_interval_after_time_advance(self, tmp_path: Path) -> None:
        orch = _build_orch(
            tmp_path,
            CIAutofixConfig(
                enabled=True,
                repo="owner/repo",
                token="tok",
                poll_interval_s=60,
            ),
        )

        fake_monitor = MagicMock()
        fake_monitor.poll.return_value = []
        fake_pipeline = MagicMock()

        with (
            patch(
                "bernstein.core.quality.ci_monitor.CIMonitor",
                return_value=fake_monitor,
            ),
            patch(
                "bernstein.core.quality.ci_fix.CIAutofixPipeline",
                return_value=fake_pipeline,
            ),
        ):
            orch._maybe_poll_ci_autofix()
            # Fake the clock: rewind last poll to beyond the interval.
            orch._last_ci_poll_ts = time.time() - 120
            orch._maybe_poll_ci_autofix()

        assert fake_monitor.poll.call_count == 2

    def test_poll_exception_logged_not_raised(self, tmp_path: Path) -> None:
        orch = _build_orch(
            tmp_path,
            CIAutofixConfig(
                enabled=True,
                repo="owner/repo",
                token="tok",
                poll_interval_s=1,
            ),
        )

        fake_monitor = MagicMock()
        fake_monitor.poll.side_effect = RuntimeError("network down")
        fake_pipeline = MagicMock()

        with (
            patch(
                "bernstein.core.quality.ci_monitor.CIMonitor",
                return_value=fake_monitor,
            ),
            patch(
                "bernstein.core.quality.ci_fix.CIAutofixPipeline",
                return_value=fake_pipeline,
            ),
        ):
            created = orch._maybe_poll_ci_autofix()

        assert created == []


# ---------------------------------------------------------------------------
# CIMonitor.poll synchronous wrapper
# ---------------------------------------------------------------------------


class TestCIMonitorPollWrapper:
    def test_poll_creates_fix_task_for_each_failure(self) -> None:
        from bernstein.core.quality.ci_monitor import CIFailure, CIMonitor, FailureContext

        monitor = CIMonitor()
        pipeline = MagicMock()
        pipeline.create_fix_task.side_effect = ["task-1", "task-2"]

        failures = [
            CIFailure(
                run_id=1,
                workflow_name="CI",
                branch="main",
                commit_sha="a",
                failure_url="https://github.com/o/r/actions/runs/1",
                timestamp="",
            ),
            CIFailure(
                run_id=2,
                workflow_name="CI",
                branch="main",
                commit_sha="b",
                failure_url="https://github.com/o/r/actions/runs/2",
                timestamp="",
            ),
        ]

        async def _poll_failures(*_args: Any, **_kwargs: Any) -> list[CIFailure]:
            return failures

        async def _parse_logs(*_args: Any, **_kwargs: Any) -> FailureContext:
            return FailureContext(test_name="", error_message="boom")

        with (
            patch.object(CIMonitor, "poll_failures", _poll_failures),
            patch.object(CIMonitor, "parse_failure_logs", _parse_logs),
        ):
            created = monitor.poll("o/r", "tok", pipeline)

        assert created == ["task-1", "task-2"]
        assert pipeline.create_fix_task.call_count == 2

    def test_poll_no_repo_returns_empty(self) -> None:
        from bernstein.core.quality.ci_monitor import CIMonitor

        monitor = CIMonitor()
        pipeline = MagicMock()
        assert monitor.poll("", "tok", pipeline) == []
        assert monitor.poll("o/r", "", pipeline) == []
        pipeline.create_fix_task.assert_not_called()

    def test_poll_handles_parse_error_per_run(self) -> None:
        from bernstein.core.quality.ci_monitor import CIFailure, CIMonitor, FailureContext

        monitor = CIMonitor()
        pipeline = MagicMock()
        pipeline.create_fix_task.return_value = "task-ok"

        good = CIFailure(
            run_id=1,
            workflow_name="CI",
            branch="main",
            commit_sha="a",
            failure_url="u1",
            timestamp="",
        )
        bad = CIFailure(
            run_id=2,
            workflow_name="CI",
            branch="main",
            commit_sha="b",
            failure_url="u2",
            timestamp="",
        )

        async def _poll_failures(*_args: Any, **_kwargs: Any) -> list[CIFailure]:
            return [good, bad]

        async def _parse_logs(_self: CIMonitor, _repo: str, run_id: int, _token: str) -> FailureContext:
            if run_id == 2:
                raise RuntimeError("bad log")
            return FailureContext(test_name="", error_message="boom")

        with (
            patch.object(CIMonitor, "poll_failures", _poll_failures),
            patch.object(CIMonitor, "parse_failure_logs", _parse_logs),
        ):
            created = monitor.poll("o/r", "tok", pipeline)

        assert created == ["task-ok"]
        assert pipeline.create_fix_task.call_count == 1


# ---------------------------------------------------------------------------
# Tick integration
# ---------------------------------------------------------------------------


class TestTickWiring:
    def test_tick_body_references_maybe_poll(self) -> None:
        """The _tick_internal source must call _maybe_poll_ci_autofix."""
        src = inspect.getsource(Orchestrator._tick_internal)
        assert "_maybe_poll_ci_autofix" in src, "Orchestrator._tick_internal does not wire up the CI autofix poll"

    def test_tick_invokes_poll_when_enabled(self, tmp_path: Path) -> None:
        """A single tick with the flag enabled calls _maybe_poll_ci_autofix."""
        orch = _build_orch(
            tmp_path,
            CIAutofixConfig(enabled=True, repo="o/r", token="tok", poll_interval_s=1),
        )

        with patch.object(Orchestrator, "_maybe_poll_ci_autofix", return_value=["task-abc"]) as mock_poll:
            orch.tick()

        assert mock_poll.called
