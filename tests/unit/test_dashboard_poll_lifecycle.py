"""A dashboard poll owns its slot until its result reaches the widgets."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from textual.worker import Worker, WorkerState

from bernstein.cli.dashboard_app import BernsteinApp


class Central:
    """Successful real polling requests, with a controlled cluster response."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.calls = 0
        self.active = 0
        self.maximum_active = 0
        self.unavailable = False

    def get(self, url: str, *, timeout: float, headers: dict[str, str]) -> httpx.Response:
        request = httpx.Request("GET", url)
        if self.unavailable:
            raise httpx.ConnectError("fixture central unavailable", request=request)
        path = request.url.path
        if path != "/cluster/status":
            return httpx.Response(200, json=[] if path == "/tasks" else {}, request=request)
        with self.lock:
            self.calls += 1
            number = self.calls
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            if not self.release.wait(10):
                raise AssertionError("test did not release its cluster response")
            return httpx.Response(
                200,
                json={"nodes": [{"id": f"node-{number}", "name": f"node-{number}", "status": "online"}]},
                request=request,
            )
        finally:
            with self.lock:
                self.active -= 1


class ObservedApp(BernsteinApp):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[dict[str, Any]] = []
        self.refresh_before_success = False

    def _apply_data(self, data: dict[str, Any]) -> None:
        if self.refresh_before_success:
            self.refresh_before_success = False
            # A real refresh caller runs before the current frame is applied.
            self.action_refresh()
        super()._apply_data(data)
        self.frames.append(data)


@pytest.fixture
def central(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Central]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BERNSTEIN_SERVER_URL", "http://central.invalid:8052")
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "fixture-token")
    fixture = Central()
    monkeypatch.setattr(httpx, "get", fixture.get)
    yield fixture
    fixture.release.set()


async def eventually(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def frame_node(app: ObservedApp) -> str:
    return str(app.frames[-1]["cluster"]["nodes"][0]["name"])


@pytest.mark.asyncio
async def test_slow_cluster_poll_reaches_the_mounted_dashboard(central: Central) -> None:
    app = ObservedApp()
    async with app.run_test() as pilot:
        await eventually(lambda: central.calls == 1)
        try:
            # Exercise the real one-second timer while the HTTP response waits.
            await pilot.pause(1.1)
        finally:
            central.release.set()
        await eventually(lambda: bool(app.frames))
        assert frame_node(app) == "node-1"
        assert central.maximum_active == 1


@pytest.mark.asyncio
async def test_periodic_and_manual_refresh_coalesce_pending_and_running_polls(central: Central) -> None:
    app = ObservedApp()
    async with app.run_test():
        await eventually(lambda: central.calls == 1)
        try:
            app._schedule_poll()
            app.action_refresh()
            app._schedule_poll()
            await asyncio.sleep(0.05)
            assert central.calls == 1
            assert central.maximum_active == 1
        finally:
            central.release.set()
        await eventually(lambda: bool(app.frames))
        assert frame_node(app) == "node-1"


@pytest.mark.asyncio
async def test_refresh_does_not_duplicate_a_poll_waiting_to_start(
    central: Central, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = ObservedApp()
    queued: list[Worker[dict[str, Any]]] = []

    def enqueue(work: Callable[[], dict[str, Any]], **kwargs: Any) -> Worker[dict[str, Any]]:
        worker: Worker[dict[str, Any]] = Worker(app, work, group="poll", thread=True)
        queued.append(worker)
        return worker

    monkeypatch.setattr(app, "run_worker", enqueue)
    async with app.run_test():
        app.action_refresh()
        app._schedule_poll()
        assert len(queued) == 1
        assert queued[0].state == WorkerState.PENDING


@pytest.mark.asyncio
async def test_terminal_result_is_applied_before_a_refresh_can_start_another_batch(central: Central) -> None:
    app = ObservedApp()
    app.refresh_before_success = True
    async with app.run_test() as pilot:
        await eventually(lambda: central.calls == 1)
        central.release.set()
        await eventually(lambda: bool(app.frames))
        await pilot.pause()
        assert central.calls == 1
        assert frame_node(app) == "node-1"


@pytest.mark.asyncio
async def test_next_poll_starts_after_the_previous_frame_is_applied(central: Central) -> None:
    app = ObservedApp()
    async with app.run_test():
        await eventually(lambda: central.calls == 1)
        central.release.set()
        await eventually(lambda: bool(app.frames))
        assert frame_node(app) == "node-1"
        app.action_refresh()
        await eventually(lambda: len(app.frames) >= 2)
        assert frame_node(app) == "node-2"
        assert central.maximum_active == 1


@pytest.mark.asyncio
async def test_unavailable_poll_does_not_block_later_recovery(central: Central) -> None:
    central.unavailable = True
    app = ObservedApp()
    async with app.run_test():
        await eventually(lambda: bool(app.frames))
        assert app.frames[-1]["server_unreachable"] is True
        central.unavailable = False
        central.release.set()
        app.action_refresh()
        await eventually(lambda: len(app.frames) >= 2)
        assert app.frames[-1]["server_unreachable"] is False
        assert frame_node(app) == "node-1"


@pytest.mark.asyncio
async def test_unowned_completion_cannot_replace_the_current_frame(central: Central) -> None:
    app = ObservedApp()
    async with app.run_test():
        await eventually(lambda: central.calls == 1)
        original = next(worker for worker in app.workers if worker.group == "poll")
        central.release.set()
        await eventually(lambda: bool(app.frames))
        app.action_refresh()
        await eventually(lambda: len(app.frames) >= 2)
        assert frame_node(app) == "node-2"
        before = len(app.frames)
        app.on_worker_state_changed(Worker.StateChanged(original, WorkerState.SUCCESS))
        assert len(app.frames) == before
        assert frame_node(app) == "node-2"


@pytest.mark.asyncio
async def test_shutdown_neither_starts_nor_applies_late_polling_work(central: Central) -> None:
    class ClosingApp(ObservedApp):
        async def _close_all(self) -> None:
            await super()._close_all()
            central.release.set()
            await eventually(lambda: central.active == 0)
            await asyncio.sleep(0.05)

    app = ClosingApp()
    async with app.run_test() as pilot:
        await eventually(lambda: central.calls == 1)
        await pilot.press("q")
    assert central.calls == 1
    assert not app.frames
    assert app._exception is None


@pytest.mark.asyncio
async def test_poll_slots_belong_to_each_app_instead_of_the_process(central: Central) -> None:
    first, second = ObservedApp(), ObservedApp()

    async def observe(app: ObservedApp) -> None:
        async with app.run_test():
            await eventually(lambda: central.calls >= 2)
            central.release.set()
            await eventually(lambda: bool(app.frames))

    await asyncio.gather(observe(first), observe(second))
    assert first.frames and second.frames
    assert central.maximum_active == 2
