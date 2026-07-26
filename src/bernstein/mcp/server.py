"""Bernstein MCP server.

Exposes Bernstein's orchestration layer as MCP tools so any MCP client
(Cursor, Claude Code, Cline, Windsurf, …) can drive multi-agent work
through Bernstein.

Transport:
    stdio  - for local IDE integration (default ``bernstein mcp``)
    sse    - for remote/web integration (``bernstein mcp --transport sse``)

Tools registered by ``create_mcp_server`` below. The list is exhaustive and
is asserted against the live registration set by
``tests/unit/test_mcp_server.py``, so a tool added here without a docstring
line fails that test. Tiers are declared in
:data:`bernstein.core.protocols.mcp.tool_tiers.TOOL_TIERS`; the widest tier
plus lineage exposes all of them.

    bernstein_run           - start an orchestration run (optionally as a
                              subtask via ``parent_task_id``)
    bernstein_status        - liveness, task counts, cost, and an optional
                              status-filtered task list
    bernstein_run_status    - verifiable run handle, polled by run id
    bernstein_task_capsule  - signed spawn capsule for a worker (#2545)
    bernstein_claim         - claim the next dependency-gated task
    bernstein_post_message  - post progress on a claimed task
    bernstein_post_artifact - attach an artefact to a task
    bernstein_cancel        - cancel one task and its subtask tree (#3078)
    bernstein_shutdown_orchestrator - whole-orchestrator shutdown signal
    bernstein_approve       - approve a pending/blocked task
    bernstein_complete      - complete a task the caller is executing
    load_skill              - load a skill pack body / reference / script

Registered from sibling modules by the same ``create_mcp_server`` call:

    bernstein_scenario        - list / run / poll scenarios via an ``action``
                                selector (routine_tools)
    bernstein_verify_lineage  - verify an artefact against the audit chain
                                (resources.lineage, lineage builds only)

Deprecated aliases (#3087), callable but never advertised, removed in
:data:`bernstein.core.protocols.mcp.tool_tiers.ALIAS_REMOVAL_RELEASE`:

    bernstein_health          -> bernstein_status
    bernstein_tasks           -> bernstein_status
    bernstein_cost            -> bernstein_status
    bernstein_create_subtask  -> bernstein_run
    bernstein_task_handle     -> bernstein_run_status
    bernstein_update          -> bernstein_post_message
    bernstein_context         -> bernstein_task_capsule
    bernstein_stop            -> bernstein_shutdown_orchestrator
    bernstein_scenarios       -> bernstein_scenario
    bernstein_scenario_status -> bernstein_scenario
    verify_chain              -> bernstein_verify_lineage
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import logging
import os
from datetime import UTC
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP

# Patch FastMCP FuncMetadata to support CreateTaskResult without validation error
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from mcp.types import (
    CallToolResult,
    CancelTaskRequest,
    CancelTaskResult,
    CreateTaskResult,
    GetTaskPayloadRequest,
    GetTaskRequest,
    GetTaskResult,
    ListTasksRequest,
    ListTasksResult,
    Task,
    TaskExecutionMode,
    TextContent,
    ToolExecution,
)
from mcp.types import (
    Tool as MCPTool,
)

from bernstein.core.protocols.mcp.tool_tiers import (
    ToolTier,
    resolve_active_tier,
    tool_in_tier,
)
from bernstein.mcp.approval_gate import (
    completion_refusal_payload,
    is_approvable,
    is_worker_completable,
    refusal_payload,
)
from bernstein.mcp.cost_meter import measure_call, wrap_envelope
from bernstein.mcp.input_validation import (
    ValidationError,
    get_registry,
    validate_or_error,
    validation_error_response,
)

_orig_convert_result = FuncMetadata.convert_result


def _patched_convert_result(self, result: Any) -> Any:
    if isinstance(result, CreateTaskResult | CallToolResult):
        return result
    return _orig_convert_result(self, result)


FuncMetadata.convert_result = _patched_convert_result

_DEFAULT_SERVER_URL = "http://127.0.0.1:8052"


def _package_version() -> str:
    """Return the installed Bernstein distribution version."""
    try:
        return version("bernstein")
    except PackageNotFoundError:
        return "0+unknown"


# Advertised to MCP clients on connect and therefore the only Bernstein text
# guaranteed to sit in the connected model's context for the whole session.
# It spends that budget on the control loop, not on a system description:
# a client that starts a run and then polls wrongly pays for a second run.
# Budget and tool-name accuracy are asserted in tests/unit/test_mcp_server.py.
_SERVER_INSTRUCTIONS = (
    "Bernstein is a deterministic orchestrator for CLI coding agents, one git "
    "worktree per task, so runs replay byte-identically. Verifying the audit "
    "chain offline needs the install audit key.\n"
    "Driving a run:\n"
    "1. bernstein_run starts a run and returns immediately with a task_id, "
    "which is the run id. It does not wait for the run to finish.\n"
    "2. Poll bernstein_run_status with run_id set to that value. The handle "
    "is reprojected from the run journal, so it is safe to poll from anywhere.\n"
    "3. Runs take minutes to hours. Poll on a slow cadence, tens of seconds "
    "apart. A handle still reading working is normal progress, not a stall, "
    "so do not start the goal again.\n"
    "4. Stop polling once status is terminal: completed, failed or cancelled. "
    "input_required means the run is waiting on you.\n"
    "For anything deeper, call load_skill with a name from the skill index and "
    "load only the pack the task needs."
)

# Timeout for all httpx calls to the task server (seconds).
_HTTP_TIMEOUT = 5.0

# Advisory delay a caller should wait before its first poll of a run handle,
# reported as ``poll_after_ms`` on the ``bernstein_run`` response. Matches the
# ``pollInterval`` carried on the projected Tasks-extension task, so a client
# that reads either field paces itself the same way.
_POLL_AFTER_MS = 5000

# Env var holding the bearer token the task server expects when auth is
# enabled. When unset, MCP tools fall back to sending no Authorization
# header so the default unauth task-server mode keeps working.
_AUTH_TOKEN_ENV = "BERNSTEIN_AUTH_TOKEN"

logger = logging.getLogger(__name__)


def _auth_headers() -> dict[str, str]:
    """Return the Authorization header for task-server requests, if configured.

    Reads ``BERNSTEIN_AUTH_TOKEN`` from the environment at call time so
    operators can rotate the token without restarting the MCP server.
    When the var is missing or empty, returns an empty dict so callers
    continue to work against an unauthenticated task server (the default
    local-dev mode).

    Returns:
        ``{"Authorization": "Bearer <token>"}`` when the token is set,
        otherwise an empty dict.
    """
    tok = os.environ.get(_AUTH_TOKEN_ENV, "")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _error_response(exc: Exception, *, hint: str = "Task server may be restarting") -> str:
    """Return a JSON error string instead of letting the exception propagate.

    This keeps the MCP server alive - a crashed tool handler on stdio
    transport means all Bernstein tools are lost for the rest of the
    agent session (no reconnect).
    """
    logger.warning("MCP tool error: %s", exc)
    return json.dumps({"error": str(exc), "hint": hint})


def _approval_refusal_response(task_id: str, current_status: str) -> str:
    """Render the shared approval refusal as the JSON string MCP tools return."""
    return json.dumps(refusal_payload(task_id, current_status), indent=2)


def _completion_refusal_response(task_id: str, current_status: str) -> str:
    """Render the shared completion refusal as the JSON string MCP tools return."""
    return json.dumps(completion_refusal_payload(task_id, current_status), indent=2)


def _validation_error_response(err: ValidationError) -> str:
    """Render a validation failure as the JSON string FastMCP tools return."""
    return validation_error_response(err)


def _validate_or_error(tool_name: str, params: dict[str, Any]) -> ValidationError | None:
    """Validate ``params`` against ``tool_name``'s schema."""
    return validate_or_error(tool_name, params)


def _deprecated_alias_payload(old_name: str, payload: str) -> str:
    """Wrap an alias result so it names its replacement and removal release.

    The alias keeps the historical result shape under ``result`` while the
    envelope states, in the payload itself, that the name is deprecated,
    what replaces it, and the release the alias disappears in (#3087).
    """
    from bernstein.core.protocols.mcp.tool_tiers import (
        ALIAS_REMOVAL_RELEASE,
        DEPRECATED_TOOL_ALIASES,
    )

    replacement = DEPRECATED_TOOL_ALIASES[old_name]
    try:
        parsed: Any = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        parsed = payload
    return json.dumps(
        {
            "deprecated": True,
            "tool": old_name,
            "replacement": replacement,
            "removal_release": ALIAS_REMOVAL_RELEASE,
            "notice": (
                f"{old_name} is deprecated; call {replacement} instead. "
                f"This alias is removed in {ALIAS_REMOVAL_RELEASE}."
            ),
            "result": parsed,
        },
        indent=2,
    )


def _get_journal_head(task_id: str) -> str:
    from pathlib import Path

    from bernstein.core.replay.journal import EventJournal, run_journal_path
    from bernstein.core.tasks.checkpoint_retry import task_run_id

    run_id = task_run_id(task_id)
    sdd_dir = Path.cwd() / ".sdd"
    try:
        journal_path = run_journal_path(sdd_dir, run_id)
        if journal_path.exists():
            journal = EventJournal.resume(run_id, sdd_dir)
            return journal.head()
    except Exception:
        pass
    return ""


#: Retention window advertised on every Tasks-extension task row, in
#: milliseconds.
#:
#: The task server evicts nothing, so ``null`` (the extension's spelling of
#: "unlimited") would be the literal answer. It is not sendable: ``Task.ttl``
#: is a required field, and the SDK serialises every response with
#: ``exclude_none=True``, so a ``None`` ttl is dropped from the wire payload
#: and the client rejects the row as malformed. A finite window is therefore
#: advertised instead. It under-claims retention, which is the safe
#: direction: a client that forgets the handle after this window stops
#: polling, and the run itself is unaffected. 24h covers the minutes-to-hours
#: span a Bernstein run occupies.
_TASK_TTL_MS = 86_400_000


