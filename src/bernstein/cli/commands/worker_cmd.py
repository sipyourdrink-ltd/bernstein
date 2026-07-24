"""Worker command: join a Bernstein cluster as a remote worker node.

Usage:
  bernstein worker --server http://central:8052 --token SECRET
  bernstein worker --server http://central:8052 --token SECRET --name gpu-node-1 --slots 8

The worker registers itself with the central task server, starts a
heartbeat loop, and polls for tasks to execute locally via the CLI
agent adapter.

Cluster mode enables bearer auth on the central server, so ``--token`` (or the
``BERNSTEIN_AUTH_TOKEN`` env var) is required: pass the value the central node
was started with (``BERNSTEIN_AUTH_TOKEN`` or ``BERNSTEIN_CLUSTER_AUTH_SECRET``).
When the central auto-generates one it is written to ``.sdd/runtime/auth.token``.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from contextlib import suppress
from pathlib import Path

import click
import httpx

from bernstein.cli.helpers import adapter_cli_choice, console
from bernstein.core.capacity_wake import CapacityWake, WakeReason
from bernstein.core.poll_config import PollConfig, validate_poll_config

logger = logging.getLogger(__name__)


def _detect_worker_adapter() -> str:
    """Detect the best available CLI agent on this worker node.

    Returns:
        Adapter name (e.g. "claude", "codex", "gemini").
    """
    with suppress(Exception):
        from bernstein.core.agent_discovery import discover_agents_cached

        disc = discover_agents_cached()
        for agent in disc.agents:
            if agent.logged_in:
                return agent.name
    return "claude"


def _adapter_model_profile(adapter_name: str) -> tuple[str | None, list[str]]:
    """Resolve ``(default_model, supported_models)`` for an adapter.

    Consults the local agent-discovery cache -- the same source
    :func:`_detect_worker_adapter` uses -- so the worker advertises and defaults
    to the models the adapter it actually runs supports, rather than a fixed
    Claude tier list (#2804). Returns ``(None, [])`` when the adapter is not
    locally discoverable, leaving the caller to keep its prior behaviour.
    """
    with suppress(Exception):
        from bernstein.core.agent_discovery import discover_agents_cached

        for agent in discover_agents_cached().agents:
            if agent.name == adapter_name:
                default = agent.default_model or None
                models = list(agent.available_models or [])
                if default and default not in models:
                    models = [default, *models]
                return default, models
    return None, []


class WorkerLoop:
    """Main loop for a worker node: register, heartbeat, claim + execute tasks.

    The worker is intentionally simple - a thin loop that:
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
        pool: str | None = None,
        pool_hash: str | None = None,
        model: str | None = None,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._name = name or socket.gethostname()
        self._max_agents = max_agents
        self._roles = roles or ["backend", "qa", "security", "frontend"]
        self._labels = labels or {}
        self._auth_token = auth_token
        self._pool = pool
        self._pool_hash = pool_hash
        self._enrolment_keyid: str | None = None
        self._adapter_name = adapter or _detect_worker_adapter()
        # Advertise the adapter as a node label so the cluster topology view
        # (``bernstein cluster nodes``) can show which agent backend each node
        # runs (#2874). An operator-supplied ``adapter`` label still wins.
        self._labels.setdefault("adapter", self._adapter_name)
        # An adapter passed explicitly to the worker is an operator pin and
        # must not be overridden by model-name inference at spawn time
        # (#2751); an auto-detected adapter is not a pin.
        self._adapter_pinned = adapter is not None
        self._model = model
        # Resolve, once, what this node's adapter can run so a task with no
        # explicit model gets an adapter-appropriate default (rather than a
        # Claude tier name the adapter would refuse) and the node advertises a
        # truthful capability list to server-side placement (#2804).
        self._spawn_default_model = self._resolve_spawn_default_model()
        self._supported_models = self._resolve_supported_models()
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
        # Terminal outcomes reaped from finished agents, drained to the server
        # by _report_finished. (task_id, ok, detail). Worker-side reporting is
        # authoritative and does not depend on the in-agent completion curl
        # (#2808): without it a finished agent leaves its task CLAIMED forever.
        self._pending_reports: list[tuple[str, bool, str]] = []
        self._wake = CapacityWake()

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    @staticmethod
    def _is_claude_adapter(adapter_name: str) -> bool:
        """Whether an adapter serves the Claude tier-name cascade (sonnet/opus/haiku)."""
        with suppress(Exception):
            from bernstein.core.bandit_router import BanditRouter

            return bool(BanditRouter.router_applicable(adapter_name))
        return adapter_name == "claude"

    def _resolve_spawn_default_model(self) -> str | None:
        """Default model to hand the spawner for tasks that carry none.

        An explicit ``--model`` always wins. Claude-family adapters run the
        tier-named cascade the planner emits, so no default is forced (leaving
        the existing routing untouched). Non-Claude adapters get their own
        discovered default, so an unpinned Claude tier name coerces to a model
        the adapter can actually run instead of raising ``ModelNotConfiguredError``
        (#2804).
        """
        if self._model:
            return self._model
        if self._is_claude_adapter(self._adapter_name):
            return None
        default, _models = _adapter_model_profile(self._adapter_name)
        return default

    def _resolve_supported_models(self) -> list[str]:
        """Model set this node advertises to server-side placement.

        Claude-family adapters keep advertising the tier names planner-emitted
        tasks reference. Other adapters advertise the concrete models the local
        discovery cache reports for them, so a qwen node stops falsely claiming
        the Claude tiers it would refuse -- the placement filter in
        ``best_node_for_task`` trusts this list (#2804).
        """
        if self._is_claude_adapter(self._adapter_name):
            return ["sonnet", "opus", "haiku"]
        _default, models = _adapter_model_profile(self._adapter_name)
        return models

    def _resolve_pool_hash(self) -> str | None:
        """Resolve the pool hash to enrol against, if a pool was requested.

        Prefers an explicit ``--pool-hash``; otherwise projects the local audit
        chain to find the active hash for ``--pool`` name. Returns ``None`` when
        no pool was requested (the worker behaves exactly as before).
        """
        if self._pool_hash:
            return self._pool_hash
        if not self._pool:
            return None
        with suppress(Exception):
            from bernstein.core.sandbox.pool_registry import project_pool_registry
            from bernstein.core.security.audit_chain import AuditChainStore

            audit_dir = self._workdir / ".sdd" / "audit"
            if audit_dir.is_dir():
                events = [e for e in AuditChainStore(audit_dir).query() if str(e.event_type).startswith("pool.")]
                return project_pool_registry(events).get(self._pool)
        return None

    def _enrol(self, client: httpx.Client) -> None:
        """Sign an enrolment receipt binding the install identity to the pool.

        Additive: only runs when ``--pool`` (or ``--pool-hash``) was given. The
        receipt is signed with the worker's Ed25519 install identity, written to
        the content-addressed enrolment store, and offered to the server over
        the same outbound-only channel the worker already uses -- no inbound
        socket is ever opened (#2547).
        """
        pool_hash = self._resolve_pool_hash()
        if not pool_hash:
            if self._pool:
                console.print(f"[yellow]Pool {self._pool!r} not resolvable locally; skipping enrolment.[/yellow]")
            return
        import time as _time

        from bernstein.core.identity.http_signing import default_keystore
        from bernstein.core.sandbox.pool_enrolment import build_enrolment_receipt

        try:
            keystore = default_keystore()
            receipt = build_enrolment_receipt(
                keystore=keystore,
                pool_hash=pool_hash,
                worker_name=self._name,
                created=int(_time.time()),
            )
        except Exception as exc:  # keystore/permission failure must not crash the worker
            logger.warning("pool enrolment signing skipped: %s", exc)
            return

        self._enrolment_keyid = receipt.keyid
        self._write_enrolment(receipt)
        pool_label = self._pool or pool_hash[:16]
        console.print(f"[green]Enrolled[/green] into pool [bold]{pool_label}[/bold] as key {receipt.keyid[:12]}...")

        with suppress(httpx.HTTPError):
            client.post(
                f"{self._server_url}/cluster/pools/enroll",
                json=receipt.to_dict(),
                headers=self._headers(),
                timeout=10.0,
            )

    def _write_enrolment(self, receipt: object) -> None:
        """Persist a signed enrolment receipt to the content-addressed store."""
        with suppress(Exception):
            enrol_dir = self._workdir / ".sdd" / "sandbox" / "enrolments"
            enrol_dir.mkdir(parents=True, exist_ok=True)
            keyid = getattr(receipt, "keyid", "worker")
            safe = "".join(c for c in keyid if c.isalnum() or c in ("-", "_")) or "worker"
            (enrol_dir / f"{safe}.json").write_text(receipt.to_canonical_json(), encoding="utf-8")  # type: ignore[attr-defined]

    @property
    def available_slots(self) -> int:
        # Reap completed tasks; capacity wake fires if any slot freed.
        self._reap_finished()
        return max(0, self._max_agents - len(self._active_tasks))

    def _reap_finished(self) -> bool:
        """Remove tasks whose agent process has exited.

        Each reaped task is queued as a terminal outcome (success/failure from
        the agent exit code) for _report_finished to post to the server, so a
        finished agent transitions its task instead of leaving it CLAIMED
        (#2808). Success/failure is derived from the exit code: a clean exit
        (0) completes the task; a non-zero exit or signal kill fails it.

        Returns:
            ``True`` if at least one task was reaped (a slot became available).
        """
        from bernstein.core.platform_compat import process_alive

        finished: list[tuple[str, bool, str]] = []
        for task_id, pid in list(self._active_tasks.items()):
            exited = False
            ok = True
            detail = "worker reaped agent process (exit status unavailable)"
            try:
                reaped_pid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                # Already reaped elsewhere; the exit status is gone. Treat as a
                # completion and let server-side verification have the final say.
                exited = True
            else:
                if reaped_pid == pid:
                    exited = True
                    exit_code = os.waitstatus_to_exitcode(status)
                    ok = exit_code == 0
                    detail = f"agent exited with code {exit_code}"
                elif not process_alive(pid):
                    exited = True
            if exited:
                finished.append((task_id, ok, detail))
        for task_id, ok, detail in finished:
            del self._active_tasks[task_id]
            self._pending_reports.append((task_id, ok, detail))
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
                "supported_models": self._supported_models,
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
            if resp.status_code in (401, 403):
                # An auth rejection is a credential/config error, not a
                # transient fault: re-sending the same header every 5s never
                # succeeds and hides the real cause (#2805 / #2802). Abort.
                self._fail_fast_auth(resp.status_code)
                return None
            logger.warning("Registration failed: %d %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            logger.warning("Registration error: %s", exc)
        return None

    def _fail_fast_auth(self, status_code: int) -> None:
        """Stop the worker on an unrecoverable registration auth rejection.

        Prints an actionable message naming the credential the central server
        requires and where to obtain it, then aborts the run loop instead of
        retrying a request that cannot succeed.
        """
        console.print(
            f"[red]Registration rejected ({status_code}):[/red] the central server "
            "requires a bearer token this worker did not present (or presented an "
            "unaccepted one)."
        )
        console.print(
            "  Pass it with [bold]--token[/bold] or the [bold]BERNSTEIN_AUTH_TOKEN[/bold] env var. "
            "When the central node auto-generates a token it writes it to "
            "[bold].sdd/runtime/auth.token[/bold]; copy that value to this worker.\n"
            "  In cluster mode the same token must equal BERNSTEIN_CLUSTER_AUTH_SECRET if one is set."
        )
        self._running = False
        self._wake.signal_abort()

    def _heartbeat(self, client: httpx.Client, node_id: str) -> bool:
        """Send heartbeat with updated capacity. Returns True on success."""
        payload = {
            "capacity": {
                "max_agents": self._max_agents,
                "available_slots": self.available_slots,
                "active_agents": len(self._active_tasks),
                "gpu_available": False,
                "supported_models": self._supported_models,
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

    def _claim_task(self, client: httpx.Client, role: str, node_id: str | None = None) -> dict | None:
        """Try to claim the next task for a given role. Returns task dict or None.

        The worker's node id is recorded as the claim owner so the server can
        release this node's in-flight tasks when it leaves the cluster (#2801).
        """
        params = {"claimed_by_session": node_id} if node_id else None
        with suppress(httpx.HTTPError):
            resp = client.get(
                f"{self._server_url}/tasks/next/{role}",
                params=params,
                headers=self._headers(),
                timeout=10.0,
            )
            if resp.status_code == 200:
                return resp.json()
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

    def _report_finished(self, client: httpx.Client) -> None:
        """Post queued terminal outcomes for reaped agents to the server.

        This is what drives a remotely executed task to a terminal state: the
        in-agent completion curl may never fire (crash, kill, wrong URL), so
        the worker reports completion/failure itself (#2808). A task already
        terminal server-side simply no-ops the redundant report.
        """
        if not self._pending_reports:
            return
        pending, self._pending_reports = self._pending_reports, []
        for task_id, ok, detail in pending:
            if ok:
                self._complete_task(client, task_id, detail)
            else:
                self._fail_task(client, task_id, detail)

    def _spawn_agent(self, task: dict) -> int | None:
        """Spawn a CLI agent process to work on a task. Returns PID or None."""
        from bernstein import get_templates_dir
        from bernstein.adapters.registry import get_adapter
        from bernstein.core.models import Task
        from bernstein.core.spawner import AgentSpawner

        task_id = task.get("id", "unknown")
        title = task.get("title", "")

        logger.info("Spawning agent for task %s: %s", task_id, title[:60])

        try:
            adapter = get_adapter(self._adapter_name)
            spawner = AgentSpawner(
                adapter=adapter,
                templates_dir=get_templates_dir(self._workdir) / "roles",
                workdir=self._workdir,
                adapter_pinned=self._adapter_pinned,
                # Adapter-appropriate default so a task carrying no explicit
                # model coerces to a model the adapter can run instead of a
                # Claude tier name it would refuse (#2804).
                default_model=self._spawn_default_model,
            )
            # Spawned agents reach the central server through the standard
            # env vars. Adapters launch agents with an allowlist-filtered
            # environment (build_filtered_env), so only allowlisted vars
            # propagate; both vars below are in the base allowlist in
            # adapters/env_isolation.py.
            os.environ["BERNSTEIN_SERVER_URL"] = self._server_url
            if self._auth_token:
                os.environ["BERNSTEIN_AUTH_TOKEN"] = self._auth_token
            # Reconstruct the full Task from the claimed dict so per-step
            # model / cli / effort / scope / metadata are honoured rather than
            # discarded (#2804). from_dict coerces enum fields and tolerates
            # missing optionals.
            session = spawner.spawn_for_tasks([Task.from_dict(task)])
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
            if not self._running:
                # Fail-fast path (e.g. auth rejection) already aborted the
                # loop; do not print a misleading retry notice.
                return None
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

    def _claim_available_tasks(self, client: httpx.Client, node_id: str | None = None) -> None:
        """Claim tasks for each role while slots remain."""
        for role in self._roles:
            if self.available_slots <= 0:
                return
            task = self._claim_task(client, role, node_id)
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
        with suppress(httpx.HTTPError):
            client.delete(
                f"{self._server_url}/cluster/nodes/{node_id}",
                headers=self._headers(),
                timeout=5.0,
            )
            console.print(f"[dim]Unregistered node {node_id}[/dim]")

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

        console.print(f"[bold cyan]Bernstein Worker[/bold cyan]: {self._name}")
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
            self._enrol(client)
            last_heartbeat = time.monotonic()

            while self._running:
                node_id, last_heartbeat = self._do_heartbeat(client, node_id, heartbeat_s, last_heartbeat)
                if node_id is None:
                    continue

                if self.available_slots > 0:
                    self._claim_available_tasks(client, node_id)

                # Drive reaped agents to a terminal state on the server. Reading
                # available_slots above already reaped finished agents into the
                # pending queue.
                self._report_finished(client)

                if self._wake.wait(timeout_s=poll_s) == WakeReason.ABORT:
                    break

            # Flush any outcomes reaped in the final cycle before leaving.
            self._reap_finished()
            self._report_finished(client)
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
    # Derived from the live adapter registry (same source as the seed ``cli:``
    # allowlist and ``run --cli``) so a worker resolves any selectable adapter
    # instead of a stale hardcoded subset (issue #2807).
    type=adapter_cli_choice(),
    help="CLI agent adapter (default: auto-detect).",
)
@click.option(
    "--model",
    default=None,
    help="Default model for tasks that carry no explicit model (default: the adapter's own default).",
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
@click.option(
    "--pool",
    default=None,
    help="Named sandbox pool to enrol into (signs an Ed25519 enrolment receipt).",
)
@click.option(
    "--pool-hash",
    default=None,
    help="Explicit pool hash to enrol against (overrides --pool name resolution).",
)
def worker(
    server: str,
    name: str | None,
    slots: int,
    roles: str,
    labels: tuple[str, ...],
    token: str | None,
    adapter: str | None,
    model: str | None,
    poll_interval: int,
    poll_interval_ms: int | None,
    heartbeat_interval_ms: int,
    pool: str | None,
    pool_hash: str | None,
) -> None:
    """Join a Bernstein cluster as a worker node.

    \b
    The worker connects to a central Bernstein server, registers itself,
    and starts pulling tasks to execute locally. Use this to distribute
    work across multiple machines.

    \b
    Cluster mode requires a bearer token: pass --token (or set
    BERNSTEIN_AUTH_TOKEN) to the value the central node was started with.

    \b
    Examples:
      bernstein worker --server http://central:8052 --token SECRET
      bernstein worker --server http://central:8052 --token SECRET --name gpu-box --slots 8
      bernstein worker --server http://central:8052 --token SECRET --label gpu=true
      bernstein worker --server http://central:8052 --token SECRET --poll-interval-ms 2000
      BERNSTEIN_SERVER_URL=http://central:8052 BERNSTEIN_AUTH_TOKEN=SECRET bernstein worker
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
        model=model,
        poll_interval=poll_interval,
        poll_config=cfg,
        pool=pool,
        pool_hash=pool_hash,
    )
    loop.run()
