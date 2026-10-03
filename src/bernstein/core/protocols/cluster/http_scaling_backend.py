"""HTTP scaling backend — calls an external fleet API to scale workers.

Plugs into the :class:`ScalingBackendRegistry` via the
``bernstein.scaling_backends`` entry-point group (name ``http``).

The backend is generic: it calls any HTTP endpoint that implements:
- ``GET  <base_url>/api/cluster/autoscale/status`` → ``{"current": N, "target": N}``
- ``PATCH <base_url>/api/cluster/autoscale/config`` ← ``{"target": N}``

Auth: Bearer token in the ``Authorization`` header.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from bernstein.core.protocols.cluster.cluster_autoscaler import (
    ScaleResult,
    ScalingBackend,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HTTPScalingConfig:
    base_url: str = ""
    auth_token: str = ""
    timeout_s: float = 10.0

    @classmethod
    def from_env(cls) -> HTTPScalingConfig:
        return cls(
            base_url=os.environ.get("BERNSTEIN_SCALING_URL", "").rstrip("/"),
            auth_token=os.environ.get("BERNSTEIN_SCALING_TOKEN", ""),
        )


class HTTPScalingBackend(ScalingBackend):
    """Scale workers via an external HTTP fleet API."""

    def __init__(self, config: HTTPScalingConfig | None = None) -> None:
        self._config = config or HTTPScalingConfig.from_env()

    @property
    def name(self) -> str:
        return "http"

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._config.auth_token:
            h["Authorization"] = f"Bearer {self._config.auth_token}"
        return h

    def current_node_count(self) -> int:
        if not self._config.base_url:
            return 0
        try:
            resp = httpx.get(
                f"{self._config.base_url}/api/cluster/autoscale/status",
                headers=self._headers(),
                timeout=self._config.timeout_s,
            )
            resp.raise_for_status()
            return resp.json().get("current", 0)
        except httpx.HTTPError as exc:
            logger.warning("Failed to get node count from %s: %s", self._config.base_url, exc)
            return 0

    def scale_to(self, target_count: int) -> ScaleResult:
        if not self._config.base_url:
            return ScaleResult(
                success=False,
                previous_count=0,
                new_count=0,
                backend=self.name,
                error="no base_url configured",
            )

        previous = self.current_node_count()
        try:
            resp = httpx.patch(
                f"{self._config.base_url}/api/cluster/autoscale/config",
                json={"target": target_count},
                headers=self._headers(),
                timeout=self._config.timeout_s,
            )
            resp.raise_for_status()
            return ScaleResult(
                success=True,
                previous_count=previous,
                new_count=target_count,
                backend=self.name,
            )
        except httpx.HTTPError as exc:
            logger.warning("Scale to %d failed: %s", target_count, exc)
            return ScaleResult(
                success=False,
                previous_count=previous,
                new_count=previous,
                backend=self.name,
                error=str(exc),
            )