def _resolve_run_journal(sdd_dir: Path, run_id: str) -> tuple[str, Path]:
    """Return the ``(run id, journal path)`` a poll identifier resolves to.

    A caller of ``bernstein_task_handle`` holds whichever identifier
    ``bernstein_run`` handed it: the task id, or the journal run id that
    :func:`bernstein.core.tasks.checkpoint_retry.task_run_id` derives from
    the task id. Both must reach the same journal, and the order is fixed so
    an identifier can never resolve two ways:

    1. ``run_id`` read as a journal run id, when that journal exists on disk.
    2. ``task_run_id(run_id)``, when that journal exists on disk.
    3. ``run_id`` as given, so an identifier with no journal at all keeps
       projecting the empty working handle it projects today.

    Rule 1 before rule 2 is what makes a task id already shaped like
    ``task-*`` unambiguous: it names its own journal if one exists, and only
    otherwise is it slugified a second time.

    Every candidate goes through ``run_journal_path``, so the containment
    barrier stays the single check on both forms and neither can address a
    journal outside the runs root.

    Args:
        sdd_dir: The project ``.sdd`` directory.
        run_id: The identifier the caller polled with.

    Returns:
        The resolved run id and its containment-checked journal path.

    Raises:
        JournalPathError: ``run_id`` cannot safely name a journal directory.
    """
    from bernstein.core.replay.journal import JournalPathError, run_journal_path
    from bernstein.core.tasks.checkpoint_retry import task_run_id

    direct = run_journal_path(sdd_dir, run_id)
    if direct.is_file():
        return run_id, direct

    derived_id = task_run_id(run_id)
    if derived_id != run_id:
        try:
            derived = run_journal_path(sdd_dir, derived_id)
        except JournalPathError:
            derived = None
        if derived is not None and derived.is_file():
            return derived_id, derived

    return run_id, direct


def _project_task_helper(data: dict[str, Any]) -> Any:
    from datetime import datetime

    task_id = data["id"]
    head_hash = _get_journal_head(task_id)
    mcp_task_id = f"{task_id}:{head_hash}" if head_hash else task_id

    # Map every Bernstein TaskStatus onto an MCP Tasks status. Terminal states
    # MUST project to a terminal MCP status: a spec-compliant client only calls
    # getTaskResult once get_task reports terminal, so leaving e.g. the normal
    # ``done -> closed`` success path as "working" makes the client poll forever.
    status_str = data.get("status", "open")
    _MCP_STATUS_BY_TASK_STATUS = {
        # in-progress / recoverable -> working
        "open": "working",
        "claimed": "working",
        "in_progress": "working",
        "waiting_for_subtasks": "working",
        "orphaned": "working",
        # needs input or approval -> input_required
        "blocked": "input_required",
        "pending_approval": "input_required",
        "planned": "input_required",
        "blocked_by_abandon": "input_required",
        # terminal success -> completed
        "done": "completed",
        "closed": "completed",
        # terminal failure -> failed
        "failed": "failed",
        "abandoned": "failed",
        "refused": "failed",
        # terminal cancel -> cancelled
        "cancelled": "cancelled",
    }
    mcp_status = _MCP_STATUS_BY_TASK_STATUS.get(status_str)
    if mcp_status is None:
        logger.warning("Unrecognized task status %r; defaulting MCP status to working", status_str)
        mcp_status = "working"

    status_message = data.get("result_summary")
    if not status_message:
        if mcp_status == "working":
            status_message = "Task is running"
        elif mcp_status == "input_required":
            status_message = "Task requires input or approval"
        elif mcp_status == "completed":
            status_message = "Task completed successfully"
        elif mcp_status == "failed":
            status_message = "Task failed"
        elif mcp_status == "cancelled":
            status_message = "Task was cancelled"

    created_at_ts = data.get("created_at")
    created_at = datetime.fromtimestamp(created_at_ts, tz=UTC) if created_at_ts else datetime.now(UTC)

    # Derive lastUpdatedAt from the task's newest recorded transition
    # (created -> claimed -> completed -> closed) so two reads of an unchanged
    # task project the SAME timestamp. Stamping wall-clock now() here is
    # non-idempotent: it manufactures phantom updates for change-detection
    # clients and breaks the deterministic-projection contract. now() stays
    # only as a last resort when the payload carries no timestamp at all.
    transition_ts = (
        data.get("created_at"),
        data.get("claimed_at"),
        data.get("completed_at"),
        data.get("closed_at"),
    )
    newest_ts = max((t for t in transition_ts if t is not None), default=None)
    last_updated = datetime.fromtimestamp(newest_ts, tz=UTC) if newest_ts is not None else datetime.now(UTC)

    return Task(
        taskId=mcp_task_id,
        status=mcp_status,
        statusMessage=status_message,
        createdAt=created_at,
        lastUpdatedAt=last_updated,
        ttl=_TASK_TTL_MS,
        pollInterval=5000,
    )


async def _run_impl(
    server_url: str,
    *,
    goal: str,
    role: str,
    priority: int,
    scope: str,
    complexity: str,
    estimated_minutes: int,
    parent_task_id: str | None = None,
    ctx: Context | None = None,
) -> str | CreateTaskResult:
    """Start a run (or a subtask run) and return the pollable handle body.

    Shared by ``bernstein_run`` and the deprecated ``bernstein_create_subtask``
    alias so both names queue work through one code path.
    """
    try:
        payload: dict[str, Any] = {
            "title": goal[:120],
            "description": goal,
            "role": role,
            "priority": priority,
            "scope": scope,
            "complexity": complexity,
            "estimated_minutes": estimated_minutes,
        }
        endpoint = "/tasks"
        if parent_task_id is not None:
            payload["parent_task_id"] = parent_task_id
            endpoint = "/tasks/self-create"

        # Whether THIS call carried task metadata, not whether the client
        # is capable of tasks. A tasks-capable client still sends plain
        # tools/call requests, and those require a CallToolResult; only a
        # task-augmented call may be answered with a CreateTaskResult.
        is_task_call = False
        traceparent = None
        tracestate = None
        baggage = None

        if ctx is not None:
            try:
                rc = ctx.request_context
                if rc is not None:
                    if rc.experimental is not None:
                        is_task_call = bool(getattr(rc.experimental, "is_task", False))
                    if rc.meta is not None:
                        extra = rc.meta.model_extra or {}
                        traceparent = extra.get("traceparent")
                        tracestate = extra.get("tracestate")
                        baggage = extra.get("baggage")
            except Exception:
                pass

        headers = _auth_headers()
        if traceparent:
            headers["traceparent"] = traceparent
        if tracestate:
            headers["tracestate"] = tracestate
        if baggage:
            headers["baggage"] = baggage

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(f"{server_url}{endpoint}", json=payload, headers=headers)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()

        if is_task_call:
            task_obj = _project_task_helper(data)
            return CreateTaskResult(task=task_obj)

        from bernstein.core.tasks.checkpoint_retry import task_run_id

        body: dict[str, Any] = {
            "task_id": data["id"],
            "title": data["title"],
            "status": data["status"],
            "run_id": task_run_id(data["id"]),
            "poll_after_ms": _POLL_AFTER_MS,
        }
        if parent_task_id is not None:
            body["parent_task_id"] = data.get("parent_task_id", parent_task_id)
        return json.dumps(body, indent=2)
    except Exception as exc:
        return _error_response(exc)


#: Compact task-row fields ``bernstein_status`` reports for a status filter
#: when ``detail`` is off. The full server rows need ``detail=true``.
_COMPACT_TASK_FIELDS = ("id", "title", "role", "status")


async def _status_impl(server_url: str, *, status: str | None = None, detail: bool = False) -> str:
    """Liveness, counts, cost, and an optional status-filtered task list.

    The folded read surface (#3087): the tool answering at all is the MCP
    liveness signal, the ``/status`` payload supplies counts and cost, and a
    ``status`` filter pulls the matching tasks in one call. A task-server
    outage is reported with ``live: true`` plus the error, because the MCP
    server itself answered.
    """
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{server_url}/status", headers=_auth_headers())
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            tasks: list[dict[str, Any]] | None = None
            if status:
                tasks_resp = await client.get(
                    f"{server_url}/tasks",
                    params={"status": status},
                    headers=_auth_headers(),
                )
                tasks_resp.raise_for_status()
                tasks = tasks_resp.json()

        per_role_raw: list[dict[str, Any]] = data.get("per_role", [])
        body: dict[str, Any] = {
            "live": True,
            "counts": {
                "total": data.get("total", 0),
                "open": data.get("open", 0),
                "claimed": data.get("claimed", 0),
                "done": data.get("done", 0),
                "failed": data.get("failed", 0),
            },
            "cost": {
                "total_cost_usd": data.get("total_cost_usd", 0.0),
                "per_role": [{"role": r["role"], "cost_usd": r.get("cost_usd", 0.0)} for r in per_role_raw],
            },
        }
        if detail:
            body["per_role"] = per_role_raw
        if tasks is not None:
            body["status_filter"] = status
            if detail:
                body["tasks"] = tasks
            else:
                body["tasks"] = [{k: t.get(k) for k in _COMPACT_TASK_FIELDS} for t in tasks]
        return json.dumps(body, indent=2)
    except Exception as exc:
        # The MCP server answered, so liveness is true even though the task
        # server is not reachable; the folded health signal must say so.
        logger.warning("MCP tool error: %s", exc)
        return json.dumps(
            {"live": True, "error": str(exc), "hint": "Task server may be restarting"},
            indent=2,
        )


async def _tasks_alias_impl(server_url: str, status: str | None = None) -> str:
    """Historical ``bernstein_tasks`` body: the raw task list."""
    try:
        params: dict[str, str] = {}
        if status:
            params["status"] = status
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{server_url}/tasks", params=params, headers=_auth_headers())
            resp.raise_for_status()
            data: list[dict[str, Any]] = resp.json()
        return json.dumps(data, indent=2)
    except Exception as exc:
        return _error_response(exc)


