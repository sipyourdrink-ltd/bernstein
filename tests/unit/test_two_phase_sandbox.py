"""Unit tests for two-phase sandboxed execution (Codex-style)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from bernstein.core.container import (
    ContainerConfig,
    ContainerManager,
    NetworkMode,
    TwoPhaseSandboxConfig,
    _detect_setup_commands,
)
from bernstein.core.models import ContainerIsolationConfig

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# _detect_setup_commands
# ---------------------------------------------------------------------------


class TestDetectSetupCommands:
    def test_detects_uv_lock(self, tmp_path: Path) -> None:
        (tmp_path / "uv.lock").touch()
        cmds = _detect_setup_commands(tmp_path)
        assert cmds == ["uv sync --frozen"]

    def test_detects_requirements_txt(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").touch()
        cmds = _detect_setup_commands(tmp_path)
        assert cmds == ["pip install -r requirements.txt"]

    def test_detects_yarn_lock(self, tmp_path: Path) -> None:
        (tmp_path / "yarn.lock").touch()
        cmds = _detect_setup_commands(tmp_path)
        assert cmds == ["yarn install --frozen-lockfile"]

    def test_detects_package_lock_json(self, tmp_path: Path) -> None:
        (tmp_path / "package-lock.json").touch()
        cmds = _detect_setup_commands(tmp_path)
        assert cmds == ["npm ci"]

    def test_detects_package_json_fallback(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").touch()
        cmds = _detect_setup_commands(tmp_path)
        assert cmds == ["npm install"]

    def test_detects_gemfile_lock(self, tmp_path: Path) -> None:
        (tmp_path / "Gemfile.lock").touch()
        cmds = _detect_setup_commands(tmp_path)
        assert cmds == ["bundle install"]

    def test_detects_go_sum(self, tmp_path: Path) -> None:
        (tmp_path / "go.sum").touch()
        cmds = _detect_setup_commands(tmp_path)
        assert cmds == ["go mod download"]

    def test_detects_cargo_lock(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.lock").touch()
        cmds = _detect_setup_commands(tmp_path)
        assert cmds == ["cargo fetch"]

    def test_returns_empty_when_no_manifest(self, tmp_path: Path) -> None:
        cmds = _detect_setup_commands(tmp_path)
        assert cmds == []

    def test_uv_lock_takes_priority_over_requirements(self, tmp_path: Path) -> None:
        # uv.lock is higher priority than requirements.txt
        (tmp_path / "uv.lock").touch()
        (tmp_path / "requirements.txt").touch()
        cmds = _detect_setup_commands(tmp_path)
        assert cmds == ["uv sync --frozen"]


# ---------------------------------------------------------------------------
# TwoPhaseSandboxConfig defaults
# ---------------------------------------------------------------------------


class TestTwoPhaseSandboxConfig:
    def test_defaults(self) -> None:
        cfg = TwoPhaseSandboxConfig()
        assert cfg.setup_commands == ()
        assert cfg.phase1_timeout_s == 300
        assert cfg.phase1_network_mode == NetworkMode.BRIDGE
        assert cfg.phase2_network_mode == NetworkMode.NONE

    def test_custom_setup_commands(self) -> None:
        cfg = TwoPhaseSandboxConfig(setup_commands=("pip install boto3",))
        assert cfg.setup_commands == ("pip install boto3",)

    def test_custom_timeout(self) -> None:
        cfg = TwoPhaseSandboxConfig(phase1_timeout_s=600)
        assert cfg.phase1_timeout_s == 600


# ---------------------------------------------------------------------------
# ContainerIsolationConfig two_phase fields
# ---------------------------------------------------------------------------


class TestContainerIsolationConfigTwoPhase:
    def test_defaults_off(self) -> None:
        cfg = ContainerIsolationConfig()
        assert cfg.two_phase_sandbox is False
        assert cfg.sandbox_setup_commands == ()

    def test_can_enable(self) -> None:
        cfg = ContainerIsolationConfig(two_phase_sandbox=True)
        assert cfg.two_phase_sandbox is True

    def test_custom_setup_commands(self) -> None:
        cfg = ContainerIsolationConfig(
            two_phase_sandbox=True,
            sandbox_setup_commands=("pip install -r requirements.txt",),
        )
        assert cfg.sandbox_setup_commands == ("pip install -r requirements.txt",)


# ---------------------------------------------------------------------------
# ContainerManager.run_phase1_setup
# ---------------------------------------------------------------------------


def _make_manager(tmp_path: Path, two_phase: TwoPhaseSandboxConfig | None = None) -> ContainerManager:
    """Build a ContainerManager with a mocked runtime command."""
    cfg = ContainerConfig(
        image="bernstein-agent:test",
        two_phase_sandbox=two_phase,
    )
    with patch("bernstein.core.agents.container._resolve_runtime_cmd", return_value="docker"):
        mgr = ContainerManager(cfg, tmp_path)
    return mgr


class TestRunPhase1Setup:
    def test_returns_true_when_no_commands(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        result = mgr.run_phase1_setup("sess-001", [])
        assert result is True

    def test_success_returns_true(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        completed = MagicMock(spec=subprocess.CompletedProcess)
        completed.returncode = 0
        completed.stdout = "OK"
        completed.stderr = ""

        with patch("subprocess.run", return_value=completed) as mock_run:
            result = mgr.run_phase1_setup("sess-001", ["uv sync --frozen"])

        assert result is True
        mock_run.assert_called_once()
        # Verify the command includes the setup script and uses bridge network
        args = mock_run.call_args[0][0]
        assert "--rm" in args
        assert "uv sync --frozen" in " ".join(args)
        assert "bridge" in args

    def test_failure_returns_false(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        completed = MagicMock(spec=subprocess.CompletedProcess)
        completed.returncode = 1
        completed.stdout = ""
        completed.stderr = "pip: command not found"

        with patch("subprocess.run", return_value=completed):
            result = mgr.run_phase1_setup("sess-001", ["pip install boto3"])

        assert result is False

    def test_timeout_returns_false(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=5)):
            result = mgr.run_phase1_setup("sess-001", ["pip install -r requirements.txt"], timeout_s=5)
        assert result is False

    def test_os_error_returns_false(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        with patch("subprocess.run", side_effect=OSError("docker not found")):
            result = mgr.run_phase1_setup("sess-001", ["npm install"])
        assert result is False

    def test_uses_phase1_network_mode_from_config(self, tmp_path: Path) -> None:
        two_phase = TwoPhaseSandboxConfig(phase1_network_mode=NetworkMode.HOST)
        mgr = _make_manager(tmp_path, two_phase=two_phase)
        completed = MagicMock(spec=subprocess.CompletedProcess)
        completed.returncode = 0
        completed.stdout = ""
        completed.stderr = ""

        with patch("subprocess.run", return_value=completed) as mock_run:
            mgr.run_phase1_setup("sess-001", ["npm install"])

        args = mock_run.call_args[0][0]
        net_idx = args.index("--network")
        assert args[net_idx + 1] == "host"

    def test_multiple_commands_joined_with_and(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        completed = MagicMock(spec=subprocess.CompletedProcess)
        completed.returncode = 0
        completed.stdout = ""
        completed.stderr = ""

        with patch("subprocess.run", return_value=completed) as mock_run:
            mgr.run_phase1_setup("sess-001", ["cmd1", "cmd2", "cmd3"])

        # All commands should be joined with && in a single sh -c call
        args = mock_run.call_args[0][0]
        shell_cmd = args[-1]  # Last arg is the sh -c script
        assert shell_cmd == "cmd1 && cmd2 && cmd3"

    def test_setup_container_name_contains_setup_suffix(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        completed = MagicMock(spec=subprocess.CompletedProcess)
        completed.returncode = 0
        completed.stdout = ""
        completed.stderr = ""

        with patch("subprocess.run", return_value=completed) as mock_run:
            mgr.run_phase1_setup("sess-xyz", ["npm install"])

        args = mock_run.call_args[0][0]
        name_idx = args.index("--name")
        assert "sess-xyz-setup" in args[name_idx + 1]

    def test_workspace_is_mounted(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        completed = MagicMock(spec=subprocess.CompletedProcess)
        completed.returncode = 0
        completed.stdout = ""
        completed.stderr = ""

        with patch("subprocess.run", return_value=completed) as mock_run:
            mgr.run_phase1_setup("sess-001", ["uv sync"])

        args = mock_run.call_args[0][0]
        volume_args = [args[i + 1] for i, a in enumerate(args) if a == "--volume"]
        assert any("/workspace" in v for v in volume_args)


# ---------------------------------------------------------------------------
# Phase 2 network override in spawn_in_container
# ---------------------------------------------------------------------------


class TestSpawnInContainerNetworkOverride:
    def test_network_mode_override_applied(self, tmp_path: Path) -> None:
        """Phase 2 should use the overridden network mode (NONE), not the base config."""
        two_phase = TwoPhaseSandboxConfig()
        mgr = _make_manager(tmp_path, two_phase=two_phase)

        # Mock the subprocess.run used by spawn_in_container
        run_result = MagicMock(spec=subprocess.CompletedProcess)
        run_result.returncode = 0
        run_result.stdout = "abc123deadbeef"
        run_result.stderr = ""

        # Mock the _get_container_pid call (inspect)
        inspect_result = MagicMock(spec=subprocess.CompletedProcess)
        inspect_result.returncode = 0
        inspect_result.stdout = "12345"

        # Mock the forced remove (cleanup of initially created container)
        rm_result = MagicMock(spec=subprocess.CompletedProcess)
        rm_result.returncode = 0
        rm_result.stdout = ""

        call_count = 0

        def side_effect(args: list[str], **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if "rm" in args:
                return rm_result
            if "inspect" in args:
                return inspect_result
            if "create" in args:
                # Return a container ID for the create call
                r = MagicMock(spec=subprocess.CompletedProcess)
                r.returncode = 0
                r.stdout = "abc123"
                r.stderr = ""
                return r
            return run_result

        with patch("subprocess.run", side_effect=side_effect) as mock_run:
            mgr.spawn_in_container(
                "sess-001",
                ["sh", "-c", "echo hello"],
                network_mode_override=NetworkMode.NONE,
            )

        # Find the main `docker run` call (not rm, not create, not inspect)
        run_calls = [c for c in mock_run.call_args_list if "run" in c[0][0] and "-d" in c[0][0]]
        assert len(run_calls) == 1
        run_args = run_calls[0][0][0]
        # Network mode should be "none", not "host" (the default)
        net_idx = run_args.index("--network")
        assert run_args[net_idx + 1] == "none"
