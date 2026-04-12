"""SEC-012: Sandbox escape detection for containerized agents.

Monitors filesystem, network, and process boundaries of sandboxed agents.
Alerts on violations that indicate a container escape attempt.

Usage::

    from bernstein.core.sandbox_escape_detector import (
        SandboxEscapeDetector,
        EscapeViolation,
        BoundaryConfig,
    )

    detector = SandboxEscapeDetector(config)
    violations = detector.check_filesystem(agent_id, paths_accessed)
    violations += detector.check_network(agent_id, connections)
    violations += detector.check_processes(agent_id, processes)
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class ViolationType(StrEnum):
    """Types of sandbox boundary violations."""

    FILESYSTEM_ESCAPE = "filesystem_escape"
    NETWORK_VIOLATION = "network_violation"
    PROCESS_VIOLATION = "process_violation"
    CAPABILITY_ESCALATION = "capability_escalation"
    MOUNT_VIOLATION = "mount_violation"


class ViolationSeverity(StrEnum):
    """Severity levels for violations."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class EscapeViolation:
    """A detected sandbox boundary violation.

    Attributes:
        agent_id: The agent that triggered the violation.
        violation_type: Category of violation.
        severity: How severe the violation is.
        description: Human-readable description.
        evidence: Raw evidence (path, connection, process info).
        timestamp: When the violation was detected.
    """

    agent_id: str
    violation_type: ViolationType
    severity: ViolationSeverity
    description: str
    evidence: str
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class BoundaryConfig:
    """Configuration for sandbox boundary enforcement.

    Attributes:
        allowed_paths: Glob patterns for paths agents may access.
        denied_paths: Paths agents must not access (override allowed).
        allowed_ports: Network ports agents may connect to.
        allowed_hosts: Hostnames agents may connect to.
        max_processes: Maximum number of processes an agent may spawn.
        denied_executables: Executables agents must not run.
        allowed_mounts: Mount paths that are permitted.
    """

    allowed_paths: tuple[str, ...] = (
        "/workspace/*",
        "/tmp/*",
        "/home/*",
    )
    denied_paths: tuple[str, ...] = (
        "/etc/shadow",
        "/etc/passwd",
        "/proc/*/mem",
        "/sys/*",
        "/dev/*",
        "/root/*",
        "/var/run/docker.sock",
    )
    allowed_ports: tuple[int, ...] = (80, 443, 8052)
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    max_processes: int = 50
    denied_executables: tuple[str, ...] = (
        "nsenter",
        "unshare",
        "mount",
        "umount",
        "chroot",
        "pivot_root",
        "capsh",
    )
    allowed_mounts: tuple[str, ...] = ("/workspace",)


def _matches_pattern(value: str, patterns: tuple[str, ...]) -> bool:
    """Check if a value matches any of the given glob-like patterns.

    Args:
        value: The value to check.
        patterns: Tuple of glob-like patterns (supports ``*`` wildcard).

    Returns:
        True if the value matches any pattern.
    """
    for pattern in patterns:
        regex_pattern = re.escape(pattern).replace(r"\*", ".*")
        if re.fullmatch(regex_pattern, value):
            return True
    return False


