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
import ntpath
import os
import platform
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

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
    if sig == 9:  # SIGKILL - force-kill on Windows
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


def kill_process_group_graceful(
    pgid: int,
    *,
    grace_seconds: float = 3.0,
    poll_interval: float = 0.1,
) -> bool:
    """Send SIGTERM to a process group, then SIGKILL if it fails to exit.

    Bernstein adapters spawn child processes with ``start_new_session=True``
    so the PID equals the PGID.  Reap paths (wall-clock timeout and stale
    heartbeat) invoke :meth:`CLIAdapter.kill`, which historically only sent
    SIGTERM without waiting or escalating - wedged agents that trap SIGTERM
    (e.g. ``trap '' TERM``) survive the reap and leak resources until the
    next orchestrator startup.

    This helper performs the standard TERM → poll → KILL escalation used
    elsewhere (see ``orchestration/drain.py``) so every kill path is
    guaranteed to reap the process group.

    Args:
        pgid: Process group ID (on Unix) or PID (on Windows).
        grace_seconds: Total time to wait for SIGTERM to take effect
            before escalating to SIGKILL.  Defaults to 3s - reap paths
            need to be aggressive because the agent already failed to
            heartbeat or exceeded its wall-clock timeout.
        poll_interval: How often to poll :func:`process_alive` during the
            grace window.  Smaller values make the helper exit sooner when
            the process dies cleanly after SIGTERM.

    Returns:
        ``True`` if SIGTERM was delivered successfully (even if SIGKILL
        later had to be used).  ``False`` when the group was already dead
        or the initial SIGTERM could not be sent.
    """
    return reap_process_group(
        pgid,
        grace_seconds=grace_seconds,
        poll_interval=poll_interval,
    ).delivered


# Reap-method identifiers recorded in :class:`ProcessReapReceipt`.
_REAP_METHOD_POSIX = "posix_process_group"
_REAP_METHOD_WINDOWS = "windows_process_tree"


@dataclass(frozen=True)
class ProcessReapReceipt:
    """Structured outcome of a process-tree reap.

    A deterministic projection of what the reap path did: which platform
    mechanism delivered the stop, whether the graceful stop was delivered,
    and whether escalation to a force-kill was required.  Callers mirror
    the receipt into the audit chain so a failure window can be
    reconstructed offline.

    Attributes:
        pgid: Process group ID (POSIX) or lead PID (Windows) targeted.
        os_name: Normalised OS name (``"linux"``, ``"macos"``, ``"windows"``).
        method: Delivery mechanism identifier
            (``"posix_process_group"`` or ``"windows_process_tree"``).
        delivered: Whether the initial graceful stop was delivered.
        escalated: Whether a force-kill was required after the grace window.
        grace_seconds: The grace window that applied to this reap.
    """

    pgid: int
    os_name: str
    method: str
    delivered: bool
    escalated: bool
    grace_seconds: float

    def to_details(self) -> dict[str, object]:
        """Return the receipt as a plain dict for audit-chain payloads."""
        return {
            "pgid": self.pgid,
            "os_name": self.os_name,
            "method": self.method,
            "delivered": self.delivered,
            "escalated": self.escalated,
            "grace_seconds": self.grace_seconds,
        }


