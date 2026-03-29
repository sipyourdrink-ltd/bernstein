"""Unit tests for orchestrator container-isolation wiring (_build_container_config)."""

from __future__ import annotations

from bernstein.core.container import (
    ContainerConfig,
    ContainerRuntime,
    NetworkMode,
    TwoPhaseSandboxConfig,
)
from bernstein.core.models import ContainerIsolationConfig
from bernstein.core.orchestrator import _build_container_config

# ---------------------------------------------------------------------------
# _build_container_config — disabled
# ---------------------------------------------------------------------------


class TestBuildContainerConfigDisabled:
    def test_returns_none_when_disabled(self) -> None:
        iso = ContainerIsolationConfig(enabled=False)
        assert _build_container_config(iso) is None

    def test_returns_none_by_default(self) -> None:
        iso = ContainerIsolationConfig()
        assert _build_container_config(iso) is None


# ---------------------------------------------------------------------------
# _build_container_config — enabled, basic fields
# ---------------------------------------------------------------------------


class TestBuildContainerConfigEnabled:
    def test_returns_container_config_when_enabled(self) -> None:
        iso = ContainerIsolationConfig(enabled=True)
        cfg = _build_container_config(iso)
        assert isinstance(cfg, ContainerConfig)

    def test_image_propagated(self) -> None:
        iso = ContainerIsolationConfig(enabled=True, image="my-org/agent:v2")
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.image == "my-org/agent:v2"

    def test_default_image(self) -> None:
        iso = ContainerIsolationConfig(enabled=True)
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.image == "bernstein-agent:latest"

    def test_runtime_docker(self) -> None:
        iso = ContainerIsolationConfig(enabled=True, runtime="docker")
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.runtime == ContainerRuntime.DOCKER

    def test_runtime_podman(self) -> None:
        iso = ContainerIsolationConfig(enabled=True, runtime="podman")
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.runtime == ContainerRuntime.PODMAN

    def test_invalid_runtime_falls_back_to_docker(self) -> None:
        iso = ContainerIsolationConfig(enabled=True, runtime="unknown-runtime")
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.runtime == ContainerRuntime.DOCKER

    def test_network_mode_host(self) -> None:
        iso = ContainerIsolationConfig(enabled=True, network_mode="host")
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.network_mode == NetworkMode.HOST

    def test_network_mode_none(self) -> None:
        iso = ContainerIsolationConfig(enabled=True, network_mode="none")
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.network_mode == NetworkMode.NONE

    def test_invalid_network_mode_falls_back_to_host(self) -> None:
        iso = ContainerIsolationConfig(enabled=True, network_mode="vxlan")
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.network_mode == NetworkMode.HOST

    def test_resource_limits_cpu(self) -> None:
        iso = ContainerIsolationConfig(enabled=True, cpu_cores=4.0)
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.resource_limits.cpu_cores == 4.0

    def test_resource_limits_memory(self) -> None:
        iso = ContainerIsolationConfig(enabled=True, memory_mb=2048)
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.resource_limits.memory_mb == 2048

    def test_resource_limits_pids(self) -> None:
        iso = ContainerIsolationConfig(enabled=True, pids_limit=128)
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.resource_limits.pids_limit == 128

    def test_drop_capabilities_propagated(self) -> None:
        caps = ("NET_RAW", "SYS_ADMIN")
        iso = ContainerIsolationConfig(enabled=True, drop_capabilities=caps)
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.security.drop_capabilities == caps


# ---------------------------------------------------------------------------
# _build_container_config — two-phase sandbox
# ---------------------------------------------------------------------------


class TestBuildContainerConfigTwoPhase:
    def test_two_phase_disabled_by_default(self) -> None:
        iso = ContainerIsolationConfig(enabled=True)
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.two_phase_sandbox is None

    def test_two_phase_enabled(self) -> None:
        iso = ContainerIsolationConfig(enabled=True, two_phase_sandbox=True)
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert isinstance(cfg.two_phase_sandbox, TwoPhaseSandboxConfig)

    def test_two_phase_default_network_modes(self) -> None:
        iso = ContainerIsolationConfig(enabled=True, two_phase_sandbox=True)
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.two_phase_sandbox is not None
        assert cfg.two_phase_sandbox.phase1_network_mode == NetworkMode.BRIDGE
        assert cfg.two_phase_sandbox.phase2_network_mode == NetworkMode.NONE

    def test_two_phase_custom_setup_commands(self) -> None:
        iso = ContainerIsolationConfig(
            enabled=True,
            two_phase_sandbox=True,
            sandbox_setup_commands=("pip install boto3", "pip install requests"),
        )
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.two_phase_sandbox is not None
        assert cfg.two_phase_sandbox.setup_commands == ("pip install boto3", "pip install requests")

    def test_two_phase_empty_setup_commands_triggers_auto_detect(self) -> None:
        # Empty setup_commands means auto-detection will run at execution time
        iso = ContainerIsolationConfig(enabled=True, two_phase_sandbox=True, sandbox_setup_commands=())
        cfg = _build_container_config(iso)
        assert cfg is not None
        assert cfg.two_phase_sandbox is not None
        assert cfg.two_phase_sandbox.setup_commands == ()
