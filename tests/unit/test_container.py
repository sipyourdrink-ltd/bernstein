"""Unit tests for container manager behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bernstein.core.container import ContainerConfig, ContainerError, ContainerManager


def test_manager_init_fails_when_runtime_binary_missing(tmp_path: Path) -> None:
    with patch("bernstein.core.container.shutil.which", return_value=None):
        with pytest.raises(ContainerError, match="not found on PATH"):
            ContainerManager(ContainerConfig(), tmp_path)


def test_create_invokes_runtime_with_workspace_and_env(tmp_path: Path) -> None:
    with (
        patch("bernstein.core.container._resolve_runtime_cmd", return_value="docker"),
        patch(
            "bernstein.core.container.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="container-id\n", stderr=""),
        ) as run_mock,
    ):
        manager = ContainerManager(ContainerConfig(), tmp_path)
        handle = manager.create("S-1", env={"API_KEY": "x"})

    called_args = run_mock.call_args.args[0]
    joined = " ".join(called_args)
    assert handle.container_id == "container-id"
    assert "--env API_KEY=x" in joined
    assert "/workspace:rw" in joined
    assert manager.get_handle("S-1") is not None


def test_create_raises_on_nonzero_exit(tmp_path: Path) -> None:
    with (
        patch("bernstein.core.container._resolve_runtime_cmd", return_value="docker"),
        patch(
            "bernstein.core.container.subprocess.run",
            return_value=SimpleNamespace(returncode=1, stdout="", stderr="boom"),
        ),
    ):
        manager = ContainerManager(ContainerConfig(), tmp_path)
        with pytest.raises(ContainerError, match="Container creation failed"):
            manager.create("S-2")


def test_destroy_untracks_handle(tmp_path: Path) -> None:
    with (
        patch("bernstein.core.container._resolve_runtime_cmd", return_value="docker"),
        patch(
            "bernstein.core.container.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="container-id\n", stderr="", strip=lambda: ""),
        ),
    ):
        manager = ContainerManager(ContainerConfig(), tmp_path)
        handle = manager.create("S-3")
        manager.destroy(handle)

    assert manager.get_handle("S-3") is None