def reap_process_group(
    pgid: int,
    *,
    grace_seconds: float = 3.0,
    poll_interval: float = 0.1,
) -> ProcessReapReceipt:
    """Reap a process group and return a structured receipt.

    Same TERM -> poll -> force-kill escalation as
    :func:`kill_process_group_graceful` (which delegates here), but the
    outcome is returned as a :class:`ProcessReapReceipt` so callers can
    record *how* the reap was performed instead of a bare bool.

    Args:
        pgid: Process group ID (on Unix) or PID (on Windows).
        grace_seconds: Total time to wait for the graceful stop to take
            effect before escalating to a force-kill.
        poll_interval: How often to poll :func:`process_alive` during the
            grace window.

    Returns:
        A frozen :class:`ProcessReapReceipt` describing the reap outcome.
    """
    os_name = _detect_os_name()
    method = _REAP_METHOD_WINDOWS if IS_WINDOWS else _REAP_METHOD_POSIX

    def _receipt(*, delivered: bool, escalated: bool) -> ProcessReapReceipt:
        return ProcessReapReceipt(
            pgid=pgid,
            os_name=os_name,
            method=method,
            delivered=delivered,
            escalated=escalated,
            grace_seconds=grace_seconds,
        )

    if pgid <= 0:
        return _receipt(delivered=False, escalated=False)

    # Best-effort TERM first; if it fails the group is already gone.
    if not kill_process_group(pgid, signal.SIGTERM):
        return _receipt(delivered=False, escalated=False)

    # Poll for graceful exit.  Using the lead PID as a liveness proxy is
    # safe because it is guaranteed to be the session leader (start_new_session=True).
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not process_alive(pgid):
            return _receipt(delivered=True, escalated=False)
        time.sleep(poll_interval)

    # Still alive after grace period - escalate.
    escalated = False
    if process_alive(pgid):
        logger.warning(
            "Process group %d did not exit within %.1fs of SIGTERM; sending SIGKILL",
            pgid,
            grace_seconds,
        )
        kill_sig = signal.SIGKILL if is_signal_supported("SIGKILL") else 9
        kill_process_group(pgid, kill_sig)
        escalated = True
    return _receipt(delivered=True, escalated=escalated)


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
    """Kill a process on Windows via ``taskkill`` with PowerShell fallback.

    Args:
        pid: Process ID.
        force: If True, adds ``/F`` (force terminate).
        tree: If True, adds ``/T`` (kill child processes).

    Returns:
        True if the process was killed successfully.
    """
    # Try taskkill first (faster when it works)
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
            timeout=5,
        )
        if result.returncode == 0:
            return True
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("taskkill failed for PID %d: %s", pid, exc)

    # Fallback: PowerShell Stop-Process (more reliable for stubborn processes)
    try:
        ps_cmd = f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        # PowerShell returns 0 even if process doesn't exist, so verify
        return not _win_process_alive(pid)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("PowerShell Stop-Process failed for PID %d: %s", pid, exc)
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


# ---------------------------------------------------------------------------
# Process-group spawn keywords
# ---------------------------------------------------------------------------

# CREATE_NEW_PROCESS_GROUP is only defined by the subprocess module on
# Windows; the literal value is stable Win32 API surface.
_WIN_CREATE_NEW_PROCESS_GROUP = 0x00000200


def process_group_popen_kwargs() -> dict[str, Any]:
    """Return the ``subprocess.Popen`` keywords that isolate a process tree.

    Deterministic projection of the current platform onto spawn flags:

    * POSIX: ``{"start_new_session": True}`` - the child becomes a session
      leader, so its PID equals its PGID and the whole tree can be reaped
      with ``os.killpg``.
    * Windows: ``{"creationflags": CREATE_NEW_PROCESS_GROUP}`` - the child
      anchors its own process group so console control events and
      ``taskkill /T`` tree termination target the agent tree, not the
      orchestrator.

    ``start_new_session`` is silently ignored by CPython on Windows, so
    call sites that spread these kwargs get real group semantics on both
    platforms instead of POSIX-only behaviour.

    Returns:
        Keyword arguments to spread into ``subprocess.Popen``.
    """
    if IS_WINDOWS:
        creationflags: int = getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            _WIN_CREATE_NEW_PROCESS_GROUP,
        )
        return {"creationflags": creationflags}
    return {"start_new_session": True}


# ---------------------------------------------------------------------------
# Windows Job Objects
# ---------------------------------------------------------------------------

# Win32 constants used by the Job Object primitives.
_WIN_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_WIN_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WIN_PROCESS_SET_QUOTA = 0x0100
_WIN_PROCESS_TERMINATE = 0x0001


