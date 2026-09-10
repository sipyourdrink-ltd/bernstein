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
forward and is what the other bounded waits in this tree already use.
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


def _is_wall_clock_read(node: ast.AST) -> bool:
    """Is *node* a ``time.time()`` call?"""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "time"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "time"
    )


def _bounding_reads(loop: ast.While) -> bool:
    """Does *loop* bound itself on the wall clock?

    Three shapes, because a bounded wait can be written any of these ways and
    only the first is visible in the loop's test::

        while time.time() < deadline:              # in the condition
        while True:
            ...
            if time.time() >= deadline: break      # in an if that breaks
        while True:
            now = time.time()
            if now >= deadline: break              # read, then compared

    The second and third are what this guard originally missed, and both were
    live in ``cli/run_bootstrap.py`` - one of them directly beneath a comment
    asserting the timing there was monotonic.

    A body read only counts when the loop can exit on a comparison, so an
    ordinary ``time.time()`` used for a timestamp or a log line inside a loop
    is not mistaken for a bound.
    """
    if any(_is_wall_clock_read(child) for child in ast.walk(loop.test)):
        return True

    if not any(isinstance(node, ast.Break) for node in ast.walk(loop)):
        return False

    # Names bound to a wall-clock read anywhere in the body.
    wall_clock_names: set[str] = set()
    for node in ast.walk(loop):
        if isinstance(node, ast.Assign) and _is_wall_clock_read(node.value):
            wall_clock_names.update(t.id for t in node.targets if isinstance(t, ast.Name))

    for node in ast.walk(loop):
        if not isinstance(node, ast.If):
            continue
        if not any(isinstance(stmt, ast.Break) for stmt in ast.walk(node)):
            continue
        for child in ast.walk(node.test):
            if _is_wall_clock_read(child):
                return True
            if isinstance(child, ast.Name) and child.id in wall_clock_names:
                return True
    return False


def _wall_clock_waits() -> tuple[list[str], int]:
    """Return offending ``path:line`` values and the number of files scanned.

    ``time.time()`` compared against a stored absolute instant - ``expires_at``,
    a JWT ``exp`` - is correct and common in this tree, so only reads that
    *bound a loop* count. Measuring your own elapsed time needs a clock
    guaranteed to move forward; comparing against someone else's timestamp
    does not.

    The file count comes back so a caller can prove the corpus was not empty.
    A scan that silently walks nothing passes every assertion below.
    """
    offenders: list[str] = []
    scanned = 0
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - unreadable file
            continue
        scanned += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.While) and _bounding_reads(node):
                offenders.append(f"{path.relative_to(SRC).as_posix()}:{node.lineno}")
    return offenders, scanned


def test_no_bounded_wait_loops_on_the_wall_clock() -> None:
    """A wall clock can move backwards, so a loop bounded by one is not bounded."""
    offenders, _scanned = _wall_clock_waits()
    assert offenders == [], (
        "these loops bound themselves with time.time(), which NTP or an "
        "operator can step backwards mid-wait; use time.monotonic(): " + ", ".join(offenders)
    )


def test_the_guard_actually_scanned_the_source_tree() -> None:
    """An empty corpus passes the assertion above for the wrong reason.

    ``SRC`` is built from ``__file__``; a move of this file, or a layout
    change, turns the scan into a walk over nothing and the guard goes quietly
    green forever. The detector self-test below cannot notice that - it uses a
    synthetic AST and never touches ``SRC``.
    """
    _offenders, scanned = _wall_clock_waits()

    assert SRC.is_dir(), f"{SRC} is not a directory - the guard is scanning nothing"
    assert scanned > 500, f"only {scanned} files parsed; the corpus looks wrong"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("while time.time() < deadline:\n    pass\n", True),
        ("while True:\n    if time.time() >= deadline:\n        break\n", True),
        ("while True:\n    now = time.time()\n    if now >= deadline:\n        break\n", True),
        ("while True:\n    if time.monotonic() >= deadline:\n        break\n", False),
        ("while True:\n    now = time.monotonic()\n    if now >= deadline:\n        break\n", False),
        # A timestamp taken inside a loop that never exits on it is not a bound.
        ("while True:\n    stamp = time.time()\n    if done:\n        break\n", False),
        ("while running:\n    pass\n", False),
    ],
    ids=[
        "condition",
        "body-break-direct",
        "body-break-via-name",
        "body-break-monotonic",
        "body-break-via-name-monotonic",
        "timestamp-not-a-bound",
        "no-clock",
    ],
)
def test_the_detector_recognises_every_shape(source: str, expected: bool) -> None:
    """All three spellings of a bounded wait, and the things that only look like one.

    The two body-break cases are what this guard originally missed, so they
    are pinned here rather than left to the corpus scan to notice.
    """
    loop = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.While))

    assert _bounding_reads(loop) is expected


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
