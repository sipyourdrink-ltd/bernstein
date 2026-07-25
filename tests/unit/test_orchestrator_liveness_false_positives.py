"""False-positive regressions for the orchestrator-liveness verdict.

Companion to ``test_orchestrator_liveness_verdict.py``. That file pins the
cases where the verdict must FIRE; this one pins the cases where it must not,
each of which shipped as a defect and was reproduced against the real code.

Two harness rules, because breaking either is what let the first round of
defects through a green suite:

* **The clock fakes monotonic and wall together.** A real ``time.sleep(s)``
  advances both. A harness that advances only one silently changes which of the
  two the code under test is measuring.
* **``/health`` fixtures carry the ``components`` block.** A real server always
  emits it (``status_dashboard._health_components``). Omitting it is what made
  the stale-pidfile guard look alive when it was being overridden.
  ``test_health_fixture_matches_the_real_server`` pins that shape against the
  real producer so the fixtures cannot drift back.
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
from bernstein.core.orchestration.process_utils import (
    LIVENESS_ALIVE,
    LIVENESS_GONE,
    LIVENESS_UNKNOWN,
    classify_pidfile_liveness,
)


def _markers() -> tuple[str, ...]:
    """Every command line an orchestrator process can legitimately have.

    Imported at point of use: a tree that only knows the single ``-m`` spelling
    should fail the tests that depend on the others behaviourally, not take the
    whole module out with a collection error.
    """
    from bernstein.core.orchestration import process_utils

    return getattr(process_utils, "ORCHESTRATOR_PROCESS_MARKERS", (process_utils.ORCHESTRATOR_PROCESS_MARKER,))


DEAD_PID = 999999

STATUS_OPEN = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0}
FULL_OPEN = {"total": 1, "open": 1, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 0}


def _health(spawner_status: str, *, agent_count: int = 0) -> dict[str, Any]:
    """A ``/health`` payload shaped like the real one. See the module docstring."""
    return {
        "status": "ok",
        "agent_count": agent_count,
        "components": {
            "server": {"status": "ok"},
            "spawner": {"status": spawner_status, "pid": DEAD_PID, "detail": "process not found"},
            "database": {"status": "ok", "type": "TaskStore", "detail": ""},
            "agents": {"status": "ok", "active": agent_count, "detail": "no active agents"},
        },
    }


HEALTH_DOWN = _health("down")


class _Clock:
    """A fake clock where monotonic and wall advance together, as they really do.

    ``step_wall`` moves ONLY the wall clock, which is what an NTP correction, a
    container clock sync, or a laptop resume does to a running process.
    """

    def __init__(self) -> None:
        self.elapsed = 0.0
        self.wall_skew = 0.0
        self._base_wall = time.time()
        self._base_mono = time.monotonic()

    def time(self) -> float:
        return self._base_wall + self.elapsed + self.wall_skew

    def monotonic(self) -> float:
        return self._base_mono + self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds

    def step_wall(self, seconds: float) -> None:
        self.wall_skew += seconds


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime


def _drive(
    *,
    clock: _Clock,
    health: dict[str, Any] | None = None,
    status: dict[str, Any] | None = STATUS_OPEN,
    full_counts: dict[str, Any] | None = FULL_OPEN,
    outage_polls: tuple[int, int] | None = None,
    wall_step: tuple[int, float] | None = None,
    timeout_s: float = 3600.0,
    poll_interval_s: float = 2.0,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """Drive the real wait loop. Returns (verdict, counters).

    ``outage_polls`` makes ``/status`` and ``/health`` unreachable for an
    inclusive poll range. ``wall_step`` applies a wall-clock-only jump before a
    given poll.
    """
    health_payload = HEALTH_DOWN if health is None else health
    counts = {"polls": 0, "observed": 0}

    def fake_get(path: str) -> Any:
        if path == "/status":
            counts["polls"] += 1
            if wall_step is not None and counts["polls"] == wall_step[0]:
                clock.step_wall(wall_step[1])
            if outage_polls is not None and outage_polls[0] <= counts["polls"] <= outage_polls[1]:
                return None
            counts["observed"] += 1
            return status
        if path == "/health":
            if outage_polls is not None and outage_polls[0] <= counts["polls"] <= outage_polls[1]:
                return None
            return health_payload
        if path == "/tasks/counts":
            return full_counts
        return None

    with (
        patch.object(rb, "server_get", side_effect=fake_get),
        patch.object(rb, "time", clock),
        patch.object(rb, "_signal_orchestrator_shutdown"),
    ):
        verdict = rb._wait_for_run_completion(poll_interval_s=poll_interval_s, timeout_s=timeout_s)
    return verdict, counts


# ---------------------------------------------------------------------------
# Defect 1: the confirmation window was measured on the wall clock.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("jump", [11.5, 20.0, 60.0, 300.0])
def test_a_wall_clock_step_does_not_shorten_the_confirmation_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jump: float
) -> None:
    """A forward wall-clock step must not satisfy the window.

    The window exists to give the recovery supervisor time to restart the
    orchestrator, and the supervisor sleeps on ``time.monotonic()``. Measuring
    the window on ``time.time()`` let an NTP correction, a container clock sync,
    or a laptop resume collapse 15 seconds into 4 seconds of real time. The
    suspend-resume case is the worst: the supervisor's monotonic clock does not
    advance at all while suspended, so it wakes having made zero extra restart
    attempts against a window that already reads as satisfied.
    """
    (_runtime(tmp_path) / "spawner.pid").write_text(str(DEAD_PID))
    monkeypatch.chdir(tmp_path)

    clock = _Clock()
    verdict, counts = _drive(clock=clock, wall_step=(1, jump))

    assert verdict is not None, "a sustained dead orchestrator must still reach a verdict"
    assert clock.elapsed >= rb._ORCHESTRATOR_GONE_CONFIRM_WINDOW_S, (
        f"verdict after {clock.elapsed}s of monotonic time with a +{jump}s wall step; "
        f"the window is {rb._ORCHESTRATOR_GONE_CONFIRM_WINDOW_S}s and must be measured "
        f"on the clock the recovery supervisor actually sleeps on"
    )
    assert counts["observed"] >= rb._ORCHESTRATOR_GONE_CONFIRMATIONS


# ---------------------------------------------------------------------------
# Defect 2: the two observers were not independent, and GONE needed only one.
# ---------------------------------------------------------------------------


def test_gone_requires_positive_local_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The task server can veto a verdict; it can never supply one.

    ``/health``'s spawner component reads the SAME ``spawner.pid`` with a bare
    ``os.kill``, with none of the local classifier's guards: no mtime
    attribution, no command-line identity, no zombie rejection. It is a weaker
    read of the same evidence, not a second witness. Accepting its ``down`` as
    corroboration made every one of those guards overridable, and only in the
    direction that reaps.
    """
    runtime = _runtime(tmp_path)
    pidfile = runtime / "spawner.pid"
    monkeypatch.chdir(tmp_path)

    # No pidfile at all. The classifier calls this unknown; so must the caller.
    assert rb._orchestrator_liveness(HEALTH_DOWN) == (LIVENESS_UNKNOWN, None)

    # A pidfile that cannot be parsed.
    pidfile.write_text("not-a-pid")
    assert rb._orchestrator_liveness(HEALTH_DOWN)[0] == LIVENESS_UNKNOWN

    # The empty file that a torn write of `spawner.pid` leaves behind.
    pidfile.write_text("")
    assert rb._orchestrator_liveness(HEALTH_DOWN)[0] == LIVENESS_UNKNOWN

    # A pidfile written before this run began.
    pidfile.write_text(str(DEAD_PID))
    old = time.time() - 86400.0
    os.utime(pidfile, (old, old))
    assert rb._orchestrator_liveness(HEALTH_DOWN, pidfile_not_before=rb._CLI_RUN_EPOCH)[0] == LIVENESS_UNKNOWN

    # Positive local evidence, uncontradicted, is still a verdict.
    pidfile.write_text(str(DEAD_PID))
    assert rb._orchestrator_liveness(HEALTH_DOWN) == (LIVENESS_GONE, DEAD_PID)
    assert rb._orchestrator_liveness({"agent_count": 0}) == (LIVENESS_GONE, DEAD_PID)
    # ... and the server's `ok` still vetoes it.
    assert rb._orchestrator_liveness(_health("ok"))[0] == LIVENESS_UNKNOWN