async def _cost_alias_impl(server_url: str) -> str:
    """Historical ``bernstein_cost`` body: the cost projection of /status."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{server_url}/status", headers=_auth_headers())
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        per_role_raw: list[dict[str, Any]] = data.get("per_role", [])
        cost_summary: dict[str, Any] = {
            "total_cost_usd": data.get("total_cost_usd", 0.0),
            "per_role": [{"role": r["role"], "cost_usd": r.get("cost_usd", 0.0)} for r in per_role_raw],
        }
        return json.dumps(cost_summary, indent=2)
    except Exception as exc:
        return _error_response(exc)


def _register_query_tools(mcp: FastMCP[None], server_url: str) -> None:
    """Register the read surface: run and the folded status tool."""

    @mcp.tool()
    async def bernstein_run(  # pyright: ignore[reportUnusedFunction]
        goal: str,
        role: str = "backend",
        priority: int = 2,
        scope: str = "medium",
        complexity: str = "medium",
        estimated_minutes: int = 30,
        parent_task_id: str | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Start an orchestration run by posting a task to the Bernstein server.

        A run executes real work and takes minutes to hours. This call
        returns as soon as the run is queued, not when it finishes. Do not
        re-issue it while waiting: that starts a second run. To follow the
        run, wait ``poll_after_ms`` and then call ``bernstein_run_status``,
        passing either the returned ``task_id`` or the returned ``run_id``.
        Poll it until ``status`` is terminal (``completed``, ``failed`` or
        ``cancelled``).

        Args:
            goal: Description of what you want Bernstein to accomplish.
            role: Specialist role to assign (backend, frontend, qa, security, …).
            priority: 1=critical, 2=normal, 3=nice-to-have.
            scope: Task scope - small, medium, or large.
            complexity: Task complexity - low, medium, or high.
            estimated_minutes: Rough time estimate in minutes.
            parent_task_id: When set, the run is created as a subtask of this
                task, and the parent transitions to ``waiting_for_subtasks``.

        Returns:
            JSON with the created task ID, title and status, plus the
            ``run_id`` naming the run journal and the advisory
            ``poll_after_ms`` delay before the first poll. When the call
            itself is task-augmented (the client sent ``task`` in the request
            params), a Tasks-extension ``CreateTaskResult`` is returned
            instead so the client can poll the run.
        """
        args: dict[str, Any] = {
            "goal": goal,
            "role": role,
            "priority": priority,
            "scope": scope,
            "complexity": complexity,
            "estimated_minutes": estimated_minutes,
        }
        if parent_task_id is not None:
            args["parent_task_id"] = parent_task_id
        err = _validate_or_error("bernstein_run", args)
        if err is not None:
            return _validation_error_response(err)
        return await _run_impl(
            server_url,
            goal=goal,
            role=role,
            priority=priority,
            scope=scope,
            complexity=complexity,
            estimated_minutes=estimated_minutes,
            parent_task_id=parent_task_id,
            ctx=ctx,
        )

    @mcp.tool()
    async def bernstein_status(  # pyright: ignore[reportUnusedFunction]
        status: str | None = None,
        detail: bool = False,
    ) -> str:
        """Liveness, task counts, cost, and an optional filtered task list.

        One read answers the whole "how is the server doing" question: the
        tool responding at all is the MCP liveness check, ``counts`` carries
        the task totals, ``cost`` the USD spend, and passing ``status``
        appends the matching tasks. ``detail`` switches the per-role and
        per-task rows from compact to full.

        Args:
            status: Optional task filter - open, claimed, in_progress, done,
                failed, blocked, or cancelled. When set, the matching tasks
                are included under ``tasks``.
            detail: Include the full per-role breakdown and, with ``status``,
                the full task rows instead of compact ones.

        Returns:
            JSON with ``live``, ``counts``, ``cost``, and optionally
            ``per_role`` / ``status_filter`` / ``tasks``. A task-server
            outage still answers with ``live: true`` plus an ``error``.
        """
        args: dict[str, Any] = {"detail": detail}
        if status is not None:
            args["status"] = status
        err = _validate_or_error("bernstein_status", args)
        if err is not None:
            return _validation_error_response(err)
        return await _status_impl(server_url, status=status, detail=detail)


def _read_audit_chain_head(audit_dir: Path) -> str:
    """Return the current audit-chain head hash without needing the HMAC key.

    The head is the ``hmac`` field of the last byte-strict-valid record in the
    newest live log segment. Reading the stored tail (rather than recomputing
    it) lets a read-only polling tool embed the chain head into a run handle
    without resolving or creating an operator key.
    """
    if not audit_dir.is_dir():
        return ""
    segments = sorted(audit_dir.glob("*.jsonl"))
    if not segments:
        return ""
    from bernstein.core.security.audit import _chain_tail_from_bytes  # pyright: ignore[reportPrivateUsage]

    try:
        raw = segments[-1].read_bytes()
    except OSError:
        return ""
    return _chain_tail_from_bytes(raw) or ""


#: Last progress vector emitted per run id, used only to suppress ticks that
#: do not strictly advance (#3085). Process-local: each server instance emits
#: its own monotone sequence, and the sequence values themselves are pure
#: folds of the journal, so two instances never disagree on a tick's content.
_PROGRESS_TICKS: dict[str, Any] = {}


def _progress_notification_payload(vector: Any) -> dict[str, Any]:
    """Build the ``notifications/progress`` payload from a progress vector.

    Every field is journal-derived: ``progress`` is the fold's earned-steps
    scalar, ``total`` the declared evidence producers (omitted when none are
    declared), and ``message`` a rendering of fold counters only. No wall
    clock and no model-produced value enters the payload, so two folds over
    the same journal produce byte-identical payloads.
    """
    payload: dict[str, Any] = {
        "progress": vector.earned_steps,
        "total": vector.evidence_declared if vector.evidence_declared > 0 else None,
        "message": (
            f"phase={vector.ledger_phase or 'unknown'}"
            f" checkpoints={vector.checkpoints}"
            f" diffs={vector.diffs_captured}"
            f" gates={vector.gate_attempts}"
            f" evidence={vector.evidence_passed}/{vector.evidence_declared}"
        ),
    }
    return payload


def _should_emit_progress(previous: Any, current: Any) -> bool:
    """Whether ``current`` may be notified after ``previous`` was.

    The first tick for a run always emits. After that, only a vector that
    :meth:`ProgressVector.strictly_advances` the previous tick emits: an
    unchanged or regressed fold is suppressed, so the notified sequence is
    strictly monotone by construction rather than by client-side filtering.
    """
    if previous is None:
        return True
    return bool(current.strictly_advances(previous))


async def _maybe_emit_progress(ctx: Context | None, sdd_dir: Path, run_id: str) -> None:
    """Emit a journal-fold progress tick for ``run_id``, when asked and due.

    Emits only when the request carried a ``progressToken`` (the SDK's
    ``report_progress`` is a no-op otherwise, and the token is checked here
    too so no fold work happens for callers that did not ask). Any failure -
    fold or emission - is swallowed: a progress tick must never raise into
    the tool result (#3085).
    """
    if ctx is None:
        return
    try:
        rc = ctx.request_context
        token = rc.meta.progressToken if rc is not None and rc.meta is not None else None
        if token is None:
            return
        from bernstein.core.replay.progress import project_task_progress

        vector = project_task_progress(sdd_dir, run_id, run_id=run_id)
        previous = _PROGRESS_TICKS.get(run_id)
        if not _should_emit_progress(previous, vector):
            return
        payload = _progress_notification_payload(vector)
        await ctx.report_progress(
            progress=float(payload["progress"]),
            total=float(payload["total"]) if payload["total"] is not None else None,
            message=str(payload["message"]),
        )
        _PROGRESS_TICKS[run_id] = vector
    except Exception:
        # Best-effort by contract: a progress signal is advisory and must
        # never break the poll that carried it.
        logger.debug("progress notification failed for %s", run_id, exc_info=True)


async def _run_status_impl(run_id: str, workdir: str = ".", ctx: Context | None = None) -> str:
    """Project the verifiable run handle for ``run_id`` (shared by the alias).

    When the request carried a ``progressToken``, a ``notifications/progress``
    tick is emitted from the chain-computed progress fold before the handle is
    returned (#3085). Emission is best-effort: a failure to notify never
    reaches the tool result.
    """
    try:
        from bernstein.core.protocols.mcp.tasks_extension import RunHandle
        from bernstein.core.replay.journal import (
            JournalPathError,
            load_events,
        )

        base = Path(workdir).resolve()
        # Shared barrier rather than a local containment check, so this
        # surface cannot drift from the rest of the run-journal readers.
        # _resolve_run_journal applies it to every candidate id.
        try:
            resolved_id, journal_path = _resolve_run_journal(base / ".sdd", run_id)
        except JournalPathError as exc:
            return _error_response(
                exc,
                hint="run_id must be a plain run identifier",
            )
        events = load_events(journal_path)
        chain_head = _read_audit_chain_head(base / ".sdd" / "audit")
        # Both id forms project the identical handle: the handle is a
        # projection of the journal, not of how the caller addressed it.
        handle = RunHandle.from_journal(
            task_id=resolved_id,
            run_id=resolved_id,
            events=events,
            chain_head=chain_head,
        )
        await _maybe_emit_progress(ctx, base / ".sdd", resolved_id)
        return json.dumps(handle.to_wire(), indent=2)
    except Exception as exc:
        return _error_response(exc, hint="Run journal not found")


def _register_run_status_tool(mcp: FastMCP[None]) -> None:
    """Register the ``bernstein_run_status`` Tasks-extension polling tool.

    The tool reprojects a verifiable run handle from the on-disk run journal
    and the audit-chain head, so a stateless MCP client can poll a run it
    started and retrieve results without holding a session (issue #2364).
    """

    @mcp.tool()
    async def bernstein_run_status(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        workdir: str = ".",
        ctx: Context | None = None,
    ) -> str:
        """Return a verifiable Tasks-extension handle for a run, by run id.

        The handle's status is a pure projection of the run journal, and it
        embeds the run's audit-chain head so the client can later verify the
        task it watched corresponds to the audited run (``bernstein audit
        verify`` or the offline verifier). Polling is stateless: any server
        instance reprojects the same handle from the on-disk journal. A poll
        that carries a ``progressToken`` also receives a
        ``notifications/progress`` tick derived from the journal fold, and
        only when the fold strictly advanced since the previous tick.

        Args:
            run_id: The run to project. Either identifier ``bernstein_run``
                returned is accepted: the ``task_id``, or the ``run_id``
                naming the journal. Resolution is journal run id first, then
                the task id slugified into a journal run id, so the two forms
                reach one journal and project an identical handle. Must be a
                plain identifier - path separators and traversal are refused.
            workdir: Project root directory (default: current directory).

        Returns:
            JSON of the Tasks-extension task-handle body (``taskId``,
            ``runId``, ``status``, ``journalHead``, ``chainHead``,
            ``receiptHash``, ``pollToken``, ...).
        """
        err = _validate_or_error("bernstein_run_status", {"run_id": run_id, "workdir": workdir})
        if err is not None:
            return _validation_error_response(err)
        return await _run_status_impl(run_id, workdir, ctx)


