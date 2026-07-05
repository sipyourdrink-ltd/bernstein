"""Integration tests for the ``bernstein-worker`` subprocess wrapper.

``bernstein-worker`` (``src/bernstein/core/orchestration/worker.py``) is
the per-agent process wrapper. It writes a PID metadata file, spawns the
inner CLI as a child, forwards SIGTERM/SIGINT to the child, then cleans
up its PID file on exit. The unit suite in ``tests/unit/test_worker.py``
currently xfails the integration half (citing a stale shim issue from
the decomposition era); this file picks up the slack as an integration
test, exercising real OS process semantics.

Failure modes covered:

| Mode                                       | Test |
|--------------------------------------------|------|
| Worker writes PID metadata then cleans up  | ``test_pid_metadata_written_and_cleaned_on_exit`` |
| SIGTERM forwarded to child and worker exits | ``test_sigterm_forwarded_to_child_and_worker_exits`` |
| SIGINT forwarded to child                  | ``test_sigint_forwarded_to_child`` |
| Worker exits with child's exit code        | ``test_worker_exits_with_child_exit_code`` |
| Worker honours non-zero child exit         | ``test_worker_propagates_nonzero_exit_code`` |
| File handle cleanup after worker exit      | ``test_pid_file_removed_after_clean_exit`` |
| Missing command -> 127                     | ``test_missing_command_returns_127`` |

All tests are POSIX (``os.killpg`` / ``start_new_session``) so they skip
on Windows.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="bernstein-worker signal forwarding uses POSIX process groups",
    ),
]

_WORKER_CMD = [sys.executable, "-m", "bernstein.core.orchestration.worker"]


def _wait_for_file(path: Path, *, timeout_s: float = 5.0) -> None:
    """Block until *path* exists; raises TimeoutError on the deadline.

    No bare ``time.sleep`` for synchronization - the loop polls every
    50ms but only as a fallback for the kernel's filesystem signalling.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.025)
    raise TimeoutError(f"file {path} did not appear within {timeout_s}s")


