"""FleetAggregator — fans out to per-project task servers.

Owns one :class:`httpx.AsyncClient` and one per-project background SSE task.
Per-project state is exposed as a :class:`ProjectSnapshot` and a unified
event stream merges every project's SSE feed into a single async queue.

The aggregator never blocks the dashboard on a single offline project: a
project that cannot be reached transitions to :attr:`ProjectState.OFFLINE`
and continues to be retried with exponential backoff until success.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bernstein.core.fleet.config import ProjectConfig

logger = logging.getLogger(__name__)


class ProjectState(StrEnum):
    """Top-level liveness for one project row."""

    INITIALIZING = "initializing"
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    PAUSED = "paused"


@dataclass(slots=True)
class ProjectSnapshot:
    """Latest known state for one project.

    Attributes:
        name: Display name from config.
        state: Top-level liveness.
        agents: Number of live agents.
        active_agents_roles: Sorted list of roles currently working.
        pending_approvals: Number of approvals queued.
        last_sha: Last known commit SHA (``""`` when unknown).
        cost_usd: Rolling 7-day cost in USD.
        cost_history: Last 7 daily cost samples for sparkline rendering.
        last_event_ts: Epoch seconds of the last successful update.
        last_error: Most recent error message from a failed fetch.
        offline_since: Epoch seconds when ``state`` first flipped to OFFLINE.
        run_state: Plain-language run state from the task server.
    """

    name: str
    state: ProjectState = ProjectState.INITIALIZING
    agents: int = 0
    active_agents_roles: list[str] = field(default_factory=list[str])
    pending_approvals: int = 0
    last_sha: str = ""
    cost_usd: float = 0.0
    cost_history: list[float] = field(default_factory=list[float])
    last_event_ts: float = 0.0
    last_error: str = ""
    offline_since: float | None = None
    run_state: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this snapshot."""
        return {
            "name": self.name,
            "state": self.state.value,
            "agents": self.agents,
            "active_agents_roles": list(self.active_agents_roles),
            "pending_approvals": self.pending_approvals,
            "last_sha": self.last_sha,
            "cost_usd": self.cost_usd,
            "cost_history": list(self.cost_history),
            "last_event_ts": self.last_event_ts,
            "last_error": self.last_error,
            "offline_since": self.offline_since,
            "run_state": self.run_state,
        }


@dataclass(slots=True, frozen=True)
class AggregatorEvent:
    """One unified bus event tagged with its source project name.

    Attributes:
        project: Name of the source project.
        event: Raw SSE event type (``task.created``, ``cost.update``, ...).
        data: Decoded JSON payload (or empty dict when not parseable).
        ts: Epoch seconds at which the event was received.
    """

    project: str
    event: str
    data: dict[str, Any]
    ts: float