async def _task_capsule_impl(task_id: str, workdir: str = ".", verify: bool = False) -> str:
    """Read (and optionally verify) a worker's signed context capsule."""
    try:
        from bernstein.core.agents.context_capsule import (
            project_capsule,
            read_capsule_record,
            verify_context_capsule,
        )
        from bernstein.core.security.audit import load_or_create_audit_key
        from bernstein.core.security.audit_chain import AuditChainStore

        base = Path(workdir).resolve()
        sdd_dir = base / ".sdd"
        signed = read_capsule_record(sdd_dir, task_id)
        if signed is None:
            return _error_response(ValueError(f"no context capsule for task {task_id}"), hint="Capsule not found")
        body: dict[str, Any] = {"capsule": project_capsule(signed)}
        if verify:
            chain = AuditChainStore(sdd_dir / "audit", key=load_or_create_audit_key())
            result = verify_context_capsule(sdd_dir=sdd_dir, chain=chain, task_id=task_id)
            body["verify"] = {
                "ok": result.ok,
                "reason": result.reason,
                "is_mock": result.is_mock,
                "signature_ok": result.signature_ok,
                "chain_ok": result.chain_ok,
                "journal_ok": result.journal_ok,
            }
        return json.dumps(body, indent=2)
    except Exception as exc:
        return _error_response(exc, hint="Context capsule not found")


def _register_task_capsule_tool(mcp: FastMCP[None]) -> None:
    """Register the ``bernstein_task_capsule`` capsule tool (#2545).

    A spawned worker reads one signed, chain-anchored answer to "what was I
    given" -- task id, run id, params hash, worktree, role, budget envelope
    remaining, dependency state, and the audit-chain head at spawn -- instead of
    piecing it together from scattered env vars. The tool is served through the
    same deny-by-default input firewall as every other MCP tool.
    """

    @mcp.tool()
    async def bernstein_task_capsule(  # pyright: ignore[reportUnusedFunction]
        task_id: str,
        workdir: str = ".",
        verify: bool = False,
    ) -> str:
        """Return the worker's context capsule, optionally verified offline.

        Args:
            task_id: The task whose capsule to read. Must be a plain
                identifier - path separators and traversal are refused.
            workdir: Project root directory (default: current directory).
            verify: When true, recompute the capsule offline from the run
                journal and audit chain and include the verdict.

        Returns:
            JSON of the capsule projection (and, when ``verify`` is set, the
            offline verification result). A mock-layer fixture is reported as
            such and never verifies as real.
        """
        err = _validate_or_error("bernstein_task_capsule", {"task_id": task_id, "workdir": workdir, "verify": verify})
        if err is not None:
            return _validation_error_response(err)
        return await _task_capsule_impl(task_id, workdir, verify)


async def _post_message_impl(
    server_url: str,
    *,
    task_id: str,
    body: str,
    sender: str,
    kind: str = "finding",
    sender_card_fingerprint: str | None = None,
) -> str:
    """Append a signed mailbox entry (shared by the ``bernstein_update`` alias)."""
    try:
        payload: dict[str, Any] = {"sender": sender, "kind": kind, "body": body}
        if sender_card_fingerprint is not None:
            payload["sender_card_fingerprint"] = sender_card_fingerprint
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                f"{server_url}/tasks/{task_id}/messages",
                json=payload,
                headers=_auth_headers(),
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
        return json.dumps(data, indent=2)
    except Exception as exc:
        return _error_response(exc)


#: Statuses ``POST /tasks/{id}/cancel`` accepts, mirrored from the route
#: (``src/bernstein/core/routes/task_crud.py`` ``cancel_task``). The tool
#: reads the task first and refuses non-cancellable states without sending a
#: state-changing request, the same read-before-act gate ``bernstein_approve``
#: and ``bernstein_complete`` apply; the server remains the authority and a
#: race that slips past the read is answered by its 409.
_CANCELLABLE_STATUSES: frozenset[str] = frozenset(
    {"open", "claimed", "in_progress", "blocked", "waiting_for_subtasks", "planned"}
)


def _count_descendants(rows: list[dict[str, Any]], *, root_id: str) -> int:
    """Count rows whose ``parent_task_id`` chain reaches ``root_id``.

    Walks the parent references client-side over one task listing, so the
    count reflects the post-cancel reality the server reports rather than a
    number invented from the request.
    """
    parent_by_id = {str(r.get("id")): r.get("parent_task_id") for r in rows if r.get("id")}
    count = 0
    for row_id in parent_by_id:
        if row_id == root_id:
            continue
        seen: set[str] = set()
        cursor = parent_by_id.get(row_id)
        while isinstance(cursor, str) and cursor not in seen:
            if cursor == root_id:
                count += 1
                break
            seen.add(cursor)
            cursor = parent_by_id.get(cursor)
    return count


def _shutdown_impl(workdir: str) -> str:
    """Write the SHUTDOWN signal (shared by the ``bernstein_stop`` alias)."""
    from bernstein.mcp.signal_paths import ShutdownSignalPathError, shutdown_signal_path

    # Shared barrier rather than a local containment check, so this
    # surface cannot drift from the other workdir-derived writers. The
    # path is resolved and proven contained before any directory is
    # created, so a refused call leaves nothing behind.
    try:
        shutdown_file = shutdown_signal_path(workdir)
    except ShutdownSignalPathError as exc:
        return _error_response(
            exc,
            hint="workdir must be an existing Bernstein project root",
        )
    try:
        shutdown_file.parent.mkdir(parents=True, exist_ok=True)
        shutdown_file.write_text("mcp-stop\n", encoding="utf-8")
        return json.dumps({"status": "shutdown signal sent", "path": str(shutdown_file)})
    except Exception as exc:
        return _error_response(exc, hint="Could not write shutdown signal")


