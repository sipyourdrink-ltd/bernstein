"""An orphan verdict must be taken on the task's live status, not the snapshot.

Regression for 2026-09-02: the tick snapshot is fetched once at the top of the
tick, so a task that reached `done` and had its branch merged after the fetch
still looked open when the orphan path judged it - and was reopened.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from bernstein.core.agents import agent_lifecycle
from bernstein.core.agents.agent_lifecycle import handle_orphaned_task
from bernstein.core.tasks.models import Task


class _Sentinel(Exception):
    """Raised in place of the work that follows the already-resolved check."""


def _task(status: str) -> Task:
    return Task.from_dict(
        {"id": "t1", "title": "phase-1: shout helper", "description": "d", "role": "resolver", "status": status}
    )


class _Client:
    """Serves the live status the tick snapshot has not caught up with."""

    def __init__(self, live_status: str | None) -> None:
        self.live_status = live_status
        self.gets: list[str] = []

    def get(self, url: str, **_k: Any) -> Any:
        self.gets.append(url)
        if self.live_status is None:
            raise httpx.ConnectError("server down")
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: _task(self.live_status).to_dict())


@pytest.fixture(autouse=True)
def _stub_downstream(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Silence metrics IO and mark the point past the already-resolved check."""
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        agent_lifecycle,
        "emit_orphan_metrics",
        lambda *_a, **kw: calls.append(("metrics", kw.get("error_type"))),
    )

    def _proceeded(*_a: Any, **_k: Any) -> bool:
        raise _Sentinel

    monkeypatch.setattr(agent_lifecycle, "_handle_failure_detection", _proceeded)
    return calls


def _orch(tmp_path: Path, client: _Client) -> SimpleNamespace:
    return SimpleNamespace(
        _client=client,
        _workdir=tmp_path,
        _config=SimpleNamespace(recovery="retry", max_crash_retries=2, server_url="http://x"),
    )


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        id="resolver-1", provider="codex", exit_code=0, model_config=None, spawn_ts=1.0, heartbeat_ts=0.0
    )


def test_a_task_done_since_the_snapshot_is_not_reopened(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, _stub_downstream: list[tuple[str, Any]]
) -> None:
    """Snapshot says claimed, server says done -> already resolved, no action."""
    client = _Client(live_status="done")
    with caplog.at_level("INFO"):
        handle_orphaned_task(_orch(tmp_path, client), "t1", _session(), {"claimed": [_task("claimed")]})
    assert any("already resolved" in r.message for r in caplog.records)
    assert client.gets == ["http://x/tasks/t1"], "a snapshot hit must still re-read live"
    assert ("metrics", "already_resolved") in _stub_downstream


def test_a_genuinely_open_task_still_proceeds(tmp_path: Path) -> None:
    client = _Client(live_status="claimed")
    with pytest.raises(_Sentinel):
        handle_orphaned_task(_orch(tmp_path, client), "t1", _session(), {"claimed": [_task("claimed")]})


def test_the_snapshot_is_the_fallback_when_the_server_is_unreachable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed re-fetch must not abort the orphan path; the snapshot still rules."""
    client = _Client(live_status=None)
    with caplog.at_level("WARNING"):
        handle_orphaned_task(_orch(tmp_path, client), "t1", _session(), {"done": [_task("done")]})
    assert any("live re-fetch failed" in r.message for r in caplog.records)
