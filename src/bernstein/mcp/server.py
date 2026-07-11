"""Bernstein MCP server.

Exposes Bernstein's orchestration layer as MCP tools so any MCP client
(Cursor, Claude Code, Cline, Windsurf, …) can drive multi-agent work
through Bernstein.

Transport:
    stdio  - for local IDE integration (default ``bernstein mcp``)
    sse    - for remote/web integration (``bernstein mcp --transport sse``)

Tools:
    bernstein_run     - start an orchestration run with a goal
    bernstein_status  - get task counts summary
    bernstein_tasks   - list tasks with optional status filter
    bernstein_task_handle - verifiable Tasks-extension run handle (poll a run)
    bernstein_cost    - get cost summary across all roles
    bernstein_stop    - graceful shutdown (writes SHUTDOWN signal)
    bernstein_approve - approve a pending/blocked task
    bernstein_health  - liveness check (always succeeds)
    load_skill        - load a skill pack body / reference / script (oai-004)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from bernstein.core.protocols.mcp.tool_tiers import (
    ToolTier,
    resolve_active_tier,
    tool_in_tier,
)
from bernstein.mcp.cost_meter import measure_call, wrap_envelope
from bernstein.mcp.input_validation import (
    ValidatedPayload,
    ValidationError,
    to_jsonrpc_error,
    validate_tool_call,
)

_DEFAULT_SERVER_URL = "http://127.0.0.1:8052"

# Timeout for all httpx calls to the task server (seconds).
_HTTP_TIMEOUT = 5.0

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


def _validation_error_response(err: ValidationError) -> str:
    """Render a validation failure as the JSON string FastMCP tools return.

    Carries the full structured error so MCP clients can show users which
    field failed and why, without leaking server internals.
    """
    payload = {"error": err.message, "jsonrpc_error": to_jsonrpc_error(err)}
    return json.dumps(payload)


def _validate_or_error(tool_name: str, params: dict[str, Any]) -> ValidationError | None:
    """Validate ``params`` against ``tool_name``'s schema.

    Returns ``None`` when the call is allowed, or a ``ValidationError`` the
    caller should render via :func:`_validation_error_response`. Stripping
    ``None`` values from the params dict keeps every tool's optional-arg
    convention working; the schema can still mark them as nullable when
    that's the intended contract.
    """
    cleaned = {k: v for k, v in params.items() if v is not None}
    result = validate_tool_call(tool_name, cleaned)
    if isinstance(result, ValidatedPayload):
        return None
    return result


def _register_health_tool(mcp: FastMCP[None]) -> None:
    """Register the ``bernstein_health`` liveness-check tool."""

    @mcp.tool()
    async def bernstein_health(  # pyright: ignore[reportUnusedFunction]
    ) -> str:
        """Liveness check - always succeeds if the MCP server is running.

        Use this to verify the Bernstein MCP connection is still alive.

        Returns:
            JSON with status "ok".
        """
        return json.dumps({"status": "ok"})


def _register_query_tools(mcp: FastMCP[None], server_url: str) -> None:
    """Register read-only query tools: run, status, tasks, cost."""

    @mcp.tool()
    async def bernstein_run(  # pyright: ignore[reportUnusedFunction]
        goal: str,
        role: str = "backend",
        priority: int = 2,
        scope: str = "medium",
        complexity: str = "medium",
        estimated_minutes: int = 30,
    ) -> str:
        """Start an orchestration run by posting a task to the Bernstein server.

        Args:
            goal: Description of what you want Bernstein to accomplish.
            role: Specialist role to assign (backend, frontend, qa, security, …).
            priority: 1=critical, 2=normal, 3=nice-to-have.
            scope: Task scope - small, medium, or large.
            complexity: Task complexity - low, medium, or high.
            estimated_minutes: Rough time estimate in minutes.

        Returns:
            JSON with the created task ID, title, and status.
        """
        err = _validate_or_error(
            "bernstein_run",
            {
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
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(f"{server_url}/tasks", json=payload, headers=_auth_headers())
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
            return json.dumps(
                {"task_id": data["id"], "title": data["title"], "status": data["status"]},
                indent=2,
            )
        except Exception as exc:
            return _error_response(exc)

    @mcp.tool()
    async def bernstein_status(  # pyright: ignore[reportUnusedFunction]
    ) -> str:
        """Return a summary of all task counts from the Bernstein server.

        Returns:
            JSON with total, open, claimed, done, failed counts plus
            a per-role breakdown.
        """
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(f"{server_url}/status", headers=_auth_headers())
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
            return json.dumps(data, indent=2)
        except Exception as exc:
            return _error_response(exc)

    @mcp.tool()
    async def bernstein_tasks(  # pyright: ignore[reportUnusedFunction]
        status: str | None = None,
    ) -> str:
        """List tasks from the Bernstein server.

        Args:
            status: Optional filter - open, claimed, in_progress, done,
                failed, blocked, or cancelled.

        Returns:
            JSON array of task objects.
        """
        err = _validate_or_error("bernstein_tasks", {"status": status})
        if err is not None:
            return _validation_error_response(err)
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

    @mcp.tool()
    async def bernstein_cost(  # pyright: ignore[reportUnusedFunction]
    ) -> str:
        """Return cost summary (total USD spent and per-role breakdown).

        Returns:
            JSON with total_cost_usd and per-role cost breakdown.
        """
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


def _register_task_handle_tool(mcp: FastMCP[None]) -> None:
    """Register the ``bernstein_task_handle`` Tasks-extension polling tool.

    The tool reprojects a verifiable run handle from the on-disk run journal
    and the audit-chain head, so a stateless MCP client can poll a run it
    started and retrieve results without holding a session (issue #2364).
    """

    @mcp.tool()
    async def bernstein_task_handle(  # pyright: ignore[reportUnusedFunction]
        run_id: str,
        workdir: str = ".",
    ) -> str:
        """Return a verifiable Tasks-extension handle for a run, by run id.

        The handle's status is a pure projection of the run journal, and it
        embeds the run's audit-chain head so the client can later verify the
        task it watched corresponds to the audited run (``bernstein audit
        verify`` or the offline verifier). Polling is stateless: any server
        instance reprojects the same handle from the on-disk journal.

        Args:
            run_id: The run identifier whose journal to project. Must be a
                plain identifier - path separators and traversal are refused.
            workdir: Project root directory (default: current directory).

        Returns:
            JSON of the Tasks-extension task-handle body (``taskId``,
            ``runId``, ``status``, ``journalHead``, ``chainHead``,
            ``receiptHash``, ``pollToken``, ...).
        """
        err = _validate_or_error("bernstein_task_handle", {"run_id": run_id, "workdir": workdir})
        if err is not None:
            return _validation_error_response(err)
        try:
            from bernstein.core.protocols.mcp.tasks_extension import RunHandle
            from bernstein.core.replay.journal import JOURNAL_FILENAME, load_events

            base = Path(workdir).resolve()
            runs_root = (base / ".sdd" / "runs").resolve()
            journal_path = (runs_root / run_id / JOURNAL_FILENAME).resolve()
            # Realpath-containment check (CodeQL): a crafted run_id must not
            # escape the runs root via ``..`` or an absolute path. A plain run
            # id resolves to ``<runs_root>/<run_id>/journal.jsonl``; anything
            # else is refused.
            if journal_path.parent.parent != runs_root:
                return _error_response(
                    ValueError("run_id escapes the runs directory"),
                    hint="run_id must be a plain run identifier",
                )
            events = load_events(journal_path)
            chain_head = _read_audit_chain_head(base / ".sdd" / "audit")
            handle = RunHandle.from_journal(
                task_id=run_id,
                run_id=run_id,
                events=events,
                chain_head=chain_head,
            )
            return json.dumps(handle.to_wire(), indent=2)
        except Exception as exc:
            return _error_response(exc, hint="Run journal not found")


def _register_action_tools(mcp: FastMCP[None], server_url: str) -> None:
    """Register mutation tools: stop, approve, create_subtask."""

    @mcp.tool()
    async def bernstein_stop(  # pyright: ignore[reportUnusedFunction]
        workdir: str = ".",
    ) -> str:
        """Request a graceful Bernstein shutdown by writing a SHUTDOWN signal.

        Writes ``.sdd/runtime/signals/SHUTDOWN`` in the project directory,
        which the orchestrator detects and shuts down gracefully.

        Args:
            workdir: Project root directory (default: current directory).

        Returns:
            Confirmation message.
        """
        err = _validate_or_error("bernstein_stop", {"workdir": workdir})
        if err is not None:
            return _validation_error_response(err)
        try:
            signals_dir = Path(workdir) / ".sdd" / "runtime" / "signals"
            signals_dir.mkdir(parents=True, exist_ok=True)
            shutdown_file = signals_dir / "SHUTDOWN"
            shutdown_file.write_text("mcp-stop\n", encoding="utf-8")
            return json.dumps({"status": "shutdown signal sent", "path": str(shutdown_file)})
        except Exception as exc:
            return _error_response(exc, hint="Could not write shutdown signal")

    @mcp.tool()
    async def bernstein_approve(  # pyright: ignore[reportUnusedFunction]
        task_id: str,
        note: str = "Approved via MCP",
    ) -> str:
        """Approve a pending or blocked task, marking it complete.

        This is used for approval gates - when a task is awaiting human
        sign-off before proceeding.

        Args:
            task_id: ID of the task to approve.
            note: Optional approval note recorded as the result summary.

        Returns:
            JSON with the updated task status.
        """
        err = _validate_or_error("bernstein_approve", {"task_id": task_id, "note": note})
        if err is not None:
            return _validation_error_response(err)
        try:
            payload: dict[str, Any] = {"result_summary": note}
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(
                    f"{server_url}/tasks/{task_id}/complete",
                    json=payload,
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

    @mcp.tool()
    async def bernstein_create_subtask(  # pyright: ignore[reportUnusedFunction]
        parent_task_id: str,
        goal: str,
        role: str = "auto",
        priority: int = 2,
        scope: str = "medium",
        complexity: str = "medium",
        estimated_minutes: int | None = None,
    ) -> str:
        """Create a subtask linked to a parent task.

        Agents call this to decompose their current work into subtasks
        during execution.  The parent task is automatically transitioned
        to WAITING_FOR_SUBTASKS status.

        Args:
            parent_task_id: ID of the parent task that this subtask belongs to.
            goal: Description of what the subtask should accomplish.
            role: Specialist role to assign (backend, frontend, qa, …).
            priority: 1=critical, 2=normal, 3=nice-to-have.
            scope: Task scope - small, medium, or large.
            complexity: Task complexity - low, medium, or high.
            estimated_minutes: Rough time estimate in minutes.

        Returns:
            JSON with the created subtask ID, parent_task_id, title, and status.
        """
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
        try:
            payload: dict[str, Any] = {
                "parent_task_id": parent_task_id,
                "title": goal[:120],
                "description": goal,
                "role": role,
                "priority": priority,
                "scope": scope,
                "complexity": complexity,
            }
            if estimated_minutes is not None:
                payload["estimated_minutes"] = estimated_minutes
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(
                    f"{server_url}/tasks/self-create",
                    json=payload,
                    headers=_auth_headers(),
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
            return json.dumps(
                {
                    "task_id": data["id"],
                    "parent_task_id": data.get("parent_task_id", parent_task_id),
                    "title": data["title"],
                    "status": data["status"],
                },
                indent=2,
            )
        except Exception as exc:
            return _error_response(exc)


def _register_skill_tools(mcp: FastMCP[None]) -> None:
    """Register the ``load_skill`` progressive-disclosure tool (oai-004).

    Args:
        mcp: FastMCP instance to register the tool on.
    """

    @mcp.tool()
    async def load_skill(  # pyright: ignore[reportUnusedFunction]
        name: str,
        reference: str | None = None,
        script: str | None = None,
    ) -> str:
        """Load a skill pack body (and optionally a reference or script).

        Agents receive only a compact skill index in their system prompt.
        Call this tool to fetch the full ``SKILL.md`` body for a skill
        when you decide it's relevant to the current task. Pass
        ``reference`` to get a deeper-context file or ``script`` to read
        the content of an executable helper.

        Args:
            name: Skill name (matches the index entry, e.g. ``"backend"``).
            reference: Optional filename under ``references/`` - for
                example ``"python-conventions.md"``.
            script: Optional filename under ``scripts/`` - for example
                ``"lint.sh"``. The script content is returned as text; the
                MCP harness does not execute it.

        Returns:
            JSON with ``name``, ``body``, ``available_references``,
            ``available_scripts``, and the optional fetched content.
        """
        err = _validate_or_error(
            "load_skill",
            {"name": name, "reference": reference, "script": script},
        )
        if err is not None:
            return _validation_error_response(err)
        try:
            # Local import so the MCP module stays cheap to import even when
            # the skills tree is missing (e.g. dev CLI without templates).
            from pathlib import Path as _Path

            from bernstein import get_templates_dir
            from bernstein.core.skills.load_skill_tool import (
                load_skill as _load_skill_impl,
            )
            from bernstein.core.skills.load_skill_tool import (
                result_as_dict,
            )

            templates_root = get_templates_dir(_Path.cwd())
            templates_roles_dir = templates_root / "roles"
            result = _load_skill_impl(
                name=name,
                reference=reference,
                script=script,
                templates_roles_dir=templates_roles_dir,
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


def _apply_cost_meter(mcp: FastMCP[None]) -> None:
    """Wrap every registered tool so its response carries a meter envelope.

    Each Bernstein tool returns a JSON string. This rewraps each tool's
    callable so the string is passed through :func:`wrap_envelope`, which
    attaches a per-call ``_meter`` record (latency, cost, trace id, status)
    when the meter is enabled and is a no-op otherwise. Wrapping centrally
    here keeps every tool handler free of envelope plumbing and guarantees a
    uniform shape across the stdio, SSE, and skill/scenario tools.

    Args:
        mcp: The FastMCP server whose tools should be metered.
    """
    import functools

    # FastMCP exposes no public per-tool rewrap hook, so wrap each tool's
    # callable directly via the tool manager's registry (same access pattern
    # as _apply_tool_tier above).
    for tool in mcp._tool_manager._tools.values():  # pyright: ignore[reportPrivateUsage]
        original = tool.fn
        tool_name = tool.name

        @functools.wraps(original)
        async def metered(*args: Any, __orig: Any = original, __name: str = tool_name, **kwargs: Any) -> str:
            with measure_call(__name) as meter:
                payload = await __orig(*args, **kwargs)
            return wrap_envelope(payload, meter)

        tool.fn = metered


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
    mcp: FastMCP[None] = FastMCP(name)
    register_capability_resource(mcp)
    register_prompt_resources(mcp)
    _register_health_tool(mcp)
    _register_query_tools(mcp, server_url)
    _register_action_tools(mcp, server_url)
    _register_task_handle_tool(mcp)
    _register_skill_tools(mcp)
    # rt-003: scenario <-> Routine bridge tools.
    from bernstein.mcp.routine_tools import register_scenario_tools

    register_scenario_tools(mcp, server_url)

    if lineage_enabled:
        from bernstein.mcp.resources.lineage import register_lineage_resources

        root = lineage_root if lineage_root is not None else Path.cwd() / ".sdd" / "lineage"
        register_lineage_resources(mcp, lineage_root=root, enabled=True)

    _apply_tool_tier(mcp, active_tier)
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
