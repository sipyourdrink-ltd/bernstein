"""Unit tests for SkyvernAdapter - HTTP-based Skyvern server driver."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters._contract import STRATEGY_MATRIX, EventChannel, OutputMode
from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.computer_use import ComputerUseTerminalState
from bernstein.adapters.registry import get_adapter, selectable_adapter_names
from bernstein.adapters.skyvern import (
    SkyvernAdapter,
    SkyvernRunRefused,
    SkyvernRunTimeout,
    SkyvernServerUnreachable,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(*, status: int = 200, body: dict | None = None) -> MagicMock:
    """Return a mock urlopen response with the given status and JSON body."""
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read = MagicMock(return_value=json.dumps(body or {}).encode())
    return resp


def _spawn_kwargs(tmp_path: Path, **overrides: object) -> dict:
    """Minimal spawn kwargs the orchestrator supplies."""
    base = {
        "prompt": "Navigate to example.com and find the contact form",
        "workdir": tmp_path,
        "model_config": ModelConfig(model="sonnet", effort="high"),
        "session_id": "skyvern-test-1",
        "mcp_config": None,
        "task_scope": "medium",
        "budget_multiplier": 1.0,
        "system_addendum": "",
        "multimodal_context": None,
        "timeout_seconds": 30,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Registration + strategy
# ---------------------------------------------------------------------------


def test_adapter_registered() -> None:
    assert "skyvern" in selectable_adapter_names()
    adapter = get_adapter("skyvern")
    assert isinstance(adapter, SkyvernAdapter)
    assert isinstance(adapter, CLIAdapter)


def test_adapter_declares_artifact_output_mode() -> None:
    strategy = STRATEGY_MATRIX.get("skyvern")
    assert strategy is not None
    assert strategy.output_mode is OutputMode.ARTIFACT
    assert strategy.event_channel is EventChannel.POLL_PTY


def test_name_returns_skyvern() -> None:
    assert SkyvernAdapter().name() == "Skyvern"


# ---------------------------------------------------------------------------
# spawn() error paths
# ---------------------------------------------------------------------------


def test_spawn_raises_on_server_unreachable(tmp_path: Path) -> None:
    """URLError when posting the task raises SkyvernServerUnreachable."""
    import urllib.error

    adapter = SkyvernAdapter()

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection failed")):
        with pytest.raises(SkyvernServerUnreachable) as exc_info:
            adapter.spawn(**_spawn_kwargs(tmp_path))

    assert exc_info.value.terminal_state is ComputerUseTerminalState.DRIVER_FAILURE


def test_spawn_raises_on_connection_refused(tmp_path: Path) -> None:
    """ConnectionRefusedError when posting the task raises SkyvernServerUnreachable."""
    adapter = SkyvernAdapter()

    with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("connection refused")):
        with pytest.raises(SkyvernServerUnreachable) as exc_info:
            adapter.spawn(**_spawn_kwargs(tmp_path))

    assert exc_info.value.terminal_state is ComputerUseTerminalState.DRIVER_FAILURE


def test_spawn_raises_on_run_refused(tmp_path: Path) -> None:
    """HTTP 200 with no run_id/task_id raises SkyvernRunRefused."""
    adapter = SkyvernAdapter()

    # Skyvern returns 200 but the body has neither run_id nor task_id
    with patch("urllib.request.urlopen", return_value=_mock_response(body={"status": "queued"})):
        with pytest.raises(SkyvernRunRefused) as exc_info:
            adapter.spawn(**_spawn_kwargs(tmp_path))

    assert exc_info.value.terminal_state is ComputerUseTerminalState.REFUSED


def test_spawn_raises_on_run_refused_empty_body(tmp_path: Path) -> None:
    """HTTP 200 with an empty body raises SkyvernRunRefused."""
    adapter = SkyvernAdapter()

    with patch("urllib.request.urlopen", return_value=_mock_response(body={})):
        with pytest.raises(SkyvernRunRefused) as exc_info:
            adapter.spawn(**_spawn_kwargs(tmp_path))

    assert exc_info.value.terminal_state is ComputerUseTerminalState.REFUSED


# ---------------------------------------------------------------------------
# _watch_run() timeout path
# ---------------------------------------------------------------------------


def test_watch_run_raises_on_timeout(tmp_path: Path) -> None:
    """Poll loop that never reaches terminal status raises SkyvernRunTimeout."""
    adapter = SkyvernAdapter()

    # Spawn succeeds with a run_id, but the poll endpoint always returns
    # a non-terminal status.
    spawn_body = {"run_id": "run-abc123"}
    poll_body = {"status": "running"}  # never completes

    def urlopen_mock(req: object, **_kwargs: object) -> MagicMock:
        req_url = getattr(req, "full_url", getattr(req, "get_url", lambda: "")())
        if "/v1/run/tasks" in req_url:
            return _mock_response(body=spawn_body)
        return _mock_response(body=poll_body)

    with patch("urllib.request.urlopen", side_effect=urlopen_mock):
        with pytest.raises(SkyvernRunTimeout) as exc_info:
            adapter.spawn(**_spawn_kwargs(tmp_path, timeout_seconds=1))

    assert exc_info.value.terminal_state is ComputerUseTerminalState.TIMEOUT


# ---------------------------------------------------------------------------
# spawn() happy path
# ---------------------------------------------------------------------------


def test_spawn_returns_spawn_result(tmp_path: Path) -> None:
    """spawn() returns a SpawnResult when Skyvern accepts the task."""
    adapter = SkyvernAdapter()

    spawn_body = {"run_id": "run-xyz789", "task_id": None}
    poll_body = {"status": "completed"}

    def urlopen_mock(req: object, **_kwargs: object) -> MagicMock:
        req_url = getattr(req, "full_url", getattr(req, "get_url", lambda: "")())
        if "/v1/run/tasks" in req_url:
            return _mock_response(body=spawn_body)
        return _mock_response(body=poll_body)

    with patch("urllib.request.urlopen", side_effect=urlopen_mock):
        result = adapter.spawn(**_spawn_kwargs(tmp_path, timeout_seconds=5))

    assert isinstance(result, SpawnResult)
    assert result.pid == 0  # no subprocess - HTTP-based adapter
    assert isinstance(result.log_path, Path)


def test_spawn_returns_spawn_result_with_task_id(tmp_path: Path) -> None:
    """spawn() accepts task_id as the run identifier when run_id is absent."""
    adapter = SkyvernAdapter()

    spawn_body = {"task_id": "task-def456"}  # Skyvern uses task_id in some versions
    poll_body = {"status": "completed"}

    def urlopen_mock(req: object, **_kwargs: object) -> MagicMock:
        req_url = getattr(req, "full_url", getattr(req, "get_url", lambda: "")())
        if "/v1/run/tasks" in req_url:
            return _mock_response(body=spawn_body)
        return _mock_response(body=poll_body)

    with patch("urllib.request.urlopen", side_effect=urlopen_mock):
        result = adapter.spawn(**_spawn_kwargs(tmp_path, timeout_seconds=5))

    assert isinstance(result, SpawnResult)
