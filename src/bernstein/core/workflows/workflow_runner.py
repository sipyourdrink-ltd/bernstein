"""DAG runner for declarative YAML workflow manifests.

Drives a :class:`bernstein.core.workflows.workflow_spec.WorkflowSpec`
through a topological execution: every layer of ready nodes runs in
parallel, agent-typed nodes dispatch through the existing
:class:`bernstein.core.spawner.AgentSpawner`, and command-typed nodes
shell out via :func:`subprocess.run`.

Notes:

* Approval gates (``interactive: true``) are deliberately stubbed.  Any
  encounter raises ``NotImplementedError`` referencing ticket #1110,
  which owns that feature.
* Audit emission is best-effort: when no audit log is wired in we fall
  back to a structured log line so workflow runs are still observable
  in production.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bernstein.core.spawner import AgentSpawner
    from bernstein.core.workflows.workflow_spec import LoopSpec, WorkflowNode, WorkflowSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class NodeStatus(StrEnum):
    """Terminal status for a single workflow node execution."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class WorkflowRunError(RuntimeError):
    """Raised for unrecoverable runner-level failures.

    Used for cycles caught at run time, missing agent spawners on
    agent-typed nodes, exhausted loop iterations, and the explicit
    interactive-gate stub.  Per-node failures are reported via
    :class:`NodeExecution.status` rather than as exceptions so the
    runner can surface partial results to the caller.
    """


@dataclass
class NodeExecution:
    """Outcome of running one node (with all its loop iterations).

    Attributes:
        node_id: The id of the executed node.
        status: Terminal status.  ``SKIPPED`` is used when a node is
            preempted because an upstream dependency failed.
        iterations: How many times the node fired.  ``1`` for
            non-looping nodes; up to ``loop.max_iterations`` for loops.
        exit_code: Final exit code for command-typed nodes.  Always 0
            on success; non-zero or ``None`` on failure (``None`` means
            the process never produced an exit code, e.g. timeout).
        stdout: Captured stdout of the last iteration.
        stderr: Captured stderr of the last iteration.
        session_id: Agent session id for agent-typed nodes (last
            iteration if looping).  Empty string for command nodes.
        error: Human-readable error message when status is FAILED.
        wall_time_seconds: Wall clock spent in this node, end-to-end.
    """

    node_id: str
    status: NodeStatus
    iterations: int = 0
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    session_id: str = ""
    error: str = ""
    wall_time_seconds: float = 0.0


@dataclass
class WorkflowExecution:
    """Aggregate outcome of running a whole workflow.

    Attributes:
        spec_name: Workflow ``name`` from the manifest.
        run_id: Random run identifier; surfaces in audit events so the
            same logical run can be tied together across nodes.
        nodes: Per-node results, in the order they finished.
        wall_time_seconds: Total wall clock for the run.
        succeeded: ``True`` only if every executed node ended in
            :attr:`NodeStatus.SUCCESS`.
    """

    spec_name: str
    run_id: str
    nodes: list[NodeExecution] = field(default_factory=list)
    wall_time_seconds: float = 0.0
    succeeded: bool = False


# ---------------------------------------------------------------------------
# Audit hook
# ---------------------------------------------------------------------------

# An audit emitter is a sync callable matching the loose contract used
# elsewhere in core (handlers in core/protocols/acp use the same shape):
# ``(event_type, resource_id, details)``.  Keeping it as a Callable
# instead of importing :class:`AuditLog` avoids a hard dependency on the
# security stack - the runner runs fine without it.
AuditEmitter = Callable[[str, str, dict[str, Any]], None]


