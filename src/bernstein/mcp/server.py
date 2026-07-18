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

import base64
import binascii
import json
import logging
import os
from datetime import UTC
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
    TextContent,
)

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

_orig_convert_result = FuncMetadata.convert_result


def _patched_convert_result(self, result: Any) -> Any:
    if isinstance(result, CreateTaskResult):
        return result
    return _orig_convert_result(self, result)


FuncMetadata.convert_result = _patched_convert_result

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
        ttl=None,
        pollInterval=5000,
    )


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
        ctx: Context | None = None,
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

            client_supports_tasks = False
            traceparent = None
            tracestate = None
            baggage = None

            if ctx is not None:
                try:
                    rc = ctx.request_context
                    if rc is not None:
                        if rc.experimental is not None:
                            client_supports_tasks = bool(getattr(rc.experimental, "client_supports_tasks", False))
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
                resp = await client.post(f"{server_url}/tasks", json=payload, headers=headers)
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()

            if client_supports_tasks:
                task_obj = _project_task_helper(data)
                return CreateTaskResult(task=task_obj)

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
            from bernstein.core.replay.journal import (
                JournalPathError,
                load_events,
                run_journal_path,
            )

            base = Path(workdir).resolve()
            # Shared barrier rather than a local containment check, so this
            # surface cannot drift from the rest of the run-journal readers.
            try:
                journal_path = run_journal_path(base / ".sdd", run_id)
            except JournalPathError as exc:
                return _error_response(
                    exc,
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


def _register_context_tool(mcp: FastMCP[None]) -> None:
    """Register the ``bernstein_context`` capsule tool (#2545).

    A spawned worker reads one signed, chain-anchored answer to "what was I
    given" -- task id, run id, params hash, worktree, role, budget envelope
    remaining, dependency state, and the audit-chain head at spawn -- instead of
    piecing it together from scattered env vars. The tool is served through the
    same deny-by-default input firewall as every other MCP tool.
    """

    @mcp.tool()
    async def bernstein_context(  # pyright: ignore[reportUnusedFunction]
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
        err = _validate_or_error("bernstein_context", {"task_id": task_id, "workdir": workdir, "verify": verify})
        if err is not None:
            return _validation_error_response(err)
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


def _register_action_tools(mcp: FastMCP[None], server_url: str) -> None:
    """Register mutation tools: stop, approve, create_subtask, claim, update."""

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
    async def bernstein_update(  # pyright: ignore[reportUnusedFunction]
        task_id: str,
        body: str,
        sender: str,
        kind: str = "finding",
        sender_card_fingerprint: str | None = None,
    ) -> str:
        """Post an incremental progress update as a signed journal entry.

        Wraps the worker mailbox: the update is DLP-redacted, HMAC-chained
        onto the mailbox journal, Ed25519-signed, and mirrored to the audit
        chain (``task.mailbox_message``) before returning. The result IS the
        signed journal entry - a worker holds a progress record it can verify
        offline against the same chain ``bernstein audit verify`` walks, not a
        bare status string.

        Args:
            task_id: The task the update is addressed to.
            body: The progress message body (<= 4096 bytes).
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
        err = _validate_or_error(
            "bernstein_post_artifact",
            {
                "task_id": task_id,
                "key": key,
                "artifact_type": artifact_type,
                "poster": poster,
                "body": body,
                "columns": columns,
                "rows": rows,
                "url": url,
                "link_kind": link_kind,
            },
        )
        if err is not None:
            return _validation_error_response(err)
        try:
            payload: dict[str, Any] = {
                "key": key,
                "artifact_type": artifact_type,
                "poster": poster,
            }
            if body:
                payload["body"] = body
            if columns is not None:
                payload["columns"] = columns
            if rows is not None:
                payload["rows"] = rows
            if url:
                payload["url"] = url
            if link_kind:
                payload["link_kind"] = link_kind
            # Carry the caller identity in the request header (the server's
            # authorization principal), not only in the body. The server refuses
            # posts against a task this identity does not hold the claim for.
            headers = {**_auth_headers(), "x-bernstein-agent-id": poster}
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
        async def metered(*args: Any, __orig: Any = original, __name: str = tool_name, **kwargs: Any) -> Any:
            with measure_call(__name) as meter:
                payload = await __orig(*args, **kwargs)
            if not isinstance(payload, str):
                return payload
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
            ttl=None,
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
            ttl=None,
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
    mcp: FastMCP[None] = FastMCP(name)
    register_capability_resource(mcp)
    register_prompt_resources(mcp)
    _register_health_tool(mcp)
    _register_query_tools(mcp, server_url)
    _register_action_tools(mcp, server_url)
    _register_task_handle_tool(mcp)
    _register_context_tool(mcp)
    _register_skill_tools(mcp)
    _register_tasks_extension(mcp, server_url)
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
