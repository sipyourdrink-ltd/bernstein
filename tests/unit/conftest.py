"""Unit test conftest - ensure the local src/ takes priority on sys.path.

In git worktrees the parent project's venv may appear earlier on sys.path
than the worktree's own src/, causing new modules to be shadowed.  This
conftest inserts the worktree's src/ at position 0 so that imports always
resolve to the locally checked-out code.

It also installs the autouse no-network guard (see ``_no_network``): unit
tests are hermetic and must not open a real outbound connection. Loopback is
allowed so local mock servers keep working; a test that genuinely needs the
network lives in ``tests/integration/`` or opts out with
``@pytest.mark.allow_network``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# This file lives at tests/unit/conftest.py → parent.parent is the repo root
_SRC = str(Path(__file__).resolve().parent.parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Re-export the shared adapter-test fixture so adapter test modules can opt
# in via ``pytestmark = pytest.mark.usefixtures("no_watchdog_threads")``.
# The helpers module keeps the ``make_popen_mock`` / ``inner_cmd`` callables
# test modules import directly.
from tests.unit._adapter_test_helpers import no_watchdog_threads  # noqa: F401
from tests.unit._no_network import block_network


@pytest.fixture(autouse=True)
def _block_real_network(request: pytest.FixtureRequest) -> Iterator[None]:
    """Block non-loopback connections for every unit test.

    A unit test that opens a real outbound connection is flaky by
    construction: it passes only while the remote host answers. The guard
    converts any such attempt into a clear, deterministic ``RuntimeError`` that
    names the host so the fix (mock it) is obvious.

    Loopback (``127.0.0.0/8``, ``::1``, ``localhost``) and Unix-domain sockets
    are allowed so the many unit tests that spin a local mock server keep
    working. A test that legitimately must reach the network either lives in
    ``tests/integration/`` (where this fixture does not apply) or marks itself
    ``@pytest.mark.allow_network`` and documents why.
    """
    if request.node.get_closest_marker("allow_network") is not None:
        yield
        return
    with block_network():
        yield


@pytest.fixture(autouse=True)
def _reap_leaked_timeout_watchdogs():
    """Cancel any timeout-watchdog Timer a spawn test leaves running.

    ``CLIAdapter._start_timeout_watchdog`` starts a ``threading.Timer`` that
    production code cancels when the agent is reaped. Adapter unit tests
    routinely discard the ``SpawnResult`` (they only assert on the manifest
    or log), so each spawn leaks a live Timer that sleeps for the full
    timeout. Across a shard's worth of adapter tests these accumulate until
    the process can no longer start new threads
    (``RuntimeError: can't start new thread``), which flakes an unrelated
    later test in the same shard. Cancel any that survive a test so worker
    threads never pile up.
    """
    yield
    import threading

    for thread in threading.enumerate():
        if isinstance(thread, threading.Timer) and thread.name.startswith("timeout-watchdog"):
            thread.cancel()


@pytest.fixture(autouse=True)
def _reap_leaked_timeout_watchdogs():
    """Cancel any timeout-watchdog Timer a spawn test leaves running.

    ``CLIAdapter._start_timeout_watchdog`` starts a ``threading.Timer`` that
    production code cancels when the agent is reaped. Adapter unit tests
    routinely discard the ``SpawnResult`` (they only assert on the manifest
    or log), so each spawn leaks a live Timer that sleeps for the full
    timeout. Across a shard's worth of adapter tests these accumulate until
    the process can no longer start new threads
    (``RuntimeError: can't start new thread``), which flakes an unrelated
    later test in the same shard. Cancel any that survive a test so worker
    threads never pile up.
    """
    yield
    import threading

    for thread in threading.enumerate():
        if isinstance(thread, threading.Timer) and thread.name.startswith("timeout-watchdog"):
            thread.cancel()
