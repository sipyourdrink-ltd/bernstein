import time

from bernstein.plugins import hookimpl, hookspec
from bernstein.plugins.manager import PluginManager


class BackgroundSpec:
    @hookspec(background=True)
    def on_slow_hook(self, duration: float) -> None:
        """A hook that takes some time to execute."""


class SlowPlugin:
    def __init__(self):
        self.called = False
        self.finished = False

    @hookimpl
    def on_slow_hook(self, duration: float) -> None:
        self.called = True
        time.sleep(duration)
        self.finished = True


def test_background_hook_is_non_blocking():
    pm = PluginManager()
    # Replace spec for testing
    pm._pm.add_hookspecs(BackgroundSpec)

    plugin = SlowPlugin()
    pm.register(plugin, name="slow_plugin")

    start_time = time.time()
    # Fire a hook that takes 0.5s but should be backgrounded
    pm._safe_call("on_slow_hook", duration=0.5)
    end_time = time.time()

    # It should have returned immediately (much less than 0.5s)
    elapsed = end_time - start_time
    assert elapsed < 0.1, f"Hook call took {elapsed}s, expected it to be backgrounded"

    # Wait a bit for it to actually finish
    max_wait = 1.0
    wait_start = time.time()
    while not plugin.finished and time.time() - wait_start < max_wait:
        time.sleep(0.05)


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


def test_sync_hook_blocks():
    pm = PluginManager()
    pm._pm.add_hookspecs(SyncSpec)

    plugin = BlockingPlugin()
    pm.register(plugin, name="blocking_plugin")

    start_time = time.time()
    pm._safe_call("on_sync_hook", duration=0.2)
    end_time = time.time()

    elapsed = end_time - start_time
    assert elapsed >= 0.2, f"Sync hook call took {elapsed}s, expected it to block"
    assert plugin.called
    assert plugin.finished
