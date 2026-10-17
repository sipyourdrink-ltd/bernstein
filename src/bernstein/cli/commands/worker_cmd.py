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

A central published through a Cloudflare tunnel behind an Access application
also needs ``CF_ACCESS_CLIENT_ID`` and ``CF_ACCESS_CLIENT_SECRET`` (an Access
service token) in the worker's environment; both headers ride on every request.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import click
import httpx

from bernstein.cli.helpers import access_proxy_headers, adapter_cli_choice, console
from bernstein.core.capacity_wake import CapacityWake, WakeReason
from bernstein.core.log_safe import for_log
from bernstein.core.orchestration.terminal_report_outbox import (
    OUTCOME_DONE,
    OUTCOME_FAILED,
    OUTCOME_UNKNOWN,
    SETTLE_ACKED,
    SETTLE_CONFLICT,
    SETTLE_DUPLICATE,
    SETTLE_GONE,
    SETTLE_RESOLVED,
    SETTLE_SUPERSEDED,
    TerminalReport,
    TerminalReportOutbox,
)
from bernstein.core.poll_config import PollConfig, validate_poll_config

if TYPE_CHECKING:
    import subprocess

logger = logging.getLogger(__name__)

#: Server states that mean the outcome this worker owed already landed.
#: ``pending_approval`` counts: an approval-gated task reached the state the
#: report asked for, it just has a human gate before taking effect.
_SUCCESS_STATES = frozenset({"done", "closed", "pending_approval"})
#: Server states that mean the task ended, but not the way this worker reported.
_FAILURE_STATES = frozenset({"failed", "cancelled", "refused", "abandoned"})

#: Responses that acknowledge a report outright.
_ACK_STATES = frozenset({200, 201})
#: Credential rejections: not transient, but not a reason to stop the node.
_AUTH_STATES = frozenset({401, 403})
#: How long an auth-rejected report is held before it is tried again. A
#: credential fix by the operator must recover the report without a restart.
_AUTH_HOLD_S = 300.0
#: How long a report is held when the central says its task does not exist, and
#: how many times that verdict has to repeat before it is believed.
#:
#: A 404 is not evidence of deletion on its own. ``_require_task_access`` answers
#: 404 for a task that exists in a tenant this token cannot see, an unknown
#: tenant answers 404, and so does a misrouted ingress -- and the first two
#: produce the very ``Task '<id>' not found`` body a real deletion produces.
#: 401 and 403 already hold on that reasoning; a credential-shaped 404 must not
#: be the one rejection that silently discards every queued outcome. Holding
#: forever would be the mirror defect, so an answer that positively names the
#: task is believed once it has repeated across :data:`_GONE_HOLD_S` intervals.
_GONE_HOLD_S = 900.0
_GONE_CONFIRMATIONS = 3

#: How long a terminal report may take to come back before this worker gives up
#: on it. Deliberately far above the ordinary 10s request budget, because
#: ``/complete`` is not an ordinary request: it runs a synchronous ``git fetch``
#: of the agent branch with its own 60s timeout
#: (:func:`bernstein.core.routes.task_crud._fetch_remote_agent_branch`) and then
#: an auto-commit before it answers. A short client timeout turns every slow
#: fetch into a false transport failure, and the first retry - which full jitter
#: puts within a second - lands while the original is still running, so the
#: central fetches the same ref and auto-commits twice. A report that genuinely
#: outlives this ceiling is not retried blind either; see
#: :meth:`WorkerLoop._settle_from_response`.
_TERMINAL_REPORT_TIMEOUT_S = 75.0

#: States that cannot legally accept a terminal report from this node, and will
#: not become able to by being asked again. These are distinct from the racing
#: states (``open``/``claimed``/``in_progress``), where a 409 is a version
#: conflict that a later attempt really can win, and from the terminal states
#: handled by :meth:`WorkerLoop._outcome_matches`.
#:
#: They are deliberately NOT settled away. The outcome they carry is still true
#: and still owed - the task is parked, not resolved - and discarding a real
#: outcome because the central is temporarily unwilling to take it is the exact
#: defect this outbox exists to fix. What they were missing is visibility.
_PARKED_STATES = frozenset(
    {
        "planned",
        "blocked",
        "waiting_for_subtasks",
        "suspended",
        "blocked_by_abandon",
        "blocked_by_failed_dep",
    }
)
#: Retries per escalation for a parked task, and the periodicity after the first.
#: With the 300s backoff cap the first ERROR arrives inside about half an hour
#: and then repeats hourly, which survives a worker restart (the attempt count
#: is durable) without emitting an ERROR every five minutes forever.
_PARKED_ESCALATION_ATTEMPTS = 12