def _register_action_tools(mcp: FastMCP[None], server_url: str) -> None:
    """Register mutation tools: claim, post_message, post_artifact, cancel,
    shutdown_orchestrator, approve, complete."""

    @mcp.tool()
    async def bernstein_claim(  # pyright: ignore[reportUnusedFunction]
        claimer_id: str,
        role: str | None = None,
        project: str | None = None,
        capability: str | None = None,
        completed_ids: list[str] | None = None,
        max_attempts: int | None = None,
        claimer_card_fingerprint: str | None = None,
    ) -> str:
        """Claim the next eligible task and return a verifiable claim receipt.

        Drives the dependency-gated claim path: a task is offered only when
        every id in its ``depends_on`` is present in ``completed_ids``. Unlike
        a raw claim, the result is a signed, content-addressed **claim
        receipt** the worker holds and can re-verify offline against the audit
        chain (``bernstein audit verify``), not a mutable task projection. A
        filter that matches no eligible task returns a signed *refusal*
        receipt - a claim attempt is never a silent skip.

        Args:
            claimer_id: The claiming worker's identity.
            role: Only claim tasks for this role (e.g. ``backend``).
            project: Only claim tasks in this project namespace.
            capability: Only claim tasks requiring this capability.
            completed_ids: Task ids whose dependencies are satisfied; a task
                is eligible only when all of its ``depends_on`` are listed.
            max_attempts: Skip tasks at or above this attempt count.
            claimer_card_fingerprint: ``sha256:`` fingerprint of the claimer's
                agent card key, bound into the receipt.

        Returns:
            JSON of the signed claim receipt (``taskId``, ``granted``,
            ``backlogHead``, ``filterDigest``, ``chainHead``, ``receiptHash``,
            ``signature``, ``pollToken``, ...).
        """
        completed = completed_ids or []
        err = _validate_or_error(
            "bernstein_claim",
            {
                "claimer_id": claimer_id,
                "role": role,
                "project": project,
                "capability": capability,
                "completed_ids": completed,
                "max_attempts": max_attempts,
                "claimer_card_fingerprint": claimer_card_fingerprint,
            },
        )
        if err is not None:
            return _validation_error_response(err)
        try:
            payload: dict[str, Any] = {"claimer_id": claimer_id, "completed_ids": completed}
            if role is not None:
                payload["role"] = role
            if project is not None:
                payload["project"] = project
            if capability is not None:
                payload["capability"] = capability
            if max_attempts is not None:
                payload["max_attempts"] = max_attempts
            if claimer_card_fingerprint is not None:
                payload["claimer_card_fingerprint"] = claimer_card_fingerprint
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(
                    f"{server_url}/tasks/claim-receipt",
                    json=payload,
                    headers=_auth_headers(),
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
            return json.dumps(data, indent=2)
        except Exception as exc:
            return _error_response(exc)

    @mcp.tool()
    async def bernstein_post_message(  # pyright: ignore[reportUnusedFunction]
        task_id: str,
        body: str,
        sender: str,
        kind: str = "finding",
        sender_card_fingerprint: str | None = None,
    ) -> str:
        """Post a message to a task's mailbox as a signed journal entry.

        Wraps the worker mailbox: the message is DLP-redacted, HMAC-chained
        onto the mailbox journal, Ed25519-signed, and mirrored to the audit
        chain (``task.mailbox_message``) before returning. The result IS the
        signed journal entry - a worker holds a progress record it can verify
        offline against the same chain ``bernstein audit verify`` walks, not a
        bare status string. This tool never changes a task's fields; it
        appends to the task's mailbox.

        Args:
            task_id: The task the message is addressed to.
            body: The message body (<= 4096 bytes).
            sender: The posting worker's identity.
            kind: Typed message kind - one of ``finding`` / ``artefact_ref``
                / ``question``.
            sender_card_fingerprint: ``sha256:`` fingerprint of the sender's
                agent card key.

        Returns:
            JSON of the signed mailbox journal entry (``seq``,
            ``prev_entry_hash``, ``entry_hash``, ``signature``,
            ``signer_public_key_pem``, ``body_hash``, ...).
        """
        err = _validate_or_error(
            "bernstein_post_message",
            {
                "task_id": task_id,
                "body": body,
                "sender": sender,
                "kind": kind,
                "sender_card_fingerprint": sender_card_fingerprint,
            },
        )
        if err is not None:
            return _validation_error_response(err)
        return await _post_message_impl(
            server_url,
            task_id=task_id,
            body=body,
            sender=sender,
            kind=kind,
            sender_card_fingerprint=sender_card_fingerprint,
        )

    @mcp.tool()
    async def bernstein_post_artifact(  # pyright: ignore[reportUnusedFunction]
        task_id: str,
        key: str,
        artifact_type: str,
        poster: str,
        body: str = "",
        columns: list[str] | None = None,
        rows: list[list[str]] | None = None,
        url: str = "",
        link_kind: str = "",
    ) -> str:
        """Attach a journal-anchored artifact to a task you hold the claim for.

        The artifact is stored content-addressed, sealed into the lineage
        spine, appended to the task's Merkle-chained journal, and mirrored to
        the audit chain. The returned record IS the receipt: its identity is the
        spine entry hash, and any reviewer can re-verify the content hash offline
        against the same chain ``bernstein audit verify`` walks. Reposting a key
        appends a new version chained to the prior one. There is no way to set
        progress here - progress is a chain-computed projection of journaled
        work, never postable.

        Args:
            task_id: The task to attach the artifact to. You must hold its claim.
            key: The artifact slot; reposting a key appends a new version.
            artifact_type: One of ``report`` (markdown ``body``), ``table``
                (``columns`` + ``rows``), or ``link`` (``url`` + ``link_kind``).
            poster: Your claim identity; posting against a task you do not hold
                is refused and the refusal is audit-recorded.
            body: Markdown body, for ``report`` artifacts.
            columns: Column headers, for ``table`` artifacts.
            rows: Rows of cells, for ``table`` artifacts.
            url: The URL, for ``link`` artifacts.
            link_kind: The declared link kind - ``preview`` / ``dashboard`` /
                ``document`` - for ``link`` artifacts.

        Returns:
            JSON of the chain-anchored artifact record (``key``, ``version``,
            ``content_hash``, ``spine_entry_hash``, ``journal_index``, ...).
        """
        # An argument left at its empty-string default means "not supplied for
        # this artifact type", so it must not reach the validator: the schema
        # constrains ``body`` / ``url`` / ``link_kind`` to the values a caller
        # that actually supplies them would send. Validate exactly the fields
        # that get posted, so the conditional shape advertised to the caller is
        # the shape the call is judged against.
        args: dict[str, Any] = {
            "task_id": task_id,
            "key": key,
            "artifact_type": artifact_type,
            "poster": poster,
        }
        if body:
            args["body"] = body
        if columns is not None:
            args["columns"] = columns
        if rows is not None:
            args["rows"] = rows
        if url:
            args["url"] = url
        if link_kind:
            args["link_kind"] = link_kind
        err = _validate_or_error("bernstein_post_artifact", args)
        if err is not None:
            return _validation_error_response(err)
        try:
            payload: dict[str, Any] = {k: v for k, v in args.items() if k != "task_id"}
            # Carry the caller identity in the request header (the server's
            # authorization principal), not only in the body. The server refuses
            # posts against a task this identity does not hold the claim for.
            headers = _auth_headers() | {"x-bernstein-agent-id": poster}
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(
                    f"{server_url}/tasks/{task_id}/artifacts",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
            return json.dumps(data, indent=2)
        except Exception as exc:
            return _error_response(exc)

    @mcp.tool()
    async def bernstein_cancel(  # pyright: ignore[reportUnusedFunction]
        task_id: str,
        reason: str = "",
    ) -> str:
        """Cancel one task and its subtask tree; the orchestrator keeps running.

        Posts to ``/tasks/{task_id}/cancel``, which cascades through the
        subtask tree (``parent_task_id`` references) so children are not left
        running after the parent is aborted. Cancellable statuses are
        ``open``, ``claimed``, ``in_progress``, ``blocked``,
        ``waiting_for_subtasks`` and ``planned``. A task already in a
        terminal state is reported with its current state rather than
        cancelled again, and an unknown task id is refused. To stop the
        whole orchestrator instead, use ``bernstein_shutdown_orchestrator``.

        Args:
            task_id: The root task to cancel. Its descendants are cancelled
                with it.
            reason: Optional reason recorded on the cancellation.

        Returns:
            JSON with the cancelled root task, its status, and the count of
            cascaded descendants - or the task's current state when it was
            already terminal.
        """
        args: dict[str, Any] = {"task_id": task_id}
        if reason:
            args["reason"] = reason
        err = _validate_or_error("bernstein_cancel", args)
        if err is not None:
            return _validation_error_response(err)
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                # Read before act, like every other terminal-route verb: an
                # unknown id or a non-cancellable state is refused without a
                # state-changing request being sent at all.
                read = await client.get(f"{server_url}/tasks/{task_id}", headers=_auth_headers())
                if read.status_code == 404:
                    return json.dumps(
                        {
                            "error": "unknown_task",
                            "task_id": task_id,
                            "message": f"No task with id {task_id!r}; nothing was cancelled.",
                        },
                        indent=2,
                    )
                read.raise_for_status()
                current = str(read.json().get("status") or "unknown")
                if current not in _CANCELLABLE_STATUSES:
                    return json.dumps(
                        {
                            "task_id": task_id,
                            "status": current,
                            "cancelled": False,
                            "message": f"Task {task_id} is already in a terminal or non-cancellable state ({current}).",
                        },
                        indent=2,
                    )
                resp = await client.post(
                    f"{server_url}/tasks/{task_id}/cancel",
                    json={"reason": reason},
                    headers=_auth_headers(),
                )
                if resp.status_code == 409:
                    # The task moved between the read and the cancel: the
                    # server's answer is the reality, reported as a state.
                    reread = await client.get(f"{server_url}/tasks/{task_id}", headers=_auth_headers())
                    moved = str(reread.json().get("status") or "unknown") if reread.is_success else "unknown"
                    return json.dumps(
                        {
                            "task_id": task_id,
                            "status": moved,
                            "cancelled": False,
                            "message": f"Task {task_id} is already in a terminal or non-cancellable state ({moved}).",
                        },
                        indent=2,
                    )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                # The route returns the root task only; count the cascaded
                # descendants from the post-cancel task list so the result
                # reports what actually happened, not what was requested.
                descendants = 0
                try:
                    listing = await client.get(
                        f"{server_url}/tasks",
                        params={"status": "cancelled"},
                        headers=_auth_headers(),
                    )
                    listing.raise_for_status()
                    cancelled_rows: list[dict[str, Any]] = listing.json()
                    descendants = _count_descendants(cancelled_rows, root_id=task_id)
                except Exception:  # pragma: no cover - counting is best-effort
                    logger.debug("descendant count failed for %s", task_id, exc_info=True)
            return json.dumps(
                {
                    "task_id": data["id"],
                    "status": data["status"],
                    "cancelled": True,
                    "cancelled_descendants": descendants,
                },
                indent=2,
            )
        except Exception as exc:
            return _error_response(exc)

    @mcp.tool()
    async def bernstein_shutdown_orchestrator(  # pyright: ignore[reportUnusedFunction]
        workdir: str = ".",
    ) -> str:
        """Shut down the ENTIRE Bernstein orchestrator for this project - every
        run, every worker - not one task; to stop a single run and keep the
        orchestrator alive, use ``bernstein_cancel`` instead.

        Writes ``.sdd/runtime/signals/SHUTDOWN`` in the project directory,
        which the orchestrator detects and shuts down gracefully.

        Args:
            workdir: Project root directory (default: current directory).

        Returns:
            Confirmation message.
        """
        err = _validate_or_error("bernstein_shutdown_orchestrator", {"workdir": workdir})
        if err is not None:
            return _validation_error_response(err)
        return _shutdown_impl(workdir)

    @mcp.tool()
    async def bernstein_approve(  # pyright: ignore[reportUnusedFunction]
        task_id: str,
        note: str = "Approved via MCP",
    ) -> str:
        """Sign off a finished result that is waiting on a decision.

        The tool reads the task first and acts only on ``pending_approval``:
        the work has run, the result is held for a decision, and accepting it
        completes the task with ``note`` as the result summary.

        Every other status is refused with a structured error naming the
        current status, and no state-changing request is sent:

        * ``planned`` - the task is held by plan mode. That decision is
          recorded on the plan, not on the task, so approve the plan
          (``bernstein plan approve <plan_id>``). Releasing one task would
          start the work while the plan is still undecided.
        * ``open``, ``claimed``, ``in_progress``, ``blocked``, ``failed``,
          terminal states, ... - there is no approval to grant. Use
          ``bernstein_complete`` to report work you are executing,
          ``bernstein_update`` to report a blocker on the task mailbox, or
          cancel the task to abandon it.

        Args:
            task_id: ID of the task to approve.
            note: Approval note recorded as the result summary.

        Returns:
            JSON with the task id, its new status, and which approval was
            granted - or a structured refusal naming the current status.
        """
        err = _validate_or_error("bernstein_approve", {"task_id": task_id, "note": note})
        if err is not None:
            return _validation_error_response(err)
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                read = await client.get(f"{server_url}/tasks/{task_id}", headers=_auth_headers())
                read.raise_for_status()
                current_status = str(read.json().get("status") or "")
                if not is_approvable(current_status):
                    return _approval_refusal_response(task_id, current_status)
                resp = await client.post(
                    f"{server_url}/tasks/{task_id}/complete",
                    json={"result_summary": note},
                    headers=_auth_headers(),
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
            return json.dumps(
                {
                    "task_id": data["id"],
                    "status": data["status"],
                    "approval": "completion_signed_off",
                    "approved_from": current_status,
                    "result_summary": data.get("result_summary"),
                },
                indent=2,
            )
        except Exception as exc:
            return _error_response(exc)

    @mcp.tool()
    async def bernstein_complete(  # pyright: ignore[reportUnusedFunction]
        task_id: str,
        result_summary: str,
    ) -> str:
        """Report the result of work you are executing.

        This is the completion verb of the MCP worker loop (claim with
        ``bernstein_claim``, report with ``bernstein_update``, finish here).
        The tool reads the task first and completes it only from a state a
        worker holds it in (``open``, ``claimed``, ``in_progress``).

        It is not a way to clear a task out of the way. A parent in
        ``waiting_for_subtasks`` is completed by its subtasks finishing, an
        ``orphaned`` task belongs to crash recovery, and a result already
        awaiting a decision is signed off with ``bernstein_approve``; all
        three are refused with a structured error naming the current status.
        Report only work that actually ran, and use ``bernstein_update``
        when the task is unfinished or stuck.

        Args:
            task_id: ID of the task to complete.
            result_summary: What the work produced. The task server rejects
                an empty summary and fails the task instead.

        Returns:
            JSON with the task id, its new status, and the recorded summary -
            or a structured refusal naming the current status.
        """
        err = _validate_or_error(
            "bernstein_complete",
            {"task_id": task_id, "result_summary": result_summary},
        )
        if err is not None:
            return _validation_error_response(err)
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                read = await client.get(f"{server_url}/tasks/{task_id}", headers=_auth_headers())
                read.raise_for_status()
                current_status = str(read.json().get("status") or "")
                if not is_worker_completable(current_status):
                    return _completion_refusal_response(task_id, current_status)
                resp = await client.post(
                    f"{server_url}/tasks/{task_id}/complete",
                    json={"result_summary": result_summary},
                    headers=_auth_headers(),
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
            return json.dumps(
                {"task_id": data["id"], "status": data["status"], "result_summary": data.get("result_summary")},
                indent=2,
            )
        except Exception as exc:
            return _error_response(exc)


def _register_skill_tools(mcp: FastMCP[None]) -> None:
    """Register the ``load_skill`` progressive-disclosure tool (oai-004).

    Args:
        mcp: FastMCP instance to register the tool on.
    """

    def _skill_loader():  # type: ignore[no-untyped-def]
        # Local imports keep the MCP module cheap to import when the skills
        # tree is missing (for example, a dev CLI without templates).
        from pathlib import Path as _Path

        from bernstein import get_templates_dir
        from bernstein.core.skills.loader import default_loader_from_templates

        templates_root = get_templates_dir(_Path.cwd())
        return default_loader_from_templates(templates_root / "roles")

    def _skill_index_json() -> str:
        from bernstein.core.skills.index_builder import serialize_skill_discovery_index

        return serialize_skill_discovery_index(_skill_loader())

    from bernstein.core.skills.index_builder import SKILL_INDEX_RESOURCE_URI

    @mcp.resource(
        SKILL_INDEX_RESOURCE_URI,
        name="skill_index",
        description="Compact index of loadable Bernstein skills and their content hashes.",
        mime_type="application/json",
    )
    def skill_index() -> str:  # pyright: ignore[reportUnusedFunction]
        return _skill_index_json()

    @mcp.tool()
    async def load_skill(  # pyright: ignore[reportUnusedFunction]
        name: str | None = None,
        reference: str | None = None,
        script: str | None = None,
    ) -> str:
        """Discover skills, or load a named skill body, reference, or script.

        Omit ``name`` to receive the compact skill index. Pass a skill name
        to fetch its full ``SKILL.md`` body. ``reference`` and ``script``
        are valid only with a named skill.

        Args:
            name: Optional skill name (for example ``"backend"``).
            reference: Optional filename under ``references/`` - for
                example ``"python-conventions.md"``.
            script: Optional filename under ``scripts/`` - for example
                ``"lint.sh"``. The script content is returned as text; the
                MCP harness does not execute it.

        Returns:
            The compact index when ``name`` is omitted; otherwise JSON with
            ``name``, ``body``, available files, and optional fetched content.
        """
        payload = {
            key: value
            for key, value in {
                "name": name,
                "reference": reference,
                "script": script,
            }.items()
            if value is not None
        }
        err = _validate_or_error(
            "load_skill",
            payload,
        )
        if err is not None:
            return _validation_error_response(err)
        try:
            if name is None:
                return _skill_index_json()

            from bernstein.core.skills.load_skill_tool import (
                load_skill as _load_skill_impl,
            )
            from bernstein.core.skills.load_skill_tool import (
                result_as_dict,
            )

            result = _load_skill_impl(
                name=name,
                reference=reference,
                script=script,
                loader=_skill_loader(),
            )
            return json.dumps(result_as_dict(result), indent=2)
        except Exception as exc:
            return _error_response(exc, hint="Skill not found or templates missing")


#: Env override for the lineage MCP exposure. When unset the default is
#: ``True`` for stdio and ``False`` for SSE (ADR-009 §7.3).
_LINEAGE_MCP_ENV = "BERNSTEIN_LINEAGE_MCP_ENABLED"


def _lineage_mcp_default(*, default: bool) -> bool:
    """Resolve whether the lineage MCP resources should be registered."""
    raw = os.environ.get(_LINEAGE_MCP_ENV)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


#: Result shape shared by every failure path a Bernstein tool renders:
#: :func:`_error_response` emits ``{"error", "hint"}`` and the input firewall
#: emits ``{"error", "jsonrpc_error"}``. ``additionalProperties`` stays open
#: so the two variants share one branch of the advertised ``anyOf``.
_TOOL_ERROR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {"type": "string"},
        "hint": {"type": "string"},
    },
    "additionalProperties": True,
}


def _run_success_schema() -> dict[str, Any]:
    """Schema of the ``bernstein_run`` handle body for a plain (non-task) call."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["task_id", "title", "status", "run_id", "poll_after_ms"],
        "properties": {
            "task_id": {"type": "string"},
            "title": {"type": "string"},
            "status": {"type": "string"},
            "run_id": {"type": "string"},
            "poll_after_ms": {"type": "integer"},
            "parent_task_id": {"type": "string"},
        },
    }


def _status_success_schema() -> dict[str, Any]:
    """Schema of the folded ``bernstein_status`` body."""
    count_field = {"type": "integer"}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["live", "counts", "cost"],
        "properties": {
            "live": {"type": "boolean"},
            "counts": {
                "type": "object",
                "additionalProperties": False,
                "required": ["total", "open", "claimed", "done", "failed"],
                "properties": {
                    "total": dict(count_field),
                    "open": dict(count_field),
                    "claimed": dict(count_field),
                    "done": dict(count_field),
                    "failed": dict(count_field),
                },
            },
            "cost": {
                "type": "object",
                "additionalProperties": False,
                "required": ["total_cost_usd", "per_role"],
                "properties": {
                    "total_cost_usd": {"type": "number"},
                    "per_role": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["role", "cost_usd"],
                            "properties": {
                                "role": {"type": "string"},
                                "cost_usd": {"type": "number"},
                            },
                        },
                    },
                },
            },
            "per_role": {"type": "array", "items": {"type": "object"}},
            "status_filter": {"type": "string"},
            "tasks": {"type": "array", "items": {"type": "object"}},
        },
    }


def _status_outage_schema() -> dict[str, Any]:
    """Schema of the ``bernstein_status`` body when the task server is down."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["live", "error", "hint"],
        "properties": {
            "live": {"type": "boolean"},
            "error": {"type": "string"},
            "hint": {"type": "string"},
        },
    }


def _structured_payload_schemas() -> dict[str, dict[str, Any]]:
    """Payload (pre-envelope) output schemas for the structured tools (#3086).

    The three most-polled tools declare an ``outputSchema`` and return
    ``structuredContent``. The run-handle schema is generated from
    :meth:`RunHandle.wire_schema` rather than written by hand, so the
    advertised fields are the fields ``to_wire`` emits.
    """
    from bernstein.core.protocols.mcp.tasks_extension import RunHandle

    return {
        "bernstein_run": {"anyOf": [_run_success_schema(), _TOOL_ERROR_SCHEMA]},
        "bernstein_status": {"anyOf": [_status_success_schema(), _status_outage_schema(), _TOOL_ERROR_SCHEMA]},
        "bernstein_run_status": {"anyOf": [RunHandle.wire_schema(), _TOOL_ERROR_SCHEMA]},
    }


def _output_schema_for(tool_name: str) -> dict[str, Any] | None:
    """Return the ``outputSchema`` to advertise for ``tool_name``, if any.

    Describes the result exactly as emitted for the current meter state:
    the meter envelope (``result`` + ``_meter``) when the cost meter is
    enabled, the bare payload otherwise (#3086).
    """
    from bernstein.mcp.cost_meter import cost_meter_enabled, envelope_schema

    payload_schema = _structured_payload_schemas().get(tool_name)
    if payload_schema is None:
        return None
    if cost_meter_enabled():
        return envelope_schema(payload_schema)
    return payload_schema


def _apply_cost_meter(mcp: FastMCP[None]) -> None:
    """Wrap every registered tool so its response carries a meter envelope.

    Each Bernstein tool returns a JSON string. This rewraps each tool's
    callable so the string is passed through :func:`wrap_envelope`, which
    attaches a per-call ``_meter`` record (latency, cost, trace id, status)
    when the meter is enabled and is a no-op otherwise. Wrapping centrally
    here keeps every tool handler free of envelope plumbing and guarantees a
    uniform shape across the stdio, SSE, and skill/scenario tools.

    Tools listed in :func:`_structured_payload_schemas` additionally return
    a ``CallToolResult`` whose ``structuredContent`` is the parsed envelope,
    while the text content block carries the identical string it always did
    (#3086): a structured client validates typed fields, a text client sees
    no change.

    Args:
        mcp: The FastMCP server whose tools should be metered.
    """
    import functools

    structured = frozenset(_structured_payload_schemas())

    # FastMCP exposes no public per-tool rewrap hook, so wrap each tool's
    # callable directly via the tool manager's registry (same access pattern
    # as _apply_tool_tier above).
    for tool in mcp._tool_manager._tools.values():  # pyright: ignore[reportPrivateUsage]
        original = tool.fn
        tool_name = tool.name
        is_structured = tool_name in structured

        @functools.wraps(original)
        async def metered(
            *args: Any,
            __orig: Any = original,
            __name: str = tool_name,
            __structured: bool = is_structured,
            **kwargs: Any,
        ) -> Any:
            with measure_call(__name) as meter:
                payload = await __orig(*args, **kwargs)
            if not isinstance(payload, str):
                return payload
            wrapped = wrap_envelope(payload, meter)
            if __structured:
                try:
                    parsed: Any = json.loads(wrapped)
                except (json.JSONDecodeError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    return CallToolResult(
                        content=[TextContent(type="text", text=wrapped)],
                        structuredContent=parsed,
                    )
            return wrapped

        tool.fn = metered


def _apply_advertised_schemas(mcp: FastMCP[None]) -> None:
    """Advertise each tool's enforced schema as its ``inputSchema``.

    FastMCP derives the advertised schema from the Python signature, which
    carries none of the constraints the input firewall enforces: a caller is
    shown ``scope: string`` while ``validate_tool_call`` requires one of
    ``small`` / ``medium`` / ``large``. The caller sends a plausible value and
    gets a rejection it had no way to predict. Replacing the derived schema
    with the schema from ``tool_schemas/<tool>.json`` gives a tool one schema
    instead of two, so a constrained argument can be filled correctly on the
    first call.

    Only the advertised copy is replaced. Argument coercion still runs through
    FastMCP's signature-derived model, and enforcement still runs through
    ``validate_tool_call`` inside each handler.

    Args:
        mcp: The FastMCP server whose tools should advertise their schemas.
    """
    registry = get_registry()
    # FastMCP exposes no public per-tool schema override, so patch the tool
    # manager's registry after registration (same access pattern as
    # _apply_tool_tier).
    for name, tool in mcp._tool_manager._tools.items():  # pyright: ignore[reportPrivateUsage]
        schema = registry.get(name)
        if schema is None:
            # Deny-by-default means such a tool is registered but not callable.
            # Leave the derived schema alone and make the mismatch visible.
            logger.warning("MCP tool %s has no schema file; advertising the derived schema", name)
            continue
        # Deep-copy so a client-side mutation of the advertised schema cannot
        # reach the process-wide registry the validator reads.
        tool.parameters = copy.deepcopy(schema)


def _register_deprecated_aliases(mcp: FastMCP[None], server_url: str) -> None:
    """Register the deprecated tool-name aliases (#3087).

    Called after the tier filter, so an alias is registered only when its
    replacement survived the filter: an alias never widens the surface. Each
    alias keeps its historical argument shape and schema file, answers with
    its historical payload under ``result``, and names its replacement and
    the removal release in the payload itself. Aliases are hidden from the
    ``tools/list`` response by :func:`_shape_tools_list` and the whole set is
    gated by ``BERNSTEIN_MCP_DEPRECATED_ALIASES``.

    The ``verify_chain`` alias is registered by the lineage registrar, since
    it needs the lineage store; every other alias lands here.
    """
    from bernstein.core.protocols.mcp.tool_tiers import (
        DEPRECATED_TOOL_ALIASES,
        deprecated_aliases_enabled,
    )

    if not deprecated_aliases_enabled():
        return

    registered = set(mcp._tool_manager._tools)  # pyright: ignore[reportPrivateUsage]

    def _wants(old_name: str) -> bool:
        return DEPRECATED_TOOL_ALIASES[old_name] in registered

    if _wants("bernstein_health"):

        async def bernstein_health() -> str:
            """Deprecated: call bernstein_status, whose response is the liveness signal."""
            return _deprecated_alias_payload("bernstein_health", json.dumps({"status": "ok"}))

        mcp.tool(name="bernstein_health")(bernstein_health)

    if _wants("bernstein_tasks"):

        async def bernstein_tasks(status: str | None = None) -> str:
            """Deprecated: call bernstein_status with the status filter instead."""
            err = _validate_or_error("bernstein_tasks", {"status": status})
            if err is not None:
                return _validation_error_response(err)
            return _deprecated_alias_payload("bernstein_tasks", await _tasks_alias_impl(server_url, status))

        mcp.tool(name="bernstein_tasks")(bernstein_tasks)

    if _wants("bernstein_cost"):

        async def bernstein_cost() -> str:
            """Deprecated: call bernstein_status, whose response carries the cost fields."""
            return _deprecated_alias_payload("bernstein_cost", await _cost_alias_impl(server_url))

        mcp.tool(name="bernstein_cost")(bernstein_cost)

    if _wants("bernstein_create_subtask"):

        async def bernstein_create_subtask(
            parent_task_id: str,
            goal: str,
            role: str = "auto",
            priority: int = 2,
            scope: str = "medium",
            complexity: str = "medium",
            estimated_minutes: int | None = None,
        ) -> str:
            """Deprecated: call bernstein_run with parent_task_id instead."""
            err = _validate_or_error(
                "bernstein_create_subtask",
                {
                    "parent_task_id": parent_task_id,
                    "goal": goal,
                    "role": role,
                    "priority": priority,
                    "scope": scope,
                    "complexity": complexity,
                    "estimated_minutes": estimated_minutes,
                },
            )
            if err is not None:
                return _validation_error_response(err)
            payload = await _run_impl(
                server_url,
                goal=goal,
                role=role,
                priority=priority,
                scope=scope,
                complexity=complexity,
                estimated_minutes=estimated_minutes if estimated_minutes is not None else 30,
                parent_task_id=parent_task_id,
            )
            assert isinstance(payload, str)  # no ctx, so never a CreateTaskResult
            return _deprecated_alias_payload("bernstein_create_subtask", payload)

        mcp.tool(name="bernstein_create_subtask")(bernstein_create_subtask)

    if _wants("bernstein_task_handle"):

        async def bernstein_task_handle(run_id: str, workdir: str = ".") -> str:
            """Deprecated: call bernstein_run_status instead (same arguments)."""
            err = _validate_or_error("bernstein_task_handle", {"run_id": run_id, "workdir": workdir})
            if err is not None:
                return _validation_error_response(err)
            return _deprecated_alias_payload("bernstein_task_handle", await _run_status_impl(run_id, workdir))

        mcp.tool(name="bernstein_task_handle")(bernstein_task_handle)

    if _wants("bernstein_update"):

        async def bernstein_update(
            task_id: str,
            body: str,
            sender: str,
            kind: str = "finding",
            sender_card_fingerprint: str | None = None,
        ) -> str:
            """Deprecated: call bernstein_post_message instead (same arguments)."""
            err = _validate_or_error(
                "bernstein_update",
                {
                    "task_id": task_id,
                    "body": body,
                    "sender": sender,
                    "kind": kind,
                    "sender_card_fingerprint": sender_card_fingerprint,
                },
            )
            if err is not None:
                return _validation_error_response(err)
            return _deprecated_alias_payload(
                "bernstein_update",
                await _post_message_impl(
                    server_url,
                    task_id=task_id,
                    body=body,
                    sender=sender,
                    kind=kind,
                    sender_card_fingerprint=sender_card_fingerprint,
                ),
            )

        mcp.tool(name="bernstein_update")(bernstein_update)

    if _wants("bernstein_context"):

        async def bernstein_context(task_id: str, workdir: str = ".", verify: bool = False) -> str:
            """Deprecated: call bernstein_task_capsule instead (same arguments)."""
            err = _validate_or_error("bernstein_context", {"task_id": task_id, "workdir": workdir, "verify": verify})
            if err is not None:
                return _validation_error_response(err)
            return _deprecated_alias_payload("bernstein_context", await _task_capsule_impl(task_id, workdir, verify))

        mcp.tool(name="bernstein_context")(bernstein_context)

    if _wants("bernstein_stop"):

        async def bernstein_stop(workdir: str = ".") -> str:
            """Deprecated: call bernstein_shutdown_orchestrator (whole-orchestrator
            shutdown) or bernstein_cancel (one run) instead."""
            err = _validate_or_error("bernstein_stop", {"workdir": workdir})
            if err is not None:
                return _validation_error_response(err)
            return _deprecated_alias_payload("bernstein_stop", _shutdown_impl(workdir))

        mcp.tool(name="bernstein_stop")(bernstein_stop)

    if _wants("bernstein_scenarios"):

        async def bernstein_scenarios() -> str:
            """Deprecated: call bernstein_scenario with action="list" instead."""
            from bernstein.mcp.routine_tools import list_scenarios

            try:
                payload = json.dumps(list_scenarios(), indent=2)
            except Exception as exc:
                payload = _error_response(exc)
            return _deprecated_alias_payload("bernstein_scenarios", payload)

        mcp.tool(name="bernstein_scenarios")(bernstein_scenarios)

    if _wants("bernstein_scenario_status"):

        async def bernstein_scenario_status(orchestration_id: str) -> str:
            """Deprecated: call bernstein_scenario with action="status" instead."""
            from bernstein.mcp.routine_tools import fetch_scenario_status

            err = _validate_or_error("bernstein_scenario_status", {"orchestration_id": orchestration_id})
            if err is not None:
                return _validation_error_response(err)
            try:
                result = await fetch_scenario_status(orchestration_id, server_url=server_url)
                payload = json.dumps(result, indent=2)
            except Exception as exc:
                payload = _error_response(exc)
            return _deprecated_alias_payload("bernstein_scenario_status", payload)

        mcp.tool(name="bernstein_scenario_status")(bernstein_scenario_status)


def _apply_tool_tier(mcp: FastMCP[None], active_tier: ToolTier) -> None:
    """Drop every registered tool that is outside ``active_tier``.

    Tools are registered unconditionally above, then filtered here so the
    tier annotation stays a property of the registration (see
    :data:`bernstein.core.protocols.mcp.tool_tiers.TOOL_TIERS`) rather than
    a branch in each registrar. A dropped tool is neither advertised in the
    ``tools/list`` response nor callable.

    Args:
        mcp: The FastMCP server whose tools should be filtered.
        active_tier: The currently selected tier.
    """
    # FastMCP exposes no public per-tool filter, so drop out-of-tier tools
    # directly from the tool manager's registry after registration.
    out_of_tier = [name for name in list(mcp._tool_manager._tools) if not tool_in_tier(name, active_tier)]
    for name in out_of_tier:
        mcp._tool_manager._tools.pop(name, None)


#: The ``execution.taskSupport`` mode each tool advertises in ``tools/list``.
#:
#: A tool that is absent from this map advertises nothing, which the Tasks
#: extension reads as ``forbidden``. Only ``bernstein_run`` answers a
#: task-augmented call with a ``CreateTaskResult``, so it is the only entry
#: declared ``optional``. ``bernstein_run_status`` is the stateless polling
#: fallback: it always answers immediately and never returns a task handle,
#: so it declares ``forbidden`` explicitly rather than leaving a caller to
#: infer the default.
_TOOL_TASK_SUPPORT: dict[str, TaskExecutionMode] = {
    "bernstein_run": "optional",
    "bernstein_run_status": "forbidden",
}


def _shape_tools_list(mcp: FastMCP[None]) -> None:
    """Re-register the ``tools/list`` handler with the advertised shape.

    Three concerns share the one wrapper because they all act on the entries
    FastMCP already built:

    * deprecated aliases are dropped - they stay callable for one release
      but are never advertised (#3087);
    * ``execution.taskSupport`` is stamped from :data:`_TOOL_TASK_SUPPORT` -
      the MCP Tasks extension defaults a tool with no ``execution`` hint to
      ``forbidden``, so without it no client ever sends a task-augmented
      call;
    * ``outputSchema`` is stamped for the tools that return structured
      content, describing the envelope exactly as it is emitted for the
      current cost-meter state (#3086).

    FastMCP 1.28.1 carries none of these fields at registration time
    (``@mcp.tool()`` takes no such arguments), so the low-level handler is
    wrapped here. When an SDK release adds first-class support the migration
    is deleting this function and moving the declarations into the
    registrations.

    Args:
        mcp: The FastMCP server whose ``tools/list`` response is shaped.
    """
    from bernstein.core.protocols.mcp.tool_tiers import DEPRECATED_TOOL_ALIASES

    fastmcp_list_tools = mcp.list_tools

    async def shaped_list_tools() -> list[MCPTool]:
        tools = [tool for tool in await fastmcp_list_tools() if tool.name not in DEPRECATED_TOOL_ALIASES]
        for tool in tools:
            mode = _TOOL_TASK_SUPPORT.get(tool.name)
            if mode is not None:
                tool.execution = ToolExecution(taskSupport=mode)
            output_schema = _output_schema_for(tool.name)
            if output_schema is not None:
                tool.outputSchema = output_schema
        return tools

    mcp._mcp_server.list_tools()(shaped_list_tools)


# MCP ``tasks/list`` is a paginated request whose only client-supplied knob is
# an opaque cursor. We page the task server in fixed windows and encode the next
# offset into the cursor, so a client can walk past the legacy 500-item cap the
# server applies to unpaginated GET /tasks calls.
_LIST_TASKS_PAGE_SIZE = 100

_CURSOR_PREFIX = "offset="


def _encode_task_cursor(offset: int) -> str:
    """Encode a pagination offset as an opaque, deterministic cursor token."""
    return base64.urlsafe_b64encode(f"{_CURSOR_PREFIX}{offset}".encode()).decode()


def _decode_task_cursor(cursor: str | None) -> int:
    """Decode a cursor produced by :func:`_encode_task_cursor` back to an offset.

    A missing cursor starts at offset 0. A malformed cursor is a client error
    (the token did not originate from this server) and raises ``ValueError``.
    """
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        if raw.startswith(_CURSOR_PREFIX):
            return max(0, int(raw[len(_CURSOR_PREFIX) :]))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        pass
    raise ValueError(f"Invalid pagination cursor: {cursor!r}")


def _register_tasks_extension(mcp: FastMCP[None], server_url: str) -> None:
    """Register custom experimental handlers for the MCP Tasks extension."""
    import httpx

    @mcp._mcp_server.experimental.get_task()
    async def get_task(req: GetTaskRequest) -> GetTaskResult:
        parts = req.params.taskId.split(":", 1)
        task_id = parts[0]
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{server_url}/tasks/{task_id}", headers=_auth_headers())
            resp.raise_for_status()
            data = resp.json()
        task_obj = _project_task_helper(data)
        return GetTaskResult(
            taskId=task_obj.taskId,
            status=task_obj.status,
            statusMessage=task_obj.statusMessage,
            createdAt=task_obj.createdAt,
            lastUpdatedAt=task_obj.lastUpdatedAt,
            ttl=_TASK_TTL_MS,
            pollInterval=5000,
        )

    @mcp._mcp_server.experimental.get_task_result()
    async def get_task_result(req: GetTaskPayloadRequest) -> CallToolResult:
        parts = req.params.taskId.split(":", 1)
        task_id = parts[0]
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{server_url}/tasks/{task_id}", headers=_auth_headers())
            resp.raise_for_status()
            data = resp.json()
        status = data.get("status")
        error_states = {"failed", "refused", "abandoned", "orphaned", "blocked_by_abandon"}
        terminal_states = error_states | {"done", "closed", "cancelled"}
        # Only a terminal task has a final result. Calling getTaskResult on an
        # in-flight task must not fabricate a "Task completed" success.
        if status not in terminal_states:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Result not available; task still {status}")],
                isError=True,
            )
        is_error = status in error_states
        result_summary = data.get("result_summary") or ""
        if not result_summary:
            result_summary = "Task failed" if is_error else "Task completed"
        return CallToolResult(
            content=[TextContent(type="text", text=result_summary)],
            isError=is_error,
        )

    @mcp._mcp_server.experimental.list_tasks()
    async def list_tasks(req: ListTasksRequest) -> ListTasksResult:
        # Translate the opaque cursor into an offset and always send explicit
        # limit/offset, so the server returns the paginated envelope instead of
        # the legacy flat list hard-capped at 500 (which strands the tail for
        # any operator with more than 500 tasks).
        offset = _decode_task_cursor(req.params.cursor if req.params else None)
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{server_url}/tasks",
                params={"limit": _LIST_TASKS_PAGE_SIZE, "offset": offset},
                headers=_auth_headers(),
            )
            resp.raise_for_status()
            envelope = resp.json()
        # Envelope shape: {tasks, total, limit, offset}. Compute nextCursor from
        # the offset/limit the server actually applied (it clamps both), so the
        # walk terminates exactly when the last page has been served.
        tasks_data = envelope["tasks"]
        total = envelope["total"]
        next_offset = envelope["offset"] + envelope["limit"]
        mcp_tasks = [_project_task_helper(t) for t in tasks_data]
        next_cursor = _encode_task_cursor(next_offset) if next_offset < total else None
        return ListTasksResult(tasks=mcp_tasks, nextCursor=next_cursor)

    @mcp._mcp_server.experimental.cancel_task()
    async def cancel_task(req: CancelTaskRequest) -> CancelTaskResult:
        parts = req.params.taskId.split(":", 1)
        task_id = parts[0]
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(f"{server_url}/tasks/{task_id}/cancel", headers=_auth_headers())
            resp.raise_for_status()
            data = resp.json()
        task_obj = _project_task_helper(data)
        return CancelTaskResult(
            taskId=task_obj.taskId,
            status=task_obj.status,
            statusMessage=task_obj.statusMessage,
            createdAt=task_obj.createdAt,
            lastUpdatedAt=task_obj.lastUpdatedAt,
            ttl=_TASK_TTL_MS,
            pollInterval=5000,
        )


