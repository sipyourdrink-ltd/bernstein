"""Tests for MCP server sandboxing configuration (MCP-016)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.mcp_sandbox import (
    BUILTIN_PROFILES,
    SandboxConfig,
    SandboxProfile,
    build_sandbox_command,
    load_sandbox_config,
    validate_sandbox_config,
)

# ---------------------------------------------------------------------------
# SandboxProfile
# ---------------------------------------------------------------------------


class TestSandboxProfile:
    """Tests for the SandboxProfile frozen dataclass."""

    def test_defaults(self) -> None:
        p = SandboxProfile(name="test")
        assert p.name == "test"
        assert p.read_only_paths == []
        assert p.writable_paths == []
        assert p.network_enabled is False
        assert p.memory_limit_mb == 512
        assert p.cpu_limit == 1.0
        assert p.timeout_seconds == 300

    def test_frozen(self) -> None:
        p = SandboxProfile(name="frozen")
        with pytest.raises(AttributeError):
            p.name = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# BUILTIN_PROFILES
# ---------------------------------------------------------------------------


class TestBuiltinProfiles:
    """Tests for the three builtin profiles."""

    def test_strict_profile(self) -> None:
        p = BUILTIN_PROFILES["strict"]
        assert p.name == "strict"
        assert p.network_enabled is False
        assert "/" in p.read_only_paths
        assert p.memory_limit_mb == 256

    def test_standard_profile(self) -> None:
        p = BUILTIN_PROFILES["standard"]
        assert p.name == "standard"
        assert p.network_enabled is False
        assert "." in p.writable_paths
        assert p.memory_limit_mb == 512

    def test_permissive_profile(self) -> None:
        p = BUILTIN_PROFILES["permissive"]
        assert p.name == "permissive"
        assert p.network_enabled is True
        assert p.memory_limit_mb == 1024

    def test_all_three_present(self) -> None:
        assert set(BUILTIN_PROFILES) == {"strict", "standard", "permissive"}


# ---------------------------------------------------------------------------
# SandboxConfig
# ---------------------------------------------------------------------------


class TestSandboxConfig:
    """Tests for the SandboxConfig frozen dataclass."""

    def test_defaults(self) -> None:
        cfg = SandboxConfig()
        assert cfg.enabled is False
        assert cfg.profile == "standard"
        assert cfg.runtime == "docker"
        assert cfg.image == "bernstein/mcp-sandbox:latest"
        assert cfg.extra_mounts == []

    def test_frozen(self) -> None:
        cfg = SandboxConfig()
        with pytest.raises(AttributeError):
            cfg.enabled = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# build_sandbox_command
# ---------------------------------------------------------------------------


class TestBuildSandboxCommand:
    """Tests for building the container runtime command."""

    def test_basic_docker_run(self) -> None:
        cfg = SandboxConfig(enabled=True)
        profile = BUILTIN_PROFILES["standard"]
        cmd = build_sandbox_command(
            server_command=["node", "server.js"],
            config=cfg,
            profile=profile,
            workdir="/home/user/project",
        )
        assert cmd[0] == "docker"
        assert "run" in cmd
        assert "--rm" in cmd
        assert "node" in cmd
        assert "server.js" in cmd

    def test_contains_read_only_flag(self) -> None:
        cfg = SandboxConfig(enabled=True)
        profile = BUILTIN_PROFILES["strict"]
        cmd = build_sandbox_command(
            server_command=["echo"],
            config=cfg,
            profile=profile,
            workdir="/tmp/test",
        )
        assert "--read-only" in cmd

    def test_network_none_when_disabled(self) -> None:
        cfg = SandboxConfig(enabled=True)
        profile = BUILTIN_PROFILES["standard"]
        cmd = build_sandbox_command(
            server_command=["echo"],
            config=cfg,
            profile=profile,
            workdir="/tmp/test",
        )
        assert "--network=none" in cmd

    def test_no_network_flag_when_enabled(self) -> None:
        cfg = SandboxConfig(enabled=True)
        profile = BUILTIN_PROFILES["permissive"]
        cmd = build_sandbox_command(
            server_command=["echo"],
            config=cfg,
            profile=profile,
            workdir="/tmp/test",
        )
        assert "--network=none" not in cmd

    def test_memory_limit(self) -> None:
        cfg = SandboxConfig(enabled=True)
        profile = BUILTIN_PROFILES["strict"]
        cmd = build_sandbox_command(
            server_command=["echo"],
            config=cfg,
            profile=profile,
            workdir="/tmp/test",
        )
        mem_idx = cmd.index("--memory")
        assert cmd[mem_idx + 1] == "256m"

    def test_cpu_limit(self) -> None:
        cfg = SandboxConfig(enabled=True)
        profile = BUILTIN_PROFILES["standard"]
        cmd = build_sandbox_command(
            server_command=["echo"],
            config=cfg,
            profile=profile,
            workdir="/tmp/test",
        )
        cpu_idx = cmd.index("--cpus")
        assert cmd[cpu_idx + 1] == "1.0"

    def test_workdir_mount_rw_when_writable(self) -> None:
        cfg = SandboxConfig(enabled=True)
        profile = BUILTIN_PROFILES["standard"]
        cmd = build_sandbox_command(
            server_command=["echo"],
            config=cfg,
            profile=profile,
            workdir="/home/user/project",
        )
        assert "-v" in cmd
        mount_args = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-v"]
        workdir_mounts = [m for m in mount_args if "/home/user/project" in m]
        assert any(":rw" in m for m in workdir_mounts)

    def test_workdir_mount_ro_when_no_writable(self) -> None:
        cfg = SandboxConfig(enabled=True)
        profile = BUILTIN_PROFILES["strict"]
        cmd = build_sandbox_command(
            server_command=["echo"],
            config=cfg,
            profile=profile,
            workdir="/home/user/project",
        )
        mount_args = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-v"]
        workdir_mounts = [m for m in mount_args if "/home/user/project" in m]
        assert any(":ro" in m for m in workdir_mounts)

    def test_extra_mounts(self) -> None:
        cfg = SandboxConfig(
            enabled=True,
            extra_mounts=["/data/models:/models:ro"],
        )
        profile = BUILTIN_PROFILES["standard"]
        cmd = build_sandbox_command(
            server_command=["echo"],
            config=cfg,
            profile=profile,
            workdir="/tmp/test",
        )
        mount_args = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-v"]
        assert "/data/models:/models:ro" in mount_args

    def test_podman_runtime(self) -> None:
        cfg = SandboxConfig(enabled=True, runtime="podman")
        profile = BUILTIN_PROFILES["standard"]
        cmd = build_sandbox_command(
            server_command=["node", "srv.js"],
            config=cfg,
            profile=profile,
            workdir="/tmp/test",
        )
        assert cmd[0] == "podman"

    def test_custom_image(self) -> None:
        cfg = SandboxConfig(enabled=True, image="my-org/custom:v2")
        profile = BUILTIN_PROFILES["standard"]
        cmd = build_sandbox_command(
            server_command=["echo"],
            config=cfg,
            profile=profile,
            workdir="/tmp/test",
        )
        assert "my-org/custom:v2" in cmd

    def test_server_command_at_end(self) -> None:
        cfg = SandboxConfig(enabled=True)
        profile = BUILTIN_PROFILES["standard"]
        server = ["npx", "-y", "@example/mcp-server"]
        cmd = build_sandbox_command(
            server_command=server,
            config=cfg,
            profile=profile,
            workdir="/tmp/test",
        )
        assert cmd[-3:] == server


# ---------------------------------------------------------------------------
# validate_sandbox_config
# ---------------------------------------------------------------------------


class TestValidateSandboxConfig:
    """Tests for configuration validation."""

    def test_valid_config(self) -> None:
        cfg = SandboxConfig(enabled=True)
        assert validate_sandbox_config(cfg) == []

    def test_invalid_runtime(self) -> None:
        cfg = SandboxConfig(runtime="lxc")
        errors = validate_sandbox_config(cfg)
        assert len(errors) == 1
        assert "runtime" in errors[0].lower() or "lxc" in errors[0]

    def test_unknown_profile(self) -> None:
        cfg = SandboxConfig(profile="ultra-secure")
        errors = validate_sandbox_config(cfg)
        assert len(errors) == 1
        assert "ultra-secure" in errors[0]

    def test_empty_image(self) -> None:
        cfg = SandboxConfig(image="")
        errors = validate_sandbox_config(cfg)
        assert any("image" in e.lower() for e in errors)

    def test_multiple_errors(self) -> None:
        cfg = SandboxConfig(runtime="rkt", profile="nonexistent", image="")
        errors = validate_sandbox_config(cfg)
        assert len(errors) == 3


# ---------------------------------------------------------------------------
# load_sandbox_config
# ---------------------------------------------------------------------------


class TestLoadSandboxConfig:
    """Tests for YAML-based config loading."""

    def test_none_path_returns_default(self) -> None:
        cfg = load_sandbox_config(None)
        assert cfg == SandboxConfig()

    def test_missing_file_returns_default(self, tmp_path: Path) -> None:
        cfg = load_sandbox_config(tmp_path / "nonexistent.yaml")
        assert cfg == SandboxConfig()

    def test_empty_file_returns_default(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.yaml"
        p.write_text("")
        cfg = load_sandbox_config(p)
        assert cfg == SandboxConfig()

    def test_no_mcp_sandbox_key(self, tmp_path: Path) -> None:
        p = tmp_path / "bernstein.yaml"
        p.write_text("other_key: true\n")
        cfg = load_sandbox_config(p)
        assert cfg == SandboxConfig()

    def test_full_config(self, tmp_path: Path) -> None:
        p = tmp_path / "bernstein.yaml"
        p.write_text(
            textwrap.dedent("""\
                mcp_sandbox:
                  enabled: true
                  profile: strict
                  runtime: podman
                  image: custom/sandbox:v1
                  extra_mounts:
                    - /data:/data:ro
            """)
        )
        cfg = load_sandbox_config(p)
        assert cfg.enabled is True
        assert cfg.profile == "strict"
        assert cfg.runtime == "podman"
        assert cfg.image == "custom/sandbox:v1"
        assert cfg.extra_mounts == ["/data:/data:ro"]

    def test_partial_config_uses_defaults(self, tmp_path: Path) -> None:
        p = tmp_path / "bernstein.yaml"
        p.write_text(
            textwrap.dedent("""\
                mcp_sandbox:
                  enabled: true
            """)
        )
        cfg = load_sandbox_config(p)
        assert cfg.enabled is True
        assert cfg.profile == "standard"
        assert cfg.runtime == "docker"
        assert cfg.image == "bernstein/mcp-sandbox:latest"
        assert cfg.extra_mounts == []
