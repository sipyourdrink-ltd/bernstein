"""Bounded waits measure elapsed time with a monotonic clock.

``time.time()`` is a wall clock. It is the right tool for an *absolute* instant
- a JWT ``exp``, a card's ``expires_at`` - because those are agreed with
someone else and have to survive a restart. It is the wrong tool for "how long
have I been waiting", because it is not guaranteed to move forward at one
second per second, or forward at all: NTP steps it, an operator sets it, a VM
resumes from a snapshot with a stale one.

A loop written as::

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ...

therefore has no bound. A clock stepped backwards by an hour turns a 30-second
drain into an hour-long one; a step forwards ends the wait before the work it
was waiting for could finish. ``time.monotonic()`` is defined to only move
forward and is what the other fifteen bounded waits in this tree already use.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "bernstein"

#: This file scans the whole source tree, so a source change anywhere can
#: break it. The marker is what lets ``run_tests.py --affected`` select it;
#: without it the guard only runs in the merge group.
pytestmark = pytest.mark.whole_tree_guard


# ---------------------------------------------------------------------------
# Guard: no bounded wait reintroduces the wall clock
# ---------------------------------------------------------------------------


def _wall_clock_waits() -> list[str]:
    """Return ``path:line`` for every ``while`` loop testing ``time.time()``.

    Scoped to loop *conditions* on purpose. ``time.time()`` compared against a
    stored absolute instant - ``expires_at``, a JWT ``exp`` - is correct and
    common in this tree; it is only measuring one's own elapsed time that
    needs a clock guaranteed to move forward.
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - unreadable file
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.While):
                continue
            for child in ast.walk(node.test):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "time"
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "time"
                ):
                    offenders.append(f"{path.relative_to(SRC).as_posix()}:{node.lineno}")
    return offenders


def test_no_bounded_wait_loops_on_the_wall_clock() -> None:
    """A wall clock can move backwards, so a loop bounded by one is not bounded."""
    offenders = _wall_clock_waits()
    assert offenders == [], (
        "these loops bound themselves with time.time(), which NTP or an "
        "operator can step backwards mid-wait; use time.monotonic(): " + ", ".join(offenders)
    )


def test_the_guard_can_see_a_wall_clock_wait() -> None:
    """A scan that matched nothing would pass the guard for the wrong reason."""
    tree = ast.parse("import time\ndeadline = time.time() + 5\nwhile time.time() < deadline:\n    pass\n")
    whiles = [n for n in ast.walk(tree) if isinstance(n, ast.While)]

    assert len(whiles) == 1
    found = [
        child
        for child in ast.walk(whiles[0].test)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "time"
        and isinstance(child.func.value, ast.Name)
        and child.func.value.id == "time"
    ]
    assert found, "the detector no longer recognises the shape it exists to find"


# ---------------------------------------------------------------------------
# Behaviour: a backwards clock step does not extend a bounded wait
# ---------------------------------------------------------------------------


def test_stopping_the_autofix_daemon_honours_its_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stop()`` waits for the process to exit, bounded by ``timeout_seconds``.

    Time is simulated so the test measures the loop rather than the machine:
    ``sleep`` advances a monotonic counter instead of blocking, and the wall
    clock steps an hour backwards partway through - one NTP correction, not an
    exotic scenario.

    On the wall clock every read after that step is earlier than the deadline
    computed before it, so the loop runs for the length of the step. The
    process being waited on never exits here, so the bound is the only thing
    that can end it.
    """
    from bernstein.core.autofix import daemon

    elapsed = {"monotonic": 0.0, "slept": 0.0}
    real_time = time.time()

    def fake_monotonic() -> float:
        return elapsed["monotonic"]

    def fake_wall_clock() -> float:
        # Steps back an hour once a little simulated time has passed.
        return real_time - 3600.0 if elapsed["monotonic"] > 0.2 else real_time

    def fake_sleep(seconds: float) -> None:
        elapsed["monotonic"] += seconds
        elapsed["slept"] += seconds
        if elapsed["slept"] > 60.0:
            raise AssertionError(
                f"stop() slept {elapsed['slept']:.1f}s against a 0.5s timeout - "
                "the wait is bounded by a wall clock that stepped backwards"
            )

    monkeypatch.setattr(daemon, "_read_pid", lambda _workdir: 4242)
    monkeypatch.setattr(daemon, "_process_alive", lambda _pid: True)
    monkeypatch.setattr(daemon.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(daemon.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(daemon.time, "time", fake_wall_clock)
    monkeypatch.setattr(daemon.time, "sleep", fake_sleep)

    assert daemon.stop(tmp_path, timeout_seconds=0.5) == 4242
    assert elapsed["slept"] <= 1.0, f"waited {elapsed['slept']}s for a 0.5s timeout"