def create_mcp_server(
    server_url: str = _DEFAULT_SERVER_URL,
    name: str = "bernstein",
    *,
    lineage_enabled: bool = False,
    lineage_root: Path | None = None,
    tier: str | None = None,
) -> FastMCP[None]:
    """Build and return the Bernstein FastMCP server instance.

    Args:
        server_url: Base URL of the Bernstein task server.
        name: MCP server name advertised to clients.
        lineage_enabled: When ``True``, register the lineage resources +
            ``verify_chain`` tool. Defaults to ``False`` - callers running
            stdio should pass ``True`` explicitly (``run_stdio`` does).
        lineage_root: Override the lineage store path. Defaults to
            ``<cwd>/.sdd/lineage``.
        tier: Optional explicit tool tier (``core`` / ``standard`` / ``all``)
            overriding the ``BERNSTEIN_MCP_TOOL_TIER`` env var. When ``None``
            the env var is consulted, falling back to the ``standard``
            default. Out-of-tier tools are not advertised and not callable.

    Returns:
        Configured FastMCP instance with the active tier's tools registered.
    """
    from bernstein.mcp.capability import register_capability_resource
    from bernstein.mcp.prompts import register_prompt_resources

    active_tier = resolve_active_tier(tier)
    mcp: FastMCP[None] = FastMCP(name, instructions=_SERVER_INSTRUCTIONS)
    mcp._mcp_server.version = _package_version()
    register_capability_resource(mcp)
    register_prompt_resources(mcp)
    _register_query_tools(mcp, server_url)
    _register_action_tools(mcp, server_url)
    _register_run_status_tool(mcp)
    _register_task_capsule_tool(mcp)
    _register_skill_tools(mcp)
    _register_tasks_extension(mcp, server_url)
    # rt-003: scenario <-> Routine bridge tool.
    from bernstein.mcp.routine_tools import register_scenario_tools

    register_scenario_tools(mcp, server_url)

    if lineage_enabled:
        from bernstein.mcp.resources.lineage import register_lineage_resources

        root = lineage_root if lineage_root is not None else Path.cwd() / ".sdd" / "lineage"
        register_lineage_resources(mcp, lineage_root=root, enabled=True)

    _apply_tool_tier(mcp, active_tier)
    # Aliases are registered after the tier filter so one is registered only
    # when its replacement is in tier, and before the schema/list shaping so
    # they validate and meter like any tool while staying unadvertised.
    _register_deprecated_aliases(mcp, server_url)
    _apply_advertised_schemas(mcp)
    _shape_tools_list(mcp)
    _apply_cost_meter(mcp)
    return mcp