def _default_audit_emitter(event_type: str, resource_id: str, details: dict[str, Any]) -> None:
    """Log audit events at INFO level when no real audit log is wired in.

    This keeps workflow runs observable in production without forcing the
    user to bring up the HMAC chain - which is wired in higher layers
    (orchestrator boot) and not always present in CLI-direct runs.
    """
    logger.info("workflow.audit event=%s resource=%s details=%s", event_type, resource_id, details)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class WorkflowRunner:
    """Executes :class:`WorkflowSpec` manifests.

    Args:
        spawner: Optional :class:`AgentSpawner` for agent-typed nodes.
            When omitted, agent-typed nodes raise
            :class:`WorkflowRunError`.  Tests building command-only
            workflows can pass ``None`` here.
        workdir: Working directory for command-typed ``subprocess.run``
            invocations.  Defaults to the current process's cwd.
        audit_emitter: Optional callable for audit events.  Defaults to
            a logger that writes structured INFO lines.
        max_parallel: Cap on concurrent node executions.  ``None``
            uses ``min(layer_size, 8)`` per layer.
        env: Environment overlay applied to command nodes.  ``None``
            inherits the runner's environment.
    """

    def __init__(
        self,
        *,
        spawner: AgentSpawner | None = None,
        workdir: Path | None = None,
        audit_emitter: AuditEmitter | None = None,
        max_parallel: int | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._spawner = spawner
        self._workdir = (workdir or Path.cwd()).resolve()
        self._audit = audit_emitter or _default_audit_emitter
        self._max_parallel = max_parallel
        self._env = env

    # ----- public entry -----------------------------------------------------

    def run(
        self,
        spec: WorkflowSpec,
        *,
        goal: str = "",
        run_id: str | None = None,
    ) -> WorkflowExecution:
        """Execute ``spec`` end-to-end.

        Args:
            spec: Validated workflow manifest.
            goal: Free-text goal substituted into ``{goal}`` placeholders
                in node prompts.  Mirrors ``bernstein run -g``.
            run_id: Optional pre-allocated run id.  When ``None`` a fresh
                short id is generated so audit consumers can correlate.

        Returns:
            A :class:`WorkflowExecution` describing every node that ran.
            The runner does not raise on per-node failure: callers
            inspect ``execution.succeeded`` and per-node statuses.
        """
        rid = run_id or uuid.uuid4().hex[:12]
        execution = WorkflowExecution(spec_name=spec.name, run_id=rid)
        start = time.monotonic()

        self._audit(
            "workflow.start",
            spec.name,
            {"run_id": rid, "node_count": len(spec.nodes), "goal": goal},
        )

        results: dict[str, NodeExecution] = {}
        layers = spec.topological_order()
        aborted = False

        for layer in layers:
            if aborted:
                for node in layer:
                    skipped = NodeExecution(node_id=node.id, status=NodeStatus.SKIPPED)
                    results[node.id] = skipped
                    execution.nodes.append(skipped)
                continue

            ready_nodes: list[WorkflowNode] = []
            for node in layer:
                if not all(results[dep].status == NodeStatus.SUCCESS for dep in node.depends_on if dep in results):
                    skipped = NodeExecution(node_id=node.id, status=NodeStatus.SKIPPED)
                    results[node.id] = skipped
                    execution.nodes.append(skipped)
                    continue
                ready_nodes.append(node)

            if not ready_nodes:
                continue

            layer_results = self._execute_layer(ready_nodes, goal=goal, run_id=rid)
            for node_exec in layer_results:
                results[node_exec.node_id] = node_exec
                execution.nodes.append(node_exec)
                if node_exec.status == NodeStatus.FAILED:
                    aborted = True

        execution.wall_time_seconds = time.monotonic() - start
        execution.succeeded = not aborted and all(
            r.status == NodeStatus.SUCCESS for r in execution.nodes if execution.nodes
        )
        self._audit(
            "workflow.finish",
            spec.name,
            {
                "run_id": rid,
                "succeeded": execution.succeeded,
                "wall_time_seconds": round(execution.wall_time_seconds, 3),
            },
        )
        return execution

    # ----- internal helpers -------------------------------------------------

    def _execute_layer(
        self,
        nodes: list[WorkflowNode],
        *,
        goal: str,
        run_id: str,
    ) -> list[NodeExecution]:
        """Run a layer of ready nodes in parallel and collect their results.

        Args:
            nodes: Nodes whose dependencies are already satisfied.
            goal: Goal text passed through to agent prompts.
            run_id: Run identifier propagated into audit events.

        Returns:
            One :class:`NodeExecution` per input node.
        """
        if len(nodes) == 1:
            return [self._execute_node(nodes[0], goal=goal, run_id=run_id)]

        cap = self._max_parallel if self._max_parallel is not None else max(1, min(len(nodes), 8))
        results: list[NodeExecution] = []
        with ThreadPoolExecutor(max_workers=cap, thread_name_prefix="workflow") as pool:
            futures: dict[Future[NodeExecution], WorkflowNode] = {
                pool.submit(self._execute_node, node, goal=goal, run_id=run_id): node for node in nodes
            }
            for future in as_completed(futures):
                results.append(future.result())
        # Sort to preserve stable, deterministic order for callers.
        order = {node.id: idx for idx, node in enumerate(nodes)}
        results.sort(key=lambda r: order.get(r.node_id, 0))
        return results

    def _execute_node(
        self,
        node: WorkflowNode,
        *,
        goal: str,
        run_id: str,
    ) -> NodeExecution:
        """Run a single node, including any loop iterations.

        Args:
            node: The node to execute.
            goal: Goal text for prompt substitution.
            run_id: Run identifier for audit events.

        Returns:
            The terminal :class:`NodeExecution` for this node.
        """
        if node.interactive:
            self._audit("workflow.interactive_blocked", node.id, {"run_id": run_id})
            raise NotImplementedError(
                f"node {node.id!r} requires an interactive approval gate; approval gates ship in #1110",
            )

        self._audit(
            "workflow.node_start",
            node.id,
            {"run_id": run_id, "kind": node.kind, "loop": node.loop is not None},
        )
        start = time.monotonic()
        result: NodeExecution
        if node.loop is not None:
            result = self._execute_loop_node(node, node.loop, goal=goal, run_id=run_id)
        else:
            result = self._execute_once(node, goal=goal, run_id=run_id, iteration=1)
            result.iterations = 1
        result.wall_time_seconds = time.monotonic() - start
        self._audit(
            "workflow.node_finish",
            node.id,
            {
                "run_id": run_id,
                "status": result.status.value,
                "iterations": result.iterations,
                "exit_code": result.exit_code,
                "wall_time_seconds": round(result.wall_time_seconds, 3),
            },
        )
        return result

    def _execute_loop_node(
        self,
        node: WorkflowNode,
        loop: LoopSpec,
        *,
        goal: str,
        run_id: str,
    ) -> NodeExecution:
        """Re-fire a node until ``loop.until`` exits 0 or budget runs out.

        Args:
            node: The looping node.
            loop: The :class:`LoopSpec` attached to ``node``.
            goal: Goal text for prompt substitution.
            run_id: Run identifier for audit events.

        Returns:
            The final :class:`NodeExecution`.  When iterations exhaust
            without the predicate passing, status is FAILED with a
            descriptive ``error`` message.
        """
        last: NodeExecution | None = None
        for iteration in range(1, loop.max_iterations + 1):
            last = self._execute_once(node, goal=goal, run_id=run_id, iteration=iteration)
            last.iterations = iteration
            if last.status == NodeStatus.FAILED:
                return last
            if self._loop_predicate_passes(loop.until):
                return last
            self._audit(
                "workflow.loop_continue",
                node.id,
                {"run_id": run_id, "iteration": iteration, "predicate": loop.until},
            )

        # Exhausted without the predicate passing.
        assert last is not None
        last.status = NodeStatus.FAILED
        last.error = f"loop exhausted after {loop.max_iterations} iterations; predicate never exited 0: {loop.until!r}"
        return last

    def _loop_predicate_passes(self, predicate: str) -> bool:
        """Return ``True`` when the bash predicate exits with status 0."""
        # SECURITY: shell=True required because loop predicates are manifest-authored
        # bash expressions (e.g. "test -f marker") that rely on shell parsing; not user input.
        proc = subprocess.run(
            predicate,
            shell=True,  # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true
            cwd=str(self._workdir),
            env=self._env,
            capture_output=True,
            check=False,
            text=True,
        )
        return proc.returncode == 0

    def _execute_once(
        self,
        node: WorkflowNode,
        *,
        goal: str,
        run_id: str,
        iteration: int,
    ) -> NodeExecution:
        """Run a single iteration of a node.

        Routes by node kind: command-typed nodes shell out, agent-typed
        nodes go through the spawner.  Errors are caught and converted
        to FAILED :class:`NodeExecution` entries so the runner can
        surface them without aborting the whole DAG via exception.
        """
        if node.kind == "command":
            return self._execute_command(node)
        return self._execute_agent(node, goal=goal, run_id=run_id, iteration=iteration)

    def _execute_command(self, node: WorkflowNode) -> NodeExecution:
        """Shell out for a command-typed node.

        Uses ``shell=True`` so manifest authors can write idiomatic bash
        (pipes, redirects, &&).  ``timeout_seconds`` becomes a hard
        ``subprocess.TimeoutExpired`` boundary; on timeout we surface
        a FAILED node with ``exit_code=None`` so the upstream runner
        treats it as a definite failure.
        """
        assert node.command is not None
        try:
            # SECURITY: shell=True required because command-typed workflow nodes are
            # manifest-authored bash strings using idiomatic pipes/redirects/&&; not user input.
            proc = subprocess.run(
                node.command,
                shell=True,  # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true
                cwd=str(self._workdir),
                env=self._env,
                capture_output=True,
                check=False,
                text=True,
                timeout=node.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            return NodeExecution(
                node_id=node.id,
                status=NodeStatus.FAILED,
                exit_code=None,
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
                error=f"command timed out after {node.timeout_seconds}s",
            )
        status = NodeStatus.SUCCESS if proc.returncode == 0 else NodeStatus.FAILED
        return NodeExecution(
            node_id=node.id,
            status=status,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            error="" if status == NodeStatus.SUCCESS else f"exit {proc.returncode}",
        )

    def _execute_agent(
        self,
        node: WorkflowNode,
        *,
        goal: str,
        run_id: str,
        iteration: int,
    ) -> NodeExecution:
        """Dispatch an agent-typed node through the existing AgentSpawner.

        Builds a one-shot :class:`Task` from the node's ``agent`` (role)
        and ``prompt`` and feeds it to :meth:`AgentSpawner.spawn_for_tasks`.
        Result correlation is via the returned :class:`AgentSession.id`.
        """
        if self._spawner is None:
            return NodeExecution(
                node_id=node.id,
                status=NodeStatus.FAILED,
                error="agent-typed node requires a configured AgentSpawner",
            )
        if node.agent is None or node.prompt is None:  # pragma: no cover - guarded by validator
            return NodeExecution(
                node_id=node.id,
                status=NodeStatus.FAILED,
                error="agent-typed node missing agent/prompt fields",
            )

        from bernstein.core.tasks.models import Task

        prompt_body = _substitute_goal(node.prompt, goal)
        # Carry workflow context inside the description so audit / token
        # accounting can attribute the spend to the manifest, and so a
        # ``fresh_context`` loop creates a distinct task id per iteration
        # - the spawner uses task id as the session correlation key.
        suffix = f"@iter{iteration}" if node.fresh_context or iteration > 1 else ""
        task_id = f"wf-{node.id}-{run_id}{suffix}"
        task = Task(
            id=task_id,
            title=f"workflow:{node.id}",
            description=prompt_body,
            role=node.agent,
            cli=node.cli,
            model=node.model,
            effort=node.effort,
        )

        try:
            session = self._spawner.spawn_for_tasks([task])
        except Exception as exc:
            logger.exception("Spawner raised for node %s", node.id)
            return NodeExecution(
                node_id=node.id,
                status=NodeStatus.FAILED,
                error=f"spawn failed: {exc}",
            )

        return NodeExecution(
            node_id=node.id,
            status=NodeStatus.SUCCESS,
            session_id=session.id,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _substitute_goal(prompt: str, goal: str) -> str:
    """Substitute ``{goal}`` placeholders without breaking literal braces.

    A single-pass ``str.replace`` is sufficient - workflow prompts don't
    use full Python ``str.format`` because nodes routinely embed shell
    snippets and curly braces in code samples that we must not interpret.

    Args:
        prompt: Raw prompt text from the manifest.
        goal: Goal string to substitute in.

    Returns:
        Prompt text with ``{goal}`` replaced.
    """
    if "{goal}" not in prompt:
        return prompt
    return prompt.replace("{goal}", goal)


def shell_join(parts: list[str]) -> str:
    """Public ``shlex.join`` wrapper for tests that build commands."""
    return shlex.join(parts)
