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

import contextlib
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
    from collections.abc import Callable, Iterator
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
        or the initial SIGTERM could not be sent.  Callers that need the
        stronger "the tree is no longer running" guarantee should use
        :func:`reap_process_group` and read
        :attr:`ProcessReapReceipt.confirmed_dead` instead - a group that
        exited on its own reports ``False`` here while still satisfying it.
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
    whether the target was already gone, whether escalation to a force-kill
    was required, and - the guarantee callers actually depend on - whether
    the target is verified to no longer be running.  Callers mirror the
    receipt into the audit chain so a failure window can be reconstructed
    offline.

    ``delivered`` answers "did we hand a stop to the OS"; it is *not* the
    success predicate.  A target that had already exited accepts no stop, so
    ``delivered`` is False while the caller's guarantee holds.  Read
    :attr:`confirmed_dead` to answer "was the target observed gone", and
    :attr:`already_gone` to tell "it exited on its own" apart from "we
    stopped it".

    Every field reports something that was observed.  None of them is
    inferred from a signal having been accepted: a stop that the OS took
    delivery of is recorded as ``delivered`` or ``escalated``, and nothing
    more is read into it.  Where the reap could not establish the target's
    state at all, every claim stays False rather than defaulting to the
    convenient answer.

    On Windows ``delivered`` is False for a target that had already exited.
    Before the pinned-handle rewrite the same case reported True, because
    the old fallback ended in a liveness re-probe that a missing pid
    satisfied.  Callers that branch on ``delivered`` see that flip; they
    fall back to a single-process kill, which is a no-op against a pid that
    no longer exists.

    Attributes:
        pgid: Process group ID (POSIX) or lead PID (Windows) targeted.
        os_name: Normalised OS name (``"linux"``, ``"macos"``, ``"windows"``).
        method: Delivery mechanism identifier
            (``"posix_process_group"`` or ``"windows_process_tree"``).
        delivered: Whether the initial graceful stop was delivered.
        escalated: Whether a force-kill was required after the grace window.
        grace_seconds: The grace window that applied to this reap.
        already_gone: Whether the target was observed to have already
            exited before any stop tier ran.  Only ever True when that was
            affirmatively observed; a target whose state could not be
            established is not "already gone", and neither is a target we
            were refused permission to look at.
        confirmed_dead: Whether the target was *observed* to no longer be
            running once the reap returned.  This is the guarantee the reap
            exists to provide, and it is only ever set from an observation:
            a group id the kernel no longer knows (ESRCH), or a Windows
            handle that left the system.  A delivered force-kill does not
            set it.  False therefore means "not established", which is not
            the same as "still running" - the independent liveness helpers
            answer that question.
    """

    pgid: int
    os_name: str
    method: str
    delivered: bool
    escalated: bool
    grace_seconds: float
    already_gone: bool = False
    confirmed_dead: bool = False

    def to_details(self) -> dict[str, object]:
        """Return the receipt as a plain dict for audit-chain payloads."""
        return {
            "pgid": self.pgid,
            "os_name": self.os_name,
            "method": self.method,
            "delivered": self.delivered,
            "escalated": self.escalated,
            "grace_seconds": self.grace_seconds,
            "already_gone": self.already_gone,
            "confirmed_dead": self.confirmed_dead,
        }


def _group_observed_gone(pgid: int) -> bool:
    """Return True only when the group is *observed* to have no members left.

    Confirmation counterpart to :func:`process_group_alive`, and it mirrors
    that function's errno handling on purpose.  ``ESRCH`` is the only reply
    that establishes anything: it means the kernel has no process group with
    this id, so nothing in it can run.  ``EPERM`` means a member exists that
    we may not signal, which is evidence the group is *alive*, not evidence
    it is gone; treating it as gone is how a receipt ends up certifying a
    process that is still running.

    Measured behaviour of an unreaped group whose members have all been
    SIGKILLed but whose parent has not yet called ``wait()``:

    * macOS: ``killpg(pgid, 0)`` raises ``EPERM``.
    * Linux: ``killpg(pgid, 0)`` succeeds.

    Neither reply distinguishes "exited but unreaped" from "running", and
    ``killpg`` offers no third answer that would, so neither is treated as
    confirmation.  The reap reports the stop it delivered and leaves
    ``confirmed_dead`` False rather than guessing; an unblockable SIGKILL
    still guarantees eventual death, but that is a guarantee about the
    future, not an observation of the present.

    Args:
        pgid: Process group ID (POSIX) or lead PID (Windows).

    Returns:
        True only if the group was observed to have no members.
    """
    killpg = getattr(os, "killpg", None)
    if IS_WINDOWS or killpg is None:
        return not process_group_alive(pgid)
    try:
        killpg(pgid, 0)
    except ProcessLookupError:
        # ESRCH: no such process group.  Nothing is left to run.
        return True
    except OSError:
        # EPERM (a member exists we may not signal) or any other errno.
        # Neither says the group stopped, so neither confirms it.
        return False
    return False


#: The pin observed the target already exited before any stop tier ran.
_PIN_TARGET_EXITED = "exited"
#: The pin observed the target running, so stop tiers may act on it.
_PIN_TARGET_RUNNING = "running"
#: The pin could not establish what the number refers to.  Stop tiers must
#: not act on it, and the receipt must claim nothing about it.
_PIN_TARGET_UNOBSERVABLE = "unobservable"


class _ReapPin:
    """Identity pin held for the duration of one reap.

    The POSIX flavour is inert on purpose.  A POSIX reap targets a process
    *group*, and the kernel does not recycle a group id while the group
    still has members, so every ``killpg`` tier already acts on the group it
    was handed.  :class:`_WinReapPin` does the real work; keeping the POSIX
    path on this no-op base means Linux and macOS reaps are byte-for-byte
    the same sequence of syscalls they were before the pin existed.

    The target state is a three-way answer, not a boolean.  "Observed
    exited" and "could not observe" are different findings and must not
    collapse into one flag: the first is a result the caller asked for, the
    second is an absence of evidence.  A receipt that reports the second as
    the first certifies a process nobody looked at.
    """

    target_state: str = _PIN_TARGET_RUNNING

    @property
    def already_gone(self) -> bool:
        """True only when the target was *observed* gone before any tier ran."""
        return self.target_state == _PIN_TARGET_EXITED

    @property
    def signalable(self) -> bool:
        """True when a stop tier may act on this target.

        False both for a target already observed gone (nothing to stop) and
        for a target whose identity could not be established (signalling it
        could hit a process the caller never asked us to touch).
        """
        return self.target_state == _PIN_TARGET_RUNNING

    def observed_gone(self, pgid: int) -> bool:
        """Return True only when the target is observed to no longer exist.

        Args:
            pgid: Process group ID (POSIX) or lead PID (Windows).

        Returns:
            True if the observation succeeded and found nothing.
        """
        return _group_observed_gone(pgid)

    def confirm_exit(self, pgid: int, *, force_signal_delivered: bool) -> bool:
        """Return True only when the target is *observed* no longer running.

        ``force_signal_delivered`` is deliberately not treated as
        confirmation.  SIGKILL cannot be caught, blocked, or ignored, so it
        guarantees the group dies - but ``killpg(pgid, sig)`` succeeds when
        it could signal *any one* member, so a delivered SIGKILL does not
        establish that every member received it, and it says nothing about
        the instant the receipt is written.  A guarantee about the future is
        not an observation of the present, and only the observation is
        reported.

        Args:
            pgid: Process group ID (POSIX) or lead PID (Windows).
            force_signal_delivered: Whether an unblockable force-kill was
                accepted for the group.  Recorded by the caller as
                ``escalated``; not evidence of death.

        Returns:
            True if the target was observed to no longer be running.
        """
        del force_signal_delivered
        return self.observed_gone(pgid)

    def close(self) -> None:
        """Release any OS resource the pin holds.  No-op on POSIX."""


class _WinReapPin(_ReapPin):
    """An open Windows process handle held for the duration of one reap.

    Windows recycles pids aggressively - a busy runner can hand the same
    number to an unrelated process within seconds.  Any reap that
    re-resolves a bare pid between tiers can therefore end up signalling, or
    reporting on, a process it was never asked to touch, and force-killing
    someone else's process is far worse than a mis-reported receipt.

    While *any* handle to a process object is open the kernel will not reuse
    that pid, so opening one handle up front and holding it until the reap
    returns is the pid-reuse guard: for the whole reap window the number
    either refers to the process we were asked to reap, or to nothing.

    Two residual gaps are handled explicitly rather than hidden:

    * A pid can be recycled *before* the pin is taken.  The pin therefore
      also reads the target's creation time and refuses to treat a process
      that started after the reap request as the reap target - a process
      born after the caller asked us to stop pid N cannot be the process the
      caller meant.  Call sites that hold a live ``Popen`` for the child
      (every adapter spawn path) have no window at all: the parent's own
      handle already pins the pid.
    * ``TerminateProcess`` is asynchronous, so a liveness probe fired
      straight after a kill can still observe ``STILL_ACTIVE`` on a process
      that is already on its way out.  :meth:`confirm_exit` waits on the
      handle instead of racing the probe.

    Whether the handle could be opened is not the same question as whether
    the target is gone.  ``OpenProcess`` refuses both for a pid no process
    has (``ERROR_INVALID_PARAMETER``) and for a pid held by a process this
    user may not touch (``ERROR_ACCESS_DENIED``).  Only the first is
    evidence of an exit; the second is a live process we cannot observe, and
    the pin reports it as such so no tier signals it and no receipt claims
    it stopped.
    """

    def __init__(self, pid: int, handle: int, *, target_state: str) -> None:
        self.pid = pid
        self._handle = handle
        self.target_state = target_state

    def alive(self) -> bool:
        """Return True when the pinned process has not exited yet."""
        return bool(self._handle) and _win_handle_alive(self._handle)

    def observed_exited(self) -> bool:
        """Return True only when the handle *reports* the process exited.

        Distinct from ``not alive()``: a handle whose exit code cannot be
        read is not alive, but it is not observed exited either.
        """
        return bool(self._handle) and _win_handle_liveness(self._handle) is False

    def observed_gone(self, pgid: int) -> bool:
        """Return the pinned handle's own verdict, not a re-probe of the pid.

        ``_win_process_alive`` cannot open a pid it lacks rights to and
        reports it as not running, so re-probing the number would turn
        "could not observe" back into "observed gone" one layer down.  The
        handle already names the target.

        Args:
            pgid: Lead PID of the reaped tree (unused).

        Returns:
            True if the handle reports the process exited.
        """
        del pgid
        return self.observed_exited()

    def terminate(self, exit_code: int = 1) -> bool:
        """Terminate the pinned process through the handle we already hold."""
        return bool(self._handle) and _win_terminate_handle(self._handle, exit_code)

    def wait_for_exit(self, timeout_ms: int) -> bool:
        """Block until the pinned process exits or *timeout_ms* elapses."""
        return bool(self._handle) and _win_wait_for_exit(self._handle, timeout_ms)

    def confirm_exit(self, pgid: int, *, force_signal_delivered: bool) -> bool:
        """Wait on the pinned handle for real evidence that the target left.

        Windows has an observation available that POSIX does not: the handle
        becomes signalled when the process leaves the system.  That is the
        only thing accepted here.  A force-kill request is not, and neither
        is a re-probe of the bare pid - ``_win_process_alive`` cannot open a
        pid it lacks rights to and reports it as not running, which is the
        same "could not observe read as observed gone" mistake one layer
        down.

        Args:
            pgid: Lead PID of the reaped tree (unused: the pinned handle
                names the target more precisely than the number does).
            force_signal_delivered: Whether a force-kill was delivered
                (unused: the handle wait is the evidence).

        Returns:
            True if the target was observed to leave the system.
        """
        del pgid, force_signal_delivered
        if self.wait_for_exit(_WIN_EXIT_CONFIRM_MS):
            return True
        return self.observed_exited()

    def close(self) -> None:
        """Close the pinned handle, releasing the pid back to the kernel."""
        if not self._handle:
            return
        handle, self._handle = self._handle, 0
        try:
            _win_kernel32().CloseHandle(handle)
        except Exception as exc:  # pragma: no cover - teardown must not raise
            logger.debug("Could not close reap pin for PID %d: %s", self.pid, exc)


@contextlib.contextmanager
def _pin_reap_target(pgid: int) -> Iterator[_ReapPin]:
    """Yield an identity pin for *pgid*, held until the reap finishes.

    Args:
        pgid: Process group ID (POSIX) or lead PID (Windows).

    Yields:
        A :class:`_ReapPin` - inert on POSIX, a pinned process handle on
        Windows.  Never raises: a pin that cannot be established degrades to
        the inert base behaviour so the guard can never break a reap.
    """
    pin: _ReapPin = _ReapPin()
    if IS_WINDOWS:
        try:
            pin = _win_pin_process(pgid) or pin
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Could not pin PID %d for reap: %s", pgid, exc)
    try:
        yield pin
    finally:
        pin.close()


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

    The guarantee is "the target is no longer running", reported as
    :attr:`ProcessReapReceipt.confirmed_dead`.  A target that had already
    exited before the reap ran satisfies that guarantee even though no stop
    could be delivered to it, so ``delivered`` and ``confirmed_dead`` are
    reported separately instead of being collapsed into one flag.

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

    def _receipt(
        *,
        delivered: bool,
        escalated: bool,
        already_gone: bool = False,
        confirmed_dead: bool = False,
    ) -> ProcessReapReceipt:
        return ProcessReapReceipt(
            pgid=pgid,
            os_name=os_name,
            method=method,
            delivered=delivered,
            escalated=escalated,
            grace_seconds=grace_seconds,
            already_gone=already_gone,
            confirmed_dead=confirmed_dead,
        )

    if pgid <= 0:
        return _receipt(delivered=False, escalated=False)

    # Pin the target's identity for the whole reap.  On Windows that is an
    # open kernel handle, which stops the kernel recycling the pid while any
    # tier below is running, so no tier can signal - or report on - a process
    # that merely inherited the number.  Inert on POSIX (see _ReapPin).
    with _pin_reap_target(pgid) as pin:
        if pin.already_gone:
            # Nothing to stop.  The tree the caller asked about was observed
            # not running, which is exactly the guarantee they wanted, so
            # this is a success even though no stop was delivered.
            return _receipt(
                delivered=False,
                escalated=False,
                already_gone=True,
                confirmed_dead=True,
            )

        if not pin.signalable:
            # The number could not be resolved to a process we are allowed
            # to observe.  Signalling it could hit a process the caller
            # never asked about, so no tier runs; and not knowing is not
            # proof of an exit, so the receipt claims neither.
            logger.warning(
                "Reap target %d could not be identified; no stop was attempted and no outcome is claimed",
                pgid,
            )
            return _receipt(delivered=False, escalated=False)

        # Best-effort TERM first.
        if not kill_process_group(pgid, signal.SIGTERM):
            # The stop could not be delivered.  That is only a reap failure
            # when the target is still running; a target that has already
            # exited is the outcome the caller asked for.  Observe rather
            # than assume, and ask the pin (which already names the target)
            # instead of re-probing the bare number.
            gone = pin.observed_gone(pgid)
            return _receipt(
                delivered=False,
                escalated=False,
                already_gone=gone,
                confirmed_dead=gone,
            )

        # Poll for graceful exit.  Probe the whole process *group* rather than
        # the lead PID: the session leader can reap while a child it spawned
        # keeps the group alive, and a lead-PID-only check (``process_alive``)
        # would then declare the group dead the moment the leader exits and
        # skip the SIGKILL escalation the surviving tree still needs (#2643).
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not process_group_alive(pgid):
                return _receipt(delivered=True, escalated=False, confirmed_dead=True)
            time.sleep(poll_interval)

        # Group still alive after grace period - escalate.
        if not process_group_alive(pgid):
            # Exited right on the grace boundary; no force tier needed.
            return _receipt(delivered=True, escalated=False, confirmed_dead=True)
        logger.warning(
            "Process group %d did not exit within %.1fs of SIGTERM; sending SIGKILL",
            pgid,
            grace_seconds,
        )
        kill_sig = signal.SIGKILL if is_signal_supported("SIGKILL") else 9
        forced = kill_process_group(pgid, kill_sig)
        return _receipt(
            delivered=True,
            escalated=True,
            confirmed_dead=pin.confirm_exit(pgid, force_signal_delivered=forced),
        )


def process_alive(pid: int | None) -> bool:
    """Check whether a process is still running, cross-platform.

    On Unix, uses ``os.kill(pid, 0)`` (signal 0 = existence check).
    On Windows, uses ``ctypes`` to call ``OpenProcess`` and then
    ``GetExitCodeProcess`` to distinguish live from zombie processes.

    Args:
        pid: Process ID to check. ``None`` is accepted and answers False:
            several callers read a PID out of session state or a runtime JSON
            file where it is legitimately absent (a session running on a
            remote runtime bridge has no local PID), and "there is no process
            here" is the same answer as for a zero or negative PID.

    Returns:
        True if the process exists and is running.
    """
    if pid is None or pid <= 0:
        return False

    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    # Windows: use ctypes kernel32 calls
    return _win_process_alive(pid)


def process_group_alive(pgid: int) -> bool:
    """Check whether *any* member of a process group is still running.

    On POSIX, probes the whole group with ``os.killpg(pgid, 0)`` (signal 0 =
    existence check): a surviving child keeps the group alive even after the
    group leader (the lead PID) has already been reaped.  :func:`process_alive`
    only tracks the lead PID, so a reap path that used it as the liveness proxy
    would report the group dead the moment the leader exits and skip the
    SIGKILL escalation the surviving children still need (issue #2643).

    On Windows there are no POSIX process groups, so this falls back to the
    lead-PID liveness check; the Windows reap path terminates the whole tree
    via a Job Object / ``taskkill /T`` keyed on that PID.

    Args:
        pgid: Process group ID (on Unix) or lead PID (on Windows).

    Returns:
        True if the group (POSIX) or the lead process (Windows) is running.
    """
    if pgid <= 0:
        return False

    if IS_WINDOWS:
        return process_alive(pgid)

    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        # ESRCH: the group has no members left.
        return False
    except PermissionError:
        # EPERM: a process exists but we may not signal it - the group is alive.
        return True
    except OSError:
        return False
    return True


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

# Access rights the reap pin asks for.  PROCESS_QUERY_LIMITED_INFORMATION is
# the minimum needed to read liveness and creation time,
# _WIN_PROCESS_TERMINATE lets us stop the target through the same handle, and
# SYNCHRONIZE lets us *wait* for it to leave the system instead of racing a
# liveness probe.
_WIN_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WIN_SYNCHRONIZE = 0x00100000

#: ``GetExitCodeProcess`` sentinel for "this process has not exited yet".
_WIN_STILL_ACTIVE = 259

#: ``WaitForSingleObject`` return meaning "the handle was signalled".
_WIN_WAIT_OBJECT_0 = 0x0

#: How long a reap waits for a terminated process to actually leave the
#: system before calling the stop unconfirmed.  ``TerminateProcess`` is
#: asynchronous, so a probe fired immediately after it can still observe
#: ``STILL_ACTIVE`` on a process that is already on its way out.
_WIN_EXIT_CONFIRM_MS = 2000

#: ``taskkill`` exit code for "the process is not running".
_WIN_TASKKILL_NOT_FOUND = 128

#: Offset between the FILETIME epoch (1601-01-01 UTC) and the Unix epoch.
_WIN_FILETIME_EPOCH_OFFSET_S = 11644473600.0

#: FILETIME resolution: 100 ns ticks per second.
_WIN_FILETIME_TICKS_PER_S = 10_000_000.0

#: ``OpenProcess`` last-error for "no process has this id".  This is the
#: only refusal that is evidence the target exited.
_WIN_ERROR_INVALID_PARAMETER = 87

#: ``OpenProcess`` last-error for "the pid is in use by a process you may
#: not touch".  Evidence the target is *alive* and unobservable, never
#: evidence that it is gone.
_WIN_ERROR_ACCESS_DENIED = 5


def _win_last_error(kernel32: Any) -> int:
    """Return ``GetLastError()``, or 0 when it cannot be read.

    Args:
        kernel32: The kernel32 handle the failing call was made through.

    Returns:
        The Win32 last-error code, or 0 when unavailable.  0 is not a real
        failure code, so callers treat it as "unknown" and stay
        conservative.
    """
    try:
        return int(kernel32.GetLastError())
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("GetLastError unavailable: %s", exc)
        return 0


def _win_open_process(pid: int, access: int) -> tuple[int | None, int]:
    """Return an open kernel handle to *pid* and the refusal reason.

    Args:
        pid: Process ID.
        access: ``OpenProcess`` desired-access mask.

    Returns:
        ``(handle, last_error)``.  ``handle`` is a handle value; ``0`` when
        ``OpenProcess`` itself refused; ``None`` when the call could not be
        made at all.  The two are kept apart deliberately: a missing
        kernel32 is an environment failure and says nothing about the
        target, whereas a refused open is evidence about the pid - and
        *which* evidence depends on ``last_error``, so it is returned rather
        than discarded.
    """
    try:
        kernel32 = _win_kernel32()
    except Exception as exc:
        logger.debug("kernel32 unavailable while opening PID %d: %s", pid, exc)
        return None, 0
    try:
        handle = int(kernel32.OpenProcess(access, False, pid))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("OpenProcess failed for PID %d: %s", pid, exc)
        return None, 0
    return handle, (0 if handle else _win_last_error(kernel32))


def _win_handle_liveness(handle: int) -> bool | None:
    """Return whether the process behind *handle* is still running.

    Args:
        handle: An open process handle with query rights.

    Returns:
        True when the process is running, False when it has exited, and
        None when the state could not be read at all.  The third answer
        exists so an unreadable handle is never mistaken for an exit.
    """
    import ctypes
    import ctypes.wintypes

    try:
        exit_code = ctypes.wintypes.DWORD()
        if not _win_kernel32().GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        return bool(exit_code.value == _WIN_STILL_ACTIVE)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("GetExitCodeProcess failed: %s", exc)
        return None


def _win_handle_alive(handle: int) -> bool:
    """Return True when the process behind *handle* has not exited yet.

    An unreadable handle is reported as not alive so no caller keeps
    waiting on it.  Callers that need to tell "exited" from "could not
    read" must use :func:`_win_handle_liveness` instead.
    """
    return _win_handle_liveness(handle) is True


def _win_handle_start_time(handle: int) -> float | None:
    """Return the creation time of *handle*'s process as a Unix timestamp.

    Args:
        handle: An open process handle with query rights.

    Returns:
        Seconds since the Unix epoch, or None when the time is unavailable.
    """
    import ctypes
    import ctypes.wintypes

    try:
        creation = ctypes.wintypes.FILETIME()
        exited = ctypes.wintypes.FILETIME()
        kernel = ctypes.wintypes.FILETIME()
        user = ctypes.wintypes.FILETIME()
        ok = _win_kernel32().GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("GetProcessTimes failed: %s", exc)
        return None
    return ticks / _WIN_FILETIME_TICKS_PER_S - _WIN_FILETIME_EPOCH_OFFSET_S


def _win_wait_for_exit(handle: int, timeout_ms: int) -> bool:
    """Block until *handle*'s process exits or *timeout_ms* elapses.

    ``TerminateProcess`` only *requests* termination, so this is what turns
    "we asked it to die" into "it is gone" without a polling race.

    Args:
        handle: An open process handle with ``SYNCHRONIZE`` rights.
        timeout_ms: Maximum time to wait, in milliseconds.

    Returns:
        True if the process exited within the timeout.
    """
    try:
        return int(_win_kernel32().WaitForSingleObject(handle, timeout_ms)) == _WIN_WAIT_OBJECT_0
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("WaitForSingleObject failed: %s", exc)
        return False


def _win_terminate_handle(handle: int, exit_code: int = 1) -> bool:
    """Terminate the process behind *handle*.

    Takes a handle rather than a pid on purpose: the handle already names
    one specific process object, so this can never hit a different process
    that inherited the same pid number.

    Args:
        handle: An open process handle with terminate rights.
        exit_code: Exit code to report for the terminated process.

    Returns:
        True if the terminate request was accepted.
    """
    try:
        return bool(_win_kernel32().TerminateProcess(handle, exit_code))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("TerminateProcess failed: %s", exc)
        return False


def _win_pin_process(pid: int) -> _WinReapPin | None:
    """Open and return a pid-reuse pin for *pid* (Windows only).

    Asks for terminate + synchronize + query rights so the whole reap can be
    driven through the one handle, and degrades to query-only rights rather
    than giving up the guard.

    Args:
        pid: Process ID to pin.

    Returns:
        A :class:`_WinReapPin`, or None when the kernel boundary is not
        callable at all.  None means "guard unavailable", never "target
        gone" - callers fall back to their pre-pin behaviour instead of
        treating an unreadable environment as proof about the process.
    """
    if pid <= 0:
        # There is no such pid to begin with, on any Windows build.
        return _WinReapPin(pid, 0, target_state=_PIN_TARGET_EXITED)
    requested_at = time.time()
    full = _WIN_PROCESS_QUERY_LIMITED_INFORMATION | _WIN_PROCESS_TERMINATE | _WIN_SYNCHRONIZE
    handle, last_error = _win_open_process(pid, full)
    if handle is None:
        return None
    if not handle:
        # Terminate rights are the likeliest thing to be refused; retry with
        # the query-only rights the identity check actually needs before
        # concluding anything about the pid.
        handle, last_error = _win_open_process(pid, _WIN_PROCESS_QUERY_LIMITED_INFORMATION)
        if handle is None:
            return None
    if not handle:
        # OpenProcess refused.  Which refusal it was decides what we may
        # report.  ERROR_INVALID_PARAMETER means no process holds this pid,
        # which is an observed exit.  ERROR_ACCESS_DENIED means one does and
        # we may not touch it, which is an observed *live* process we cannot
        # follow - reporting that as gone would certify a running process.
        # Any other code is simply unknown.  Only the first is an exit.
        if last_error == _WIN_ERROR_INVALID_PARAMETER:
            return _WinReapPin(pid, 0, target_state=_PIN_TARGET_EXITED)
        logger.warning(
            "PID %d could not be opened for reap (error %d); it is not signalled and its state is not claimed",
            pid,
            last_error,
        )
        return _WinReapPin(pid, 0, target_state=_PIN_TARGET_UNOBSERVABLE)

    liveness = _win_handle_liveness(handle)
    if liveness is None:
        # The handle is open but will not answer.  That is an absence of
        # evidence, not an exit.  Keep it open so the pid stays pinned, and
        # report a state no tier will act on and no receipt will claim.
        logger.warning(
            "PID %d has an open handle whose exit state cannot be read; "
            "it is not signalled and its state is not claimed",
            pid,
        )
        return _WinReapPin(pid, handle, target_state=_PIN_TARGET_UNOBSERVABLE)
    if not liveness:
        return _WinReapPin(pid, handle, target_state=_PIN_TARGET_EXITED)

    pin = _WinReapPin(pid, handle, target_state=_PIN_TARGET_RUNNING)
    start_time = _win_handle_start_time(handle)
    if start_time is not None and start_time > requested_at:
        # This process started after we were asked to reap the pid, so it
        # cannot be the process the caller meant - the number was recycled,
        # or the clock stepped backwards under us.  Either way the process
        # we can see is not the target, and the target's own state is
        # therefore unknown.  Drop the pin without signalling it, and claim
        # nothing: "this is not the process you asked about" is not the same
        # finding as "the process you asked about has exited".
        logger.warning(
            "PID %d does not identify the reap target (the process holding it started "
            "%.3fs after the request); refusing to signal it and claiming no outcome",
            pid,
            start_time - requested_at,
        )
        pin.close()
        return _WinReapPin(pid, 0, target_state=_PIN_TARGET_UNOBSERVABLE)
    return pin


def _win_taskkill(pid: int, *, force: bool = False, tree: bool = False) -> bool:
    """Stop a Windows process (optionally its whole tree), pid-reuse safe.

    Pins the pid with an open handle first, so neither the ``taskkill`` tree
    stop nor the terminate fallback can act on a process that merely
    inherited the number.  The fallback terminates through the pinned handle
    rather than re-resolving the pid, and confirms the exit by waiting on the
    handle instead of racing an asynchronous ``TerminateProcess`` with a
    liveness probe.

    Args:
        pid: Process ID.
        force: If True, adds ``/F`` (force terminate).
        tree: If True, adds ``/T`` (kill child processes).

    Returns:
        True if this call stopped the process.  False when the process was
        already gone (matching the POSIX ``os.kill`` semantics
        :func:`kill_process` documents) or could not be stopped.
    """
    pin = _win_pin_process(pid)
    if pin is None:
        # The guard could not be established.  Run the tree stop alone
        # instead of reaching for a fallback that force-kills a bare pid we
        # cannot prove is still the process we were asked to stop.
        return _win_run_taskkill(pid, force=force, tree=tree) == 0
    try:
        if not pin.signalable:
            # Either the target was observed gone (nothing to stop) or its
            # identity could not be established (must not be stopped).
            # Neither delivered a stop, which is what this bool reports.
            return False
        return _win_stop_pinned(pin, force=force, tree=tree)
    finally:
        pin.close()


def _win_run_taskkill(pid: int, *, force: bool, tree: bool) -> int:
    """Run ``taskkill`` for *pid* and return its exit code.

    Args:
        pid: Process ID to target.
        force: If True, adds ``/F`` (force terminate).
        tree: If True, adds ``/T`` (kill child processes).

    Returns:
        The ``taskkill`` exit code, or -1 when it could not be run.
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
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("taskkill failed for PID %d: %s", pid, exc)
        return -1
    return int(result.returncode)


def _win_stop_pinned(pin: _WinReapPin, *, force: bool, tree: bool) -> bool:
    """Stop the process behind an established pin.

    Args:
        pin: An open pin whose target was alive when it was taken.
        force: If True, adds ``/F`` (force terminate) to ``taskkill``.
        tree: If True, adds ``/T`` (kill child processes) to ``taskkill``.

    Returns:
        True if the target is confirmed stopped.
    """
    pid = pin.pid
    # taskkill is the only cheap way to reach the *tree*; the pin holds the
    # lead pid steady while it runs, so /T resolves children of the process
    # we meant and not of a namesake.
    returncode = _win_run_taskkill(pid, force=force, tree=tree)
    if returncode == 0:
        # taskkill accepted the stop.  Return without waiting: the grace
        # window belongs to the caller, and the reap's own poll loop is what
        # decides whether the tree needs the force tier.
        return True
    if returncode == _WIN_TASKKILL_NOT_FOUND:
        logger.debug("taskkill reports PID %d is not running", pid)

    # taskkill refuses to stop a console process without /F, and cannot stop
    # one at all when the caller lacks rights it needs.  Terminate the lead
    # through the handle we already hold: no pid lookup, so no chance of
    # hitting an unrelated process, and no external interpreter to time out.
    if pin.terminate():
        return pin.wait_for_exit(_WIN_EXIT_CONFIRM_MS)
    # The terminate request itself was refused.  Report a stop only if the
    # handle affirmatively says the process exited anyway; a handle that
    # will not answer is not an exit.
    return pin.observed_exited()


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
