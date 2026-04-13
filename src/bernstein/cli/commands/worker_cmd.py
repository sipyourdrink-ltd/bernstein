"""Worker command: join a Bernstein cluster as a remote worker node.

Usage:
  bernstein worker --server http://central:8052
  bernstein worker --server http://central:8052 --name gpu-node-1 --slots 8
  bernstein worker --server http://central:8052 --token SECRET

The worker registers itself with the central task server, starts a
heartbeat loop, and polls for tasks to execute locally via the CLI
agent adapter.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from pathlib import Path

import click
import httpx

from bernstein.cli.helpers import console
from bernstein.core.capacity_wake import CapacityWake, WakeReason
from bernstein.core.poll_config import PollConfig, validate_poll_config

logger = logging.getLogger(__name__)


def _detect_worker_adapter() -> str:
    """Detect the best available CLI agent on this worker node.

    Returns:
        Adapter name (e.g. "claude", "codex", "gemini").
    """
    try:
        from bernstein.core.agent_discovery import discover_agents_cached

        disc = discover_agents_cached()
        for agent in disc.agents:
            if agent.logged_in:
                return agent.name
    except Exception:
        pass
    return "claude"


class WorkerLoop:
    """Main loop for a worker node: register, heartbeat, claim + execute tasks.

    The worker is intentionally simple — a thin loop that:
    1. Registers with the central server via the heartbeat client
    2. Polls for available tasks matching its supported roles
    3. Spawns short-lived CLI agents to execute claimed tasks
    4. Reports completion/failure back to the server
    5. Sends heartbeats with updated capacity

    The worker exits cleanly on SIGINT/SIGTERM.
    """

    def __init__(
        self,
        server_url: str,
        name: str | None = None,
        max_agents: int = 6,
        roles: list[str] | None = None,
        labels: dict[str, str] | None = None,
        auth_token: str | None = None,
        adapter: str | None = None,
        poll_interval: int = 10,
        poll_config: PollConfig | None = None,
        workdir: Path | None = None,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._name = name or socket.gethostname()
        self._max_agents = max_agents
        self._roles = roles or ["backend", "qa", "security", "frontend"]
        self._labels = labels or {}
        self._auth_token = auth_token
        self._adapter_name = adapter or _detect_worker_adapter()
        # Prefer explicit PollConfig; fall back to legacy poll_interval (seconds).
        if poll_config is not None:
            self._poll_config = poll_config
        else:
            self._poll_config = validate_poll_config(
                {
                    "poll_interval_ms": poll_interval * 1_000,
                    "heartbeat_interval_ms": 15_000,
                }
            )
        self._workdir = workdir or Path.cwd()
        self._running = False
        self._active_tasks: dict[str, int] = {}  # task_id -> pid
        self._wake = CapacityWake()

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    @property
    def available_slots(self) -> int:
        # Reap completed tasks; capacity wake fires if any slot freed.
        self._reap_finished()
        return max(0, self._max_agents - len(self._active_tasks))

    def _reap_finished(self) -> bool:
        """Remove tasks whose agent process has exited.

        Returns:
            ``True`` if at least one task was reaped (a slot became available).
        """
        from bernstein.core.platform_compat import process_alive

        finished: list[str] = []
        for task_id, pid in self._active_tasks.items():
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                finished.append(task_id)
                continue
            # Check if process is still alive
            if not process_alive(pid):
                finished.append(task_id)
        for task_id in finished:
            del self._active_tasks[task_id]
        if finished:
            self._wake.signal_capacity()
        return bool(finished)

    def _register(self, client: httpx.Client) -> str | None:
        """Register with the central server. Returns node ID or None."""
        payload = {
            "name": self._name,
            "url": "",
            "capacity": {
                "max_agents": self._max_agents,
                "available_slots": self.available_slots,
                "active_agents": len(self._active_tasks),
                "gpu_available": False,
                "supported_models": ["sonnet", "opus", "haiku"],
            },
            "labels": self._labels,
            "cell_ids": [],
        }
        try:
            resp = client.post(
                f"{self._server_url}/cluster/nodes",
                json=payload,
                headers=self._headers(),
                timeout=10.0,
            )
            if resp.status_code == 201:
                node_id = resp.json().get("id")
                logger.info("Registered as node %s with %s", node_id, self._server_url)
                return node_id
            logger.warning("Registration failed: %d %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            logger.warning("Registration error: %s", exc)
        return None

    def _heartbeat(self, client: httpx.Client, node_id: str) -> bool:
        """Send heartbeat with updated capacity. Returns True on success."""
        payload = {
            "capacity": {
                "max_agents": self._max_agents,
                "available_slots": self.available_slots,
                "active_agents": len(self._active_tasks),
                "gpu_available": False,
                "supported_models": ["sonnet", "opus", "haiku"],
            },
        }
        try:
            resp = client.post(
                f"{self._server_url}/cluster/nodes/{node_id}/heartbeat",
                json=payload,
                headers=self._headers(),
                timeout=10.0,
            )
            if resp.status_code == 200:
                return True
            if resp.status_code == 404:
                logger.warning("Node %s evicted; will re-register", node_id)
                return False
        except httpx.HTTPError as exc:
            logger.warning("Heartbeat error: %s", exc)
        return True  # Don't re-register on transient errors

    def _claim_task(self, client: httpx.Client, role: str) -> dict | None:
        """Try to claim the next task for a given role. Returns task dict or None."""
        try:
            resp = client.get(
                f"{self._server_url}/tasks/next/{role}",
                headers=self._headers(),
                timeout=10.0,
            )
            if resp.status_code == 200:
                return resp.json()
        except httpx.HTTPError:
            pass
        return None

    def _complete_task(self, client: httpx.Client, task_id: str, summary: str) -> None:
        """Report task completion to the central server."""
        try:
            client.post(
                f"{self._server_url}/tasks/{task_id}/complete",
                json={"result_summary": summary},
                headers=self._headers(),
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            logger.warning("Failed to report completion for %s: %s", task_id, exc)

    def _fail_task(self, client: httpx.Client, task_id: str, reason: str) -> None:
        """Report task failure to the central server."""
        try:
            client.post(
                f"{self._server_url}/tasks/{task_id}/fail",
                json={"reason": reason},
                headers=self._headers(),
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            logger.warning("Failed to report failure for %s: %s", task_id, exc)

    def _spawn_agent(self, task: dict) -> int | None:
        """Spawn a CLI agent process to work on a task. Returns PID or None."""
        from bernstein.core.spawner import AgentSpawner

        task_id = task.get("id", "unknown")
        title = task.get("title", "")
        description = task.get("description", "")
        role = task.get("role", "backend")

        logger.info("Spawning agent for task %s: %s", task_id, title[:60])

        try:
            spawner = AgentSpawner(
                adapter_name=self._adapter_name,
                workdir=self._workdir,
                server_url=self._server_url,
                auth_token=self._auth_token,
            )
            session = spawner.spawn_for_task(
                task_id=task_id,
                title=title,
                description=description,
                role=role,
            )
            if session and session.pid:
                return session.pid
        except Exception as exc:
            logger.warning("Failed to spawn agent for task %s: %s", task_id, exc)
        return None

    def _register_with_retry(self, client: httpx.Client) -> str | None:
        """Try to register, retrying until success or abort signal."""
        node_id = None
        while self._running and node_id is None:
            node_id = self._register(client)
            if node_id is not None:
                break
            console.print("[yellow]Registration failed, retrying in 5s...[/yellow]")
            if self._wake.wait(timeout_s=5.0) == WakeReason.ABORT:
                return None
        return node_id

    def _do_heartbeat(
        self, client: httpx.Client, node_id: str, heartbeat_s: float, last_heartbeat: float
    ) -> tuple[str | None, float]:
        """Send heartbeat if due, re-registering on eviction.

        Returns:
            (node_id or None on abort, updated last_heartbeat timestamp).
        """
        now = time.monotonic()
        if now - last_heartbeat < heartbeat_s:
            return node_id, last_heartbeat

        ok = self._heartbeat(client, node_id)
        if ok:
            return node_id, time.monotonic()

        # Re-register after eviction
        new_id = self._register(client)
        if new_id is None:
            if self._wake.wait(timeout_s=5.0) == WakeReason.ABORT:
                return None, last_heartbeat
            return None, last_heartbeat
        return new_id, time.monotonic()

    def _claim_available_tasks(self, client: httpx.Client) -> None:
        """Claim tasks for each role while slots remain."""
        for role in self._roles:
            if self.available_slots <= 0:
                return
            task = self._claim_task(client, role)
            if task is None:
                continue
            task_id = task.get("id", "unknown")
            pid = self._spawn_agent(task)
            if pid is not None:
                self._active_tasks[task_id] = pid
                console.print(f"  [green]Claimed[/green] {task_id}: {task.get('title', '')[:50]} (pid={pid})")

    def _unregister(self, client: httpx.Client, node_id: str | None) -> None:
        """Graceful shutdown: unregister from the server."""
        if node_id is None:
            return
        try:
            client.delete(
                f"{self._server_url}/cluster/nodes/{node_id}",
                headers=self._headers(),
                timeout=5.0,
            )
            console.print(f"[dim]Unregistered node {node_id}[/dim]")
        except httpx.HTTPError:
            pass

    def run(self) -> None:
        """Main worker loop. Blocks until SIGINT/SIGTERM."""
        self._running = True

        def _handle_signal(signum: int, _frame: object) -> None:
            console.print(f"\n[yellow]Received signal {signum}, shutting down worker...[/yellow]")
            self._running = False
            self._wake.signal_abort()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        poll_s = self._poll_config.poll_interval_ms / 1_000.0
        heartbeat_s = (self._poll_config.heartbeat_interval_ms or 15_000) / 1_000.0

        console.print(f"[bold cyan]Bernstein Worker[/bold cyan] — {self._name}")
        console.print(f"  Server: {self._server_url}")
        console.print(f"  Max agents: {self._max_agents}")
        console.print(f"  Roles: {', '.join(self._roles)}")
        console.print(f"  Adapter: {self._adapter_name}")
        console.print(f"  Poll interval: {poll_s:.1f}s | Heartbeat: {heartbeat_s:.1f}s")
        console.print()

        with httpx.Client() as client:
            node_id = self._register_with_retry(client)
            if node_id is None or not self._running:
                return

            console.print(f"[green]Registered[/green] as node [bold]{node_id}[/bold]")
            last_heartbeat = time.monotonic()

            while self._running:
                node_id, last_heartbeat = self._do_heartbeat(client, node_id, heartbeat_s, last_heartbeat)
                if node_id is None:
                    continue

                if self.available_slots > 0:
                    self._claim_available_tasks(client)

                if self._wake.wait(timeout_s=poll_s) == WakeReason.ABORT:
                    break

            self._unregister(client, node_id)

        console.print("[bold]Worker stopped.[/bold]")


@click.command("worker")
@click.option(
    "--server",
    required=True,
    envvar="BERNSTEIN_SERVER_URL",
    help="URL of the central Bernstein task server (e.g. http://central:8052).",
)
@click.option(
    "--name",
    default=None,
    help="Worker node name (default: hostname).",
)
@click.option(
    "--slots",
    default=6,
    show_default=True,
    help="Maximum concurrent agents on this worker.",
)
@click.option(
    "--roles",
    default="backend,qa,security,frontend",
    show_default=True,
    help="Comma-separated roles this worker accepts.",
)
@click.option(
    "--label",
    "labels",
    multiple=True,
    help="Node labels as key=value (can repeat: --label gpu=true --label region=us-east).",
)
@click.option(
    "--token",
    default=None,
    envvar="BERNSTEIN_AUTH_TOKEN",
    help="Bearer token for cluster auth.",
)
@click.option(
    "--adapter",
    default=None,
    type=click.Choice(["claude", "codex", "gemini", "qwen", "aider", "auto"], case_sensitive=False),
    help="CLI agent adapter (default: auto-detect).",
)
@click.option(
    "--poll-interval",
    default=10,
    show_default=True,
    help="Seconds between task polling cycles (ignored if --poll-interval-ms is set).",
)
@click.option(
    "--poll-interval-ms",
    default=None,
    type=int,
    help="Milliseconds between task polling cycles (overrides --poll-interval).",
)
@click.option(
    "--heartbeat-interval-ms",
    default=15_000,
    show_default=True,
    help="Milliseconds between heartbeats to the central server.",
)
def worker(
    server: str,
    name: str | None,
    slots: int,
    roles: str,
    labels: tuple[str, ...],
    token: str | None,
    adapter: str | None,
    poll_interval: int,
    poll_interval_ms: int | None,
    heartbeat_interval_ms: int,
) -> None:
    """Join a Bernstein cluster as a worker node.

    \b
    The worker connects to a central Bernstein server, registers itself,
    and starts pulling tasks to execute locally. Use this to distribute
    work across multiple machines.

    \b
    Examples:
      bernstein worker --server http://central:8052
      bernstein worker --server http://central:8052 --name gpu-box --slots 8
      bernstein worker --server http://central:8052 --label gpu=true
      bernstein worker --server http://central:8052 --poll-interval-ms 2000
      BERNSTEIN_SERVER_URL=http://central:8052 bernstein worker
    """
    from bernstein.core.poll_config import PollConfigValidationError, validate_poll_config

    # Parse labels
    label_dict: dict[str, str] = {}
    for lbl in labels:
        if "=" in lbl:
            k, v = lbl.split("=", 1)
            label_dict[k.strip()] = v.strip()
        else:
            console.print(f"[red]Invalid label format:[/red] {lbl!r} (expected key=value)")
            raise SystemExit(1)

    role_list = [r.strip() for r in roles.split(",") if r.strip()]

    # Build PollConfig, resolving ms vs seconds precedence.
    effective_poll_ms = poll_interval_ms if poll_interval_ms is not None else poll_interval * 1_000
    try:
        cfg = validate_poll_config(
            {
                "poll_interval_ms": effective_poll_ms,
                "heartbeat_interval_ms": heartbeat_interval_ms,
            }
        )
    except PollConfigValidationError as exc:
        console.print(f"[red]Invalid poll configuration:[/red] {exc}")
        raise SystemExit(1) from exc

    loop = WorkerLoop(
        server_url=server,
        name=name,
        max_agents=slots,
        roles=role_list,
        labels=label_dict,
        auth_token=token,
        adapter=adapter if adapter != "auto" else None,
        poll_interval=poll_interval,
        poll_config=cfg,
    )
    loop.run()