def test_no_pidfile_plus_server_down_never_reaches_a_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: no pidfile on disk must not exit non-zero, whatever /health says.

    With ``pid`` as ``None`` there was not even a pid for the restart-detection
    reset to compare against, so a restart could not have cancelled the verdict.
    """
    _runtime(tmp_path)  # runtime dir exists, spawner.pid does not
    monkeypatch.chdir(tmp_path)

    verdict, _counts = _drive(clock=_Clock(), timeout_s=600.0)
    assert verdict is None


# ---------------------------------------------------------------------------
# Defect 3: the mtime guard was dead code whenever the server had an opinion,
# and the fixture that was supposed to prove otherwise omitted `components`.
# ---------------------------------------------------------------------------


def test_stale_pidfile_guard_holds_against_a_realistic_health_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A day-old pidfile must not condemn this run, WITH a real /health payload.

    The earlier version of this guard passed only because its fixture omitted
    the ``components`` block that a real server always emits. Restore the block
    and the guard was inert.
    """
    pidfile = _runtime(tmp_path) / "spawner.pid"
    pidfile.write_text(str(DEAD_PID))
    old = time.time() - 86400.0
    os.utime(pidfile, (old, old))
    monkeypatch.chdir(tmp_path)

    verdict, counts = _drive(clock=_Clock(), health=HEALTH_DOWN, timeout_s=600.0)
    assert verdict is None, f"a previous run's pidfile produced a verdict after {counts['observed']} observations"


