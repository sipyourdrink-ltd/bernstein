"""Typed Docker/Podman sandbox configuration and spawn helpers."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

from bernstein.core.container import (
    ContainerConfig,
    ContainerHandle,
    ContainerManager,
    ContainerRuntime,
    MountSpec,
    NetworkMode,
    ResourceLimits,
    SecurityProfile,
)

if TYPE_CHECKING:
    from pathlib import Path

_AGENT_IMAGE = "bernstein-agent:latest"

SandboxRuntime = Literal["docker", "podman"]

_VALID_RUNTIMES = {"docker", "podman"}
_VALID_NETWORKS = {"none", "bridge", "host"}


@dataclass(frozen=True)
class DockerSandbox:
    """Container sandbox settings used for agent execution.

    Args:
        enabled: Whether the sandbox is active.
        runtime: Container runtime backend.
        default_image: Default image for adapters without an explicit override.
        adapter_images: Per-adapter image overrides keyed by adapter name.
        cpu_cores: CPU limit passed to the container runtime.
        memory_mb: Memory limit passed to the container runtime.
        disk_mb: Writable-layer disk quota when supported by the runtime.
        pids_limit: PID limit for sandboxed agents.
        network_mode: Network mode for execution. Defaults to ``"none"``.
        drop_capabilities: Linux capabilities removed from the container.
        read_only_rootfs: Whether to mount the root filesystem read-only.
        extra_mounts: Additional bind mounts for the sandbox container.
    """

    enabled: bool = False
    runtime: SandboxRuntime = "docker"
    default_image: str = _AGENT_IMAGE
    adapter_images: dict[str, str] = field(default_factory=dict[str, str])
    cpu_cores: float | None = 2.0
    memory_mb: int | None = 4096
    disk_mb: int | None = None
    pids_limit: int | None = 256
    network_mode: Literal["none", "bridge", "host"] = "none"
    drop_capabilities: tuple[str, ...] = (
        "NET_RAW",
        "SYS_ADMIN",
        "SYS_PTRACE",
        "MKNOD",
    )
    read_only_rootfs: bool = False
    extra_mounts: tuple[MountSpec, ...] = ()

    def image_for_adapter(self, adapter_name: str) -> str:
        """Return the configured image for an adapter.

        Args:
            adapter_name: Adapter identifier used by the spawner.

        Returns:
            Container image name for the adapter.
        """
        normalized = adapter_name.strip().lower()
        return self.adapter_images.get(normalized, self.default_image)

    def to_container_config(self, adapter_name: str) -> ContainerConfig:
        """Convert sandbox settings into the existing container runtime config.

        Args:
            adapter_name: Adapter identifier for image selection.

        Returns:
            Equivalent :class:`ContainerConfig`.
        """
        return ContainerConfig(
            runtime=ContainerRuntime(self.runtime),
            image=self.image_for_adapter(adapter_name),
            resource_limits=ResourceLimits(
                cpu_cores=self.cpu_cores,
                memory_mb=self.memory_mb,
                disk_mb=self.disk_mb,
                pids_limit=self.pids_limit,
                read_only_rootfs=self.read_only_rootfs,
            ),
            security=SecurityProfile(drop_capabilities=self.drop_capabilities),
            network_mode=NetworkMode(self.network_mode),
            extra_mounts=self.extra_mounts,
        )


def parse_docker_sandbox(raw: object | None) -> DockerSandbox | None:
    """Parse the optional ``sandbox`` seed section.

    Args:
        raw: Raw value from ``bernstein.yaml``.

    Returns:
        Parsed sandbox config, or ``None`` when the section is missing.

    Raises:
        ValueError: If the section shape or values are invalid.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("sandbox must be a mapping")

    data = cast("Mapping[str, object]", raw)
    enabled = _parse_bool(data.get("enabled", True), "sandbox.enabled")
    runtime_raw = data.get("runtime", "docker")
    if not isinstance(runtime_raw, str) or runtime_raw not in _VALID_RUNTIMES:
        raise ValueError("sandbox.runtime must be one of docker, podman")

    network_raw = data.get("network_mode", "none")
    if not isinstance(network_raw, str) or network_raw not in _VALID_NETWORKS:
        raise ValueError("sandbox.network_mode must be one of none, bridge, host")

    image_raw = data.get("image", _AGENT_IMAGE)
    default_image: str
    adapter_images: dict[str, str]
    if isinstance(image_raw, str):
        default_image = image_raw
        adapter_images = {}
    elif isinstance(image_raw, Mapping):
        image_map = cast("Mapping[str, object]", image_raw)
        default_raw = image_map.get("default", _AGENT_IMAGE)
        if not isinstance(default_raw, str):
            raise ValueError("sandbox.image.default must be a string")
        default_image = default_raw
        adapter_images = {}
        for raw_key, raw_value in image_map.items():
            if raw_key == "default":
                continue
            if not isinstance(raw_value, str):
                raise ValueError("sandbox.image adapter entries must be strings")
            adapter_images[str(raw_key).strip().lower()] = raw_value
    else:
        raise ValueError("sandbox.image must be a string or mapping")

    return DockerSandbox(
        enabled=enabled,
        runtime=cast("SandboxRuntime", runtime_raw),
        default_image=default_image,
        adapter_images=adapter_images,
        cpu_cores=_parse_optional_float(data.get("cpu_cores"), "sandbox.cpu_cores"),
        memory_mb=_parse_optional_int(data.get("memory_mb"), "sandbox.memory_mb"),
        disk_mb=_parse_optional_int(data.get("disk_mb"), "sandbox.disk_mb"),
        pids_limit=_parse_optional_int(data.get("pids_limit"), "sandbox.pids_limit"),
        network_mode=cast("Literal['none', 'bridge', 'host']", network_raw),
        drop_capabilities=_parse_capabilities(data.get("drop_capabilities")),
        read_only_rootfs=_parse_bool(data.get("read_only_rootfs", False), "sandbox.read_only_rootfs"),
    )