def _wait_for_predicate(predicate, *, timeout_s: float = 5.0) -> None:
    """Block until *predicate()* returns truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise TimeoutError(f"predicate not satisfied within {timeout_s}s")


def _spawn_worker(
    *,
    session: str,
    pid_dir: Path,
    inner_cmd: list[str],
    workdir: Path | None = None,
    role: str = "test",
    model: str = "test-model",
    start_new_session: bool = True,
) -> subprocess.Popen[bytes]:
    """Spawn ``bernstein-worker`` wrapping *inner_cmd*.

    Always uses ``start_new_session=True`` so we can ``os.killpg`` the
    worker plus its child as a single unit.
    """
    cmd = [
        *_WORKER_CMD,
        "--role",
        role,
        "--session",
        session,
        "--pid-dir",
        str(pid_dir),
        "--model",
        model,
    ]
    if workdir is not None:
        cmd.extend(["--workdir", str(workdir)])
    cmd.append("--")
    cmd.extend(inner_cmd)
    return subprocess.Popen(
        cmd,
        start_new_session=start_new_session,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


# ---------------------------------------------------------------------------
# PID metadata lifecycle
# ---------------------------------------------------------------------------


def test_pid_metadata_written_and_cleaned_on_exit(tmp_path: Path) -> None:
    """Worker writes ``<session>.json`` with worker_pid+child_pid; removes on exit."""
    pid_dir = tmp_path / "pids"
    proc = _spawn_worker(
        session="pid-meta-001",
        pid_dir=pid_dir,
        inner_cmd=[sys.executable, "-c", "import time; time.sleep(2)"],
        workdir=tmp_path,
    )
    try:
        pid_file = pid_dir / "pid-meta-001.json"
        _wait_for_file(pid_file)

        # Wait for the second write (child_pid populated after spawn).
        def _has_child_pid() -> bool:
            try:
                info = json.loads(pid_file.read_text())
            except (OSError, json.JSONDecodeError):
                return False
            return "child_pid" in info

        _wait_for_predicate(_has_child_pid)
        info = json.loads(pid_file.read_text())
        assert info["role"] == "test"
        assert info["session"] == "pid-meta-001"
        assert info["model"] == "test-model"
        assert isinstance(info["worker_pid"], int)
        assert isinstance(info["child_pid"], int)
        # The child PID should differ from the worker PID - distinct processes.
        assert info["child_pid"] != info["worker_pid"]
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=10)

    # PID file must be cleaned up after exit.
    _wait_for_predicate(lambda: not (pid_dir / "pid-meta-001.json").exists())


def test_pid_file_removed_after_clean_exit(tmp_path: Path) -> None:
    """PID file is removed even when the child exits cleanly (no signal)."""
    pid_dir = tmp_path / "pids"
    proc = _spawn_worker(
        session="clean-exit-001",
        pid_dir=pid_dir,
        inner_cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        workdir=tmp_path,
    )
    rc = proc.wait(timeout=10)
    assert rc == 0, f"worker should exit 0, got {rc}; stderr={proc.stderr.read() if proc.stderr else b''!r}"
    _wait_for_predicate(lambda: not (pid_dir / "clean-exit-001.json").exists())


# ---------------------------------------------------------------------------
# Signal forwarding
# ---------------------------------------------------------------------------


def test_sigterm_forwarded_to_child_and_worker_exits(tmp_path: Path) -> None:
    """SIGTERM to the worker process group terminates the child quickly."""
    pid_dir = tmp_path / "pids"
    # A sleep that would block for 60s if we didn't kill it; the test
    # must complete in < 5s to prove the signal made it through.
    proc = _spawn_worker(
        session="sigterm-001",
        pid_dir=pid_dir,
        inner_cmd=[sys.executable, "-c", "import time; time.sleep(60)"],
        workdir=tmp_path,
    )
    try:
        _wait_for_file(pid_dir / "sigterm-001.json")
        start = time.monotonic()
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        rc = proc.wait(timeout=5)
        elapsed = time.monotonic() - start
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    assert rc is not None
    assert elapsed < 5.0, f"worker took {elapsed:.1f}s to exit after SIGTERM"


def test_sigint_forwarded_to_child(tmp_path: Path) -> None:
    """SIGINT (Ctrl-C) is forwarded - child observes the signal and exits.

    The child installs a SIGINT handler that touches a marker file
    before exiting with a sentinel code; the test waits for the marker
    to appear before sending the signal so we don't race the child's
    startup window.
    """
    pid_dir = tmp_path / "pids"
    handler_ready = tmp_path / "handler-ready"
    inner_script = tmp_path / "inner_sigint.py"
    inner_script.write_text(
        "import signal, sys, time\n"
        "from pathlib import Path\n"
        "def _h(*_):\n"
        "    sys.exit(123)\n"
        "signal.signal(signal.SIGINT, _h)\n"
        f"Path({str(handler_ready)!r}).write_text('ready')\n"
        "time.sleep(30)\n"
    )
    proc = _spawn_worker(
        session="sigint-001",
        pid_dir=pid_dir,
        inner_cmd=[sys.executable, str(inner_script)],
        workdir=tmp_path,
    )
    try:
        _wait_for_file(pid_dir / "sigint-001.json")
        # Wait for the inner child to install its SIGINT handler. This
        # eliminates the start-up race that would otherwise let the
        # signal kill the child before the handler is wired.
        _wait_for_file(handler_ready, timeout_s=5.0)
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        rc = proc.wait(timeout=8)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    # Worker exits with the child's exit code (123 by design).
    assert rc == 123, f"expected worker rc=123 (from child SIGINT handler), got {rc}"


# ---------------------------------------------------------------------------
# Exit-code propagation
# ---------------------------------------------------------------------------


def test_worker_exits_with_child_exit_code(tmp_path: Path) -> None:
    """Worker's exit code equals the wrapped child's exit code (success path)."""
    pid_dir = tmp_path / "pids"
    proc = _spawn_worker(
        session="exit-zero",
        pid_dir=pid_dir,
        inner_cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        workdir=tmp_path,
    )
    rc = proc.wait(timeout=10)
    assert rc == 0


def test_worker_propagates_nonzero_exit_code(tmp_path: Path) -> None:
    """A wrapped child exiting non-zero surfaces through the worker."""
    pid_dir = tmp_path / "pids"
    proc = _spawn_worker(
        session="exit-42",
        pid_dir=pid_dir,
        inner_cmd=[sys.executable, "-c", "import sys; sys.exit(42)"],
        workdir=tmp_path,
    )
    rc = proc.wait(timeout=10)
    assert rc == 42, f"expected rc=42 from wrapped child, got {rc}"


def test_missing_command_returns_127(tmp_path: Path) -> None:
    """A non-existent inner binary results in exit code 127 (POSIX 'not found').

    Pins the contract the manager relies on to distinguish "the CLI
    binary itself was missing" from "the CLI ran but returned an error".
    """
    pid_dir = tmp_path / "pids"
    proc = _spawn_worker(
        session="missing-bin",
        pid_dir=pid_dir,
        inner_cmd=["/this/binary/does/not/exist-bernstein-test"],
        workdir=tmp_path,
    )
    rc = proc.wait(timeout=10)
    assert rc == 127, f"expected rc=127 for missing binary, got {rc}"
    # And the PID file must be cleaned up even in the error path.
    _wait_for_predicate(lambda: not (pid_dir / "missing-bin.json").exists())


def test_invalid_session_id_rejected(tmp_path: Path) -> None:
    """Path-traversal-ish session ids are rejected with rc=1.

    Mirrors the production guard in ``worker.py`` - without it a manager
    bug could inject ``..`` into the session id and have the PID file
    written outside ``pid-dir``.
    """
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    proc = subprocess.Popen(
        [
            *_WORKER_CMD,
            "--role",
            "test",
            "--session",
            "../../escape",
            "--pid-dir",
            str(pid_dir),
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    rc = proc.wait(timeout=10)
    assert rc == 1, f"expected rc=1 for invalid session id, got {rc}"
    # Nothing should have been written to pid_dir or its parent.
    assert list(pid_dir.iterdir()) == []
    assert not (pid_dir.parent / "escape.json").exists()
