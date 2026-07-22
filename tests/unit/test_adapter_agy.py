"""Unit tests for the Antigravity CLI (``agy``) adapter.

The ``agy`` binary is the successor CLI for the hosted backend the legacy
``gemini`` binary served on the non-enterprise path. The adapter drives it
in print mode (``-p`` prompt, non-interactive), pins the terminal sandbox
flag, auto-approves permission prompts for unattended runs, and mirrors
the watchdog bound onto ``--print-timeout``.

The gemini adapter remains registered for enterprise / API-key operators;
``agy`` is a separate registry entry so the two lanes never share discovery
state.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.agy import (
    AGY_BINARY,
    BINARY_ENV_VAR,
    AgyAdapter,
    AgyBinaryNotInstalledError,
    resolve_agy_binary,
)
from bernstein.adapters.registry import get_adapter

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("no_watchdog_threads")  # suite-wide guard, matches other adapter suites


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.wait.return_value = None
    return m


def _inner_cmd(full_cmd: list[str]) -> list[str]:
    """Extract the actual CLI command after the '--' worker separator."""
    sep = full_cmd.index("--")
    return full_cmd[sep + 1 :]


def _which_agy(name: str) -> str | None:
    return f"/usr/local/bin/{name}" if name == AGY_BINARY else None


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------


class TestResolveAgyBinary:
    """Deterministic resolution of the ``agy`` binary."""

    def test_resolves_agy_on_path(self) -> None:
        assert resolve_agy_binary(which=_which_agy, env={}) == AGY_BINARY

    def test_strict_raises_when_missing(self) -> None:
        with pytest.raises(AgyBinaryNotInstalledError):
            resolve_agy_binary(which=lambda _n: None, env={}, strict=True)

    def test_non_strict_falls_back_to_default_name(self) -> None:
        # Non-strict mode returns the default binary name so the natural
        # FileNotFoundError surfaces at Popen time (codex/aider posture).
        assert resolve_agy_binary(which=lambda _n: None, env={}) == AGY_BINARY

    def test_override_env_var_wins(self) -> None:
        env = {BINARY_ENV_VAR: "agy-nightly"}
        which = lambda name: f"/opt/{name}" if name == "agy-nightly" else None  # noqa: E731
        assert resolve_agy_binary(which=which, env=env) == "agy-nightly"

    def test_override_missing_on_path_raises_even_non_strict(self) -> None:
        env = {BINARY_ENV_VAR: "agy-nightly"}
        with pytest.raises(AgyBinaryNotInstalledError):
            resolve_agy_binary(which=lambda _n: None, env=env)


# ---------------------------------------------------------------------------
# spawn() command construction
# ---------------------------------------------------------------------------


class TestAgyAdapterSpawn:
    """spawn() builds the print-mode invocation with sandbox pinned."""

    def _spawn(
        self,
        tmp_path: Path,
        *,
        timeout_seconds: int = 0,
        model: str = "default",
    ) -> list[str]:
        adapter = AgyAdapter()
        proc_mock = _make_popen_mock(pid=310)
        with (
            patch("bernstein.adapters.agy.shutil.which", side_effect=_which_agy),
            patch("bernstein.adapters.agy.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model=model, effort="high"),
                session_id="agy-s1",
                timeout_seconds=timeout_seconds,
            )
        return list(popen.call_args.args[0])

    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        cmd = self._spawn(tmp_path)
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.orchestration.worker"]
        assert _inner_cmd(cmd)[0] == AGY_BINARY

    def test_print_mode_flags(self, tmp_path: Path) -> None:
        inner = _inner_cmd(self._spawn(tmp_path))
        assert "-p" in inner
        assert inner[inner.index("-p") + 1] == "fix the bug"
        assert "--dangerously-skip-permissions" in inner

    def test_sandbox_flag_always_pinned(self, tmp_path: Path) -> None:
        inner = _inner_cmd(self._spawn(tmp_path))
        assert "--sandbox" in inner

    def test_model_reaches_argv(self, tmp_path: Path) -> None:
        inner = _inner_cmd(self._spawn(tmp_path, model="gemini-3.1-pro"))
        assert inner[inner.index("--model") + 1] == "gemini-3.1-pro"

    def test_default_model_keeps_server_side_selection(self, tmp_path: Path) -> None:
        assert "--model" not in _inner_cmd(self._spawn(tmp_path))

    def test_timeout_mirrored_onto_print_timeout(self, tmp_path: Path) -> None:
        inner = _inner_cmd(self._spawn(tmp_path, timeout_seconds=600))
        assert "--print-timeout" in inner
        assert inner[inner.index("--print-timeout") + 1] == "600s"

    def test_zero_timeout_omits_print_timeout(self, tmp_path: Path) -> None:
        inner = _inner_cmd(self._spawn(tmp_path))
        assert "--print-timeout" not in inner

    def test_log_path_under_runtime(self, tmp_path: Path) -> None:
        adapter = AgyAdapter()
        proc_mock = _make_popen_mock(pid=311)
        with (
            patch("bernstein.adapters.agy.shutil.which", side_effect=_which_agy),
            patch("bernstein.adapters.agy.subprocess.Popen", return_value=proc_mock),
        ):
            result = adapter.spawn(
                prompt="p",
                workdir=tmp_path,
                model_config=ModelConfig(model="default", effort="low"),
                session_id="agy-s2",
                timeout_seconds=0,
            )
        assert result.pid == 311
        assert result.log_path == tmp_path / ".sdd" / "runtime" / "agy-s2.log"


# ---------------------------------------------------------------------------
# Registry + contract wiring
# ---------------------------------------------------------------------------


class TestAgyRegistryWiring:
    """The adapter is registered, strategy-declared, and contract-tracked."""

    def test_registry_resolves_agy(self) -> None:
        adapter = get_adapter("agy")
        assert isinstance(adapter, AgyAdapter)

    def test_registry_name_is_agy(self) -> None:
        assert AgyAdapter.registry_name == "agy"

    def test_name_is_nonempty(self) -> None:
        assert AgyAdapter().name()

    def test_strategy_declared(self) -> None:
        from bernstein.adapters.conformance import assert_strategies_declared

        # Must not raise: agy has a row in STRATEGY_MATRIX.
        assert_strategies_declared(["agy"])

    def test_contract_pins_print_mode_surface(self) -> None:
        from bernstein.adapters._contract import ContractSpec

        spec = ContractSpec.load("agy")
        assert spec.binary == AGY_BINARY
        assert "-p" in spec.required_flags
        assert "--sandbox" in spec.required_flags
        assert "--dangerously-skip-permissions" in spec.required_flags
        # Resume-by-conversation-id stays pinned so an upstream rename of
        # the resume surface is caught even before native resume is wired.
        assert "--conversation" in spec.required_flags

    def test_version_floor_advisory_exists(self) -> None:
        from bernstein.adapters.advisories import ADAPTER_MIN_SAFE_VERSIONS

        assert "agy" in ADAPTER_MIN_SAFE_VERSIONS

    def test_doctor_detects_agy_binary(self) -> None:
        from bernstein.cli.commands import doctor_cmd

        def fake_which(binary: str) -> str | None:
            return "/usr/local/bin/agy" if binary == "agy" else None

        with patch.object(doctor_cmd.shutil, "which", side_effect=fake_which):
            rows = doctor_cmd.check_adapters_installed()
        agy_rows = [r for r in rows if r["name"] == "Adapter: agy"]
        assert len(agy_rows) == 1
        assert agy_rows[0]["status"] == "PASS"
