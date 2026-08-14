import threading
import time
from pathlib import Path

import pytest

from bernstein.plugins import hookimpl, hookspec
from bernstein.plugins.manager import PluginManager

# Generous upper bound for a thread hand-off. Only ever reached when dispatch
# is broken, so a loaded runner cannot turn it into a flake.
DISPATCH_TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def _trusted_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise hook-dispatch mechanics against a trusted workspace.

    Trust gating (untrusted / indeterminate workspaces) is owned by
    tests/unit/test_plugins.py; these tests assume trust has been granted.
    """
    monkeypatch.setattr("bernstein.plugins.manager.is_workspace_trusted", lambda _root: True)


class BackgroundSpec:
    @hookspec(background=True)
    def on_slow_hook(self, duration: float) -> None:
        """A hook that takes some time to execute."""


class SlowPlugin:
    """Parks inside the hook body until the test releases it.

    The park is what makes the non-blocking assertion deterministic: while the
    hook is held, a caller that ran it inline cannot have returned yet.
    """

    def __init__(self):
        self.called = False
        self.finished = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.done = threading.Event()

    @hookimpl
    def on_slow_hook(self, duration: float) -> None:
        self.called = True
        self.entered.set()
        self.release.wait(timeout=duration)
        self.finished = True
        self.done.set()


def test_background_hook_is_non_blocking(tmp_path: Path):
    pm = PluginManager(workdir=tmp_path)
    # Replace spec for testing
    pm._pm.add_hookspecs(BackgroundSpec)

    plugin = SlowPlugin()
    pm.register(plugin, name="slow_plugin")

    pm._safe_call("on_slow_hook", duration=DISPATCH_TIMEOUT)

    # The hook must actually have been dispatched. A manager that silently
    # drops the call - untrusted workspace, swallowed exception, missing
    # executor - fails here instead of passing for being fast.
    assert plugin.entered.wait(timeout=DISPATCH_TIMEOUT), "background hook was never dispatched"

    # ...and it must still be parked, which is only possible if the call was
    # handed to another thread. No wall-clock budget is involved.
    assert not plugin.done.is_set(), "_safe_call ran the hook to completion inline"

    # The backgrounded hook must also run to completion, not just start.
    plugin.release.set()
    assert plugin.done.wait(timeout=DISPATCH_TIMEOUT), "background hook never finished"
    assert plugin.called
    assert plugin.finished


class SyncSpec:
    @hookspec(background=False)
    def on_sync_hook(self, duration: float) -> None:
        """A hook that blocks."""


class BlockingPlugin:
    def __init__(self):
        self.called = False
        self.finished = False

    @hookimpl
    def on_sync_hook(self, duration: float) -> None:
        self.called = True
        time.sleep(duration)
        self.finished = True


def test_sync_hook_blocks(tmp_path: Path):
    pm = PluginManager(workdir=tmp_path)
    pm._pm.add_hookspecs(SyncSpec)

    plugin = BlockingPlugin()
    pm.register(plugin, name="blocking_plugin")

    # ``time.monotonic`` rather than ``time.time``: this is a duration, and a
    # clock step (NTP, suspend/resume) must not be able to shorten it.
    start_time = time.monotonic()
    pm._safe_call("on_sync_hook", duration=0.2)
    end_time = time.monotonic()

    elapsed = end_time - start_time
    assert elapsed >= 0.2, f"Sync hook call took {elapsed}s, expected it to block"
    assert plugin.called
    assert plugin.finished
