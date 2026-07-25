"""Regression tests for the orchestrator-liveness run-completion verdict.

This verdict decides that a run is over and its goal was not met, which turns
into a non-zero exit code on a run an operator may still be watching. Four ways
it could be wrong are pinned here, one test each.

The bias every test encodes: the verdict fires only on positive, corroborated,
sustained evidence of death, and every ambiguous reading leaves the run alone.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import bernstein.cli.run_bootstrap as rb
from bernstein.core.orchestration.bootstrap import _watchdog_check_process

# A pid high enough to be unused on any normal host. It is the value the
# adversarial review fed to both subsystems to show they disagreed about it.
DEAD_PID = 999999

#: A ``/health`` payload from a server that has no view of the spawner (no
#: ``sdd_dir`` configured), which leaves the decision to the local probe. Real
#: servers always emit the ``components`` block, so a fixture that omits it
#: entirely tests a shape that never occurs. See
#: ``test_orchestrator_liveness_false_positives.py`` for the fixture pin.
HEALTH_NO_OPINION: dict[str, Any] = {
    "agent_count": 0,
    "components": {
        "server": {"status": "ok"},
        "spawner": {"status": "unknown", "pid": None, "detail": "sdd_dir not configured"},
    },
}


def _drive_wait(
    *,
    status: dict[str, Any],
    full_counts: dict[str, Any] | None,
    health: dict[str, Any] | None = None,
    timeout_s: float = 600.0,
    tick_s: float = 2.0,
) -> tuple[dict[str, Any] | None, int]:
    """Run the real wait against a scripted server. Returns (verdict, n_polls).

    Only the server and the clock are faked. The liveness classification, the
    pidfile read and the confirmation window are the real ones, so a test that
    passes here is a statement about the shipped behaviour.

    The clock advances monotonic and wall time together, exactly as a real
    ``time.sleep`` does. Faking only one of them changes which clock the code
    under test is measured on, which is how a wall-clock confirmation window
    once passed a suite that thought it was testing a monotonic one.
    """
    health_payload = HEALTH_NO_OPINION if health is None else health
    polls = {"n": 0}

    def fake_get(path: str) -> Any:
        if path == "/status":
            polls["n"] += 1
            return status
        if path == "/health":
            return health_payload
        if path == "/tasks/counts":
            return full_counts
        return None

    elapsed = {"s": 0.0}
    base_wall, base_mono = time.time(), time.monotonic()
    clock = SimpleNamespace(
        time=lambda: base_wall + elapsed["s"],
        monotonic=lambda: base_mono + elapsed["s"],
        sleep=lambda seconds: elapsed.__setitem__("s", elapsed["s"] + tick_s),
    )

    with (
        patch.object(rb, "server_get", side_effect=fake_get),
        patch.object(rb, "time", clock),
        patch.object(rb, "_signal_orchestrator_shutdown"),
    ):
        return rb._wait_for_run_completion(timeout_s=timeout_s), polls["n"]


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime


# ---------------------------------------------------------------------------
# Finding 1: the verdict was unreachable for two of the four statuses it
# claims to cover, and the run silently passed instead.
# ---------------------------------------------------------------------------


def test_stuck_in_progress_task_is_visible_to_the_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A task stuck in ``in_progress``/``orphaned`` must reach the verdict.

    ``GET /status`` has buckets for open/claimed/done/failed/refused only. Two
    of the four statuses that mean "declared but never finished" --
    ``in_progress`` and ``orphaned`` -- have no bucket there at all, so counting
    them from that payload always yields zero however many tasks are stuck.

    The consequence was worse than a missed verdict: with open == claimed == 0
    the run also read as QUIESCENT, so a run whose only task was orphaned by a
    dead orchestrator was reported finished and healthy, and exited 0.
    """
    (_runtime(tmp_path) / "spawner.pid").write_text(str(DEAD_PID))
    monkeypatch.chdir(tmp_path)
    # Backdate nothing: the pidfile belongs to this run, so it is trustworthy.

    # What /status can say about one orphaned task: everything looks empty.
    status = {"total": 1, "open": 0, "claimed": 0, "done": 0, "failed": 0, "refused": 0}
    # What the full histogram says about the same instant.
    full_counts = {"total": 1, "open": 0, "claimed": 0, "in_progress": 0, "orphaned": 1, "done": 0, "failed": 0}

    verdict, _polls = _drive_wait(status=status, full_counts=full_counts)

    assert verdict is not None, "a task orphaned by a dead orchestrator must produce a verdict, not exit 0"
    # And the verdict must carry counts the health mapping can actually read.
    from bernstein.core.quality.retrospective import run_healthy_from_status_counts

    counts = verdict.get("task_counts", verdict)
    assert run_healthy_from_status_counts(counts) is False


