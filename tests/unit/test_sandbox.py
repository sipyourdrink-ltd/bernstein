"""Unit tests for Docker/Podman sandbox configuration and spawn helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.container import ContainerHandle, NetworkMode

from bernstein.core.sandbox import DockerSandbox, parse_docker_sandbox, spawn_in_sandbox


def test_parse_docker_sandbox_supports_adapter_images() -> None:
    """Adapter-specific images should override the default image."""

    sandbox = parse_docker_sandbox(
        {
            "runtime": "docker",
            "image": {
                "default": "bernstein/base:latest",
                "claude": "bernstein/claude:latest",
            },
            "disk_mb": 2048,
        }
    )

    assert sandbox is not None
    assert sandbox.image_for_adapter("claude") == "bernstein/claude:latest"
    assert sandbox.image_for_adapter("codex") == "bernstein/base:latest"
    assert sandbox.disk_mb == 2048
    assert sandbox.network_mode == "none"


def test_parse_docker_sandbox_rejects_invalid_runtime() -> None:
    """Only Docker and Podman are accepted sandbox runtimes."""

    with pytest.raises(ValueError, match="sandbox.runtime"):
        parse_docker_sandbox({"runtime": "firecracker"})


def test_spawn_in_sandbox_uses_adapter_specific_image(tmp_path: Path) -> None:
    """The manager should be created with the image chosen for the adapter."""

    sandbox = DockerSandbox(
        enabled=True,
        default_image="bernstein/base:latest",
        adapter_images={"claude": "bernstein/claude:latest"},
    )
    fake_handle = ContainerHandle(container_id="sandbox-1", session_id="S-1", pid=321)

    with patch("bernstein.core.security.sandbox.ContainerManager") as manager_cls:
        manager = manager_cls.return_value
        manager.spawn_in_container.return_value = fake_handle

        returned_manager, handle = spawn_in_sandbox(
            sandbox=sandbox,
            session_id="S-1",
            adapter_name="claude",
            cmd=["claude", "-p", "fix"],
            env={"OPENAI_API_KEY": "x"},
            workdir=tmp_path,
        )

    assert returned_manager is manager
    assert handle is fake_handle
    config = manager_cls.call_args.args[0]
    assert config.image == "bernstein/claude:latest"
    assert config.network_mode == NetworkMode.NONE
    assert config.resource_limits.disk_mb is None


def test_spawn_in_sandbox_applies_network_override(tmp_path: Path) -> None:
    """An explicit network override should replace the sandbox default."""

    sandbox = DockerSandbox(enabled=True, runtime="podman")
    fake_handle = ContainerHandle(container_id="sandbox-2", session_id="S-2", pid=654)

    with patch("bernstein.core.security.sandbox.ContainerManager") as manager_cls:
        manager = manager_cls.return_value
        manager.spawn_in_container.return_value = fake_handle

        spawn_in_sandbox(
            sandbox=sandbox,
            session_id="S-2",
            adapter_name="codex",
            cmd=["codex", "-p", "fix"],
            env=None,
            workdir=tmp_path,
            network_mode_override=NetworkMode.BRIDGE,
        )

    config = manager_cls.call_args.args[0]
    assert config.runtime.value == "podman"
    assert config.network_mode == NetworkMode.BRIDGE
