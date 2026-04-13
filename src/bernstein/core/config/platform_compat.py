"""Platform compatibility layer for Windows/Unix process management.

Provides cross-platform abstractions for process signalling, quoting, and
path handling so the rest of the codebase can call a single API without
sprinkling ``sys.platform`` checks everywhere.

On Unix (macOS/Linux), this is mostly a thin wrapper around ``os.kill``,
``os.killpg``, and ``shlex.quote``.  On Windows, equivalent semantics are
achieved via ``subprocess.run(["taskkill", ...])`` and ``ctypes`` where
the POSIX APIs are unavailable.
"""

from __future__ import annotations

import logging
import os
import platform
import shlex
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

IS_WINDOWS: bool = sys.platform == "win32"
"""True when running on Windows."""


# ---------------------------------------------------------------------------
# Platform information
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformInfo:
    """Immutable snapshot of the current platform's characteristics.

    Attributes:
        os_name: Normalised operating system identifier.
        arch: CPU architecture (e.g. ``"x86_64"``, ``"arm64"``).
        python_version: Python version string (e.g. ``"3.12.4"``).
        has_signals: Whether POSIX-style signals (SIGKILL, SIGUSR1, etc.)
            are available.  Always ``False`` on Windows.
        path_separator: Filesystem PATH separator (``":"`` on Unix,
            ``";"`` on Windows).
        temp_dir: Platform-specific temporary directory path.
    """

    os_name: Literal["linux", "macos", "windows"]
    arch: str
    python_version: str
    has_signals: bool
    path_separator: str
    temp_dir: str


def _detect_os_name() -> Literal["linux", "macos", "windows"]:
    """Return a normalised OS name from ``sys.platform``.

    Returns:
        One of ``"linux"``, ``"macos"``, or ``"windows"``.
    """
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    # Everything else (linux, freebsd, etc.) normalises to linux.
    return "linux"


def get_platform_info() -> PlatformInfo:
    """Detect and return a snapshot of the current platform.

    Returns:
        A frozen :class:`PlatformInfo` dataclass describing the runtime
        environment.
    """
    os_name = _detect_os_name()
    return PlatformInfo(
        os_name=os_name,
        arch=platform.machine() or "unknown",
        python_version=platform.python_version(),
        has_signals=os_name != "windows",
        path_separator=";" if os_name == "windows" else ":",
        temp_dir=tempfile.gettempdir(),
    )


# Well-known POSIX signals that are absent on Windows.
_POSIX_ONLY_SIGNALS: frozenset[str] = frozenset(
    {
        "SIGKILL",
        "SIGSTOP",
        "SIGUSR1",
        "SIGUSR2",
        "SIGALRM",
        "SIGHUP",
        "SIGQUIT",
        "SIGTSTP",
        "SIGCONT",
        "SIGCHLD",
        "SIGPIPE",
        "SIGTTIN",
        "SIGTTOU",
        "SIGWINCH",
        "SIGURG",
        "SIGVTALRM",
        "SIGPROF",
        "SIGIO",
        "SIGPWR",
        "SIGSYS",
    }
)


def is_signal_supported(signal_name: str) -> bool:
    """Check whether a named signal is available on the current platform.

    The check is two-fold:

    1. On Windows, well-known POSIX-only signals (``SIGKILL``, ``SIGUSR1``,
       etc.) are known to be unsupported and return ``False`` immediately.
    2. For all other names, falls back to ``hasattr(signal, signal_name)``.

    Args:
        signal_name: Signal attribute name, e.g. ``"SIGTERM"`` or
            ``"SIGKILL"``.

    Returns:
        ``True`` if the signal is available on this platform.
    """
    if IS_WINDOWS and signal_name in _POSIX_ONLY_SIGNALS:
        return False
    return hasattr(signal, signal_name)


def normalize_path(path: str) -> str:
    """Normalise a filesystem path for the current platform.

    Converts Windows-style backslashes to forward slashes on all platforms
    and collapses redundant separators via :func:`os.path.normpath`.

    Args:
        path: Raw filesystem path string.

    Returns:
        A normalised path string with consistent separators.
    """
    # First normalise via the OS (collapses .., removes redundant seps).
    normalised = os.path.normpath(path)
    # On non-Windows, ensure no stray backslashes from Windows-origin paths.
    if not IS_WINDOWS:
        normalised = normalised.replace("\\", "/")
    return normalised


def get_process_kill_cmd(pid: int) -> list[str]:
    """Return a platform-specific command to terminate a process.

    On Unix, returns ``["kill", "<pid>"]``.  On Windows, returns
    ``["taskkill", "/F", "/PID", "<pid>"]``.

    Args:
        pid: Process ID to target.

    Returns:
        Command-line tokens suitable for :func:`subprocess.run`.
    """
    if IS_WINDOWS:
        return ["taskkill", "/F", "/PID", str(pid)]
    return ["kill", str(pid)]


def skip_on_windows(
    reason: str = "Not supported on Windows",
) -> Callable[[Callable[..., object]], Callable[..., object]]:
    """Pytest marker decorator that skips a test on Windows.

    Wraps :func:`pytest.mark.skipif` with a Windows check so callers
    don't need to repeat ``sys.platform == "win32"`` everywhere.

    Args:
        reason: Human-readable skip reason shown in test output.

    Returns:
        A pytest decorator that skips the decorated test on Windows.

    Example::

        @skip_on_windows("chmod semantics differ on Windows")
        def test_file_permissions() -> None:
            ...
    """
    import pytest as _pytest

    marker: pytest.MarkDecorator = _pytest.mark.skipif(
        IS_WINDOWS,
        reason=reason,
    )
    # The MarkDecorator is callable and returns the wrapped function.
    return marker  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------