def test_quiescence_does_not_fire_while_a_task_is_in_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the same hole: quiescence must not swallow the case.

    With a LIVE orchestrator and one task in ``in_progress``, open and claimed
    are both zero. The old open/claimed-only test called that quiescent and
    returned a healthy payload. It is not quiescent: a task is running.
    """
    (_runtime(tmp_path) / "spawner.pid").write_text(str(os.getpid()))  # alive
    monkeypatch.chdir(tmp_path)

    status = {"total": 1, "open": 0, "claimed": 0, "done": 0, "failed": 0, "refused": 0}
    full_counts = {"total": 1, "open": 0, "claimed": 0, "in_progress": 1, "orphaned": 0, "done": 0, "failed": 0}

    verdict, _polls = _drive_wait(status=status, full_counts=full_counts, timeout_s=60.0)

    assert verdict is None, "a running task must not be reported as a quiescent, healthy run"


# ---------------------------------------------------------------------------
# Finding 2: the verdict fired on a single poll, with nothing to outwait the
# recovery supervisor that restarts a dead orchestrator.
# ---------------------------------------------------------------------------


def test_verdict_requires_a_confirmation_window_not_one_poll(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One dead-pid reading is not evidence the run is over.

    ``bootstrap.run_watchdog`` restarts a dead orchestrator every
    ``WATCHDOG_POLL_S``, so for at least that long a dead pid is a recoverable
    state rather than a terminal one. The verdict must therefore outlast the
    supervisor's window and be confirmed across several polls, never concluded
    from the first observation.
    """
    (_runtime(tmp_path) / "spawner.pid").write_text(str(DEAD_PID))
    monkeypatch.chdir(tmp_path)

    status = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0}
    full_counts = {"total": 1, "open": 1, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 0}

    verdict, polls = _drive_wait(status=status, full_counts=full_counts, tick_s=2.0)

    assert verdict is not None, "a sustained dead orchestrator must still reach a verdict"
    assert polls > 1, f"verdict concluded after {polls} poll(s): a single observation is not evidence"
    required = getattr(rb, "_ORCHESTRATOR_GONE_CONFIRMATIONS", 0)
    window = getattr(rb, "_ORCHESTRATOR_GONE_CONFIRM_WINDOW_S", 0.0)
    assert polls >= required >= 2, (
        f"verdict concluded after {polls} poll(s); it must require at least {required} consecutive confirming polls"
    )

    from bernstein.core.orchestration.process_utils import WATCHDOG_POLL_S

    assert window > WATCHDOG_POLL_S, (
        "the confirmation window must outlast the recovery supervisor's poll period, "
        "or the verdict can win a race against a restart that was already coming"
    )
    # 2s ticks: the window itself, not just the poll count, has to have elapsed.
    assert polls * 2.0 >= window


def test_a_restart_during_the_window_cancels_the_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the supervisor restarts the orchestrator mid-window, no verdict.

    The restarted process appears under a new, live pid. That is exactly the
    observation the window exists to catch, and it must reset the streak rather
    than be averaged into it.
    """
    pidfile = _runtime(tmp_path) / "spawner.pid"
    pidfile.write_text(str(DEAD_PID))
    monkeypatch.chdir(tmp_path)

    status = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0}
    full_counts = {"total": 1, "open": 1, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 0}
    polls = {"n": 0}

    def fake_get(path: str) -> Any:
        if path == "/status":
            polls["n"] += 1
            # Second poll: the recovery supervisor restarted it.
            if polls["n"] == 2:
                pidfile.write_text(str(os.getpid()))
            return status
        if path == "/health":
            return {"agent_count": 0}
        if path == "/tasks/counts":
            return full_counts
        return None

    clock = {"t": time.time()}

    def fake_time() -> float:
        clock["t"] += 2.0
        return clock["t"]

    with (
        patch.object(rb, "server_get", side_effect=fake_get),
        patch.object(rb.time, "sleep", return_value=None),
        patch.object(rb.time, "time", side_effect=fake_time),
        patch.object(rb, "_signal_orchestrator_shutdown"),
    ):
        verdict = rb._wait_for_run_completion(timeout_s=120.0)

    assert verdict is None, "a restarted orchestrator must cancel the verdict, not be reaped through it"


# ---------------------------------------------------------------------------
# Finding 3: the documented premise ("nothing will ever advance them", "no
# pidfile means it exited and cleaned up") was not what the code did.
# ---------------------------------------------------------------------------


def test_documented_contract_matches_the_implemented_one() -> None:
    """The docstrings must describe the evidence the code actually requires.

    Two claims were false as written:

    * "Nothing will ever advance them" -- ``run_watchdog`` restarts a dead
      orchestrator, so a dead pid does not mean nothing will advance the tasks.
    * "no pidfile ... means it exited and cleaned up" -- the orchestrator never
      removes its own pidfile. Only an operator teardown does. So a missing
      pidfile is not an exit signal it can emit, and must not be read as one.
    """
    wait_doc = rb._wait_for_run_completion.__doc__ or ""
    liveness_doc = rb._orchestrator_liveness.__doc__ or ""

    assert "Nothing will\n          ever advance them" not in wait_doc, (
        "the docstring must not assert a premise the recovery supervisor contradicts"
    )
    assert "confirmation window" in wait_doc, "the docstring must document the confirmation requirement"
    assert "exited and cleaned up" not in liveness_doc, (
        "a missing pidfile is not a clean-exit signal the orchestrator emits"
    )

    from bernstein.core.orchestration.process_utils import LIVENESS_UNKNOWN, classify_pidfile_liveness

    classifier_doc = classify_pidfile_liveness.__doc__ or ""
    assert "never" in classifier_doc and "missing pidfile" in classifier_doc.lower(), (
        "the classifier must document that a missing pidfile is never death"
    )
    # And the behaviour the docstrings now claim, asserted rather than described.
    assert classify_pidfile_liveness(Path("does-not-exist.pid"))[0] == LIVENESS_UNKNOWN


def test_missing_pidfile_is_never_read_as_death(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No pidfile at all: ambiguous, so no verdict, whatever the counts say."""
    _runtime(tmp_path)  # runtime dir exists, pidfile does not
    monkeypatch.chdir(tmp_path)

    status = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0}
    full_counts = {"total": 1, "open": 1, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 0}

    verdict, _polls = _drive_wait(status=status, full_counts=full_counts, timeout_s=120.0)
    assert verdict is None