def _extract_snapshot_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Derive snapshot fields from a ``/status`` payload.

    The shape varies between versions; we look up tolerant defaults so a
    minor server change never breaks the dashboard.
    """
    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    runtime = payload.get("runtime", {}) if isinstance(payload, dict) else {}
    agents_block = payload.get("agents", {}) if isinstance(payload, dict) else {}

    if isinstance(agents_block, dict):
        items = agents_block.get("items", [])
        agents_count = agents_block.get("count", len(items) if isinstance(items, list) else 0)
    else:
        items = []
        agents_count = 0

    roles: list[str] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                role = item.get("role")
                if isinstance(role, str) and role:
                    roles.append(role)
    roles = sorted(set(roles))

    pending = 0
    approvals_block = payload.get("approvals") if isinstance(payload, dict) else None
    if isinstance(approvals_block, dict):
        pending = int(approvals_block.get("pending", 0) or 0)
    elif isinstance(summary, dict):
        pending = int(summary.get("pending_approvals", 0) or 0)

    sha = ""
    if isinstance(runtime, dict):
        sha_raw = runtime.get("last_commit_sha") or runtime.get("head_sha")
        if isinstance(sha_raw, str):
            sha = sha_raw[:12]

    cost = 0.0
    if isinstance(summary, dict):
        cost_raw = summary.get("cost_usd")
        if isinstance(cost_raw, int | float):
            cost = float(cost_raw)

    run_state = ""
    if isinstance(runtime, dict):
        rs = runtime.get("state") or runtime.get("phase")
        if isinstance(rs, str):
            run_state = rs

    return {
        "agents": int(agents_count or 0),
        "active_agents_roles": roles,
        "pending_approvals": pending,
        "last_sha": sha,
        "cost_usd": cost,
        "run_state": run_state,
    }


class FleetAggregator:
    """Fans out to per-project task servers.

    Lifecycle:
        * :meth:`start` — kicks off background polling + SSE workers.
        * :meth:`snapshots` — fast read of every project's latest state.
        * :meth:`events` — async iterator over :class:`AggregatorEvent`.
        * :meth:`stop` — cancels workers and closes the HTTP client.

    The aggregator is the single shared dependency for both the TUI and the
    web view; do not instantiate two of them per process.
    """

    def __init__(
        self,
        projects: list[ProjectConfig],
        *,
        poll_interval_s: float = 2.0,
        http_timeout_s: float = 5.0,
        backoff_min_s: float = 1.0,
        backoff_max_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
        cost_window_days: int = 7,
    ) -> None:
        """Build the aggregator.

        Args:
            projects: Project configs to fan out to.
            poll_interval_s: How often the status/cost poll runs per project.
            http_timeout_s: Per-request timeout. Kept low so a hung server
                cannot block another row's update.
            backoff_min_s: Minimum reconnect delay after a failure.
            backoff_max_s: Maximum reconnect delay after repeated failures.
            client: Optional pre-built ``httpx.AsyncClient``. Useful for tests.
            cost_window_days: How many daily samples to keep per project.
        """
        self._projects: dict[str, ProjectConfig] = {p.name: p for p in projects}
        self._poll_interval_s = poll_interval_s
        self._http_timeout_s = http_timeout_s
        self._backoff_min_s = backoff_min_s
        self._backoff_max_s = backoff_max_s
        self._cost_window_days = cost_window_days
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=http_timeout_s)
        self._snapshots: dict[str, ProjectSnapshot] = {name: ProjectSnapshot(name=name) for name in self._projects}
        self._tasks: list[asyncio.Task[None]] = []
        self._event_queue: asyncio.Queue[AggregatorEvent] = asyncio.Queue(maxsize=1024)
        self._stop_event: asyncio.Event = asyncio.Event()
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn one poller and one SSE worker per project."""
        if self._started:
            return
        self._started = True
        for project in self._projects.values():
            self._tasks.append(asyncio.create_task(self._poll_loop(project), name=f"fleet-poll-{project.name}"))
            self._tasks.append(asyncio.create_task(self._sse_loop(project), name=f"fleet-sse-{project.name}"))

    async def stop(self) -> None:
        """Cancel workers and close the HTTP client."""
        self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self._owned_client:
            await self._client.aclose()
        self._started = False

    async def __aenter__(self) -> FleetAggregator:
        await self.start()
        return self

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def projects(self) -> list[ProjectConfig]:
        """Return the project configs in registration order."""
        return list(self._projects.values())

    def snapshots(self) -> list[ProjectSnapshot]:
        """Return a snapshot copy for every project."""
        # Return shallow copies so callers can't mutate live state.
        return [
            ProjectSnapshot(
                name=s.name,
                state=s.state,
                agents=s.agents,
                active_agents_roles=list(s.active_agents_roles),
                pending_approvals=s.pending_approvals,
                last_sha=s.last_sha,
                cost_usd=s.cost_usd,
                cost_history=list(s.cost_history),
                last_event_ts=s.last_event_ts,
                last_error=s.last_error,
                offline_since=s.offline_since,
                run_state=s.run_state,
            )
            for s in self._snapshots.values()
        ]

    def snapshot(self, name: str) -> ProjectSnapshot | None:
        """Return the live snapshot for ``name`` (or ``None``)."""
        return self._snapshots.get(name)

    async def events(self) -> AsyncIterator[AggregatorEvent]:
        """Yield merged SSE events from every project until stopped."""
        while not self._stop_event.is_set():
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            yield event

    # ------------------------------------------------------------------
    # Pollers
    # ------------------------------------------------------------------

    async def _poll_loop(self, project: ProjectConfig) -> None:
        backoff = self._backoff_min_s
        while not self._stop_event.is_set():
            try:
                await self._poll_once(project)
                backoff = self._backoff_min_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._mark_offline(project.name, str(exc))
                logger.debug("fleet: poll failure for %s: %s", project.name, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._backoff_max_s)
                continue
            await asyncio.sleep(self._poll_interval_s)

    async def _poll_once(self, project: ProjectConfig) -> None:
        response = await self._client.get(project.status_url)
        response.raise_for_status()
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise httpx.HTTPError(f"non-JSON status: {exc}") from exc
        if not isinstance(payload, dict):
            raise httpx.HTTPError("status payload is not an object")

        snapshot = self._snapshots[project.name]
        fields = _extract_snapshot_fields(payload)
        snapshot.agents = fields["agents"]
        snapshot.active_agents_roles = fields["active_agents_roles"]
        snapshot.pending_approvals = fields["pending_approvals"]
        snapshot.last_sha = fields["last_sha"]
        snapshot.cost_usd = fields["cost_usd"]
        snapshot.run_state = fields["run_state"]
        snapshot.last_event_ts = time.time()
        snapshot.last_error = ""
        snapshot.offline_since = None
        # Push the latest cost sample into the rolling history.
        history = snapshot.cost_history
        history.append(fields["cost_usd"])
        if len(history) > self._cost_window_days:
            del history[: len(history) - self._cost_window_days]
        if snapshot.state in (ProjectState.OFFLINE, ProjectState.INITIALIZING, ProjectState.DEGRADED):
            snapshot.state = ProjectState.ONLINE

    def _mark_offline(self, name: str, message: str) -> None:
        snapshot = self._snapshots.get(name)
        if snapshot is None:
            return
        if snapshot.state != ProjectState.OFFLINE:
            snapshot.offline_since = time.time()
        snapshot.state = ProjectState.OFFLINE
        snapshot.last_error = message

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------

    async def _sse_loop(self, project: ProjectConfig) -> None:
        backoff = self._backoff_min_s
        while not self._stop_event.is_set():
            try:
                await self._sse_consume(project)
                # Stream ended cleanly; reconnect quickly.
                backoff = self._backoff_min_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("fleet: SSE failure for %s: %s", project.name, exc)
                self._mark_offline(project.name, f"sse: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._backoff_max_s)

    async def _sse_consume(self, project: ProjectConfig) -> None:
        async with self._client.stream(
            "GET",
            project.events_url,
            timeout=httpx.Timeout(self._http_timeout_s, read=None),
        ) as response:
            response.raise_for_status()
            event_name = ""
            data_lines: list[str] = []
            async for raw in response.aiter_lines():
                if self._stop_event.is_set():
                    return
                if raw == "":
                    if event_name and data_lines:
                        await self._emit(project.name, event_name, "\n".join(data_lines))
                    event_name = ""
                    data_lines = []
                    continue
                if raw.startswith(":"):
                    continue
                if raw.startswith("event:"):
                    event_name = raw[6:].strip()
                elif raw.startswith("data:"):
                    data_lines.append(raw[5:].lstrip())

    async def _emit(self, project_name: str, event_name: str, data_text: str) -> None:
        try:
            data: dict[str, Any] = json.loads(data_text) if data_text else {}
            if not isinstance(data, dict):
                data = {"value": data}
        except json.JSONDecodeError:
            data = {"raw": data_text}
        bus_event = AggregatorEvent(project=project_name, event=event_name, data=data, ts=time.time())
        try:
            self._event_queue.put_nowait(bus_event)
        except asyncio.QueueFull:
            # Drop the oldest to make room — the dashboard must remain live.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._event_queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._event_queue.put_nowait(bus_event)