def test_health_fixture_matches_the_real_server() -> None:
    """Pin the fixture shape against the real producer, not against a memory.

    Every ``/health`` fixture in these tests claims to be what the server sends.
    This asserts it, by running the server's own component builder over a real
    pidfile: if the key path or the status vocabulary ever changes, the fixtures
    fail here rather than quietly stopping to exercise the code they target.
    """
    import tempfile

    from bernstein.core.routes.status_dashboard import _health_components

    with tempfile.TemporaryDirectory() as td:
        sdd = Path(td) / ".sdd"
        (sdd / "runtime").mkdir(parents=True)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(sdd_dir=sdd)))
        store = SimpleNamespace(agent_count=0, jsonl_path=sdd / "runtime" / "tasks.jsonl")

        # No pidfile -> the server reports neither ok nor down.
        components = _health_components(request, store)  # type: ignore[arg-type]
        assert set(HEALTH_DOWN["components"]) == set(components), (
            "the /health fixture must carry the same component keys the server emits"
        )
        assert components["spawner"]["status"] not in ("ok", "down")

        # A dead pid -> exactly the "down" the fixtures use.
        (sdd / "runtime" / "spawner.pid").write_text(str(DEAD_PID))
        assert _health_components(request, store)["spawner"]["status"] == "down"  # type: ignore[arg-type]

        # A live pid -> exactly the "ok" the fixtures use.
        (sdd / "runtime" / "spawner.pid").write_text(str(os.getpid()))
        assert _health_components(request, store)["spawner"]["status"] == "ok"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Defect 4: the supervisor stopped restarting, which regressed against main.
# ---------------------------------------------------------------------------


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


