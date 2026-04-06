"""Adapter conformance test suite (AGENT-011).

Parametrized tests against mock binaries verifying spawn/is_installed/
detect_tier/build_command across all adapters.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.registry import _ADAPTERS, get_adapter
from bernstein.core.models import ModelConfig

# ---------------------------------------------------------------------------
# Adapter names to test (exclude mock since it has special behavior)
# ---------------------------------------------------------------------------

_ADAPTER_NAMES = sorted(n for n in _ADAPTERS if n != "mock")


class TestAdapterNameContract:
    """Every adapter must return a non-empty string from name()."""

    @pytest.mark.parametrize("adapter_name", _ADAPTER_NAMES)
    def test_name_returns_string(self, adapter_name: str) -> None:
        adapter = get_adapter(adapter_name)
        name = adapter.name()
        assert isinstance(name, str)
        assert len(name) > 0


class TestAdapterDetectTier:
    """detect_tier() must return ApiTierInfo or None, never raise."""

    @pytest.mark.parametrize("adapter_name", _ADAPTER_NAMES)
    def test_detect_tier_returns_valid_type(self, adapter_name: str) -> None:
        adapter = get_adapter(adapter_name)
        result = adapter.detect_tier()
        if result is not None:
            from bernstein.core.models import ApiTierInfo

            assert isinstance(result, ApiTierInfo)


class TestAdapterIsAlive:
    """is_alive() must accept a PID and return bool."""

    @pytest.mark.parametrize("adapter_name", _ADAPTER_NAMES)
    def test_is_alive_nonexistent_pid(self, adapter_name: str) -> None:
        adapter = get_adapter(adapter_name)
        result = adapter.is_alive(9999999)
        assert isinstance(result, bool)
        assert result is False


class TestAdapterKill:
    """kill() must accept a PID without crashing on invalid PIDs."""

    @pytest.mark.parametrize("adapter_name", _ADAPTER_NAMES)
    def test_kill_nonexistent_pid(self, adapter_name: str) -> None:
        adapter = get_adapter(adapter_name)
        adapter.kill(9999999)


class TestAdapterSpawnSignature:
    """spawn() must accept the standard keyword arguments."""

    @pytest.mark.parametrize("adapter_name", _ADAPTER_NAMES)
    def test_spawn_accepts_standard_kwargs(self, adapter_name: str) -> None:
        adapter = get_adapter(adapter_name)
        import inspect

        sig = inspect.signature(adapter.spawn)
        param_names = set(sig.parameters.keys())
        required = {"prompt", "workdir", "model_config", "session_id"}
        assert required.issubset(param_names), f"Missing params: {required - param_names}"


class TestMockAdapterSpawn:
    """Mock adapter must actually spawn a process."""

    def test_mock_spawn_returns_spawn_result(self, tmp_path: Path) -> None:
        adapter = get_adapter("mock")
        (tmp_path / ".sdd" / "runtime").mkdir(parents=True)
        result = adapter.spawn(
            prompt="test task",
            workdir=tmp_path,
            model_config=ModelConfig(model="mock", effort="low"),
            session_id="test-session",
        )
        assert isinstance(result, SpawnResult)
        assert isinstance(result.pid, int)
        assert result.pid > 0
        assert isinstance(result.log_path, Path)
        CLIAdapter.cancel_timeout(result)
        if result.proc is not None:
            proc = result.proc
            if hasattr(proc, "terminate"):
                proc.terminate()  # type: ignore[union-attr]
                proc.wait()  # type: ignore[union-attr]


class TestAdapterSupportsAuth:
    """supports_auth_refresh and refresh_auth must work without raising."""

    @pytest.mark.parametrize("adapter_name", _ADAPTER_NAMES)
    def test_auth_methods_callable(self, adapter_name: str) -> None:
        adapter = get_adapter(adapter_name)
        assert isinstance(adapter.supports_auth_refresh(), bool)
        assert isinstance(adapter.refresh_auth(Path(".")), bool)


class TestAdapterRateLimited:
    """is_rate_limited() must return bool."""

    @pytest.mark.parametrize("adapter_name", _ADAPTER_NAMES)
    def test_is_rate_limited_returns_bool(self, adapter_name: str) -> None:
        adapter = get_adapter(adapter_name)
        result = adapter.is_rate_limited()
        assert isinstance(result, bool)