class SandboxEscapeDetector:
    """Detects sandbox escape attempts by monitoring agent behaviour.

    Checks filesystem access, network connections, and process creation
    against configured boundaries.  Violations are collected and can be
    used to terminate the agent or raise alerts.

    Args:
        config: Boundary configuration.
    """

    def __init__(self, config: BoundaryConfig | None = None) -> None:
        self._config = config or BoundaryConfig()
        self._violations: list[EscapeViolation] = []

    @property
    def config(self) -> BoundaryConfig:
        """Return the boundary configuration."""
        return self._config

    @property
    def violations(self) -> list[EscapeViolation]:
        """Return all recorded violations."""
        return list(self._violations)

    def check_filesystem(
        self,
        agent_id: str,
        paths: list[str],
    ) -> list[EscapeViolation]:
        """Check filesystem access against boundary rules.

        Args:
            agent_id: Identifier of the agent.
            paths: List of file paths accessed by the agent.

        Returns:
            List of violations found.
        """
        violations: list[EscapeViolation] = []

        for path in paths:
            # Check denied paths first (always takes priority)
            if _matches_pattern(path, self._config.denied_paths):
                v = EscapeViolation(
                    agent_id=agent_id,
                    violation_type=ViolationType.FILESYSTEM_ESCAPE,
                    severity=ViolationSeverity.CRITICAL,
                    description=f"Access to denied path: {path}",
                    evidence=path,
                )
                violations.append(v)
                self._violations.append(v)
                logger.warning(
                    "SANDBOX VIOLATION: agent=%s accessed denied path %s",
                    agent_id,
                    path,
                )
                continue

            # Check if path is within allowed boundaries
            if not _matches_pattern(path, self._config.allowed_paths):
                v = EscapeViolation(
                    agent_id=agent_id,
                    violation_type=ViolationType.FILESYSTEM_ESCAPE,
                    severity=ViolationSeverity.WARNING,
                    description=f"Access to path outside sandbox: {path}",
                    evidence=path,
                )
                violations.append(v)
                self._violations.append(v)

        return violations

    def check_network(
        self,
        agent_id: str,
        connections: list[dict[str, Any]],
    ) -> list[EscapeViolation]:
        """Check network connections against boundary rules.

        Each connection dict should have ``host`` and ``port`` keys.

        Args:
            agent_id: Identifier of the agent.
            connections: List of connection dicts with ``host`` and ``port``.

        Returns:
            List of violations found.
        """
        violations: list[EscapeViolation] = []

        for conn in connections:
            host = str(conn.get("host", ""))
            port = int(conn.get("port", 0))

            if host and host not in self._config.allowed_hosts:
                v = EscapeViolation(
                    agent_id=agent_id,
                    violation_type=ViolationType.NETWORK_VIOLATION,
                    severity=ViolationSeverity.CRITICAL,
                    description=f"Connection to unauthorized host: {host}",
                    evidence=f"{host}:{port}",
                )
                violations.append(v)
                self._violations.append(v)
                logger.warning(
                    "SANDBOX VIOLATION: agent=%s connected to unauthorized host %s:%d",
                    agent_id,
                    host,
                    port,
                )

            if port and port not in self._config.allowed_ports:
                v = EscapeViolation(
                    agent_id=agent_id,
                    violation_type=ViolationType.NETWORK_VIOLATION,
                    severity=ViolationSeverity.WARNING,
                    description=f"Connection to unauthorized port: {port}",
                    evidence=f"{host}:{port}",
                )
                violations.append(v)
                self._violations.append(v)

        return violations

    def check_processes(
        self,
        agent_id: str,
        processes: list[dict[str, Any]],
    ) -> list[EscapeViolation]:
        """Check process creation against boundary rules.

        Each process dict should have ``name`` (executable name) and
        optionally ``pid``.

        Args:
            agent_id: Identifier of the agent.
            processes: List of process dicts with ``name`` and ``pid``.

        Returns:
            List of violations found.
        """
        violations: list[EscapeViolation] = []

        # Check process count
        if len(processes) > self._config.max_processes:
            v = EscapeViolation(
                agent_id=agent_id,
                violation_type=ViolationType.PROCESS_VIOLATION,
                severity=ViolationSeverity.WARNING,
                description=(f"Process count {len(processes)} exceeds limit {self._config.max_processes}"),
                evidence=f"count={len(processes)}",
            )
            violations.append(v)
            self._violations.append(v)

        # Check for denied executables
        for proc in processes:
            proc_name = str(proc.get("name", ""))
            if proc_name in self._config.denied_executables:
                v = EscapeViolation(
                    agent_id=agent_id,
                    violation_type=ViolationType.PROCESS_VIOLATION,
                    severity=ViolationSeverity.CRITICAL,
                    description=f"Denied executable: {proc_name}",
                    evidence=proc_name,
                )
                violations.append(v)
                self._violations.append(v)
                logger.warning(
                    "SANDBOX VIOLATION: agent=%s ran denied executable %s",
                    agent_id,
                    proc_name,
                )

        return violations

    def check_mounts(
        self,
        agent_id: str,
        mounts: list[str],
    ) -> list[EscapeViolation]:
        """Check container mount points against boundary rules.

        Args:
            agent_id: Identifier of the agent.
            mounts: List of mount target paths.

        Returns:
            List of violations found.
        """
        violations: list[EscapeViolation] = []

        for mount_path in mounts:
            if not any(mount_path.startswith(a) for a in self._config.allowed_mounts):
                v = EscapeViolation(
                    agent_id=agent_id,
                    violation_type=ViolationType.MOUNT_VIOLATION,
                    severity=ViolationSeverity.CRITICAL,
                    description=f"Unauthorized mount: {mount_path}",
                    evidence=mount_path,
                )
                violations.append(v)
                self._violations.append(v)

        return violations

    def clear_violations(self) -> None:
        """Clear the recorded violations list."""
        self._violations.clear()

    def has_critical_violations(self) -> bool:
        """Return True if any CRITICAL violations have been recorded."""
        return any(v.severity == ViolationSeverity.CRITICAL for v in self._violations)