#: Wall-clock one cycle's terminal-report phase may spend before it hands the
#: loop back. This is not about throughput: a due report can block for the whole
#: :data:`_TERMINAL_REPORT_TIMEOUT_S`, and the thread that blocks is the one that
#: heartbeats every 15s and claims tasks. Unbounded, a central that accepts the
#: connection and does not answer turns a backlog into N x 75s per cycle, the
#: node stops beating, gets evicted, its tasks reopen, and the reports replay
#: against claims it no longer holds.
#:
#: This bounds the multiplication, not the single worst case: one report already
#: in flight still gets its full timeout, because cutting that would reinstate
#: the slow-fetch amplification above. Making the last 75s impossible means
#: moving reports off this thread, which is a change to the worker's shape rather
#: than a number, and is reported as a decision for the maintainer instead of one
#: made quietly here.
_REPORT_PHASE_BUDGET_S = 5.0


class _TaskUnreachableError(RuntimeError):
    """The authoritative task state could not be read.

    Deliberately distinct from "the task does not exist": collapsing the two
    would let a two-second outage be read as a deleted task, which is exactly
    how a real outcome gets discarded.
    """


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Seconds requested by a ``Retry-After`` header, if it carries one.

    Only the delta-seconds form is honoured; the HTTP-date form is rare on this
    server and parsing it would need clock-skew handling for no real gain.
    """
    raw = resp.headers.get("Retry-After") if resp.headers else None
    if not raw:
        return None
    try:
        requested = float(raw)
    except (TypeError, ValueError):
        return None
    return requested if requested >= 0 else None


def _retryable_states() -> frozenset[int]:
    """Status codes the shared HTTP retry policy treats as transient.

    Read from :class:`RetryConfig` rather than restated here, so this worker and
    every other Bernstein HTTP client agree on what "transient" means. Deferred
    because importing the server package pulls FastAPI into the worker CLI,
    and the set is only needed once a report has already failed.
    """
    from bernstein.core.server.http_retry import RetryConfig

    return RetryConfig().retryable_status_codes


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
        self._task_sessions: dict[str, str] = {}  # task_id -> session_id
        # The waitable handle for each agent this node spawned, and the only
        # reliable way to read its exit status. A Popen registers itself on
        # subprocess._active when constructed, so the *next* Popen created
        # anywhere in the worker -- the git calls in _push_agent_work for a
        # different task, or the next spawn -- runs _cleanup() and reaps it,
        # recording the status on the handle. A raw os.waitpid on that pid then
        # raises ChildProcessError, which used to be read as "status
        # unknowable": a clean run whose branch was never pushed and whose task
        # was failed. Polling the handle reads whatever status was recorded,
        # whoever got there first. Handles are process-local by nature; after a
        # restart there are none, and the durable outbox -- not a Popen -- is
        # what survives.
        self._agent_procs: dict[str, subprocess.Popen[bytes]] = {}
        # The execution claim this node holds per task, captured from the grant.
        # It is the fence the terminal report must carry: without it a report
        # replayed after this node's claim was released (unregister, heartbeat
        # expiry) reads as an unfenced legacy call and is accepted over the head
        # of the node that took the task over. In-memory is sufficient - the
        # claim only has to outlive the agent, and a restart loses the pid
        # anyway, so there is nothing left to report a status for.
        self._claim_tokens: dict[str, str] = {}
        # Terminal outcomes owed the central server, drained by _report_finished.
        # Worker-side reporting is authoritative and does not depend on the
        # in-agent completion curl (#2808): without it a finished agent leaves
        # its task CLAIMED forever. This queue is durable and hydrates on
        # construction, so an outcome survives both a failed post and a worker
        # restart; a report is dropped only once the server acknowledges it.
        self._outbox = TerminalReportOutbox(self._workdir, node=self._name)
        self._wake = CapacityWake()

    def _headers(self) -> dict[str, str]:
        # A central behind Cloudflare Access needs the service-token headers on
        # every request; the edge answers 403 before the bearer is checked.
        headers: dict[str, str] = {"Content-Type": "application/json", **access_proxy_headers()}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    def _workspace_setup_error(self) -> str | None:
        """Return a setup error if the workspace cannot run agents, else None.

        A worker executes every claimed task by adding a git worktree under its
        workspace (``AgentSpawner`` -> ``WorktreeManager``). When the workspace
        is not a usable git checkout, ``git worktree add`` fails with ``fatal:
        not a git repository`` -- but only *after* the task has been claimed,
        stranding it in ``claimed`` with no live agent (#3018). This preflight
        surfaces the unusable workspace *before* the worker registers or accepts
        any claim, so the worker refuses to start with an actionable message
        instead of claiming work it cannot execute.

        A "usable" workspace is a git work tree with at least one commit: the
        worktree branch (``git worktree add <path> -b <branch>``) is created
        from ``HEAD``, so an empty repo with no commit cannot back a spawn
        either.
        """
        from bernstein.core.git.git_basic import is_git_repo, run_git

        workdir = self._workdir
        if not workdir.is_dir():
            return (
                f"Worker workspace {workdir} does not exist. Mount or check out the target "
                "repository at this path before starting the worker (the published cluster "
                "image expects the repo at /workspace)."
            )
        if not is_git_repo(workdir):
            return (
                f"Worker workspace {workdir} is not a git repository. Each task runs in a git "
                "worktree created under the workspace, so the workspace must be a git checkout "
                "of the target repo. Mount or clone the repo at this path (e.g. /workspace in "
                "the published cluster image) before starting the worker."
            )
        if not run_git(["rev-parse", "--verify", "HEAD"], workdir).ok:
            return (
                f"Worker workspace {workdir} is a git repository with no commits. "
                "'git worktree add' branches from HEAD, so the workspace repo needs at least "
                "one commit before the worker can spawn agents."
            )
        return None

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
        """Remove tasks whose agent process has exited, queueing each outcome.

        Each reaped task is queued in the durable outbox as a terminal outcome
        for _report_finished to deliver, so a finished agent transitions its
        task instead of leaving it CLAIMED (#2808). The outcome comes from the
        exit status read off the handle this worker took at spawn time: a clean
        exit (0) is a completion, a non-zero exit or signal kill a failure.
        Reading it from the handle rather than from ``os.waitpid`` is what makes
        the status reliable -- the child may already have been reaped by
        ``subprocess._cleanup()`` on the worker's behalf, and its status then
        lives only on the handle.

        A task with no handle is queued as *unknown*: a worker that restarted
        never held one, and an in-process, container, or sandbox backend gives
        none. An exit status nobody observed proves nothing, so it must not be
        reported as success; only the server's authoritative state decides an
        unknown outcome (#2808 follow-up).

        On a clean exit the agent's work is pushed to the shared remote so
        the central can fetch and merge it (#6538). Push failure downgrades
        the outcome to a failure: work that never leaves the node is not
        a completion. The resulting (branch, sha) pair is captured exactly once
        here and frozen into the queued report, so every later retry -- and
        every retry after a restart -- re-delivers the same SHA the central
        verifies against instead of re-pushing and moving HEAD under it.

        Returns:
            ``True`` if at least one task was reaped (a slot became available).
        """
        from bernstein.core.platform_compat import process_alive

        finished: list[tuple[str, str, str]] = []
        for task_id, pid in list(self._active_tasks.items()):
            proc = self._agent_procs.get(task_id)
            if proc is not None:
                exit_code = proc.poll()
                if exit_code is None:
                    continue
                outcome = OUTCOME_DONE if exit_code == 0 else OUTCOME_FAILED
                detail = f"agent exited with code {exit_code}"
            else:
                # No handle, so this process never started the agent: a worker
                # that came back after a restart, or a backend that hands out no
                # child at all. An exit status nobody observed proves nothing.
                exited = False
                outcome = OUTCOME_UNKNOWN
                detail = "worker reaped agent process (exit status unavailable)"
                try:
                    reaped_pid, status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    exited = True
                else:
                    if reaped_pid == pid:
                        exited = True
                        exit_code = os.waitstatus_to_exitcode(status)
                        outcome = OUTCOME_DONE if exit_code == 0 else OUTCOME_FAILED
                        detail = f"agent exited with code {exit_code}"
                    elif not process_alive(pid):
                        exited = True
                if not exited:
                    continue
            finished.append((task_id, outcome, detail))
        for task_id, outcome, detail in finished:
            del self._active_tasks[task_id]
            self._agent_procs.pop(task_id, None)
            delivery: tuple[str, str] | None = None
            if outcome == OUTCOME_DONE:
                delivery = self._push_agent_work(task_id)
                if delivery is None:
                    outcome = OUTCOME_FAILED
                    detail = f"{detail}; delivery failed: agent branch not pushed"
            self._task_sessions.pop(task_id, None)
            # Durable before delivery: if the post below fails or this process
            # dies, the outcome is still on disk and still owed. The fence goes
            # into the same record for the same reason: it is re-posted on every
            # retry, possibly after a restart, and a retry that lost it would be
            # accepted as an unfenced legacy call.
            self._outbox.append(
                task_id,
                outcome,
                detail,
                delivery=delivery,
                claim_token=self._claim_tokens.pop(task_id, None),
            )
        if finished:
            self._wake.signal_capacity()
        return bool(finished)

    def _push_agent_work(self, task_id: str) -> tuple[str, str] | None:
        """Push the agent's branch to the shared remote after a clean exit.

        Returns ``(branch_name, head_sha)`` on success, ``None`` on failure.
        The central fetches this branch on ``/complete`` and merges it through
        the standard gate pipeline (#6538).
        """
        import subprocess

        session_id = self._task_sessions.get(task_id)
        if not session_id:
            logger.warning("No session_id for task %s; cannot push agent work", task_id)
            return None

        branch = f"agent/{session_id}"
        worktree_path = self._workdir / ".sdd" / "worktrees" / session_id

        try:
            # Auto-commit uncommitted work in the worktree before pushing.
            if worktree_path.is_dir():
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=worktree_path,
                    capture_output=True,
                    timeout=30,
                )
                subprocess.run(
                    [
                        "git",
                        "commit",
                        "--allow-empty-message",
                        "-m",
                        f"worker auto-commit for {task_id}",
                        "--no-verify",
                    ],
                    cwd=worktree_path,
                    capture_output=True,
                    timeout=30,
                )

            # Get the HEAD SHA of the agent branch.
            sha_result = subprocess.run(
                ["git", "rev-parse", branch],
                cwd=self._workdir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if sha_result.returncode != 0:
                logger.warning("Cannot resolve %s: %s", branch, sha_result.stderr.strip())
                return None
            sha = sha_result.stdout.strip()

            # Push the agent branch to origin.
            push_result = subprocess.run(
                ["git", "push", "origin", f"{branch}:{branch}", "--force"],
                cwd=self._workdir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if push_result.returncode != 0:
                logger.warning("Push failed for %s: %s", branch, push_result.stderr.strip())
                return None

            logger.info("Pushed %s (%s) for task %s", branch, sha[:12], task_id)
            return (branch, sha)

        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Failed to push agent work for task %s: %s", task_id, exc)
            return None

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
            if resp.status_code in (404, 410):
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

    def _complete_task(
        self,
        client: httpx.Client,
        task_id: str,
        summary: str,
        delivery: tuple[str, str] | None = None,
        claim_token: str | None = None,
    ) -> httpx.Response | None:
        """Report task completion to the central server.

        When *delivery* is ``(branch, sha)``, the body includes
        ``agent_branch`` and ``agent_sha`` so the central can fetch and
        merge the remote agent's work (#6538).

        *claim_token* fences the report to the execution claim that produced
        it, so a replay cannot rewrite a task a newer claim now owns. ``None``
        posts it unfenced, which the server still accepts for backward
        compatibility -- so a caller that has a token and omits it is silently
        choosing the unsafe path, and no error will tell it so.

        Returns:
            The response, or ``None`` if the request never completed. The
            caller decides whether that counts as an acknowledgement: the
            status code IS the outcome, and discarding it is what let a 500 or
            a dead socket look like a delivered report. ``None`` is explicitly
            NOT evidence that the central did nothing -- see
            :data:`_TERMINAL_REPORT_TIMEOUT_S`.
        """
        body: dict[str, str] = {"result_summary": summary}
        if delivery is not None:
            body["agent_branch"] = delivery[0]
            body["agent_sha"] = delivery[1]
        if claim_token is not None:
            body["claim_token"] = claim_token
        try:
            return client.post(
                f"{self._server_url}/tasks/{task_id}/complete",
                json=body,
                headers=self._headers(),
                timeout=_TERMINAL_REPORT_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            logger.warning("Failed to report completion for %s: %s", for_log(task_id), exc)
            return None

    def _fail_task(
        self,
        client: httpx.Client,
        task_id: str,
        reason: str,
        claim_token: str | None = None,
    ) -> httpx.Response | None:
        """Report task failure to the central server.

        *claim_token* fences the report as in :meth:`_complete_task`. This is
        the path a live claim uses to correct a completion it later disproved,
        so a token lost here is what lets one node's dead report flip another
        node's finished task.

        Returns:
            The response, or ``None`` if the request never completed.
        """
        body: dict[str, str] = {"reason": reason}
        if claim_token is not None:
            body["claim_token"] = claim_token
        try:
            return client.post(
                f"{self._server_url}/tasks/{task_id}/fail",
                json=body,
                headers=self._headers(),
                timeout=_TERMINAL_REPORT_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            logger.warning("Failed to report failure for %s: %s", for_log(task_id), exc)
            return None

    def _release_task(self, client: httpx.Client, task_id: str, reason: str) -> None:
        """Release a claimed task back to the open pool after a failed spawn.

        A task the worker claimed but could not start (agent spawn failed) must
        return to ``open`` so another node can take it, instead of staying
        ``claimed`` with no live agent (#3018). Distinct from :meth:`_fail_task`,
        which marks the task terminally failed rather than re-queuing it.
        """
        try:
            client.post(
                f"{self._server_url}/tasks/{task_id}/release",
                json={"reason": reason},
                headers=self._headers(),
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            logger.warning("Failed to release task %s back to pool: %s", task_id, exc)

    def _report_finished(self, client: httpx.Client) -> None:
        """Deliver every terminal outcome the server has not acknowledged yet.

        This is what drives a remotely executed task to a terminal state: the
        in-agent completion curl may never fire (crash, kill, wrong URL), so the
        worker reports completion/failure itself (#2808).

        The outcomes come from the durable outbox and only one thing removes
        them: an acknowledgement from the central. A 5xx, a dead socket, or a
        409 therefore leaves the report queued and retried on a later cycle
        instead of discarding it.

        Two bounds stop this phase from eating the loop that carries the
        heartbeats and claims. A report that is not due yet is simply absent from
        :meth:`due`, so nothing here sleeps (#3309). And a report that *is* due
        can still block for the full :data:`_TERMINAL_REPORT_TIMEOUT_S` waiting on
        a server that is doing a legitimately slow fetch, so the phase stops
        starting new reports once it has spent :data:`_REPORT_PHASE_BUDGET_S`.
        Deferring is not discarding: a skipped report stays queued and due and is
        taken on the next cycle, which is why the budget can be small.
        """
        deadline = time.monotonic() + _REPORT_PHASE_BUDGET_S
        reports = self._outbox.due()
        for index, report in enumerate(reports):
            # The first report of a cycle is always attempted, however late this
            # thread already is: a budget that starved the queue entirely would
            # trade a stall for a wedge.
            if index and time.monotonic() > deadline:
                deferred = reports[index:]
                logger.warning(
                    "deferring %d terminal report(s), oldest %s, after %.0fs of report work this cycle; "
                    "each stays queued and due, and this cycle goes back to heartbeats and claims",
                    len(deferred),
                    for_log(deferred[0].task_id),
                    _REPORT_PHASE_BUDGET_S,
                )
                return
            self._deliver_report(client, report)

    def _deliver_report(self, client: httpx.Client, report: TerminalReport) -> None:
        """Attempt one queued report and settle it from the response."""
        if report.outcome == OUTCOME_UNKNOWN:
            self._resolve_unknown_outcome(client, report)
            return
        if report.outcome == OUTCOME_DONE:
            delivery = self._delivery_of(report)
            resp = self._complete_task(client, report.task_id, report.detail, delivery, claim_token=report.claim_token)
        else:
            resp = self._fail_task(client, report.task_id, report.detail, claim_token=report.claim_token)
        self._settle_from_response(client, report, resp)

    @staticmethod
    def _delivery_of(report: TerminalReport) -> tuple[str, str] | None:
        """Replay the (branch, sha) frozen at reap time, or None if absent.

        Deliberately replayed rather than recomputed: the central verifies the
        reported SHA against the branch it fetches (#6538), so re-pushing on a
        retry would move HEAD out from under the verdict the janitor renders.
        """
        if report.agent_branch and report.agent_sha:
            return (report.agent_branch, report.agent_sha)
        return None

    def _settle_from_response(
        self,
        client: httpx.Client,
        report: TerminalReport,
        resp: httpx.Response | None,
    ) -> None:
        """Decide whether one delivery attempt settled, retried, or conflicts.

        Every branch either settles the report on real evidence or reschedules
        it; there is no branch that simply drops it, which is the defect this
        path existed to fix.
        """
        if resp is None:
            # No verdict came back. That is not the same as "the central did
            # nothing": a /complete whose fetch runs past
            # :data:`_TERMINAL_REPORT_TIMEOUT_S` has usually applied the
            # outcome already, and re-posting it makes the central do that work
            # a second time while the first is still running. The task record
            # settles a report that landed and holds one that never arrived, so
            # asking costs one GET and removes the amplification.
            self._reconcile(client, report, "transport-failure")
            return

        code = resp.status_code
        if code in _ACK_STATES:
            if self._acknowledges(resp, report.task_id):
                self._outbox.settle(report.report_id, SETTLE_ACKED)
            else:
                # A 200 that does not name this task is a proxy or cache
                # answering, not the central confirming the transition.
                self._outbox.note_attempt(report.report_id, f"http-{code}-unverifiable")
            return

        if code == 409 and self._claim_superseded(resp):
            # The central is not disagreeing about this task's outcome - it is
            # saying the execution claim this report answers no longer exists.
            # That is permanent, so this is the one 409 that must not go through
            # _reconcile: the task is legitimately live under a *newer* claim,
            # which reads as non-terminal, and the report would be retried
            # forever without ever being able to succeed.
            logger.warning(
                "central refused the %s report for task %s: its claim was superseded by a newer one; "
                "retiring the report and leaving the current owner's outcome in place",
                report.outcome,
                for_log(report.task_id),
            )
            self._outbox.settle(report.report_id, SETTLE_SUPERSEDED)
            return

        if code in (404, 409, 422):
            # All three mean "the server already knows something you don't":
            # 409 is already terminal, 422 auto-failed the task and released
            # the slot, 404 may be a deleted task. Only the task record says
            # which, so ask it - never blind-retry these.
            self._reconcile(client, report, f"http-{code}")
            return

        if code in _AUTH_STATES:
            logger.warning(
                "central rejected the terminal report for task %s with %d; holding it for %ds "
                "(check this worker's bearer token)",
                for_log(report.task_id),
                code,
                int(_AUTH_HOLD_S),
            )
            self._outbox.note_attempt(report.report_id, f"http-{code}", retry_after_s=_AUTH_HOLD_S)
            return

        if code == 429:
            self._outbox.note_attempt(
                report.report_id,
                "http-429",
                retry_after_s=_retry_after_seconds(resp),
            )
            return

        status = f"http-{code}"
        if code >= 500 or code in _retryable_states():
            self._outbox.note_attempt(report.report_id, status)
            return

        logger.warning(
            "central answered %d for the terminal report of task %s; retrying at the capped interval",
            code,
            for_log(report.task_id),
        )
        self._outbox.note_attempt(report.report_id, status, retry_after_s=_AUTH_HOLD_S)

    @staticmethod
    def _acknowledges(resp: httpx.Response, task_id: str) -> bool:
        """Whether a 2xx response confirms this task specifically.

        The endpoints answer with the serialised task, so the id is the proof.
        An unparseable body or a different id is not an ack: a CDN page or a
        stale cache must not be able to retire a real outcome.
        """
        try:
            body = resp.json()
        except ValueError:
            return False
        return isinstance(body, dict) and body.get("id") == task_id

    @staticmethod
    def _claim_superseded(resp: httpx.Response) -> bool:
        """Whether a 409 says the report's claim was superseded, not that a
        transition was illegal.

        Both are 409s and the distinction decides whether to retry, so the
        server answers this one with a machine-readable tag rather than a
        message the worker would have to pattern-match. An unparseable body is
        deliberately not treated as superseded: falling back to the reconcile
        path costs one extra read, whereas guessing wrong discards a report that
        might still have been deliverable.
        """
        try:
            body = resp.json()
        except ValueError:
            return False
        detail = body.get("detail") if isinstance(body, dict) else None
        return isinstance(detail, dict) and detail.get("error") == "claim_superseded"

    def _task_state(self, client: httpx.Client, task_id: str) -> str | None:
        """Read the authoritative status of a task from the central.

        Returns:
            The status string, or ``None`` when the central answered that this
            task does not exist *and* named the task doing so.

        Raises:
            _TaskUnreachableError: When the status could not be read at all,
                including a 404 that says nothing about this task.
        """
        try:
            resp = client.get(
                f"{self._server_url}/tasks/{task_id}",
                headers=self._headers(),
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise _TaskUnreachableError(str(exc)) from exc
        if resp.status_code == 404:
            if self._names_this_task(resp, task_id):
                return None
            raise _TaskUnreachableError("HTTP 404 that did not name this task")
        if resp.status_code not in _ACK_STATES:
            raise _TaskUnreachableError(f"HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise _TaskUnreachableError("unreadable task payload") from exc
        if not isinstance(body, dict) or body.get("id") != task_id:
            raise _TaskUnreachableError("task payload did not name this task")
        status = body.get("status")
        if not isinstance(status, str) or not status:
            raise _TaskUnreachableError("task payload carried no status")
        return status

    @staticmethod
    def _names_this_task(resp: httpx.Response, task_id: str) -> bool:
        """Whether a 404 is the central's own "that task is gone" answer.

        FastAPI renders an HTTPException as ``{"detail": ...}`` and both the
        deletion paths and the tenant-scope guard phrase it ``Task '<id>' not
        found``, so requiring the id here is what separates that from the
        anonymous 404 an ingress or CDN answers for any path it does not route.
        It cannot separate a wrong-tenant token, which gets the same body: that
        is why the caller still insists on repetition before believing it.
        """
        try:
            body = resp.json()
        except ValueError:
            return False
        detail = body.get("detail") if isinstance(body, dict) else None
        return isinstance(detail, str) and task_id in detail

    def _hold_unconfirmed_absence(self, report: TerminalReport, context: str) -> None:
        """Hold a report the central answered "no such task", until it repeats.

        A single absence read is not proof of deletion. A token scoped to the
        wrong tenant is answered with the very same body (see
        :data:`_GONE_HOLD_S`), and believing the first sighting costs the one
        outcome this worker owes while its task is still live. The count rides
        on the outbox record, so neither a long hold nor a process restart resets
        it, and :meth:`TerminalReportOutbox.note_absence` clears it the moment
        the central says anything else about the task.
        """
        seen = self._outbox.note_absence(report.report_id, hold_s=_GONE_HOLD_S)
        if seen >= _GONE_CONFIRMATIONS:
            logger.warning(
                "central has answered %d times that task %s does not exist (%s); dropping its %s report",
                seen,
                for_log(report.task_id),
                context,
                report.outcome,
            )
            self._outbox.settle(report.report_id, SETTLE_GONE)
            return
        logger.warning(
            "central says task %s does not exist (%d of %d); holding its %s report for %ds -- a token "
            "scoped to the wrong tenant is answered the same way",
            for_log(report.task_id),
            seen,
            _GONE_CONFIRMATIONS,
            report.outcome,
            int(_GONE_HOLD_S),
        )

    def _reconcile(self, client: httpx.Client, report: TerminalReport, context: str) -> None:
        """Settle a rejected report against the server's authoritative state.

        A 409/422 cannot be read on its own: it says the transition was
        illegal, which is equally true when this worker's own earlier attempt
        landed (a duplicate it should retire) and when something else ended the
        task differently (a conflict it must not overwrite). Asking the task
        record is what tells them apart.
        """
        try:
            status = self._task_state(client, report.task_id)
        except _TaskUnreachableError as exc:
            logger.warning(
                "cannot reconcile %s for task %s yet: %s", context, for_log(report.task_id), for_log(str(exc))
            )
            self._outbox.note_attempt(report.report_id, f"{context}-unreconcilable")
            return

        if status is None:
            self._hold_unconfirmed_absence(report, context)
            return

        if status in _SUCCESS_STATES or status in _FAILURE_STATES:
            if self._outcome_matches(report.outcome, status):
                self._warn_delivery_left_unmerged(report, status)
                self._outbox.settle(report.report_id, SETTLE_DUPLICATE)
                return
            # The server holds a different terminal outcome than this worker
            # observed (an in-agent completion, a janitor verdict, an operator).
            # The central is authoritative: retire the report, say so loudly,
            # and never post a compensating transition over the top of it.
            logger.warning(
                "task %s ended %s on the central but this node reported %s (%s); leaving the server outcome in place",
                for_log(report.task_id),
                for_log(status),
                report.outcome,
                context,
            )
            self._outbox.settle(report.report_id, SETTLE_CONFLICT)
            return

        if status in _PARKED_STATES:
            self._escalate_parked_report(report, context, status)
            return

        # Still open/claimed: the 409 came from a version conflict or the
        # auto-claim path racing, not from a settled outcome. Retry.
        self._outbox.note_attempt(report.report_id, f"{context}:{status}")

    def _escalate_parked_report(self, report: TerminalReport, context: str, status: str) -> None:
        """Keep owing a report the central will not take, and stop hiding that.

        A parked task is not a resolved one, so the retry schedule is unchanged
        and nothing is settled: the outcome stays owed until the task is
        un-parked or somebody ends it. Only the silence is the defect -- the loop
        was one 409 and one GET every few minutes, indefinitely, with no line an
        operator could act on.

        The ERROR fires on the crossing and then once every
        :data:`_PARKED_ESCALATION_ATTEMPTS` attempts rather than every cycle, so
        it stays readable, and it is derived from the durable attempt count, so
        a restart does not reset the escalation and does not re-announce a wedge
        the operator has already been told about.
        """
        self._outbox.note_attempt(report.report_id, f"{context}:{status}")
        attempts = report.attempts + 1
        if attempts % _PARKED_ESCALATION_ATTEMPTS:
            return
        logger.error(
            "task %s has refused this node's %s report %d times because it is %s; still owing it and "
            "retrying on schedule, but nothing this node does can un-park the task -- it needs to be "
            "resumed, unblocked, or ended on the central",
            for_log(report.task_id),
            report.outcome,
            attempts,
            for_log(status),
        )

    @staticmethod
    def _outcome_matches(outcome: str, server_status: str) -> bool:
        """Whether a queued outcome is equivalent to the state on the server."""
        if outcome == OUTCOME_DONE:
            return server_status in _SUCCESS_STATES
        if outcome == OUTCOME_FAILED:
            return server_status in _FAILURE_STATES
        return False

    def _warn_delivery_left_unmerged(self, report: TerminalReport, server_status: str) -> None:
        """Say so when a report that carried work is retired as a duplicate.

        :meth:`_outcome_matches` compares the outcome and nothing else, because
        the task record stores no branch or SHA -- delivery lives only on the
        completion request body. So "already done" is read as "this node's work
        landed too", which is not always true: an agent that self-completed over
        curl reports no branch at all, and this node's pushed branch can be
        refused while the task is done for someone else's reasons. The
        retirement is still correct -- the central owns the status and re-posting
        cannot un-complete the task -- but the record is about to be rewritten
        away, which makes this log the only surviving record of an artifact that
        never reached the central. The janitor's posted-artifact merge preview
        (#6612) is the remaining path to it, and it starts from the branch name.
        """
        if report.outcome != OUTCOME_DONE or report.agent_branch is None:
            return
        logger.warning(
            "task %s is already %s on the central, so this node's delivery (%s @ %s) was refused "
            "and is not merged; the janitor's posted-artefact merge preview is the remaining path to it",
            for_log(report.task_id),
            for_log(server_status),
            for_log(report.agent_branch),
            for_log(report.agent_sha or "unknown sha"),
        )

    def _resolve_unknown_outcome(self, client: httpx.Client, report: TerminalReport) -> None:
        """Decide a report whose agent exit status was never observed.

        An exit status this worker could not read proves nothing, so it is never
        reported as success. The central's task record decides instead: if the
        task already reached a terminal state (typically the agent completing
        its own task before dying), the outcome is settled as resolved; if it is
        still live, the task is failed explicitly. It is deliberately *not*
        released back to the open pool - a release invites a second execution of
        a task whose work may already be done.
        """
        try:
            status = self._task_state(client, report.task_id)
        except _TaskUnreachableError as exc:
            logger.info(
                "cannot resolve the unknown exit of task %s yet: %s",
                for_log(report.task_id),
                for_log(str(exc)),
            )
            self._outbox.note_attempt(report.report_id, "unknown-unreconcilable")
            return

        if status is None:
            self._hold_unconfirmed_absence(report, "unknown-exit")
            return
        if status in _SUCCESS_STATES or status in _FAILURE_STATES:
            self._outbox.settle(report.report_id, SETTLE_RESOLVED)
            return

        resp = self._fail_task(
            client,
            report.task_id,
            f"{report.detail}; worker could not observe the agent exit status and the task was still {status}",
            claim_token=report.claim_token,
        )
        self._settle_from_response(client, report, resp)

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
                self._task_sessions[task_id] = session.id
                # Lift the handle out of the spawner and hold it here. The
                # spawner is a local, so its _procs entry dies with this call;
                # the Popen object itself is then only kept alive by
                # subprocess._active, where the next Popen in the process reaps
                # it through _cleanup(). Holding our own reference is what makes
                # the recorded status readable at reap time instead of lost.
                # None here means the backend has no local child (in-process,
                # container, sandbox), and the reap must not invent a status.
                proc = spawner.session_process(session.id)
                if proc is not None:
                    self._agent_procs[task_id] = proc
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
                # Keep the fence the grant handed us. The claim response is the
                # only place the token is ever disclosed, so a worker that drops
                # it here can never post a fenced report later -- and an
                # unfenced report is accepted unconditionally, which is the
                # stale-write path the fence exists to close.
                token = task.get("claim_token")
                if isinstance(token, str) and token:
                    self._claim_tokens[task_id] = token
                console.print(f"  [green]Claimed[/green] {task_id}: {task.get('title', '')[:50]} (pid={pid})")
            else:
                # Spawn failed: release the claim so another node can run the
                # task, instead of leaving it stranded in 'claimed' with no live
                # agent (#3018).
                self._release_task(client, task_id, "agent spawn failed on worker; released back to pool")
                console.print(f"  [yellow]Spawn failed[/yellow] for {task_id}; released back to pool")

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

    def _depart(self, client: httpx.Client, node_id: str | None) -> None:
        """Flush what this node owes, then leave the cluster only if leaving is safe.

        Unregistering is not a goodbye, it is a write: the route behind it reopens
        every task this node claimed. Doing that while an outcome for one of those
        tasks is still queued hands the work to another node, and this node's
        report is then refused as superseded when it finally lands -- the finished
        work is discarded and redone elsewhere. The claim fence built for the same
        finding keeps the newer claim from being corrupted, which is the
        correctness half; this is the waste half.

        Staying registered is the conservative side, and it cannot strand anything.
        A node that really is gone is evicted by the heartbeat reaper, whose path
        reopens the same tasks, so the delay only ever buys a comparison against
        the reaper's clock: a restart that lands inside that window drains the
        queue against the claim it never lost and no reopen happens at all. That
        is the ordinary case for a restart, which is why this is worth the extra
        few lines -- the shutdown path used to guarantee the reopen on every one.
        """
        self._reap_finished()
        self._report_finished(client)
        owed = self._outbox.owed_here()
        if owed:
            logger.warning(
                "leaving the cluster registered: %d terminal report(s) still owed (%s); unregistering now "
                "would reopen those tasks and hand finished work to another node. The node reaper reclaims "
                "them if this worker does not come back.",
                len(owed),
                for_log(", ".join(report.task_id for report in owed[:5])),
            )
            console.print(f"[yellow]Left registered[/yellow]: {len(owed)} terminal report(s) still owed")
            return
        self._unregister(client, node_id)

    def run(self) -> None:
        """Main worker loop. Blocks until SIGINT/SIGTERM."""
        # Preflight the workspace before registering or accepting any claim. A
        # worker whose workspace is not a usable git checkout cannot spawn a
        # single agent, so it must refuse to start rather than register, claim
        # tasks, and strand them in 'claimed' (#3018).
        setup_error = self._workspace_setup_error()
        if setup_error is not None:
            console.print(f"[red]Worker cannot start:[/red] {setup_error}")
            raise SystemExit(1)

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
                node_id, last_heartbeat = self._do_heartbeat(client, node_id, heartbeat_s, last_heartbeat)  # type: ignore[arg-type]

                # Reap finished agents into the pending-report queue on every
                # cycle regardless of node_id, so an agent that exits while
                # re-registration is retrying still gets drained below instead
                # of being stranded until node_id recovers.
                self._reap_finished()

                # A None node_id here means the heartbeat failed and the
                # re-registration attempt that follows it also failed: there is
                # no live node to claim tasks under. Still fall through to the
                # same poll-interval wait the success path takes below, rather
                # than looping straight back into another _do_heartbeat call.
                # Retrying re-registration with no wait between attempts busy
                # loops for as long as the server stays unreachable (#3309).
                if node_id is not None and self.available_slots > 0:
                    self._claim_available_tasks(client, node_id)

                # Drive reaped agents to a terminal state on the server. This
                # does not require a live node_id.
                self._report_finished(client)

                if self._wake.wait(timeout_s=poll_s) == WakeReason.ABORT:
                    break

            # Say goodbye on the way out: one last reap-and-flush so a clean stop
            # does not defer reports unnecessarily, then unregister -- but only if
            # the flush left nothing owed. Departing while a report is still queued
            # reopens the task that report describes on the central.
            self._depart(client, node_id)

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