def test_stale_pidfile_from_a_previous_run_is_not_this_runs_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover pidfile predating this run must not condemn it.

    Its pid describes some earlier process, and the OS may since have handed
    that number to anything at all. Reading it as this run's death lets a file
    nobody cleaned up fail a run that has not even started.
    """
    pidfile = _runtime(tmp_path) / "spawner.pid"
    pidfile.write_text(str(DEAD_PID))
    old = time.time() - 86400.0  # a day before any CLI in this test could have started
    os.utime(pidfile, (old, old))
    monkeypatch.chdir(tmp_path)

    status = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0}
    full_counts = {"total": 1, "open": 1, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 0}

    verdict, _polls = _drive_wait(status=status, full_counts=full_counts, timeout_s=120.0)
    assert verdict is None


def test_a_blind_local_probe_is_not_enough_to_reap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLI outside the orchestrator's pid namespace must not reap it.

    The shipped image runs the orchestrator in a container, so the CLI's own
    ``os.kill`` probe reports a perfectly healthy orchestrator as dead. The task
    server shares the orchestrator's namespace and reports it running on
    ``/health``. The two observers disagree, and a disagreement is not evidence
    of death whichever way round it points.
    """
    (_runtime(tmp_path) / "spawner.pid").write_text(str(DEAD_PID))  # invisible from here
    monkeypatch.chdir(tmp_path)

    status = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0}
    full_counts = {"total": 1, "open": 1, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 0}
    health = {"agent_count": 0, "components": {"spawner": {"status": "ok", "pid": DEAD_PID}}}

    verdict, _polls = _drive_wait(status=status, full_counts=full_counts, health=health, timeout_s=120.0)
    assert verdict is None, "the server can see the orchestrator running; a blind local probe cannot reap it"

    # Corroboration in the other direction does produce a verdict.
    health_down = {"agent_count": 0, "components": {"spawner": {"status": "down"}}}
    verdict2, _polls2 = _drive_wait(status=status, full_counts=full_counts, health=health_down, timeout_s=600.0)
    assert verdict2 is not None


