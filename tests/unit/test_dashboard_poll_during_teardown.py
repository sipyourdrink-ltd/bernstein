"""A completed dashboard poll must be harmless once teardown has begun.

Textual tears down the screen tree before it closes the app's message queue or
cancels thread workers. A poll can therefore report success after widgets have
been pruned or the screen stack has been cleared. This pins completion after
the latter point, where touching the dashboard's focus used to crash shutdown.
"""

from __future__ import annotations

import asyncio
import importlib.util
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import bernstein.cli.dashboard_app as dashboard_app

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "render_tui_snapshot.py"


def test_a_poll_completing_after_screens_close_does_not_crash_teardown() -> None:
    """Thread-worker success may still be dispatched while shutdown is underway."""
    spec = importlib.util.spec_from_file_location("render_tui_snapshot_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    snapshot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(snapshot)

    release = threading.Event()

    class PollCompletesDuringTeardown(dashboard_app.BernsteinApp):
        def _schedule_poll(self) -> None:
            fetch = dashboard_app._fetch_all

            def work() -> dict[str, Any]:
                release.wait(10.0)
                return fetch()

            self.run_worker(work, thread=True, group="poll", exclusive=True)

        async def _close_all(self) -> None:
            await super()._close_all()
            assert not self._screen_stack
            release.set()
            for _ in range(500):
                await asyncio.sleep(0.002)
                if self._exception is not None:
                    break

    with patch.object(dashboard_app, "BernsteinApp", PollCompletesDuringTeardown):
        snapshot.render()