def run_stdio(server_url: str = _DEFAULT_SERVER_URL, *, tier: str | None = None) -> None:
    """Start the MCP server in stdio transport mode (for local IDE integration).

    Lineage MCP resources default ON for local stdio (ADR-009 §7.3) and can
    be opted out via ``BERNSTEIN_LINEAGE_MCP_ENABLED=0``.

    Args:
        server_url: Bernstein task server URL.
        tier: Optional explicit tool tier overriding
            ``BERNSTEIN_MCP_TOOL_TIER`` (the ``--mcp-tier`` session flag).
    """
    mcp = create_mcp_server(
        server_url=server_url,
        lineage_enabled=_lineage_mcp_default(default=True),
        tier=tier,
    )
    mcp.run(transport="stdio")


def run_sse(
    server_url: str = _DEFAULT_SERVER_URL,
    host: str = "127.0.0.1",
    port: int = 8053,
    *,
    tier: str | None = None,
) -> None:
    """Start the MCP server in SSE transport mode (for remote/web integration).

    Lineage MCP resources default OFF for SSE (ADR-009 §7.3) - operators
    that explicitly want to expose them remotely can set
    ``BERNSTEIN_LINEAGE_MCP_ENABLED=1``.

    Args:
        server_url: Bernstein task server URL.
        host: Host to bind the SSE server to.
        port: Port to bind the SSE server to.
        tier: Optional explicit tool tier overriding
            ``BERNSTEIN_MCP_TOOL_TIER`` (the ``--mcp-tier`` session flag).
    """
    mcp = create_mcp_server(
        server_url=server_url,
        lineage_enabled=_lineage_mcp_default(default=False),
        tier=tier,
    )
    import uvicorn

    uvicorn.run(mcp.sse_app(), host=host, port=port)
