"""Unit tests for DockerSandboxBackend helpers with a mocked docker client.

Live-daemon coverage lives in ``tests/integration/sandbox/``; these tests
exercise the daemon-availability probe and the run-teardown session sweep
(issue #2162) without requiring Docker.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from bernstein.core.sandbox import WorkspaceManifest
from bernstein.core.sandbox.backends.docker import DockerSandboxBackend, DockerUnavailableError


def _make_client() -> MagicMock:
    """Build a docker client mock whose containers run and exec cleanly."""
    client = MagicMock()
    container = MagicMock()
    exec_result = MagicMock()
    exec_result.exit_code = 0
    exec_result.output = b""
    container.exec_run.return_value = exec_result
    client.containers.run.return_value = container
    return client


def test_ensure_available_pings_the_daemon() -> None:
    """A responsive daemon passes the availability probe."""
    client = _make_client()
    backend = DockerSandboxBackend(client=client)
    backend.ensure_available()
    client.ping.assert_called_once()


def test_ensure_available_raises_when_ping_fails() -> None:
    """An unreachable daemon surfaces as DockerUnavailableError."""
    client = _make_client()
    client.ping.side_effect = RuntimeError("daemon down")
    backend = DockerSandboxBackend(client=client)
    with pytest.raises(DockerUnavailableError):
        backend.ensure_available()


def test_destroy_all_removes_every_tracked_session() -> None:
    """destroy_all sweeps sessions left behind at run teardown."""
    client = _make_client()
    backend = DockerSandboxBackend(client=client)
    manifest = WorkspaceManifest(root="/workspace")

    asyncio.run(backend.create(manifest))
    asyncio.run(backend.create(manifest))
    assert len(backend._sessions) == 2  # pyright: ignore[reportPrivateUsage]

    asyncio.run(backend.destroy_all())

    assert backend._sessions == {}  # pyright: ignore[reportPrivateUsage]
    container = client.containers.run.return_value
    assert container.stop.call_count == 2
    assert container.remove.call_count == 2
