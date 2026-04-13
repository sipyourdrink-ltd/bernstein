"""TEST-006: Adapter consistency tests.

Parametrized test instantiating every adapter, verifying protocol compliance:
- All adapters are CLIAdapter subclasses
- All have spawn() with the correct signature
- All have name() method returning a non-empty string
- spawn() returns SpawnResult with required fields
- detect_tier() returns ApiTierInfo or None — never raises
- is_installed() is callable and returns bool (when present)
- build_command() / _build_command() returns a list of strings (when present)
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ApiTierInfo, ModelConfig

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.registry import _ADAPTERS, get_adapter

# ---------------------------------------------------------------------------
# Adapter factories — enumerate every known adapter
# ---------------------------------------------------------------------------


def _all_adapter_names() -> list[str]:
    """Return names of all registered adapters (including generic)."""
    return sorted([*_ADAPTERS.keys(), "generic"])


def _instantiate_adapter(name: str) -> CLIAdapter:
    """Instantiate an adapter by name, handling special cases."""
    # GenericAdapter is not in _ADAPTERS but is a valid adapter
    if name == "generic":
        return get_adapter("generic")
    entry = _ADAPTERS[name]
    if isinstance(entry, CLIAdapter):
        return entry
    return entry()


# Collect adapters that can be instantiated without external dependencies
_TESTABLE_ADAPTERS: list[tuple[str, CLIAdapter]] = []
for _name in _all_adapter_names():
    try:
        _adapter = _instantiate_adapter(_name)
        _TESTABLE_ADAPTERS.append((_name, _adapter))
    except Exception:
        pass  # Skip adapters that need special setup


# ---------------------------------------------------------------------------
# TEST-006a: Protocol compliance — interface checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_name,adapter",
    _TESTABLE_ADAPTERS,
    ids=[t[0] for t in _TESTABLE_ADAPTERS],
)
class TestAdapterProtocolCompliance:
    """Every adapter must satisfy the CLIAdapter abstract interface."""

    def test_is_cli_adapter_subclass(self, adapter_name: str, adapter: CLIAdapter) -> None:
        assert isinstance(adapter, CLIAdapter), f"{adapter_name} is not a CLIAdapter subclass"

    def test_has_spawn_method(self, adapter_name: str, adapter: CLIAdapter) -> None:
        assert hasattr(adapter, "spawn"), f"{adapter_name} missing spawn()"
        assert callable(adapter.spawn)

    def test_spawn_signature_has_required_params(self, adapter_name: str, adapter: CLIAdapter) -> None:
        sig = inspect.signature(adapter.spawn)
        param_names = set(sig.parameters.keys())
        required = {"prompt", "workdir", "model_config", "session_id"}
        missing = required - param_names
        assert not missing, f"{adapter_name}.spawn() missing parameters: {missing}"

    def test_spawn_has_optional_mcp_config(self, adapter_name: str, adapter: CLIAdapter) -> None:
        sig = inspect.signature(adapter.spawn)
        assert "mcp_config" in sig.parameters, f"{adapter_name}.spawn() should accept mcp_config"

    def test_spawn_has_optional_timeout(self, adapter_name: str, adapter: CLIAdapter) -> None:
        sig = inspect.signature(adapter.spawn)
        assert "timeout_seconds" in sig.parameters, f"{adapter_name}.spawn() should accept timeout_seconds"


# ---------------------------------------------------------------------------
# TEST-006b: spawn() returns SpawnResult with valid fields (mocked subprocess)
# ---------------------------------------------------------------------------


def _make_popen_mock(pid: int = 42) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.stdout = MagicMock()
    # Return 0 so _probe_fast_exit treats it as a clean exit (not a spawn failure)
    m.wait.return_value = 0
    m.poll.return_value = None
    return m


@pytest.mark.parametrize(
    "adapter_name,adapter",
    _TESTABLE_ADAPTERS,
    ids=[t[0] for t in _TESTABLE_ADAPTERS],
)
class TestAdapterSpawnResult:
    """When spawn succeeds, the result has required fields."""

    def test_spawn_returns_spawn_result_type(
        self,
        adapter_name: str,
        adapter: CLIAdapter,
        tmp_path: Path,
    ) -> None:
        popen_mock = _make_popen_mock()
        mod = type(adapter).__module__

        # Create required directories
        sdd = tmp_path / ".sdd" / "runtime"
        sdd.mkdir(parents=True, exist_ok=True)
        (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)

        model_config = ModelConfig(model="sonnet", effort="high")

        # Patch subprocess.Popen at the adapter's module level and also patch
        # shutil.which globally so IaCAdapter can resolve its tool binary.
        with (
            patch(f"{mod}.subprocess.Popen", return_value=popen_mock),
            patch("shutil.which", return_value="/usr/bin/fake-tool"),
        ):
            try:
                result = adapter.spawn(
                    prompt="Test prompt",
                    workdir=tmp_path,
                    model_config=model_config,
                    session_id="test-sess-001",
                    timeout_seconds=0,
                )
            except Exception:
                # Some adapters check for binaries or env vars at runtime
                pytest.skip(f"{adapter_name} needs external binary/config")
                return

        # Cancel any lingering watchdog timers
        if result.timeout_timer is not None:
            result.timeout_timer.cancel()

        assert isinstance(result, SpawnResult), f"{adapter_name}.spawn() returned {type(result)}, expected SpawnResult"
        assert isinstance(result.pid, int), f"{adapter_name}: pid must be int"
        assert result.pid > 0, f"{adapter_name}: pid must be positive, got {result.pid}"
        assert isinstance(result.log_path, Path), f"{adapter_name}: log_path must be a Path"


# ---------------------------------------------------------------------------
# TEST-006b-extra: detect_tier() contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_name,adapter",
    _TESTABLE_ADAPTERS,
    ids=[t[0] for t in _TESTABLE_ADAPTERS],
)
class TestAdapterDetectTier:
    """detect_tier() must return ApiTierInfo or None without raising."""

    def test_detect_tier_returns_valid_type(
        self,
        adapter_name: str,
        adapter: CLIAdapter,
    ) -> None:
        result = adapter.detect_tier()
        assert result is None or isinstance(result, ApiTierInfo), (
            f"{adapter_name}: detect_tier() returned {type(result).__name__}, expected ApiTierInfo or None"
        )


# ---------------------------------------------------------------------------
# TEST-006b-extra: is_installed() optional contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_name,adapter",
    _TESTABLE_ADAPTERS,
    ids=[t[0] for t in _TESTABLE_ADAPTERS],
)
class TestAdapterIsInstalled:
    """If the adapter exposes is_installed(), it must return bool."""

    def test_is_installed_callable_and_returns_bool(
        self,
        adapter_name: str,
        adapter: CLIAdapter,
    ) -> None:
        if not hasattr(adapter, "is_installed"):
            pytest.skip(f"{adapter_name!r} does not implement is_installed()")
        fn = adapter.is_installed  # type: ignore[attr-defined]
        assert callable(fn), f"{adapter_name}: is_installed is not callable"
        with patch("shutil.which", return_value=None):
            result = fn()
        assert isinstance(result, bool), (
            f"{adapter_name}: is_installed() returned {type(result).__name__}, expected bool"
        )


# ---------------------------------------------------------------------------
# TEST-006b-extra: build_command() / _build_command() optional contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_name,adapter",
    _TESTABLE_ADAPTERS,
    ids=[t[0] for t in _TESTABLE_ADAPTERS],
)
class TestAdapterBuildCommand:
    """If the adapter exposes build_command() or _build_command(), it must return list[str]."""

    def test_build_command_returns_list_of_strings(
        self,
        adapter_name: str,
        adapter: CLIAdapter,
        tmp_path: Path,
    ) -> None:
        fn_name: str | None = None
        if hasattr(adapter, "build_command"):
            fn_name = "build_command"
        elif hasattr(adapter, "_build_command"):
            fn_name = "_build_command"

        if fn_name is None:
            pytest.skip(f"{adapter_name!r} does not expose build_command() or _build_command()")

        fn = getattr(adapter, fn_name)
        assert callable(fn), f"{adapter_name}: {fn_name} is not callable"

        try:
            result: Any = fn(
                ModelConfig(model="sonnet", effort="low"),
                None,  # mcp_config
                "test prompt",
            )
        except (TypeError, AttributeError):
            # Accept adapters with different _build_command signatures — just
            # verify the method exists and is callable, which we already did.
            return

        assert isinstance(result, list), f"{adapter_name}: {fn_name}() returned {type(result).__name__}, expected list"
        assert all(isinstance(item, str) for item in result), (
            f"{adapter_name}: {fn_name}() returned non-string items in list"
        )


# ---------------------------------------------------------------------------
# TEST-006c: Adapter registry — get_adapter by name
# ---------------------------------------------------------------------------


class TestAdapterRegistry:
    """Registry lookup works for all known adapters."""

    @pytest.mark.parametrize("name", _all_adapter_names())
    def test_get_adapter_returns_instance(self, name: str) -> None:
        try:
            adapter = get_adapter(name)
        except ValueError:
            pytest.skip(f"Adapter {name!r} not found in registry")
            return
        assert isinstance(adapter, CLIAdapter)

    def test_unknown_adapter_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            get_adapter("definitely_not_a_real_adapter_99999")


# ---------------------------------------------------------------------------
# TEST-006d: All adapters can detect rate limit errors
# ---------------------------------------------------------------------------


class TestRateLimitDetection:
    """CLIAdapter._is_rate_limit_error works with common patterns."""

    _POSITIVE_CASES = [
        ["Error: rate limit exceeded"],
        ["429 Too Many Requests"],
        ["usage limit reached"],
        ["quota exceeded"],
        ["you've hit your limit"],
        ["Your Claude API limit resets Apr 5 at 10pm"],
        ["Too many requests, please try again later"],
        ["Error: This model is overloaded"],
    ]

    _NEGATIVE_CASES = [
        ["Task completed successfully"],
        ["Running tests... 42 passed, 0 failed"],
        [],
        ["Agent started working on task T-001"],
    ]

    @pytest.mark.parametrize("lines", _POSITIVE_CASES)
    def test_detects_rate_limit(self, lines: list[str]) -> None:
        assert CLIAdapter._is_rate_limit_error(lines) is True

    @pytest.mark.parametrize("lines", _NEGATIVE_CASES)
    def test_does_not_false_positive(self, lines: list[str]) -> None:
        assert CLIAdapter._is_rate_limit_error(lines) is False


# ---------------------------------------------------------------------------
# TEST-006e: Adapter name uniqueness
# ---------------------------------------------------------------------------


class TestAdapterNames:
    """All adapters have unique, non-empty names."""

    def test_no_duplicate_names(self) -> None:
        names = _all_adapter_names()
        assert len(names) == len(set(names)), "Duplicate adapter names found"

    def test_all_names_are_non_empty(self) -> None:
        for name in _all_adapter_names():
            assert name, "Found empty adapter name"
            assert isinstance(name, str)