def _win_kernel32() -> Any:
    """Return the kernel32 DLL handle (Windows only).

    Isolated in a helper so tests can exercise the Job Object logic on any
    platform by substituting a mock kernel32.
    """
    import ctypes

    return ctypes.windll.kernel32  # type: ignore[attr-defined]


def _win_job_limit_info() -> Any:
    """Build a JOBOBJECT_EXTENDED_LIMIT_INFORMATION with kill-on-close set.

    The ctypes structure definitions are portable; only the kernel32 calls
    that consume the structure are Windows-specific.
    """
    import ctypes
    import ctypes.wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = (
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        )

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", ctypes.wintypes.LARGE_INTEGER),
            ("LimitFlags", ctypes.wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.wintypes.DWORD),
            ("SchedulingClass", ctypes.wintypes.DWORD),
        )

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    info = _ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = _WIN_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    return info


class WindowsJobObject:
    """Job Object supervision for a Windows process tree.

    Job Objects are the Windows-native replacement for POSIX process
    groups: every process assigned to the job (and every descendant it
    spawns) is a member, so :meth:`terminate` reaps the whole tree in one
    kernel call, and the ``KILL_ON_JOB_CLOSE`` limit guarantees the tree
    dies with the supervisor even if the orchestrator crashes.

    Inert on POSIX: :meth:`available` returns ``False`` and every other
    method raises :class:`RuntimeError` so accidental use is loud.

    Usage::

        with WindowsJobObject() as job:
            if job.create():
                job.assign(proc.pid)
            ...
            job.terminate()
    """

    def __init__(self) -> None:
        self._handle: int | None = None

    @staticmethod
    def available() -> bool:
        """Return ``True`` when Job Objects exist on this platform."""
        return IS_WINDOWS

    def _require_windows(self) -> None:
        if not IS_WINDOWS:
            raise RuntimeError("Job Objects are Windows-only; check WindowsJobObject.available() first")

    def create(self) -> bool:
        """Create an anonymous Job Object with kill-on-close semantics.

        Returns:
            ``True`` when the job was created and configured.
        """
        self._require_windows()
        import ctypes

        kernel32 = _win_kernel32()
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            logger.warning("CreateJobObjectW failed; falling back to taskkill tree termination")
            return False
        info = _win_job_limit_info()
        ok = kernel32.SetInformationJobObject(
            handle,
            _WIN_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            logger.warning("SetInformationJobObject failed; closing job handle")
            kernel32.CloseHandle(handle)
            return False
        self._handle = int(handle)
        return True

    def assign(self, pid: int) -> bool:
        """Assign *pid* (and its future descendants) to this job.

        Args:
            pid: Lead process ID to place under supervision.

        Returns:
            ``True`` when the process joined the job.
        """
        self._require_windows()
        if self._handle is None or pid <= 0:
            return False
        kernel32 = _win_kernel32()
        proc = kernel32.OpenProcess(
            _WIN_PROCESS_SET_QUOTA | _WIN_PROCESS_TERMINATE,
            False,
            pid,
        )
        if not proc:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(self._handle, proc))
        finally:
            kernel32.CloseHandle(proc)

    def terminate(self, exit_code: int = 1) -> bool:
        """Terminate every process in the job in a single kernel call.

        Args:
            exit_code: Exit code reported for the terminated processes.

        Returns:
            ``True`` when the job tree was terminated.
        """
        self._require_windows()
        if self._handle is None:
            return False
        kernel32 = _win_kernel32()
        return bool(kernel32.TerminateJobObject(self._handle, exit_code))

    def close(self) -> None:
        """Release the job handle.  Idempotent.

        With ``KILL_ON_JOB_CLOSE`` set, closing the last handle also
        terminates any processes still in the job.
        """
        if self._handle is None:
            return
        if IS_WINDOWS:
            _win_kernel32().CloseHandle(self._handle)
        self._handle = None

    def __enter__(self) -> WindowsJobObject:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Filesystem semantics (worktree handling)
# ---------------------------------------------------------------------------