def test_a_recycled_pid_does_not_vouch_for_a_stale_pidfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An alive pid that is not our process must not read as our process.

    A pid is a reused integer, not an identity. This test process is alive and
    is emphatically not an orchestrator, so a pidfile naming it describes a
    number the OS handed to somebody else. Reading that as "our orchestrator is
    healthy" would let a stale pidfile vouch for a run that is not running.

    It resolves to ``unknown``, not ``gone``: the pid is provably not ours,
    which is not the same as proof that ours died.
    """
    from bernstein.core.orchestration.process_utils import (
        LIVENESS_ALIVE,
        LIVENESS_UNKNOWN,
        ORCHESTRATOR_PROCESS_MARKER,
        classify_pidfile_liveness,
    )

    pidfile = _runtime(tmp_path) / "spawner.pid"
    pidfile.write_text(str(os.getpid()))
    monkeypatch.chdir(tmp_path)

    # Without an identity expectation, any live pid passes for ours.
    assert classify_pidfile_liveness(pidfile)[0] == LIVENESS_ALIVE
    # With one, this process is correctly rejected as not-an-orchestrator.
    assert classify_pidfile_liveness(pidfile, expect_cmdline=ORCHESTRATOR_PROCESS_MARKER)[0] == LIVENESS_UNKNOWN
    # And the CLI applies the expectation.
    assert rb._orchestrator_liveness()[0] == LIVENESS_UNKNOWN


def test_a_live_orchestrator_the_server_calls_down_is_not_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other disagreement: local sees our process running, server says down.

    A REAL process whose command line identifies it as an orchestrator, against
    a server component reporting ``down``. Neither observer wins a
    disagreement, so nothing is reaped.
    """
    import subprocess
    import sys

    from bernstein.core.orchestration.process_utils import ORCHESTRATOR_PROCESS_MARKER

    # A live stand-in whose command line carries the orchestrator marker, so
    # the identity check recognises it the way it would the real subprocess.
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep(120)  # {ORCHESTRATOR_PROCESS_MARKER}"],
    )
    try:
        (_runtime(tmp_path) / "spawner.pid").write_text(str(proc.pid))
        monkeypatch.chdir(tmp_path)

        status = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0}
        full_counts = {"total": 1, "open": 1, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 0}
        health = {"agent_count": 0, "components": {"spawner": {"status": "down"}}}

        verdict, _polls = _drive_wait(status=status, full_counts=full_counts, health=health, timeout_s=120.0)
        assert verdict is None
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Finding 4: the pid the CLI classified as gone was still fed to the restart
# path, so "gone" meant two different things in the two subsystems.
# ---------------------------------------------------------------------------


def test_supervisor_and_cli_share_one_definition_of_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both subsystems must classify the same pid through the same function.

    Otherwise one can declare the run terminally over while the other is about
    to restart the very process it called gone.
    """
    pidfile = _runtime(tmp_path) / "spawner.pid"
    pidfile.write_text(str(DEAD_PID))
    monkeypatch.chdir(tmp_path)

    cli_view, cli_pid = rb._orchestrator_liveness()
    assert isinstance(cli_view, str), (
        f"the CLI must report a three-valued classification, not {cli_view!r}: a boolean "
        "forces every ambiguous reading into 'running' or 'dead', and 'dead' is destructive"
    )

    from bernstein.core.orchestration.process_utils import LIVENESS_GONE, classify_pidfile_liveness

    supervisor_view, supervisor_pid = classify_pidfile_liveness(pidfile)
    assert cli_view == supervisor_view == LIVENESS_GONE
    assert cli_pid == supervisor_pid == DEAD_PID


def _restart_calls(**kwargs: Any) -> list[int]:
    """Run one supervisor check and report whether it spawned anything."""
    calls: list[int] = []

    def restart_fn() -> int:
        calls.append(1)
        return 4242

    _watchdog_check_process(
        name="Orchestrator",
        alive_since=None,
        restarts=0,
        give_up_logged=False,
        max_restarts=5,
        reset_after_s=120.0,
        now=time.monotonic(),
        restart_fn=restart_fn,
        **kwargs,
    )
    return calls


def test_sharing_the_classifier_does_not_make_the_supervisor_withhold_recovery() -> None:
    """The shared vocabulary must not turn into a shared refusal to act.

    An earlier revision of this file asserted the opposite: that an
    unattributable pid must not spawn. That was wrong, and it regressed
    recovery against main. The two subsystems read the same classification but
    they owe it opposite duties. For the CLI, acting on a weak reading destroys
    a running run, so anything short of positive evidence must do nothing. For
    the supervisor, NOT acting destroys the run just as thoroughly and
    permanently, because nothing recreates ``spawner.pid``. So only a positive
    ``alive`` withholds a restart here.

    See ``test_orchestrator_liveness_false_positives.py`` for the doctor-path
    scenario that made the regression concrete.
    """
    from bernstein.core.orchestration.process_utils import LIVENESS_ALIVE, LIVENESS_GONE, LIVENESS_UNKNOWN

    assert _restart_calls(pid=DEAD_PID, liveness=LIVENESS_GONE) == [1], (
        "a genuinely crashed orchestrator must be restarted"
    )
    assert _restart_calls(pid=DEAD_PID, liveness=LIVENESS_UNKNOWN) == [1], (
        "an unattributable reading is not a reason to abandon a run"
    )
    assert _restart_calls(pid=None) == [1], "a missing pidfile is not a reason to abandon a run either"
    assert _restart_calls(pid=os.getpid(), liveness=LIVENESS_ALIVE) == [], (
        "a positive alive is the one reading that withholds a restart"
    )
