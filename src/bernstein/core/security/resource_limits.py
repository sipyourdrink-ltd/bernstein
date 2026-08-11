"""Agent process resource limits.

Applies CPU, memory, and disk I/O limits to agent processes using
``resource.setrlimit`` (POSIX) or advisory tracking when OS-level
enforcement is unavailable.

Usage::

    limits = ResourceLimits(memory_mb=2048, cpu_seconds=600)
    limits.apply(pid=12345)
    exceeded = limits.check(pid=12345)
"""

from __future__ import annotations

import logging
import os
import platform
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceLimits:
    """Resource limits for an agent process.

    All limits are advisory when the platform does not support enforcement.

    Attributes:
        memory_mb: Maximum resident set size in megabytes (0 = unlimited).
        cpu_seconds: Maximum CPU time in seconds (0 = unlimited).
        open_files: Maximum number of open file descriptors (0 = unlimited).
        disk_write_mb: Advisory disk write limit in megabytes (0 = unlimited).
    """

    memory_mb: int = 0
    cpu_seconds: int = 0
    open_files: int = 0
    disk_write_mb: int = 0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ResourceLimits:
        """Parse a config dict into ResourceLimits.

        Args:
            data: Dict with optional keys ``memory_mb``, ``cpu_seconds``,
                ``open_files``, ``disk_write_mb``.

        Returns:
            Parsed ResourceLimits.

        Raises:
            ValueError: If a limit is negative or a boolean. ``0`` remains the
                documented way to say "unlimited"; a negative is a typo that
                would otherwise be applied as a limit.
        """
        return cls(
            memory_mb=_limit_from_config(data, "memory_mb"),
            cpu_seconds=_limit_from_config(data, "cpu_seconds"),
            open_files=_limit_from_config(data, "open_files"),
            disk_write_mb=_limit_from_config(data, "disk_write_mb"),
        )

    def has_any_limit(self) -> bool:
        """Return True if any limit is set (non-zero).

        Returns:
            True if at least one limit is configured.
        """
        return bool(self.memory_mb or self.cpu_seconds or self.open_files or self.disk_write_mb)


def _limit_from_config(data: Mapping[str, Any], key: str) -> int:
    """Read one limit from operator config, refusing values that mean nothing.

    ``0`` is the documented way to say "unlimited", so a negative is never a
    smaller limit - it is a typo. Left alone it survives to ``setrlimit``,
    where a negative is ``RLIM_INFINITY`` on several platforms: the config
    reads as a tightened limit, ``has_any_limit()`` agrees one is set, and the
    process runs unbounded. Refusing at parse time is the only place that
    distinction is still visible.

    A ``bool`` is refused for the same reason rather than any safety one:
    YAML reads ``memory_mb: yes`` as ``True``, and ``int(True)`` is a 1 MB
    limit that nobody wrote down.

    Strings still coerce. Config arrives from YAML and TOML where a quoted
    number is ordinary, and ``int("6")`` was already the behaviour; a
    non-numeric string raises ``ValueError`` here exactly as it did before.

    Args:
        data: The operator's limits mapping.
        key: Which limit to read.

    Returns:
        The limit as a non-negative int; ``0`` when absent or empty.

    Raises:
        ValueError: If the value is a bool or negative.
    """
    raw = data.get(key, 0)
    if isinstance(raw, bool):
        msg = f"{key} must be an integer, got a boolean ({raw!r}); YAML reads `yes`/`no` as booleans"
        raise ValueError(msg)
    value = int(raw or 0)
    if value < 0:
        msg = f"{key} must be >= 0 ({value} given); 0 means unlimited"
        raise ValueError(msg)
    return value


# ---------------------------------------------------------------------------
# Default limits for non-sandboxed agents
# ---------------------------------------------------------------------------

#: Reasonable defaults to prevent a single agent from consuming all resources.
#: 4 GB memory, 30 min CPU, 4096 file descriptors.
DEFAULT_AGENT_LIMITS = ResourceLimits(
    memory_mb=4096,
    cpu_seconds=1800,
    open_files=4096,
    disk_write_mb=0,  # No disk write limit by default
)


# ---------------------------------------------------------------------------
# Enforcement result
# ---------------------------------------------------------------------------


@dataclass
class EnforcementResult:
    """Result of applying or checking resource limits.

    Attributes:
        applied: True if OS-level enforcement was applied.
        advisory_only: True if limits are only advisory (no OS enforcement).
        memory_enforced: True if memory limit was set at the OS level.
        cpu_enforced: True if CPU time limit was set at the OS level.
        open_files_enforced: True if file descriptor limit was set.
        warnings: Any warnings generated during enforcement.
    """

    applied: bool = False
    advisory_only: bool = True
    memory_enforced: bool = False
    cpu_enforced: bool = False
    open_files_enforced: bool = False
    warnings: list[str] = field(default_factory=list[str])


# ---------------------------------------------------------------------------
# Limit checking
# ---------------------------------------------------------------------------


