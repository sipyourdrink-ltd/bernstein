import threading
from pathlib import Path

import httpx

# Import the module under test
import bernstein.core.server.server_supervisor as ss

# Helper to monkey‑patch time.sleep to a no‑op for fast loops
_original_sleep = ss.time.sleep


def _no_sleep(seconds: float):
    # No actual sleeping – just a tiny yield to allow thread switching
    pass


# ---------------------------------------------------------------------------
# Test that a read timeout does NOT count as a health failure
# ---------------------------------------------------------------------------


def test_health_check_read_timeout_does_not_increment_failure_counter(tmp_path: Path):
    # Setup a minimal workdir (required by the state but not used here)
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True)

    # Create a SupervisorState with a dummy pid (we will never spawn a real server)
    state = ss._SupervisorState(
        workdir=workdir,
        port=12345,
        bind_host="127.0.0.1",
        cluster_enabled=False,
        auth_token=None,
        evolve_mode=False,
    )
    # Pretend a server process exists – the health loop checks is_alive only on
    # failures that are not read‑timeouts, so we can keep the pid at 0.
    state.current_pid = 0

    # Monkey‑patch sleep to avoid real waiting
    ss.time.sleep = _no_sleep

    # Patch httpx.get to raise a ReadTimeout exactly once
    original_get = httpx.get

    def _mock_get(url, timeout=None):
        raise httpx.ReadTimeout("simulated read timeout")

    httpx.get = _mock_get

    # Run a single iteration of the health loop in a thread and then stop
    def run_one():
        ss._health_check_loop(state)

    t = threading.Thread(target=run_one, daemon=True)
    t.start()
    # Let the loop execute once (sleep is a no‑op, so it runs immediately)
    # Then stop it
    state.stopped = True
    t.join(timeout=1)

    # Restore original sleep and httpx.get to avoid side‑effects for other tests
    ss.time.sleep = _original_sleep
    httpx.get = original_get

    # The consecutive_health_failures counter must remain zero because a ReadTimeout
    # is explicitly ignored in the implementation.
    assert state.consecutive_health_failures == 0


# ---------------------------------------------------------------------------
# Test that a bind‑failure (EADDRINUSE) does NOT consume a restart budget slot
# ---------------------------------------------------------------------------


def test_bind_failure_does_not_consume_restart_budget(tmp_path: Path):
    # Create a temporary workdir with the expected runtime layout
    workdir = tmp_path / "work"
    runtime = workdir / ".sdd" / "runtime"
    runtime.mkdir(parents=True)

    # Write a server.log that contains the bind‑failure string
    log_path = runtime / "server.log"
    log_path.write_text("Error: Address already in use", encoding="utf-8")

    # Initialise a SupervisorState pointing at this workdir
    state = ss._SupervisorState(
        workdir=workdir,
        port=12345,
        bind_host="127.0.0.1",
        cluster_enabled=False,
        auth_token=None,
        evolve_mode=False,
    )
    # Simulate a dead server process (pid does not exist)
    state.current_pid = 99999

    # Monkey‑patch sleep and the helper that checks process liveness
    ss.time.sleep = _no_sleep
    ss._is_alive = lambda pid: False

    # Mock _launch_server – it should never be called for a bind‑failure branch
    launch_called = {"count": 0}

    def _mock_launch(state_arg):
        launch_called["count"] += 1
        return 11111

    ss._launch_server = _mock_launch

    # Run a single iteration of the supervisor loop in a thread
    def run_one():
        ss._supervisor_loop(state)

    t = threading.Thread(target=run_one, daemon=True)
    t.start()
    # Give the loop a chance to execute (sleep is a no‑op, so it runs immediately)
    # Then stop it – the loop will exit after the next iteration because we set stopped
    state.stopped = True
    t.join(timeout=1)

    # Restore original sleep to avoid side‑effects for other tests
    ss.time.sleep = _original_sleep

    # No restart should have been recorded – the budget counters stay at zero
    assert state.restart_count == 0
    assert len(state.restart_timestamps) == 0
    # The mock launch function should not have been invoked because bind failures
    # trigger a retry without consuming the budget.
    assert launch_called["count"] == 0