def test_crashed_orchestrator_whose_pidfile_was_cleaned_is_still_restarted(
    tmp_path: Path,
) -> None:
    """The regression: crashed, then ``bernstein doctor --fix`` removed the pidfile.

    ``cli/commands/status_cmd.py::_fix_stale_pids`` deletes precisely the
    pidfile of a process that has already died. Refusing to restart on the
    resulting "no pidfile" reading is permanent, because nothing recreates
    ``spawner.pid``: the run is left with no orchestrator and no recovery.
    This walks the real doctor path rather than asserting on ``pid=None``.
    """
    from bernstein.cli.commands.status_cmd import _doctor_check_stale_pids, _fix_stale_pids

    pidfile = _runtime(tmp_path) / "spawner.pid"
    pidfile.write_text(str(DEAD_PID))

    liveness, pid = classify_pidfile_liveness(pidfile, expect_cmdline=_markers())
    assert _restart_calls(pid=pid, liveness=liveness) == [1], "a crash with its pidfile intact must restart"

    checks: list[dict[str, Any]] = []
    _fix_stale_pids(_doctor_check_stale_pids(checks, tmp_path), [])
    assert not pidfile.exists(), "doctor --fix is expected to have removed the stale pidfile"

    liveness, pid = classify_pidfile_liveness(pidfile, expect_cmdline=_markers())
    assert _restart_calls(pid=pid, liveness=liveness) == [1], (
        "the same crash must still restart once its stale pidfile has been cleaned"
    )


def test_recycled_pid_after_a_crash_is_still_restarted(tmp_path: Path) -> None:
    """A pid reused by an unrelated process must not suppress recovery.

    For the supervisor the identity check runs the other way round from the
    CLI: its job is to stop a stranger's process from passing as our live
    orchestrator and withholding a restart the run needs.
    """
    pidfile = _runtime(tmp_path) / "spawner.pid"
    pidfile.write_text(str(os.getpid()))  # alive, and emphatically not an orchestrator

    liveness, pid = classify_pidfile_liveness(pidfile, expect_cmdline=_markers())
    assert liveness == LIVENESS_UNKNOWN
    assert _restart_calls(pid=pid, liveness=liveness) == [1]


def test_a_live_orchestrator_is_never_restarted(tmp_path: Path) -> None:
    """Control: the one reading that withholds a restart is a positive alive."""
    assert _restart_calls(pid=os.getpid(), liveness=LIVENESS_ALIVE) == []


def test_supervisor_stands_down_only_on_positive_teardown_evidence(tmp_path: Path) -> None:
    """Refusal needs evidence of teardown, not merely absence of evidence.

    ``bernstein stop`` kills ``watchdog.pid`` FIRST and removes pidfiles last,
    so a supervisor that is alive to see a missing pidfile is not looking at a
    teardown. Only a draining marker, or having been superseded as the
    supervisor of record, justifies standing down.
    """
    from bernstein.core.orchestration.bootstrap import _supervisor_should_stand_down

    runtime = _runtime(tmp_path)

    # Nothing on disk: no evidence of teardown, so keep supervising.
    assert _supervisor_should_stand_down(tmp_path) is None

    # A supervised process's pidfile is missing: still not teardown evidence.
    (runtime / "spawner.pid").unlink(missing_ok=True)
    assert _supervisor_should_stand_down(tmp_path) is None

    # We are the supervisor of record: keep going.
    (runtime / "watchdog.pid").write_text(str(os.getpid()))
    assert _supervisor_should_stand_down(tmp_path) is None

    # Superseded or killed: stand down.
    (runtime / "watchdog.pid").write_text(str(DEAD_PID))
    assert _supervisor_should_stand_down(tmp_path) is not None

    # Drain in progress: stand down.
    (runtime / "watchdog.pid").write_text(str(os.getpid()))
    (runtime / "draining").write_text("draining")
    assert _supervisor_should_stand_down(tmp_path) is not None


# ---------------------------------------------------------------------------
# Defect 5: the window could be satisfied by time in which recovery was
# impossible, because unreachable polls neither reset nor advanced the streak.
# ---------------------------------------------------------------------------