@dataclass
class ResourceUsage:
    """Current resource usage for a process.

    Attributes:
        rss_mb: Resident set size in megabytes.
        cpu_seconds: CPU time consumed in seconds.
        open_files: Number of open file descriptors.
        memory_exceeded: True if RSS exceeds the configured limit.
        cpu_exceeded: True if CPU time exceeds the configured limit.
    """

    rss_mb: float = 0.0
    cpu_seconds: float = 0.0
    open_files: int = 0
    memory_exceeded: bool = False
    cpu_exceeded: bool = False


# ---------------------------------------------------------------------------
# Enforcer
# ---------------------------------------------------------------------------

_IS_POSIX = os.name == "posix"


def apply_limits(limits: ResourceLimits) -> EnforcementResult:
    """Apply resource limits to the current process.

    On POSIX systems, uses ``resource.setrlimit``.  On other platforms,
    limits are recorded as advisory only.

    This should be called from the child process (e.g. in a
    ``subprocess.Popen`` preexec_fn or at agent worker startup).

    Args:
        limits: Resource limits to apply.

    Returns:
        EnforcementResult describing what was enforced.
    """
    result = EnforcementResult()

    if not _IS_POSIX:
        result.advisory_only = True
        result.warnings.append(f"Resource limits are advisory on {platform.system()}")
        return result

    import resource

    result.applied = True
    result.advisory_only = False

    # Memory limit (RLIMIT_AS on Linux for virtual memory)
    if limits.memory_mb > 0:
        limit_bytes = limits.memory_mb * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
            result.memory_enforced = True
        except (ValueError, OSError) as exc:
            result.warnings.append(f"Could not set memory limit: {exc}")
            result.memory_enforced = False

    # CPU time limit
    if limits.cpu_seconds > 0:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
            result.cpu_enforced = True
        except (ValueError, OSError) as exc:
            result.warnings.append(f"Could not set CPU limit: {exc}")
            result.cpu_enforced = False

    # Open files limit
    if limits.open_files > 0:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (limits.open_files, limits.open_files))
            result.open_files_enforced = True
        except (ValueError, OSError) as exc:
            result.warnings.append(f"Could not set file descriptor limit: {exc}")
            result.open_files_enforced = False

    return result


def make_preexec_fn(limits: ResourceLimits) -> Callable[[], None] | None:
    """Return a ``preexec_fn`` callable suitable for ``subprocess.Popen``.

    Returns ``None`` when no limits are set or the platform does not support
    ``preexec_fn`` (non-POSIX).  The returned callable calls :func:`apply_limits`
    inside the child process immediately after ``fork()``.

    Args:
        limits: Resource limits to apply in the child process.

    Returns:
        A zero-argument callable, or ``None`` if limits cannot be enforced.
    """
    if not _IS_POSIX:
        return None
    if limits.memory_mb == limits.cpu_seconds == 0 and limits.open_files == 0:
        return None

    _limits = limits  # capture for closure

    def _preexec() -> None:
        apply_limits(_limits)

    return _preexec


def check_usage(pid: int, limits: ResourceLimits) -> ResourceUsage:
    """Check current resource usage for a process against configured limits.

    Works cross-platform by reading ``/proc/{pid}/stat`` on Linux or
    using ``psutil``-like /proc parsing.  Falls back to os-level queries.

    Args:
        pid: Process ID to check.
        limits: Resource limits to compare against.

    Returns:
        ResourceUsage with current usage and limit-exceeded flags.
    """
    usage = ResourceUsage()

    if not _IS_POSIX:
        return usage

    # Try to read memory from /proc (Linux)
    try:
        statm_path = f"/proc/{pid}/statm"
        with open(statm_path) as f:
            parts = f.read().split()
        # statm: size resident shared text lib data dt (all in pages)
        page_size = os.sysconf("SC_PAGE_SIZE")
        usage.rss_mb = int(parts[1]) * page_size / (1024 * 1024)
    except (OSError, IndexError, ValueError):
        # /proc not available (macOS) -- try resource module for self
        if pid == os.getpid():
            import resource

            ru = resource.getrusage(resource.RUSAGE_SELF)
            # ru_maxrss is in KB on Linux, bytes on macOS
            if platform.system() == "Darwin":
                usage.rss_mb = ru.ru_maxrss / (1024 * 1024)
            else:
                usage.rss_mb = ru.ru_maxrss / 1024

    # Check CPU time from /proc/stat
    with suppress(OSError, IndexError, ValueError):
        stat_path = f"/proc/{pid}/stat"
        raw = Path(stat_path).read_text()
        # CPU time is fields 14 (utime) and 15 (stime) after the comm field
        # comm field is enclosed in parens and may contain spaces
        after_comm = raw[raw.rfind(")") + 2 :]
        fields = after_comm.split()
        clock_ticks = os.sysconf("SC_CLK_TCK")
        utime = int(fields[11]) / clock_ticks
        stime = int(fields[12]) / clock_ticks
        usage.cpu_seconds = utime + stime

    # Check limits
    if limits.memory_mb > 0 and usage.rss_mb > limits.memory_mb:
        usage.memory_exceeded = True
    if limits.cpu_seconds > 0 and usage.cpu_seconds > limits.cpu_seconds:
        usage.cpu_exceeded = True

    return usage
