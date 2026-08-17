"""The wall-clock cap kills real processes, so these tests spawn real processes.

A timeout tested against a mock proves the mock was configured, not that a
donor's laptop gets released.  Every test here starts an actual interpreter,
lets the cap fire or not, and inspects what the operating system did.

The limits are seconds rather than minutes so the file stays fast; the code
path is the same one a 30-minute gate takes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from bernstein.core.volunteer.wall_clock import (
    WallClockOutcome,
    run_under_wall_clock,
)

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


def _python(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def test_a_command_that_finishes_in_time_is_not_killed() -> None:
    outcome, stdout, _ = run_under_wall_clock(_python("print('gate ok')"), limit_seconds=10)

    assert outcome.killed is False
    assert outcome.exit_code == 0
    assert outcome.passed is True
    assert b"gate ok" in stdout


def test_a_failing_gate_is_a_failure_not_a_kill() -> None:
    """The two have to stay distinguishable: one is the project's verdict on
    the patch, the other is the donor's machine being taken back."""
    outcome, _, _ = run_under_wall_clock(_python("raise SystemExit(3)"), limit_seconds=10)

    assert outcome.killed is False
    assert outcome.exit_code == 3
    assert outcome.passed is False


def test_a_hanging_gate_is_killed_at_the_cap() -> None:
    """The property the cap exists for."""
    started = time.monotonic()

    outcome, _, _ = run_under_wall_clock(_python("import time; time.sleep(60)"), limit_seconds=1)

    assert outcome.killed is True
    assert outcome.exit_code is None
    assert outcome.passed is False
    assert time.monotonic() - started < 20


def test_a_fractional_ceiling_is_honoured_rather_than_rounded() -> None:
    """A caller spending one budget across several commands has a remainder.

    Rounding it down at every hand-off loses most of a short budget to the
    rounding; rounding it up hands out time the budget does not have.  So the
    ceiling is a duration, and a sub-second one has to actually fire -- an
    implementation that truncated to whole seconds would turn 0.5 into 0 or 1
    and this test would notice either way.
    """
    started = time.monotonic()

    outcome, _, _ = run_under_wall_clock(_python("import time; time.sleep(60)"), limit_seconds=0.5, grace_seconds=0.5)

    assert outcome.killed is True
    assert outcome.limit_seconds == 0.5
    assert outcome.elapsed_seconds >= 0.5
    assert time.monotonic() - started < 20


def test_the_kill_is_recorded_with_the_limit_that_fired() -> None:
    """A killed run produces a refusal, and a refusal has to say why.

    Without the limit in the record a maintainer reading it cannot tell a gate
    that is genuinely slow from a donor whose budget was set too low.
    """
    outcome, _, _ = run_under_wall_clock(_python("import time; time.sleep(60)"), limit_seconds=1)
    record = outcome.as_record()

    assert record["killed"] is True
    assert record["limit_seconds"] == 1
    assert record["exit_code"] is None
    assert isinstance(record["elapsed_seconds"], float)
    assert record["elapsed_seconds"] >= 1


def test_output_written_before_the_kill_is_kept() -> None:
    """A hung gate's last lines are usually the reason it hung."""
    script = "import sys, time; print('starting the gate'); sys.stdout.flush(); time.sleep(60)"

    _, stdout, _ = run_under_wall_clock(_python(script), limit_seconds=1)

    assert b"starting the gate" in stdout


@POSIX_ONLY
def test_a_gate_that_traps_sigterm_is_escalated_to_sigkill() -> None:
    """Recorded separately because it says something about the project's gates.

    A command that needs SIGKILL is trapping signals or stuck uninterruptibly,
    and either is worth a maintainer knowing.
    """
    script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"

    outcome, _, _ = run_under_wall_clock(_python(script), limit_seconds=1, grace_seconds=0.5)

    assert outcome.killed is True
    assert outcome.escalated_to_sigkill is True


@POSIX_ONLY
def test_the_kill_reaches_children_the_gate_spawned() -> None:
    """The failure the cap exists to prevent, minus the error message.

    ``pytest -n`` forks workers, ``npm test`` shells out, a build launches a
    compiler farm.  Killing only the process we started leaves those running on
    a stranger's machine with nothing watching them.

    The child writes its own pid to a file, then sleeps far longer than the
    cap.  After the kill, that pid must be gone.
    """
    marker = "child.pid"
    script = (
        "import os, time, sys, subprocess\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({marker!r}, 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    )

    import tempfile

    with tempfile.TemporaryDirectory() as workdir:
        outcome, _, _ = run_under_wall_clock(_python(script), limit_seconds=2, cwd=workdir, grace_seconds=0.5)
        child_pid = int((__import__("pathlib").Path(workdir) / marker).read_text())

    assert outcome.killed is True
    _assert_process_is_gone(child_pid)


def _assert_process_is_gone(pid: int, *, attempts: int = 40) -> None:
    """Poll rather than assert once: reaping is not instantaneous."""
    for _ in range(attempts):
        if not _process_alive(pid):
            return
        time.sleep(0.05)
    pytest.fail(f"pid {pid} survived the wall-clock kill; the gate's children outlived the cap")


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # On Linux a killed-but-unreaped child is a zombie, which still answers
    # signal 0.  Ask the process table what state it is in.
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    try:
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return status.stdout.strip().startswith("Z")


@POSIX_ONLY
def test_a_child_sharing_our_process_group_is_signalled_alone() -> None:
    """The guard against the cap taking the orchestrator down with the gate.

    Signalling a process group hits every member.  If the child ever ended up
    in *our* group -- ``start_new_session`` not taking, or a future edit
    dropping it -- then a group signal would include the Bernstein session
    running the task.  A wall-clock cap that kills the donor's whole session
    instead of one gate is worse than no cap at all.

    Here the child is deliberately started in the parent's group, and the test
    asserts the parent survived long enough to make the assertion.
    """
    from bernstein.core.volunteer import wall_clock

    process = subprocess.Popen(
        _python("import time; time.sleep(30)"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=False,
    )
    assert os.getpgid(process.pid) == os.getpgrp(), "precondition: the child shares our group"

    try:
        wall_clock._signal_tree(process, __import__("signal").SIGKILL)
        process.wait(timeout=10)
    finally:
        if process.poll() is None:  # pragma: no cover - only on an unexpected survival
            process.kill()
            process.wait(timeout=10)

    assert process.returncode is not None
    assert os.getpid() > 0, "the test process survived signalling a shared group"


def test_outcome_reports_a_passing_gate_only_when_everything_went_right() -> None:
    """``passed`` is the single thing a runner branches on, so it has to be
    unambiguous: finished, on time, status zero."""
    assert WallClockOutcome(False, 0, 1.0, 60, False).passed is True
    assert WallClockOutcome(False, 1, 1.0, 60, False).passed is False
    assert WallClockOutcome(True, None, 60.0, 60, False).passed is False
    assert WallClockOutcome(True, 0, 60.0, 60, True).passed is False
