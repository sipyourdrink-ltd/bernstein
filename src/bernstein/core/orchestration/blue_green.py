"""Blue-green deployment support for zero-downtime Bernstein upgrades.

Manages two parallel ``.sdd/`` environments (blue and green) and switches
traffic between them via symlink swaps.  On failure the previous
environment is restored automatically when ``rollback_on_error`` is set.

Usage::

    cfg = BlueGreenConfig(health_check_url="http://127.0.0.1:8052/status")
    deploy = BlueGreenDeployment(cfg, base_dir=Path("."))
    green = deploy.prepare_green("2.1.0")
    if deploy.health_check():
        deploy.switch_traffic()
    else:
        deploy.rollback()
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import httpx

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_VERSION_FILE = "version.json"


@dataclass(frozen=True)
class BlueGreenConfig:
    """Configuration for blue-green deployment behaviour.

    Attributes:
        strategy: Deployment strategy name (e.g. ``"rolling"``).
        health_check_url: URL to GET for a 200 OK liveness probe.
        rollback_on_error: Whether to auto-rollback on health-check failure.
        switch_delay_seconds: Grace period (seconds) before symlink swap.
    """

    strategy: str = "rolling"
    health_check_url: str = ""
    rollback_on_error: bool = True
    switch_delay_seconds: int = 10


@dataclass(frozen=True)
class DeploymentStatus:
    """Snapshot of the current deployment state.

    Attributes:
        active: Which environment is currently live (``"blue"`` or ``"green"``).
        blue_version: Version string of the blue environment.
        green_version: Version string of the green environment, if prepared.
        healthy: Whether the active environment passes health checks.
    """

    active: Literal["blue", "green"]
    blue_version: str
    green_version: str | None
    healthy: bool


class BlueGreenDeployment:
    """Manage blue-green deployments for ``.sdd/`` state directories.

    Args:
        config: Deployment configuration.
        base_dir: Project root that contains the ``.sdd/`` directory.
    """

    def __init__(self, config: BlueGreenConfig, base_dir: Path) -> None:
        self._config = config
        self._base_dir = base_dir
        self._sdd = base_dir / ".sdd"
        self._blue_dir = base_dir / ".sdd-blue"
        self._green_dir = base_dir / ".sdd-green"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def prepare_green(self, version: str) -> Path:
        """Create and populate a green environment for *version*.

        Copies configuration from the current blue environment into a fresh
        ``.sdd-green/`` directory and records the version.

        Args:
            version: Semantic version string for the green deployment.

        Returns:
            Path to the newly created green directory.
        """
        self._ensure_blue()

        if self._green_dir.exists():
            shutil.rmtree(self._green_dir)

        self._green_dir.mkdir(parents=True)

        # Copy config subdirectory from blue when present.
        blue_config = self._blue_dir / "config"
        if blue_config.is_dir():
            shutil.copytree(blue_config, self._green_dir / "config")

        self._write_version(self._green_dir, version)
        logger.info("Prepared green environment v%s at %s", version, self._green_dir)
        return self._green_dir

    def health_check(self) -> bool:
        """Probe the green environment for liveness.

        When ``health_check_url`` is configured, issues a GET and expects a
        200 status.  When no URL is configured, falls back to checking that
        the green directory exists and contains a version file.

        Returns:
            ``True`` if the green environment appears healthy.
        """
        if self._config.health_check_url:
            return self._http_health_check()
        return self._green_dir.exists() and (self._green_dir / _VERSION_FILE).is_file()

    def switch_traffic(self) -> None:
        """Swap the ``.sdd/`` symlink to point at the green environment.

        If a switch delay is configured, this method sleeps first.

        Raises:
            FileNotFoundError: If the green directory has not been prepared.
        """
        if not self._green_dir.exists():
            msg = "Green environment does not exist; call prepare_green first"
            raise FileNotFoundError(msg)

        if self._config.switch_delay_seconds > 0:
            logger.info(
                "Waiting %d s before switching traffic",
                self._config.switch_delay_seconds,
            )
            time.sleep(self._config.switch_delay_seconds)

        self._swap_symlink(self._green_dir)
        logger.info("Traffic switched to green environment")

    def rollback(self) -> None:
        """Revert the ``.sdd/`` symlink to point at the blue environment."""
        self._swap_symlink(self._blue_dir)
        logger.info("Rolled back to blue environment")

    def status(self) -> DeploymentStatus:
        """Return the current deployment status.

        Returns:
            A ``DeploymentStatus`` describing the active env, versions,
            and health.
        """
        active = self._active_env()
        blue_version = self._read_version(self._blue_dir)
        green_version = self._read_version(self._green_dir) if self._green_dir.exists() else None
        healthy = self.health_check()
        return DeploymentStatus(
            active=active,
            blue_version=blue_version,
            green_version=green_version,
            healthy=healthy,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_blue(self) -> None:
        """Ensure the blue directory exists, bootstrapping from ``.sdd/`` if needed."""
        if self._blue_dir.exists():
            return
        if self._sdd.is_dir() and not self._sdd.is_symlink():
            # Promote the existing .sdd/ into the blue slot.
            self._sdd.rename(self._blue_dir)
            self._sdd.symlink_to(self._blue_dir)
            if not (self._blue_dir / _VERSION_FILE).exists():
                self._write_version(self._blue_dir, "0.0.0")
        else:
            self._blue_dir.mkdir(parents=True)
            self._write_version(self._blue_dir, "0.0.0")
            if not self._sdd.exists():
                self._sdd.symlink_to(self._blue_dir)

    def _swap_symlink(self, target: Path) -> None:
        """Atomically replace ``.sdd/`` symlink to point at *target*."""
        if self._sdd.is_symlink():
            self._sdd.unlink()
        elif self._sdd.is_dir():
            # .sdd is a real directory — shouldn't happen after _ensure_blue,
            # but handle gracefully.
            self._sdd.rename(self._base_dir / ".sdd-backup")
        self._sdd.symlink_to(target)

    def _active_env(self) -> Literal["blue", "green"]:
        """Determine which environment the ``.sdd/`` symlink points at."""
        if self._sdd.is_symlink():
            resolved = self._sdd.resolve()
            if resolved == self._green_dir.resolve():
                return "green"
        return "blue"

    @staticmethod
    def _write_version(directory: Path, version: str) -> None:
        """Write a version file into *directory*."""
        (directory / _VERSION_FILE).write_text(
            json.dumps({"version": version, "deployed_at": time.time()}),
        )

    @staticmethod
    def _read_version(directory: Path) -> str:
        """Read the version string from *directory*, returning ``"unknown"`` on error."""
        vfile = directory / _VERSION_FILE
        if not vfile.exists():
            return "unknown"
        try:
            data = json.loads(vfile.read_text())
            return str(data.get("version", "unknown"))
        except (json.JSONDecodeError, OSError):
            return "unknown"

    def _http_health_check(self) -> bool:
        """Issue a GET to the configured health-check URL."""
        try:
            resp = httpx.get(self._config.health_check_url, timeout=5)
            return resp.status_code == 200
        except httpx.HTTPError:
            logger.warning("Health check failed for %s", self._config.health_check_url)
            return False