def test_a_server_outage_is_not_credited_as_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreachable polls must reset the streak, not silently fill the window.

    A server outage is the exact period during which recovery is impossible:
    ``bootstrap._restart_spawner`` returns ``-1`` whenever the task server is
    not alive, so the supervisor gets zero chances to restart the orchestrator
    throughout. Counting that time as confirmation fired the verdict just as the
    recovery sequence reached the orchestrator, on 3 real observations out of 13
    polls.
    """
    (_runtime(tmp_path) / "spawner.pid").write_text(str(DEAD_PID))
    monkeypatch.chdir(tmp_path)

    clock = _Clock()
    verdict, counts = _drive(clock=clock, outage_polls=(3, 12), timeout_s=3600.0)

    assert verdict is not None, "the run really did end; it must still be reported eventually"
    # The outage must have bought nothing: the confirming observations all have
    # to come after it, so the total observed count exceeds the two seen before.
    assert counts["observed"] >= 2 + rb._ORCHESTRATOR_GONE_CONFIRMATIONS, (
        f"verdict reached on {counts['observed']} observations; the 2 before the outage "
        f"cannot count towards a window the outage did not advance"
    )
    assert clock.elapsed >= rb._ORCHESTRATOR_GONE_CONFIRM_WINDOW_S


def test_an_outage_that_never_ends_never_produces_a_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The degenerate case: a server that goes away for good is not a verdict."""
    (_runtime(tmp_path) / "spawner.pid").write_text(str(DEAD_PID))
    monkeypatch.chdir(tmp_path)

    verdict, _counts = _drive(clock=_Clock(), outage_polls=(3, 10**9), timeout_s=600.0)
    assert verdict is None


def test_uninterrupted_polling_still_reaches_the_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: with a reachable server the verdict still arrives promptly.

    Guards the fix against over-correction: tightening the streak must not make
    the honest #3010 case unreachable.
    """
    (_runtime(tmp_path) / "spawner.pid").write_text(str(DEAD_PID))
    monkeypatch.chdir(tmp_path)

    clock = _Clock()
    verdict, counts = _drive(clock=clock, timeout_s=600.0)
    assert verdict is not None
    assert counts["observed"] == counts["polls"]
    assert clock.elapsed <= 4.0 * rb._ORCHESTRATOR_GONE_CONFIRM_WINDOW_S


# ---------------------------------------------------------------------------
# Also reported: the command-line marker is inert after a self-restart.
# ---------------------------------------------------------------------------


def test_marker_covers_every_shipped_launch_form() -> None:
    """All three spellings an orchestrator process can legitimately have.

    A form that is missing from the list looks exactly like a recycled pid, so
    the pid-reuse protection silently disappears for processes launched that
    way. The ``execv`` form is the one that matters most: it is what the
    orchestrator becomes after its own restart, which is precisely when a pid
    has just been freed for reuse.
    """
    launch_forms = {
        # server_launch._start_spawner
        "spawner": "/usr/bin/python3 -m bernstein.core.orchestration.orchestrator --port 8052",
        # orchestrator_cleanup.restart -> os.execv, after python rewrote argv[0]
        # from the -m module name to the module file path
        "self_restart": "/usr/bin/python3 /app/src/bernstein/core/orchestration/orchestrator.py --port 8052",
        # docker-compose.yaml entrypoint / docker/demo/demo-cycle.sh
        "compose": "python -m bernstein.core.orchestrator",
    }
    markers = _markers()
    for form, cmdline in launch_forms.items():
        assert any(m in cmdline for m in markers), f"{form} launch form is unrecognised: {cmdline}"

    assert not any(m in "/usr/bin/python3 -m http.server" for m in markers)


# ---------------------------------------------------------------------------
# Also reported: an error body was accepted as a complete task histogram.
# ---------------------------------------------------------------------------


def test_an_error_body_is_not_a_task_histogram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``{"detail": "Not Found"}`` must not read as "nothing outstanding".

    Every status key is absent from an error body, so counting it yields a
    confident zero for a run with any amount of work left. That zero would also
    disable the quiescence veto that keeps an ``in_progress`` task from being
    reported as a finished, healthy run.
    """
    monkeypatch.chdir(tmp_path)
    with patch.object(rb, "server_get", return_value={"detail": "Not Found"}):
        n_incomplete, full_counts = rb._incomplete_declared_counts(STATUS_OPEN)
    assert full_counts is None, "an error body must not be accepted as a complete histogram"
    assert n_incomplete == 1, "the count must fall back to the /status payload"

    with patch.object(rb, "server_get", return_value=FULL_OPEN):
        n_incomplete, full_counts = rb._incomplete_declared_counts(STATUS_OPEN)
    assert full_counts == FULL_OPEN
    assert n_incomplete == 1
