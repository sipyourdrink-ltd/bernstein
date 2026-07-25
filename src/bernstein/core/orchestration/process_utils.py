"""Process inspection helpers used by shutdown and supervision paths."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from bernstein.core.platform_compat import IS_WINDOWS
from bernstein.core.platform_compat import process_alive as _platform_process_alive

if TYPE_CHECKING:
    from collections.abc import Sequence

# ---------------------------------------------------------------------------
# Three-valued pidfile liveness
# ---------------------------------------------------------------------------
#
# Two subsystems act on "is the orchestrator still running": the recovery
# supervisor in ``core/orchestration/bootstrap.py`` (which restarts it) and the
# CLI completion wait in ``cli/run_bootstrap.py`` (which reports the run over).
# They must not hold different definitions of the word, so both read this one
# classifier.
#
# The classification is deliberately three-valued. A boolean forces every
# ambiguous observation into either "running" or "dead", and both callers do
# something destructive with "dead": the supervisor spawns a process, the CLI
# declares an in-flight run finished. ``unknown`` is the state that lets both
# callers do nothing, which is the correct response to an ambiguous reading.

Liveness = Literal["alive", "gone", "unknown"]

#: Poll period of the recovery supervisor (``bootstrap.run_watchdog``), which
#: restarts a dead server or orchestrator. It lives here, next to the liveness
#: classifier, because it is not only the supervisor's own tuning knob: any
#: OTHER reader that wants to conclude a dead process will stay dead has to
#: outwait it. Keeping one definition is what stops the supervisor's recovery
#: window and the CLI's confirmation window from drifting apart.
WATCHDOG_POLL_S: Final[float] = 5.0

#: The process exists (or something that cannot be distinguished from it does).
LIVENESS_ALIVE: Final[Liveness] = "alive"
#: Positive evidence of death: a pidfile this run owns, naming a dead pid.
LIVENESS_GONE: Final[Liveness] = "gone"
#: Not enough evidence to act. Never treat as death.
LIVENESS_UNKNOWN: Final[Liveness] = "unknown"

#: Substrings identifying an orchestrator process in its command line, used to
#: tell our process apart from an unrelated one that inherited a recycled pid.
#: A command line matching ANY of these is ours.
#:
#: There are three shipped launch forms and they do not share a spelling:
#:
#: 1. ``python -m bernstein.core.orchestration.orchestrator`` -- the argv built
#:    by ``core/server/server_launch.py::_start_spawner``.
#: 2. ``python .../bernstein/core/orchestration/orchestrator.py`` -- what the
#:    process becomes after its own restart. ``orchestrator_cleanup.restart``
#:    re-execs with ``os.execv(sys.executable, [sys.executable, *sys.argv])``,
#:    and the interpreter has already rewritten ``sys.argv[0]`` from the ``-m``
#:    module name to the module's file path, so the dotted form is gone. This
#:    matters most precisely when pid reuse matters most: right after a restart.
#: 3. ``python -m bernstein.core.orchestrator`` -- the ``docker-compose.yaml``
#:    entrypoint and ``docker/demo/demo-cycle.sh``, via the redirect alias.
#:
#: Missing a form is safe but costly: an unrecognised command line classifies as
#: ``LIVENESS_UNKNOWN``, which never reaps but also stops the supervisor from
#: recognising a live orchestrator.
ORCHESTRATOR_PROCESS_MARKERS: Final[tuple[str, ...]] = (
    "bernstein.core.orchestration.orchestrator",
    "bernstein/core/orchestration/orchestrator.py",
    "bernstein.core.orchestrator",
)
#: Back-compat alias for the primary (``-m``) form.
ORCHESTRATOR_PROCESS_MARKER: Final[str] = ORCHESTRATOR_PROCESS_MARKERS[0]


def cmdline_matches(cmdline: str, markers: Sequence[str]) -> bool:
    """Whether *cmdline* contains any of *markers*, separator-insensitively."""
    normalized = cmdline.replace("\\", "/")
    return any(marker.replace("\\", "/") in normalized for marker in markers)


def pid_command_line(pid: int) -> str | None:
    """Best-effort command line for *pid*, or ``None`` when it cannot be read.

    ``None`` means "could not determine" (Windows, no ``ps``, the process
    vanished mid-probe, a permissions boundary), never "no command line".
    Callers must not read it as a mismatch.
    """
    if pid <= 0 or IS_WINDOWS:
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def classify_pidfile_liveness(
    pidfile: Path,
    *,
    not_before: float | None = None,
    expect_cmdline: str | Sequence[str] | None = None,
) -> tuple[Liveness, int | None]:
    """Classify the process named by *pidfile* as alive, gone, or unknown.

    Returns ``(liveness, pid)``; ``pid`` is ``None`` whenever the pidfile could
    not be read as a positive integer.

    ``LIVENESS_GONE`` is the only value that authorises a caller to act as if
    the process will not run again, so it is returned only on positive
    evidence of death, which means all of:

    * the pidfile exists and parses to a positive pid, and
    * that pidfile is attributable to the caller's run (see ``not_before``), and
    * the pid is not a live process on this host.

    Everything else is ``LIVENESS_UNKNOWN``. In particular:

    * **A missing pidfile is never "gone".** It means the process has not
      written it yet, or an operator teardown removed it
      (``DrainCoordinator._clean_runtime`` deletes every ``*.pid``). Neither is
      death. Note that the orchestrator does NOT remove its own pidfile when it
      exits, so "pidfile gone" is not a signal it emits at all.
    * **A pidfile older than ``not_before`` is never "gone".** It was written
      before the caller's run began, so its pid describes some earlier process.
      Acting on it would let a leftover file from a previous run condemn a run
      that has not started yet.

    Both guards exist because a pid is a reused integer, not an identity:

    * ``not_before`` (a ``time.time()`` epoch, typically the caller's start)
      rejects a pidfile written before the caller's run, whose pid number may
      by now belong to anything.
    * ``expect_cmdline`` (one substring, or several of which any may match, that
      the process's command line must contain -- e.g.
      :data:`ORCHESTRATOR_PROCESS_MARKERS`) rejects the opposite recycling case:
      the pid is alive, but it is alive as some unrelated process that inherited
      the number after ours exited. Without it, a recycled pid reads as our
      healthy process and can vouch for a pidfile that is in fact stale. Pass
      every spelling the process can legitimately have -- a form that is missing
      from the list looks exactly like a recycled pid. A definitive mismatch
      yields ``LIVENESS_UNKNOWN``, not ``LIVENESS_GONE``: it proves the pid is
      not ours, which is not the same as proving ours died, and ``ps`` output is
      too weak a signal to hang a destructive decision on. When the command line
      cannot be read at all, the process is assumed to be ours -- unverifiable
      must not become an excuse to reap.

    This is a same-host check. It cannot see a process in another pid namespace
    (the shipped container image runs the orchestrator in one), so a caller that
    might be outside the process's namespace must corroborate ``LIVENESS_GONE``
    with an observer inside it before acting.
    """
    try:
        raw = pidfile.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return LIVENESS_UNKNOWN, None
    if not raw:
        return LIVENESS_UNKNOWN, None
    try:
        pid = int(raw)
    except ValueError:
        return LIVENESS_UNKNOWN, None
    if pid <= 0:
        return LIVENESS_UNKNOWN, None

    if is_process_alive(pid):
        if expect_cmdline is None:
            return LIVENESS_ALIVE, pid
        markers = (expect_cmdline,) if isinstance(expect_cmdline, str) else tuple(expect_cmdline)
        cmdline = pid_command_line(pid)
        if cmdline is None or cmdline_matches(cmdline, markers):
            return LIVENESS_ALIVE, pid
        # Alive, but not us: the number was recycled by an unrelated process.
        return LIVENESS_UNKNOWN, pid

    if not_before is not None:
        try:
            written_at = pidfile.stat().st_mtime
        except OSError:
            return LIVENESS_UNKNOWN, pid
        if written_at < not_before:
            # Leftover from an earlier run: its pid says nothing about this one.
            return LIVENESS_UNKNOWN, pid

    return LIVENESS_GONE, pid


def process_state(pid: int) -> str | None:
    """Return the OS process state string for *pid* when available.

    Uses ``ps`` on Unix.  On Windows ``ps`` is not available, so this
    always returns ``None``.
    """
    if pid <= 0:
        return None
    if IS_WINDOWS:
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    state = result.stdout.strip()
    return state or None


def is_process_alive(pid: int) -> bool:
    """Return True when *pid* exists and is not a zombie.

    Delegates the basic existence check to :func:`platform_compat.process_alive`
    which works on both Unix and Windows.  On Unix, an additional ``ps`` probe
    filters out zombie processes.
    """
    if not _platform_process_alive(pid):
        return False
    state = process_state(pid)
    return not (state is not None and state.startswith("Z"))


def list_command_lines() -> list[tuple[int, str]]:
    """Best-effort ``(pid, command)`` snapshot of local processes.

    Returns an empty list on Windows (no ``ps``) or when the probe fails, so
    callers degrade to whatever other signal they have. Used as a live
    cross-check so process-visibility surfaces can still report running
    processes after their PID files were deleted (issue #2874).
    """
    if IS_WINDOWS:
        return []
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    out: list[tuple[int, str]] = []
    for raw in result.stdout.splitlines():
        parts = raw.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        try:
            out.append((int(parts[0]), parts[1]))
        except ValueError:
            continue
    return out


def process_cwd(pid: int) -> Path | None:
    """Return the current working directory for *pid* when available."""
    if pid <= 0:
        return None
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            cwd = line[1:].strip()
            if cwd:
                return Path(cwd)
    return None
