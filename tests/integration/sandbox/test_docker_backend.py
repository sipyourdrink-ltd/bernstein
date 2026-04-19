"""Integration tests for :class:`DockerSandboxBackend` (oai-002).

Gated by a live Docker daemon. When Docker is unavailable the tests
``skip`` rather than ``fail`` so developers without Docker locally can
still run the unit suite.

The full conformance suite is run against the Docker backend so any
protocol-level drift is caught automatically.
"""

from __future__ import annotations

import importlib.util
import os
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from bernstein.core.sandbox import WorkspaceManifest
from bernstein.core.sandbox.conformance import SandboxBackendConformance

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bernstein.core.sandbox.backend import SandboxBackend


def _docker_available() -> bool:
    """Return True when the ``docker`` SDK imports and the daemon responds."""
    if importlib.util.find_spec("docker") is None:
        return False
    if os.environ.get("BERNSTEIN_SKIP_DOCKER_TESTS") == "1":
        return False
    try:
        import docker  # type: ignore[import-not-found]

        client = docker.from_env()
        client.ping()
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker SDK not installed or daemon unreachable",
)


class TestDockerConformance(SandboxBackendConformance):
    """Run the conformance suite against the Docker backend."""

    @pytest_asyncio.fixture
    async def backend(self) -> AsyncIterator[SandboxBackend]:
        from bernstein.core.sandbox.backends.docker import DockerSandboxBackend

        backend = DockerSandboxBackend()
        yield backend

    @pytest.fixture
    def manifest(self) -> WorkspaceManifest:
        return WorkspaceManifest(
            root="/workspace",
            env={"LC_ALL": "C"},
            timeout_seconds=60,
        )
