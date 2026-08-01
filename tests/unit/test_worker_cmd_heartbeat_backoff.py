"""Worker heartbeat loop backs off on repeated re-registration failure (#3309).

Before this fix, when ``_do_heartbeat`` returned ``None`` (heartbeat failed and
the subsequent re-registration attempt also failed), ``WorkerLoop.run``
executed ``continue`` -- sending control straight back to the top of the
``while self._running`` loop without ever reaching
``self._wake.wait(timeout_s=poll_s)``, the same interval wait the success path
takes. As long as re-registration kept failing, the worker spun on
``_do_heartbeat`` at full speed instead of backing off, hammering the server
with back-to-back requests.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import httpx
import pytest

from bernstein.cli.commands import worker_cmd
from bernstein.cli.commands.worker_cmd import PollConfig, WorkerLoop

if TYPE_CHECKING:
    from pathlib import Path

# Real wall-clock window for the reproduction. Short enough to keep the test
# fast; long enough to give a correctly-throttled loop (~0.1s poll interval)
# several real iterations while giving a busy loop plenty of room to run away.
_WINDOW_S = 0.5
_POLL_INTERVAL_MS = 100

# A correctly-throttled loop manages roughly _WINDOW_S / (poll interval in s)
# iterations (~5 here) plus scheduling slack. A busy loop produces many
# thousands of calls in the same window, so this bound cleanly separates the
# two behaviours without being timing-sensitive.
_MAX_EXPECTED_ATTEMPTS = 60


def _make_loop(tmp_path: Path) -> WorkerLoop:
    return WorkerLoop(
        server_url="http://central:8052",
        name="test-node",
        auth_token="secret-token",
        adapter="claude",
        workdir=tmp_path,
        poll_config=PollConfig(poll_interval_ms=_POLL_INTERVAL_MS, heartbeat_interval_ms=15_000),
    )


class TestHeartbeatFailureBacksOff:
    def test_forced_none_heartbeat_does_not_busy_loop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Repeated re-registration failure waits the poll interval between
        attempts instead of re-entering ``_do_heartbeat`` immediately."""
        loop = _make_loop(tmp_path)

        # Skip workspace preflight, registration, and enrolment -- not what
        # this test exercises -- and keep signal.signal a no-op since run()
        # is driven from a background thread here (signal.signal only works
        # on the main thread).
        monkeypatch.setattr(loop, "_workspace_setup_error", lambda: None)
        monkeypatch.setattr(loop, "_register_with_retry", lambda client: "node-1")
        monkeypatch.setattr(loop, "_enrol", lambda client: None)
        monkeypatch.setattr(worker_cmd.signal, "signal", lambda *_a, **_k: None)

        attempts = 0

        def _forced_none_heartbeat(
            client: httpx.Client, node_id: str, heartbeat_s: float, last_heartbeat: float
        ) -> tuple[str | None, float]:
            nonlocal attempts
            attempts += 1
            # Re-registration failed: no valid node_id, timestamp untouched --
            # exactly what the real failure path returns.
            return None, last_heartbeat

        monkeypatch.setattr(loop, "_do_heartbeat", _forced_none_heartbeat)

        thread = threading.Thread(target=loop.run, daemon=True)
        thread.start()
        time.sleep(_WINDOW_S)
        loop._running = False
        loop._wake.signal_abort()
        thread.join(timeout=5.0)

        assert not thread.is_alive(), "worker loop thread did not stop after abort"
        assert attempts <= _MAX_EXPECTED_ATTEMPTS, (
            f"_do_heartbeat was called {attempts} times in {_WINDOW_S}s -- the "
            "re-registration-failure path is not waiting the poll interval "
            "between retries (busy loop)."
        )
