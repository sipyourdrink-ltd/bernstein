"""Wall-clock enforcement for volunteer gate commands.

A donor lends their machine for a bounded time.  The bound has to be real: a
gate command that hangs -- waiting on a prompt, retrying a dead host, looping
on a model's bad patch -- would otherwise hold a stranger's laptop until they
notice and kill it themselves.  A volunteer program where that happens twice
has no volunteers.

Two things here are less obvious than the timeout itself.

*The kill covers the process tree, not the process.*  Gate commands spawn
children -- ``pytest -n`` forks workers, ``npm test`` shells out, a build
launches a compiler farm.  Killing only the process this module started leaves
those children running with no parent watching, which is the exact failure the
cap exists to prevent, minus the error message.  On POSIX the child is started
in its own session so the whole group can be signalled at once.

*The kill is recorded, not merely performed.*  :class:`WallClockOutcome` says
what happened -- whether the process left on its own, whether it took SIGTERM,
whether it needed SIGKILL after the grace period.  A gate that had to be killed
produces a refusal, and a refusal a maintainer can read beats a receipt that
quietly omits the run.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

#: Seconds a process gets to exit after SIGTERM before SIGKILL.
#:
#: Long enough for a test runner to flush its output and remove its temporary
#: directories, short enough that a donor is not waiting on a process that has
#: already been told to stop.
TERM_GRACE_SECONDS = 5.0

_POSIX = sys.platform != "win32"


@dataclass(frozen=True, slots=True)
class WallClockOutcome:
    """What happened to one gate command under its ceiling.

    Attributes:
        killed: Whether the cap fired.  ``False`` means the command finished on
            its own, whatever its exit code.
        exit_code: The process's exit status, or ``None`` if it was killed
            before reporting one.
        elapsed_seconds: Wall time actually consumed.
        limit_seconds: The ceiling in force.  A duration rather than a count,
            so fractional: a caller spending one budget across several commands
            has a remainder to pass on, and rounding it down at every hand-off
            loses most of a short budget while rounding it up hands out time
            the budget does not have.
        escalated_to_sigkill: Whether SIGTERM was ignored and SIGKILL followed.
            Worth recording separately: a command that needs SIGKILL is either
            wedged in uninterruptible state or trapping signals, and both are
            worth knowing about a project's gates.
    """

    killed: bool
    exit_code: int | None
    elapsed_seconds: float
    limit_seconds: float
    escalated_to_sigkill: bool

    @property
    def passed(self) -> bool:
        """A gate passes only by finishing, on time, with status zero."""
        return not self.killed and self.exit_code == 0

    def as_record(self) -> dict[str, object]:
        """The outcome as a structure a receipt or refusal can carry."""
        return {
            "killed": self.killed,
            "exit_code": self.exit_code,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "limit_seconds": self.limit_seconds,
            "escalated_to_sigkill": self.escalated_to_sigkill,
        }


def run_under_wall_clock(
    argv: Sequence[str],
    *,
    limit_seconds: float,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    grace_seconds: float = TERM_GRACE_SECONDS,
) -> tuple[WallClockOutcome, bytes, bytes]:
    """Run a gate command under a hard wall-clock ceiling.

    Executed without a shell -- ``argv`` is the command.  The manifest loader
    refuses shell strings for the same reason.

    Args:
        argv: Program and arguments.
        limit_seconds: Ceiling; exceeding it kills the process tree.
        cwd: Working directory for the command.
        env: Complete environment.  Passed through as given; building it from
            an allowlist is :func:`~bernstein.core.volunteer.sandbox_profile.sandbox_env`'s
            job, and this function deliberately does not second-guess it.
        grace_seconds: Time between SIGTERM and SIGKILL.

    Returns:
        The outcome, plus whatever the process wrote to stdout and stderr
        before it stopped.  Output captured up to the kill is kept: a hung
        gate's last lines are usually the reason it hung.
    """
    started = time.monotonic()
    process = subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=_POSIX,
    )

    try:
        stdout, stderr = process.communicate(timeout=limit_seconds)
    except subprocess.TimeoutExpired:
        escalated = _terminate_tree(process, grace_seconds=grace_seconds)
        stdout, stderr = _drain(process)
        return (
            WallClockOutcome(
                killed=True,
                exit_code=None,
                elapsed_seconds=time.monotonic() - started,
                limit_seconds=limit_seconds,
                escalated_to_sigkill=escalated,
            ),
            stdout,
            stderr,
        )

    return (
        WallClockOutcome(
            killed=False,
            exit_code=process.returncode,
            elapsed_seconds=time.monotonic() - started,
            limit_seconds=limit_seconds,
            escalated_to_sigkill=False,
        ),
        stdout,
        stderr,
    )


def _terminate_tree(process: subprocess.Popen[bytes], *, grace_seconds: float) -> bool:
    """Signal the whole process group, escalating if it does not leave.

    Returns whether SIGKILL was needed.
    """
    _signal_tree(process, signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        _signal_tree(process, signal.SIGKILL)
        with_suppressed_timeout(process)
        return True
    return False


def _signal_tree(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    """Send a signal to the child's whole group where the platform has groups.

    Guarded against the one way this could go badly wrong.  Signalling a
    process group kills every member, and if the child ever shared *our* group
    -- because ``start_new_session`` did not take, or a future edit dropped it
    -- then the group being signalled would include the orchestrator running
    this code.  A wall-clock cap that terminates the donor's whole Bernstein
    session instead of one gate is worse than no cap.  So the group is compared
    against our own before anything is sent, and a match falls back to
    signalling the child alone.

    On Windows there is no process group to signal, so the child alone is
    terminated.  That is a genuine gap rather than a hidden one: a Windows
    donor running a gate that forks can leave orphans, and the honest place to
    say so is here.
    """
    if not _POSIX:
        process.kill()
        return
    try:
        group = os.getpgid(process.pid)
        if group == os.getpgrp():
            raise _SharedProcessGroup
        os.killpg(group, sig)
    except (ProcessLookupError, PermissionError, _SharedProcessGroup):
        # Already gone, out of reach, or sharing our group: signal the child
        # alone rather than everything around it.
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(sig)


class _SharedProcessGroup(Exception):
    """The child is in our own process group; signalling it would hit us."""


def with_suppressed_timeout(process: subprocess.Popen[bytes]) -> None:
    """Reap a killed process without letting a slow reap raise.

    After SIGKILL there is nothing further to escalate to, so a wait that still
    times out is reported through the outcome rather than as an exception from
    the cleanup path.
    """
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=grace_after_sigkill())


def grace_after_sigkill() -> float:
    """Seconds to wait for the kernel to reap a SIGKILLed process."""
    return 2.0


def _drain(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    """Collect whatever the process wrote before it was stopped."""
    try:
        return process.communicate(timeout=grace_after_sigkill())
    except subprocess.TimeoutExpired:
        return b"", b""