def spawn_in_sandbox(
    *,
    sandbox: DockerSandbox,
    session_id: str,
    adapter_name: str,
    cmd: list[str],
    env: dict[str, str] | None,
    workdir: Path,
    log_path: Path | None = None,
    network_mode_override: NetworkMode | None = None,
) -> tuple[ContainerManager, ContainerHandle]:
    """Spawn a process inside a Docker or Podman sandbox.

    Args:
        sandbox: Parsed sandbox configuration.
        session_id: Bernstein agent session ID.
        adapter_name: Adapter name used to resolve the container image.
        cmd: Command to execute inside the container.
        env: Optional environment passed through to the runtime.
        workdir: Workspace mounted into the sandbox.
        log_path: Optional path for container log capture.
        network_mode_override: Optional runtime network override.

    Returns:
        Tuple of the manager instance and created container handle.
    """
    config = sandbox.to_container_config(adapter_name)
    if network_mode_override is not None and network_mode_override != config.network_mode:
        config = dataclasses.replace(config, network_mode=network_mode_override)
    manager = ContainerManager(config, workdir)
    handle = manager.spawn_in_container(
        session_id=session_id,
        cmd=cmd,
        env=env,
        workspace_override=workdir,
        log_path=log_path,
        network_mode_override=network_mode_override,
    )
    return manager, handle


def _parse_bool(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{field_name} must be a boolean")


def _parse_optional_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer or null")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        return int(value)
    raise ValueError(f"{field_name} must be an integer or null")


def _parse_optional_float(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number or null")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        return float(value)
    raise ValueError(f"{field_name} must be a number or null")


def _parse_capabilities(value: object) -> tuple[str, ...]:
    if value is None:
        return DockerSandbox().drop_capabilities
    if not isinstance(value, list):
        raise ValueError("sandbox.drop_capabilities must be a list of strings")
    raw_items = cast("list[object]", value)
    parsed: list[str] = []
    for item in raw_items:
        if not isinstance(item, str):
            raise ValueError("sandbox.drop_capabilities must contain only strings")
        parsed.append(item)
    return tuple(parsed)