def kill_process(pid: int, sig: int = 15) -> bool:
    """Send a signal to a process, cross-platform.

    On Unix, delegates to ``os.kill(pid, sig)``.  On Windows, maps
    SIGTERM (15) to ``taskkill /PID`` and SIGKILL (9) to ``taskkill /F /PID``.
    Other signals on Windows fall back to ``os.kill(pid, sig)`` which only
    supports ``SIGTERM`` natively.

    Args:
        pid: Process ID to signal.
        sig: Signal number (default 15 = SIGTERM).

    Returns:
        True if the signal was sent successfully, False if the process
        was already dead or the operation failed.
    """
    if pid <= 0:
        return False

    if not IS_WINDOWS:
        try:
            os.kill(pid, sig)
            return True
        except OSError:
            return False

    # Windows path
    if sig == signal.SIGTERM:
        return _win_taskkill(pid, force=False)
    if sig == 9:  # SIGKILL — force-kill on Windows
        return _win_taskkill(pid, force=True)
    # Best-effort: os.kill on Windows only supports SIGTERM natively
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def kill_process_group(pgid: int, sig: int = 15) -> bool:
    """Send a signal to a process group, cross-platform.

    On Unix, delegates to ``os.killpg(pgid, sig)``.  On Windows, process
    groups are not directly supported so this falls back to killing the
    single process via ``kill_process``, then attempts to kill the child
    tree with ``taskkill /T``.

    Args:
        pgid: Process group ID (on Unix) or PID (on Windows).
        sig: Signal number (default 15 = SIGTERM).

    Returns:
        True if at least the lead process was signalled successfully.
    """
    if pgid <= 0:
        return False

    if not IS_WINDOWS:
        try:
            os.killpg(pgid, sig)
            return True
        except OSError:
            return False

    # Windows: kill process tree
    force = sig == 9
    return _win_taskkill(pgid, force=force, tree=True)


def process_alive(pid: int) -> bool:
    """Check whether a process is still running, cross-platform.

    On Unix, uses ``os.kill(pid, 0)`` (signal 0 = existence check).
    On Windows, uses ``ctypes`` to call ``OpenProcess`` and then
    ``GetExitCodeProcess`` to distinguish live from zombie processes.

    Args:
        pid: Process ID to check.

    Returns:
        True if the process exists and is running.
    """
    if pid <= 0:
        return False

    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    # Windows: use ctypes kernel32 calls
    return _win_process_alive(pid)


# ---------------------------------------------------------------------------
# Shell quoting
# ---------------------------------------------------------------------------


def shell_quote(s: str) -> str:
    """Quote a string for safe use in a shell command, cross-platform.

    On Unix, delegates to ``shlex.quote``.  On Windows ``cmd.exe``,
    wraps the string in double quotes and escapes interior double-quotes
    and percent signs.

    Args:
        s: The string to quote.

    Returns:
        A safely-quoted version of *s*.
    """
    if not IS_WINDOWS:
        return shlex.quote(s)

    # Windows cmd.exe quoting: wrap in double quotes, escape specials
    if not s:
        return '""'
    # If the string contains no special characters, return as-is
    needs_quoting = any(c in s for c in ' \t"&|<>^%')
    if not needs_quoting:
        return s
    # Escape double quotes and percent signs inside the string
    escaped = s.replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# Executable and path helpers
# ---------------------------------------------------------------------------


def executable_name(name: str) -> str:
    """Append ``.exe`` suffix on Windows if not already present.

    On Unix, returns *name* unchanged.

    Args:
        name: Base executable name (e.g. ``"claude"``).

    Returns:
        Executable name with platform-appropriate suffix.
    """
    if IS_WINDOWS and not name.endswith(".exe"):
        return f"{name}.exe"
    return name


def path_separator() -> str:
    """Return the platform PATH separator.

    Returns:
        ``":"`` on Unix, ``";"`` on Windows.
    """
    return ";" if IS_WINDOWS else ":"


# ---------------------------------------------------------------------------
# Internal Windows helpers
# ---------------------------------------------------------------------------


def _win_taskkill(pid: int, *, force: bool = False, tree: bool = False) -> bool:
    """Kill a process on Windows via ``taskkill``.

    Args:
        pid: Process ID.
        force: If True, adds ``/F`` (force terminate).
        tree: If True, adds ``/T`` (kill child processes).

    Returns:
        True if taskkill exited successfully.
    """
    cmd: list[str] = ["taskkill"]
    if force:
        cmd.append("/F")
    if tree:
        cmd.append("/T")
    cmd.extend(["/PID", str(pid)])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("taskkill failed for PID %d: %s", pid, exc)
        return False


def _win_process_alive(pid: int) -> bool:
    """Check process liveness on Windows via kernel32.

    Uses ``OpenProcess`` with ``PROCESS_QUERY_LIMITED_INFORMATION`` access
    and ``GetExitCodeProcess`` to determine if the process is still running.

    This function is only called on Windows.  The ``ctypes.windll`` attribute
    does not exist on Unix, so all kernel32 calls are guarded behind the
    ``IS_WINDOWS`` check in :func:`process_alive`.

    Args:
        pid: Process ID.

    Returns:
        True if the process is alive.
    """
    import ctypes
    import ctypes.wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259

    kernel32: object = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle: int = kernel32.OpenProcess(  # type: ignore[union-attr]
        _PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid,
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):  # type: ignore[union-attr]
            return bool(exit_code.value == _STILL_ACTIVE)
        return False
    finally:
        kernel32.CloseHandle(handle)  # type: ignore[union-attr]