# Legacy Windows path-length ceiling that still applies to many Win32 file
# APIs unless the extended-length prefix is used.  CreateDirectoryW caps at
# MAX_PATH - 12 (248); staying below that keeps every worktree file
# operation safe.
_WIN_LONG_PATH_THRESHOLD = 248
_WIN_EXTENDED_PREFIX = "\\\\?\\"


def to_extended_length_path(path: str | Path) -> str:
    """Return *path* in a form safe for long Windows paths.

    On Windows, absolute paths at or beyond the legacy limit are given the
    extended-length prefix (``\\\\?\\`` or ``\\\\?\\UNC\\`` for UNC paths) so
    deep worktree trees survive Win32 file APIs.  Short paths are returned
    as normalised absolute paths without the prefix.  On POSIX the input is
    returned unchanged.

    Args:
        path: Filesystem path (string or ``Path``).

    Returns:
        A string path usable with ``open``, ``shutil``, and ``os`` calls.
    """
    raw = os.fspath(path)
    if not IS_WINDOWS:
        return raw
    if raw.startswith(_WIN_EXTENDED_PREFIX):
        return raw
    absolute = ntpath.abspath(raw)
    if len(absolute) < _WIN_LONG_PATH_THRESHOLD:
        return absolute
    if absolute.startswith("\\\\"):
        # UNC path: \\server\share\... -> \\?\UNC\server\share\...
        return _WIN_EXTENDED_PREFIX + "UNC" + absolute[1:]
    return _WIN_EXTENDED_PREFIX + absolute


def _win_clear_readonly(func: Callable[[str], object], path: str, _exc: BaseException) -> None:
    """``shutil.rmtree`` onexc handler: clear read-only and retry once."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def robust_rmtree(
    path: str | Path,
    *,
    max_attempts: int = 5,
    retry_delay_s: float = 0.1,
) -> bool:
    """Remove a directory tree, tolerating Windows filesystem semantics.

    On POSIX this is a single ``shutil.rmtree`` attempt - behaviour is
    unchanged from calling it directly, except failures are logged and
    reported instead of raised.  On Windows, read-only attributes (which
    git sets on object files) are cleared on demand, long paths get the
    extended-length prefix, and transient sharing violations (antivirus,
    indexers, a slow-to-exit child holding a handle) are retried with a
    linear backoff.

    Args:
        path: Directory tree to remove.
        max_attempts: Maximum removal attempts on Windows (must be >= 1).
        retry_delay_s: Base delay between attempts; grows linearly.

    Returns:
        ``True`` when the tree is gone (or never existed), ``False`` when
        removal ultimately failed.
    """
    raw = os.fspath(path)
    if not os.path.lexists(raw):
        return True

    if not IS_WINDOWS:
        try:
            shutil.rmtree(raw)
        except OSError as exc:
            logger.warning("Failed to remove tree %s: %s", raw, type(exc).__name__)
            return False
        return True

    target = to_extended_length_path(raw)
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            shutil.rmtree(target, onexc=_win_clear_readonly)
        except OSError as exc:
            if attempt == attempts:
                logger.warning(
                    "Failed to remove tree %s after %d attempts: %s",
                    raw,
                    attempts,
                    type(exc).__name__,
                )
                return False
            time.sleep(retry_delay_s * attempt)
        else:
            return True
    return False


def is_filesystem_link(path: Path) -> bool:
    """Return ``True`` when *path* is a symlink or an NTFS junction.

    ``Path.is_symlink()`` is ``False`` for junctions, so Windows callers
    that only check for symlinks can be bypassed by a junction pointing at
    the same target.  Junction probing is a no-op on POSIX
    (``Path.is_junction()`` always returns ``False`` there), so POSIX
    behaviour is identical to ``is_symlink()``.

    Args:
        path: Path to probe.  Only ``is_symlink``/``is_junction`` are used,
            so duck-typed stand-ins work in tests.

    Returns:
        ``True`` for symlinks and junctions; ``False`` otherwise (including
        unreadable paths).
    """
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is None:
            return False
        return bool(is_junction())
    except OSError:
        return False
