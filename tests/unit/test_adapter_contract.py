"""Parameterized contract tests — all adapters satisfy CLIAdapter interface."""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.aider import AiderAdapter
from bernstein.adapters.amp import AmpAdapter
from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.codex import CodexAdapter
from bernstein.adapters.cursor import CursorAdapter
from bernstein.adapters.gemini import GeminiAdapter
from bernstein.adapters.generic import GenericAdapter
from bernstein.adapters.kilo import KiloAdapter
from bernstein.adapters.roo_code import RooCodeAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.stdout = MagicMock()
    return m


def _popen_path(adapter: CLIAdapter) -> str:
    """Return the module path for patching subprocess.Popen for a given adapter."""
    mod = type(adapter).__module__
    return f"{mod}.subprocess.Popen"


# ---------------------------------------------------------------------------
# Adapter factories — each returns a concrete CLIAdapter instance
# ---------------------------------------------------------------------------

_ADAPTER_FACTORIES: list[tuple[str, Any]] = [
    ("AiderAdapter", lambda: AiderAdapter()),
    ("AmpAdapter", lambda: AmpAdapter()),
    ("CodexAdapter", lambda: CodexAdapter()),
    ("CursorAdapter", lambda: CursorAdapter()),
    ("GeminiAdapter", lambda: GeminiAdapter()),
    ("GenericAdapter", lambda: GenericAdapter(cli_command="test-cli")),
    ("KiloAdapter", lambda: KiloAdapter()),
    ("RooCodeAdapter", lambda: RooCodeAdapter()),
]


# ---------------------------------------------------------------------------
# Contract: all adapters are subclasses of CLIAdapter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,factory",
    _ADAPTER_FACTORIES,
    ids=[f[0] for f in _ADAPTER_FACTORIES],
)
class TestAdapterContract:
    """Every adapter must satisfy the CLIAdapter abstract interface."""

    def test_is_subclass_of_cli_adapter(self, name: str, factory: Any) -> None:
        adapter = factory()
        assert isinstance(adapter, CLIAdapter)

    def test_has_spawn_method(self, name: str, factory: Any) -> None:
        adapter = factory()
        assert hasattr(adapter, "spawn")
        assert callable(adapter.spawn)

    def test_has_name_method(self, name: str, factory: Any) -> None:
        adapter = factory()
        assert hasattr(adapter, "name")
        assert callable(adapter.name)

    def test_name_returns_non_empty_string(self, name: str, factory: Any) -> None:
        adapter = factory()
        result = adapter.name()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_has_is_alive_method(self, name: str, factory: Any) -> None:
        adapter = factory()
        assert hasattr(adapter, "is_alive")
        assert callable(adapter.is_alive)

    def test_has_kill_method(self, name: str, factory: Any) -> None:
        adapter = factory()
        assert hasattr(adapter, "kill")
        assert callable(adapter.kill)

    def test_has_detect_tier_method(self, name: str, factory: Any) -> None:
        adapter = factory()
        assert hasattr(adapter, "detect_tier")
        assert callable(adapter.detect_tier)

    def test_spawn_signature_matches_base(self, name: str, factory: Any) -> None:
        adapter = factory()
        sig = inspect.signature(adapter.spawn)
        params = list(sig.parameters.keys())
        assert "prompt" in params
        assert "workdir" in params
        assert "model_config" in params
        assert "session_id" in params
        assert "mcp_config" in params

    def test_spawn_returns_spawn_result(self, name: str, factory: Any, tmp_path: Path) -> None:
        adapter = factory()
        proc_mock = _make_popen_mock(pid=42)
        popen_target = _popen_path(adapter)

        # Claude adapter needs special handling (two Popen calls)
        side = [proc_mock, _make_popen_mock(pid=43)] if "claude" in popen_target else [proc_mock]

        with patch(popen_target, side_effect=side):
            result = adapter.spawn(
                prompt="test prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="contract-test",
            )
        assert isinstance(result, SpawnResult)
        assert isinstance(result.pid, int)
        assert isinstance(result.log_path, Path)

    def test_is_alive_returns_bool(self, name: str, factory: Any) -> None:
        adapter = factory()
        with patch("bernstein.adapters.base.process_alive", return_value=True):
            result = adapter.is_alive(99999)
        assert isinstance(result, bool)

    def test_kill_does_not_raise(self, name: str, factory: Any) -> None:
        adapter = factory()
        with patch("bernstein.adapters.base.kill_process_group"):
            adapter.kill(999)  # must not raise

    def test_kill_suppresses_oserror(self, name: str, factory: Any) -> None:
        adapter = factory()
        with patch("bernstein.adapters.base.kill_process_group", return_value=False):
            adapter.kill(99999)  # must not raise

    def test_detect_tier_returns_none_or_api_tier_info(self, name: str, factory: Any) -> None:
        adapter = factory()
        result = adapter.detect_tier()
        # Base implementation returns None; subclasses may return ApiTierInfo
        if result is not None:
            from bernstein.core.models import ApiTierInfo

            assert isinstance(result, ApiTierInfo)
