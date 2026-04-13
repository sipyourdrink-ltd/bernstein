"""Adapter conformance test suite (AGENT-011).

Parametrized tests against mock binaries verifying spawn/is_installed/
detect_tier/build_command across all adapters.

Live tests (--live flag)
------------------------
Pass ``pytest --live`` to run the ``TestLiveAdapterConformance`` suite which
spawns real adapter processes and verifies the full spawn→heartbeat→output→
shutdown lifecycle.  The mock adapter is always tested; real CLI adapters are
auto-discovered via ``shutil.which``.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.registry import _ADAPTERS, get_adapter

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


# ---------------------------------------------------------------------------
# Live adapter CLI discovery (runs at collection time, safe to call without
# --live because it only invokes shutil.which, never spawns processes).
# ---------------------------------------------------------------------------


def _installed_real_adapter_names() -> list[str]:
    """Return names of non-mock adapters whose binary is on PATH."""
    found: list[str] = []
    for name in sorted(_ADAPTERS.keys()):
        if name == "mock":
            continue
        if shutil.which(name):
            found.append(name)
    return found


_REAL_ADAPTER_NAMES = _installed_real_adapter_names()
_LIVE_ADAPTER_NAMES = ["mock", *_REAL_ADAPTER_NAMES]


# ---------------------------------------------------------------------------
# Live conformance suite — spawns real processes, requires --live flag.
# ---------------------------------------------------------------------------


class TestLiveAdapterConformance:
    """End-to-end spawn/heartbeat/output/shutdown conformance against real binaries.

    Skipped unless ``pytest --live`` is passed.  The mock adapter is always
    included; real CLI adapters are discovered via ``shutil.which``.

    Each test verifies the four observable properties described in AGENT-011:
    1. **Successful spawn** — ``SpawnResult`` with a valid PID is returned.
    2. **Heartbeat emission** — ``is_alive(pid)`` returns ``True`` after spawn.
    3. **Structured output** — log file exists and has non-empty content.
    4. **Clean shutdown** — process is no longer alive after it finishes or is
       killed.
    """

    @pytest.fixture(autouse=True)
    def _require_live(self, request: pytest.FixtureRequest) -> None:
        """Skip the entire class unless --live is supplied."""
        if not request.config.getoption("--live", default=False):
            pytest.skip("Pass --live to run live adapter conformance tests")

    # ------------------------------------------------------------------
    # Mock adapter — always available, completes naturally in ~2 s
    # ------------------------------------------------------------------

    def test_mock_spawn_succeeds(self, tmp_path: Path) -> None:
        """Mock adapter spawns a process with a valid PID and log path."""
        adapter = get_adapter("mock")
        (tmp_path / ".sdd" / "runtime").mkdir(parents=True)

        result = adapter.spawn(
            prompt="trivial task: print hello world",
            workdir=tmp_path,
            model_config=ModelConfig(model="mock", effort="low"),
            session_id="live-mock-spawn",
        )

        assert isinstance(result, SpawnResult)
        assert isinstance(result.pid, int) and result.pid > 0
        assert isinstance(result.log_path, Path)
        CLIAdapter.cancel_timeout(result)

    def test_mock_heartbeat_after_spawn(self, tmp_path: Path) -> None:
        """Mock adapter process is alive shortly after spawn (heartbeat check)."""
        adapter = get_adapter("mock")
        (tmp_path / ".sdd" / "runtime").mkdir(parents=True)

        result = adapter.spawn(
            prompt="trivial task: echo heartbeat",
            workdir=tmp_path,
            model_config=ModelConfig(model="mock", effort="low"),
            session_id="live-mock-heartbeat",
        )

        time.sleep(0.2)  # Brief pause — process must still be running
        assert adapter.is_alive(result.pid), "Process must be alive immediately after spawn"

        # Clean up — wait for natural completion
        proc = result.proc
        if hasattr(proc, "wait"):
            proc.wait(timeout=15)  # type: ignore[union-attr]
        CLIAdapter.cancel_timeout(result)

    def test_mock_structured_output(self, tmp_path: Path) -> None:
        """Mock adapter writes non-empty log output (structured output check)."""
        adapter = get_adapter("mock")
        (tmp_path / ".sdd" / "runtime").mkdir(parents=True)

        result = adapter.spawn(
            prompt="trivial task: write output",
            workdir=tmp_path,
            model_config=ModelConfig(model="mock", effort="low"),
            session_id="live-mock-output",
        )

        # Wait for the process to finish so the log is fully written
        proc = result.proc
        if hasattr(proc, "wait"):
            proc.wait(timeout=15)  # type: ignore[union-attr]

        assert result.log_path.exists(), "Log file must exist after spawn"
        content = result.log_path.read_text(encoding="utf-8")
        assert len(content) > 0, "Log file must have non-empty content"
        CLIAdapter.cancel_timeout(result)

    def test_mock_clean_shutdown(self, tmp_path: Path) -> None:
        """Mock adapter process is no longer alive after natural completion."""
        adapter = get_adapter("mock")
        (tmp_path / ".sdd" / "runtime").mkdir(parents=True)

        result = adapter.spawn(
            prompt="trivial task: then exit",
            workdir=tmp_path,
            model_config=ModelConfig(model="mock", effort="low"),
            session_id="live-mock-shutdown",
        )

        # Wait for natural completion (mock script exits after ~2 s)
        proc = result.proc
        if hasattr(proc, "wait"):
            exit_code = proc.wait(timeout=15)  # type: ignore[union-attr]
            assert exit_code == 0, f"Mock adapter must exit cleanly, got code {exit_code}"

        # Confirm the OS no longer reports the PID as alive
        assert not adapter.is_alive(result.pid), "Process must not be alive after clean exit"
        CLIAdapter.cancel_timeout(result)

    # ------------------------------------------------------------------
    # Real installed adapters — parametrized over whatever is on PATH.
    # Each test is a no-op parametrization skip when no binary is found.
    # ------------------------------------------------------------------

    @pytest.mark.parametrize("adapter_name", _REAL_ADAPTER_NAMES or ["<none>"])
    def test_real_adapter_spawn_and_kill(self, tmp_path: Path, adapter_name: str) -> None:
        """Real adapter spawns a process that is alive and can be killed cleanly."""
        if adapter_name == "<none>":
            pytest.skip("No real adapter binaries found on PATH")

        adapter = get_adapter(adapter_name)
        (tmp_path / ".sdd" / "runtime").mkdir(parents=True)

        result = adapter.spawn(
            prompt="trivial test task: print hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="test", effort="low"),
            session_id=f"live-{adapter_name}-001",
        )

        assert isinstance(result, SpawnResult), "spawn() must return SpawnResult"
        assert isinstance(result.pid, int) and result.pid > 0, "PID must be a positive integer"
        assert isinstance(result.log_path, Path), "log_path must be a Path"

        # Heartbeat: process should be alive immediately after spawn
        time.sleep(0.5)
        assert adapter.is_alive(result.pid), f"{adapter_name}: process must be alive after spawn"

        # Structured output: log file must exist
        assert result.log_path.exists(), f"{adapter_name}: log file must exist"

        # Clean shutdown via kill()
        adapter.kill(result.pid)
        time.sleep(0.5)
        assert not adapter.is_alive(result.pid), f"{adapter_name}: process must be dead after kill()"

        CLIAdapter.cancel_timeout(result)
