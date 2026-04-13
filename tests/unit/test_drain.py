"""Unit tests for graceful drain coordinator."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from bernstein.core.drain import DrainConfig, DrainCoordinator, DrainPhase, DrainReport


def test_drain_config_defaults() -> None:
    cfg = DrainConfig()
    assert cfg.wait_timeout_s == 120
    assert cfg.merge_timeout_s == 120
    assert cfg.auto_commit is True
    assert cfg.auto_merge is True


def test_drain_phase_lifecycle_fields() -> None:
    phase = DrainPhase(number=1, name="freeze", status="pending", detail="")
    phase.status = "running"
    phase.detail = "working"
    phase.finished_at = 1.0

    assert phase.name == "freeze"
    assert phase.status == "running"
    assert phase.detail == "working"
    assert phase.finished_at == pytest.approx(1.0)


def test_drain_report_defaults() -> None:
    report = DrainReport()
    assert report.tasks_done == 0
    assert report.tasks_partial == 0
    assert report.tasks_failed == 0
    assert report.merges == []


def test_build_phases_has_expected_order(tmp_path: Path) -> None:
    coordinator = DrainCoordinator(tmp_path)
    phases = DrainCoordinator._build_phases()  # pyright: ignore[reportPrivateUsage]

    assert [phase.name for phase in phases] == ["freeze", "signal", "wait", "commit", "merge", "cleanup"]
    assert [phase.number for phase in phases] == [1, 2, 3, 4, 5, 6]
    assert coordinator.cancellable is True


@pytest.mark.asyncio
async def test_cancel_phase_one_cleans_flags(tmp_path: Path) -> None:
    coordinator = DrainCoordinator(tmp_path)
    coordinator._current_phase = 1  # pyright: ignore[reportPrivateUsage]

    shutdown_file = tmp_path / ".sdd" / "runtime" / "signals" / "S-1" / "SHUTDOWN"
    shutdown_file.parent.mkdir(parents=True, exist_ok=True)
    shutdown_file.write_text("1", encoding="utf-8")
    draining_flag = tmp_path / ".sdd" / "runtime" / "draining"
    draining_flag.parent.mkdir(parents=True, exist_ok=True)
    draining_flag.write_text("draining", encoding="utf-8")

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def post(self, url: str) -> object:
            await asyncio.sleep(0)  # Async interface requirement
            return object()

    from bernstein.core import drain as drain_module

    original_async_client = drain_module.httpx.AsyncClient
    drain_module.httpx.AsyncClient = lambda timeout=5: _Client()  # type: ignore[assignment]
    try:
        await coordinator.cancel()
    finally:
        drain_module.httpx.AsyncClient = original_async_client

    assert draining_flag.exists() is False
    assert shutdown_file.exists() is False


@pytest.mark.asyncio
async def test_phase_freeze_falls_back_to_flag_on_http_error(tmp_path: Path) -> None:
    coordinator = DrainCoordinator(tmp_path)
    phase_freeze = coordinator._phase_freeze  # pyright: ignore[reportPrivateUsage]

    class _Response:
        def raise_for_status(self) -> None:
            raise httpx.HTTPError("offline")

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def post(self, url: str) -> _Response:
            await asyncio.sleep(0)  # Async interface requirement
            return _Response()

    from bernstein.core import drain as drain_module

    original_async_client = drain_module.httpx.AsyncClient
    drain_module.httpx.AsyncClient = lambda timeout=5: _Client()  # type: ignore[assignment]
    try:
        await phase_freeze()
    finally:
        drain_module.httpx.AsyncClient = original_async_client

    draining_flag = tmp_path / ".sdd" / "runtime" / "draining"
    assert draining_flag.exists()
    assert draining_flag.read_text(encoding="utf-8") == "draining"


@pytest.mark.asyncio
async def test_stop_infrastructure_uses_pid_files_and_supervisor_state(tmp_path: Path) -> None:
    coordinator = DrainCoordinator(tmp_path)
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "watchdog.pid").write_text("101", encoding="utf-8")
    (runtime_dir / "spawner.pid").write_text("202", encoding="utf-8")
    (runtime_dir / "supervisor_state.json").write_text(
        json.dumps(
            {
                "started_at": 1.0,
                "restart_count": 0,
                "current_pid": 303,
                "last_restart_at": None,
            }
        ),
        encoding="utf-8",
    )

    calls: list[tuple[int | None, str]] = []

    async def _record(pid: int | None, label: str) -> None:
        await asyncio.sleep(0)  # Async interface requirement
        calls.append((pid, label))

    with patch.object(coordinator, "_terminate_process", side_effect=_record):
        await coordinator._stop_infrastructure()  # pyright: ignore[reportPrivateUsage]

    assert calls == [(101, "watchdog"), (202, "spawner"), (303, "task server")]


@pytest.mark.asyncio
async def test_phase_cleanup_stops_infrastructure_and_removes_draining_flag(tmp_path: Path) -> None:
    coordinator = DrainCoordinator(tmp_path)
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    draining_flag = runtime_dir / "draining"
    draining_flag.write_text("draining", encoding="utf-8")

    with patch.object(coordinator, "_stop_infrastructure", new=AsyncMock()) as mock_stop:
        await coordinator._phase_cleanup()  # pyright: ignore[reportPrivateUsage]

    mock_stop.assert_called_once()
    assert draining_flag.exists() is False
