"""Task CRUD routes, agent heartbeats, bulletin board, and direct channel."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from bernstein.core.bulletin import BulletinBoard, BulletinMessage, DirectChannel
from bernstein.core.difficulty_estimator import estimate_difficulty, minutes_for_level
from bernstein.core.eu_ai_act import (
    TaskRiskAssessment,
    append_assessment_log,
    assess_task,
    build_log_record,
    merge_bernstein_risk,
    merge_eu_ai_act_risk,
)
from bernstein.core.lifecycle import IllegalTransitionError
from bernstein.core.role_classifier import classify_role
from bernstein.core.routes._rate_limit_headers import rate_limit_exception
from bernstein.core.security.sanitize import sanitize_log

# Import Pydantic models from server - this works because server.py's
# __getattr__ defers the `app` creation, so the module body (class defs)
# loads without triggering create_app().
from bernstein.core.server import (
    AgentKillResponse,
    AgentLogsResponse,
    BatchClaimRequest,
    BatchClaimResponse,
    BatchCreateRequest,
    BatchCreateResponse,
    BulletinMessageResponse,
    BulletinPostRequest,
    ChannelQueryRequest,
    ChannelQueryResponse,
    ChannelResponseRequest,
    ChannelResponseResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    PaginatedTasksResponse,
    PartialMergeRequest,
    PartialMergeResponse,
    SSEBus,
    TaskBlockRequest,
    TaskCancelRequest,
    TaskCompleteRequest,
    TaskCountsResponse,
    TaskCreate,
    TaskFailRequest,
    TaskPatchRequest,
    TaskProgressRequest,
    TaskReopenRequest,
    TaskResponse,
    TaskSelfCreate,
    TaskStore,
    TaskWaitForSubtasksRequest,
    read_log_tail,
    task_to_response,
)
from bernstein.core.task_store import ArchiveRecord, EmptyCompletionError, SnapshotEntry
from bernstein.core.tasks.contracts import (
    WORKER_CONTRACT_VERSION as _CONTRACT_VERSION,
)
from bernstein.core.tasks.contracts import (
    ContractViolation,
    RefusalKind,
    WorkerCompletion,
    WorkerRefusal,
    looks_like_contract_payload,
    parse_terminal_payload,
    parse_terminal_payload_text,
)
from bernstein.core.telemetry import start_span
from bernstein.core.tenanting import request_tenant_id, resolve_tenant_scope
from bernstein.plugins.manager import HookBlockingError, get_plugin_manager

logger = logging.getLogger(__name__)

_DRAINING_DETAIL = "Server is draining -- no new claims accepted"

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from bernstein.core.models import Task
    from bernstein.core.tenanting import TenantRegistry

router = APIRouter()

_TENANT_RESPONSES: dict[int | str, dict[str, str]] = {
    403: {"description": "Tenant scope access denied"},
    404: {"description": "Resource not found or tenant mismatch"},
}


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _get_sse_bus(request: Request) -> SSEBus:
    return request.app.state.sse_bus  # type: ignore[no-any-return]


def _get_bulletin(request: Request) -> BulletinBoard:
    return request.app.state.bulletin  # type: ignore[no-any-return]


def _get_direct_channel(request: Request) -> DirectChannel:
    return request.app.state.direct_channel  # type: ignore[no-any-return]


def _get_runtime_dir(request: Request) -> Path:
    return request.app.state.runtime_dir  # type: ignore[no-any-return]


def _get_workdir(request: Request) -> Path:
    """Return the repository root from application state."""
    return request.app.state.workdir  # type: ignore[no-any-return]


def _get_gate_report_path(request: Request, task_id: str) -> Path:
    return _get_runtime_dir(request) / "gates" / f"{task_id}.json"


def _persist_lines_changed(request: Request, agent_id: str, lines_changed: int) -> None:
    """Persist (accumulate) lines_changed for an agent session.

    Written to ``{runtime_dir}/lines_changed/{agent_id}.json`` so the
    ``GET /costs/efficiency`` endpoint can compute cost-per-line metrics.

    Args:
        request: FastAPI request (used to resolve runtime_dir).
        agent_id: Agent session identifier.
        lines_changed: Number of lines changed to add to the running total.
    """
    import logging as _logging

    runtime_dir = _get_runtime_dir(request)
    lc_dir = runtime_dir / "lines_changed"
    try:
        lc_dir.mkdir(parents=True, exist_ok=True)
        path = lc_dir / f"{agent_id}.json"
        current = 0
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                current = int(data.get("lines_changed", 0))
            except ValueError:
                current = 0
        path.write_text(
            json.dumps({"agent_id": agent_id, "lines_changed": current + lines_changed}),
            encoding="utf-8",
        )
    except OSError as exc:
        _logging.getLogger(__name__).debug("Failed to persist lines_changed for %s: %s", agent_id, exc)


def _get_tenant_registry(request: Request) -> TenantRegistry | None:
    registry = getattr(request.app.state, "tenant_registry", None)
    return registry if registry is not None else None


def _resolve_request_tenant_scope(request: Request, requested_tenant: str | None = None) -> str:
    """Resolve the tenant scope for the current request."""

    try:
        return resolve_tenant_scope(
            request_tenant_id(request),
            requested_tenant,
            registry=_get_tenant_registry(request),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _require_task_access(task: Task, request: Request, requested_tenant: str | None = None) -> None:
    """Reject access to a task outside the current tenant scope."""

    effective_tenant = _resolve_request_tenant_scope(request, requested_tenant)
    if task.tenant_id != effective_tenant:
        raise HTTPException(status_code=404, detail=f"Task '{task.id}' not found")


# ---------------------------------------------------------------------------
# Real-time behavior monitor helper
# ---------------------------------------------------------------------------


def _get_realtime_monitor(request: Request) -> object | None:
    """Return the ``RealtimeBehaviorMonitor`` from app state, if present."""
    return getattr(request.app.state, "realtime_behavior_monitor", None)


def _evict_realtime_session(request: Request, session_id: str | None) -> None:
    """Remove session state from the real-time monitor after task completion."""
    if not session_id:
        return
    monitor = _get_realtime_monitor(request)
    if monitor is None:
        return
    with suppress(Exception):
        from bernstein.core.behavior_anomaly import RealtimeBehaviorMonitor

        if isinstance(monitor, RealtimeBehaviorMonitor):
            monitor.evict_session(session_id)


# ---------------------------------------------------------------------------
# Auto-commit pre-/complete hook (defect 33)
# ---------------------------------------------------------------------------
#
# Workers were previously expected to ``git commit`` before POSTing
# /complete (the prompt contract landed in 59fdc178/88611aab).  This hook
# moves the obligation from "remember to commit" to "the route commits for
# you" so a worker that forgets still has its work delivered.  Failures are
# swallowed: the orchestrator's janitor still observes a 0-diff task on
# /complete's branch and can FAIL it, which the bounded-reopen path
# (test_janitor_reopen) handles.  See defect 33 in the scoreboard.

_AUTO_COMMIT_DENY_DIR_PREFIXES: tuple[str, ...] = (
    ".sdd/",
    "attestations/",
    "auth/",
)
_AUTO_COMMIT_DENY_EXACT: tuple[str, ...] = (
    "bernstein.yaml",
    ".claude/mcp.json",
)
_AUTO_COMMIT_DENY_GLOBS: tuple[str, ...] = (".env", ".env.*")


def _is_auto_commit_denied(path: str) -> bool:
    """Return True when *path* matches the auto-commit deny list.

    Rules:
      * Path starts with any prefix in ``_AUTO_COMMIT_DENY_DIR_PREFIXES``
        (matched against forward-slash separators).
      * Path equals any exact entry in ``_AUTO_COMMIT_DENY_EXACT``.
      * Path matches any glob in ``_AUTO_COMMIT_DENY_GLOBS`` (.env,
        .env.<anything>).
    """
    p = path.replace("\\", "/")
    if p in _AUTO_COMMIT_DENY_EXACT:
        return True
    for prefix in _AUTO_COMMIT_DENY_DIR_PREFIXES:
        if p.startswith(prefix):
            return True
    base = os.path.basename(p)
    for glob in _AUTO_COMMIT_DENY_GLOBS:
        if glob.endswith(".*"):
            stem = glob[:-2]
            # Match against the basename as well as the full path so a
            # nested dotenv variant (e.g. ``config/.env.local``) stays
            # denied -- the full-path checks alone only cover repo-root
            # ``.env.*`` files.
            if p == stem or p.startswith(stem + ".") or base == stem or base.startswith(stem + "."):
                return True
        # Exact basename/path match rather than substring containment --
        # substring containment (``glob in p``) would also match unrelated
        # paths that merely contain ".env" somewhere, e.g. ".envrc" or
        # "config.envelope.json", silently excluding legitimate files from
        # auto-commit.
        elif p == glob or base == glob:
            return True
    return False


def _is_salvage_branch(branch_name: str | None) -> bool:
    """Return True when *branch_name* is a salvage/graveyard branch.

    The existing convention (bernstein.core.git.salvage) uses
    ``salvage/<session-id>`` branches.  We also defend against any branch
    containing ``bernstein-salvage`` as a backstop in case future tooling
    changes the prefix.
    """
    if not branch_name:
        return False
    if branch_name.startswith("salvage/"):
        return True
    return "bernstein-salvage" in branch_name


def _run_auto_commit_pre_complete(
    request: Request,
    task: Task,
) -> None:
    """Auto-commit any uncommitted changes in the worker's worktree.

    Runs AFTER the worker reports done via /complete, BEFORE the task
    transitions to done in the store.  Operates only on the worker's
    primary worktree (``.sdd/worktrees/<session-id>``) and on the
    worker's ``agent/<session-id>`` branch.  Salvage / graveyard
    branches are skipped.

    Logging contract (house rule 2 - full logging, never silent):

      * ``auto_commit_pre_complete: task=<id> session=<s> reason=already_committed``
      * ``auto_commit_pre_complete: task=<id> session=<s> reason=skipped_salvage_branch branch=<name>``
      * ``auto_commit_pre_complete: task=<id> session=<s> reason=nothing_to_commit``
      * ``auto_commit_pre_complete: task=<id> session=<s> files=<list> reason=uncommitted_changes_at_complete``
      * ``auto_commit_pre_complete_failed: task=<id> session=<s> error=<msg> files=<list>`` (WARN)

    All errors are swallowed (fail-open) so the orchestrator's lifecycle
    machinery still observes a /complete - see spec step 6.
    """
    session_id = task.claimed_by_session
    if not session_id:
        logger.info(
            "auto_commit_pre_complete: task=%s session=None reason=no_session",
            sanitize_log(str(task.id)),
        )
        return

    workdir = _get_workdir(request)
    worktree_path = workdir / ".sdd" / "worktrees" / session_id

    if not worktree_path.exists() or not worktree_path.is_dir():
        logger.info(
            "auto_commit_pre_complete: task=%s session=%s reason=no_worktree path=%s",
            sanitize_log(str(task.id)),
            sanitize_log(str(session_id)),
            sanitize_log(str(worktree_path)),
        )
        return

    branch_name: str | None = None
    try:
        branch_proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if branch_proc.returncode == 0:
            branch_name = branch_proc.stdout.strip() or None
    except Exception as exc:  # pragma: no cover  # intentional-broad-except: branch lookup is best-effort
        logger.debug(
            "auto_commit_pre_complete: task=%s session=%s branch_lookup_error=%s",
            sanitize_log(str(task.id)),
            sanitize_log(str(session_id)),
            sanitize_log(str(exc)),
        )

    if _is_salvage_branch(branch_name):
        logger.info(
            "auto_commit_pre_complete: task=%s session=%s reason=skipped_salvage_branch branch=%s",
            sanitize_log(str(task.id)),
            sanitize_log(str(session_id)),
            sanitize_log(branch_name or ""),
        )
        return

    try:
        already = subprocess.run(
            ["git", "log", "-50", f"--grep={task.id}", "--fixed-strings"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if already.returncode == 0 and already.stdout.strip():
            logger.info(
                "auto_commit_pre_complete: task=%s session=%s reason=already_committed",
                sanitize_log(str(task.id)),
                sanitize_log(str(session_id)),
            )
            return
    except Exception as exc:  # intentional-broad-except: already-committed check is best-effort
        logger.warning(
            "auto_commit_pre_complete_failed: task=%s session=%s error=%s stage=already_committed_check",
            sanitize_log(str(task.id)),
            sanitize_log(str(session_id)),
            sanitize_log(str(exc)),
        )

    files_to_commit: list[str] = []
    try:
        merge_base_proc = subprocess.run(
            ["git", "merge-base", "HEAD", "origin/main"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if merge_base_proc.returncode == 0 and merge_base_proc.stdout.strip():
            base = merge_base_proc.stdout.strip().splitlines()[0]
            diff_proc = subprocess.run(
                ["git", "diff", "--name-only", f"{base}..HEAD"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if diff_proc.returncode == 0:
                files_to_commit.extend(line.strip() for line in diff_proc.stdout.splitlines() if line.strip())

        status_proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if status_proc.returncode == 0:
            for line in status_proc.stdout.splitlines():
                if len(line) < 4:
                    continue
                path = line[3:].strip()
                if " -> " in path:
                    path = path.split(" -> ", 1)[1].strip()
                if path and not _is_auto_commit_denied(path):
                    files_to_commit.append(path)
    except Exception as exc:  # intentional-broad-except: file enumeration is best-effort
        logger.warning(
            "auto_commit_pre_complete_failed: task=%s session=%s error=%s stage=file_enumeration",
            sanitize_log(str(task.id)),
            sanitize_log(str(session_id)),
            sanitize_log(str(exc)),
        )
        return

    seen: set[str] = set()
    deduped: list[str] = []
    for p in files_to_commit:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    files_to_commit = deduped

    if not files_to_commit:
        logger.info(
            "auto_commit_pre_complete: task=%s session=%s reason=nothing_to_commit",
            sanitize_log(str(task.id)),
            sanitize_log(str(session_id)),
        )
        return

    try:
        commit_set = [p for p in files_to_commit if not _is_auto_commit_denied(p)]
        if not commit_set:
            logger.info(
                "auto_commit_pre_complete: task=%s session=%s reason=nothing_to_commit (deny-list excluded all)",
                sanitize_log(str(task.id)),
                sanitize_log(str(session_id)),
            )
            return

        add_proc = subprocess.run(
            ["git", "add", "--", *commit_set],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if add_proc.returncode != 0:
            logger.warning(
                "auto_commit_pre_complete_failed: task=%s session=%s error=%s files=%s stage=git_add",
                sanitize_log(str(task.id)),
                sanitize_log(str(session_id)),
                sanitize_log((add_proc.stderr or add_proc.stdout or "").strip()[:500]),
                sanitize_log(str(commit_set)),
            )
            return

        commit_message = f"auto: {task.id} pre-/complete"
        commit_proc = subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        if commit_proc.returncode != 0:
            stderr = (commit_proc.stderr or commit_proc.stdout or "").strip()
            if "nothing to commit" in stderr or "no changes added" in stderr:
                logger.info(
                    "auto_commit_pre_complete: task=%s session=%s reason=already_committed (raced-during-stage)",
                    sanitize_log(str(task.id)),
                    sanitize_log(str(session_id)),
                )
                return
            logger.warning(
                "auto_commit_pre_complete_failed: task=%s session=%s error=%s files=%s stage=git_commit",
                sanitize_log(str(task.id)),
                sanitize_log(str(session_id)),
                sanitize_log(stderr[:500]),
                sanitize_log(str(commit_set)),
            )
            return

        logger.info(
            "auto_commit_pre_complete: task=%s session=%s files=%s reason=uncommitted_changes_at_complete",
            sanitize_log(str(task.id)),
            sanitize_log(str(session_id)),
            sanitize_log(str(commit_set)),
        )
    except Exception as exc:  # intentional-broad-except: auto-commit is best-effort, never blocks completion
        logger.warning(
            "auto_commit_pre_complete_failed: task=%s session=%s error=%s files=%s",
            sanitize_log(str(task.id)),
            sanitize_log(str(session_id)),
            sanitize_log(str(exc)),
            sanitize_log(str(files_to_commit)),
        )


def _try_check_realtime_anomaly(
    request: Request,
    task_id: str,
    session_id: str | None,
    *,
    files_changed: int,
    last_file: str,
    last_command: str,
    message: str,
) -> None:
    """Run real-time anomaly detection on a progress update (best-effort).

    Writes a kill-signal file automatically when KILL_AGENT severity is
    detected; logs warnings for lower-severity signals.  Non-blocking -
    any exception is caught and logged so the progress route always succeeds.
    """
    if not session_id:
        return
    monitor = _get_realtime_monitor(request)
    if monitor is None:
        return
    try:
        from bernstein.core.behavior_anomaly import RealtimeBehaviorMonitor

        if not isinstance(monitor, RealtimeBehaviorMonitor):
            return
        signals = monitor.record_progress(
            session_id,
            task_id,
            files_changed=files_changed,
            last_file=last_file,
            last_command=last_command,
            message=message,
        )
        for signal in signals:
            logger.warning(
                "Realtime anomaly [%s] agent=%s task=%s: %s",
                sanitize_log(str(signal.rule)),
                sanitize_log(str(signal.agent_id)),
                sanitize_log(str(signal.task_id)),
                sanitize_log(str(signal.message)),
            )
    # intentional-broad-except: best-effort anomaly probe must never break the
    # progress route; surface modes include AttributeError on partial wiring.
    except Exception:
        logger.debug("Realtime behavior check failed for task %s", sanitize_log(task_id), exc_info=True)


# ---------------------------------------------------------------------------
# Sigstore attestation helper
# ---------------------------------------------------------------------------


def _try_attest_task_completion(
    request: Request,
    task_id: str,
    agent_id: str,
    result_summary: str,
) -> None:
    """Best-effort Sigstore/Ed25519 attestation for a completed task.

    Non-blocking - logs a warning and continues if attestation fails.
    """
    import hashlib

    sdd_dir: Path | None = getattr(request.app.state, "sdd_dir", None)
    if sdd_dir is None:
        return

    try:
        from bernstein.core.sigstore_attestation import (
            AttestationConfig,
            attest_task_completion,
        )

        diff_sha256 = hashlib.sha256(result_summary.encode()).hexdigest()
        attestation_dir = sdd_dir / "attestations"
        config = AttestationConfig(attestation_dir=attestation_dir)
        record = attest_task_completion(
            task_id=task_id,
            agent_id=agent_id,
            diff_sha256=diff_sha256,
            event_hmac="",
            config=config,
        )
        method = "Ed25519 fallback" if record.fallback_used else "Sigstore/Rekor"
        logger.info(
            "Task %s attested via %s: bundle=%s",
            sanitize_log(task_id),
            sanitize_log(method),
            sanitize_log(str(record.bundle_path)),
        )
    # intentional-broad-except: attestation is opt-in telemetry (Sigstore HTTP,
    # Ed25519 key IO, Rekor rate limits); must never break the task route.
    except Exception:
        logger.warning("Attestation failed for task %s (non-fatal)", sanitize_log(task_id), exc_info=True)


def _try_generate_sbom(request: Request) -> None:
    """Best-effort SBOM generation triggered after task completion.

    Runs only when ``BERNSTEIN_SBOM_ON_COMPLETE=1`` is set in the environment
    or when ``request.app.state.sbom_on_complete`` is truthy.  Non-blocking -
    any exception is caught and logged so the task completion route always
    succeeds.

    Artifacts are written to ``<workdir>/.sdd/artifacts/sbom/``.
    """
    import os

    sbom_enabled = os.environ.get("BERNSTEIN_SBOM_ON_COMPLETE", "").strip() in ("1", "true", "yes")
    if not sbom_enabled:
        sbom_enabled = bool(getattr(request.app.state, "sbom_on_complete", False))
    if not sbom_enabled:
        return

    workdir: Path | None = getattr(request.app.state, "workdir", None)
    if workdir is None:
        return

    try:
        from bernstein.core.sbom import SBOMGenerator

        generator = SBOMGenerator(workdir)
        sbom = generator.generate(source="pip")
        artifact_path = generator.save(sbom)
        logger.info(
            "SBOM generated on task completion: %s (%d components)",
            artifact_path,
            len(sbom.components),
        )
    # intentional-broad-except: SBOM generation shells out to pip/syft and must
    # never block task completion.
    except Exception:
        logger.warning("SBOM generation failed (non-fatal)", exc_info=True)


def _update_file_health(
    request: Request,
    task_id: str,
    owned_files: list[str],
    outcome: str,
) -> None:
    """Update per-file health scores after a task completes or fails.

    Fires synchronously but swallows all exceptions so task routes always
    succeed even if health tracking has an issue.

    Args:
        request: FastAPI request (for sdd_dir access).
        task_id: ID of the task that just finished.
        owned_files: Files the task claimed ownership of.
        outcome: ``"success"`` or ``"failure"``.
    """
    if not owned_files:
        return
    sdd_dir: Path | None = getattr(request.app.state, "sdd_dir", None)
    if sdd_dir is None:
        return
    try:
        from bernstein.core.file_health import FileHealthTracker

        tracker = FileHealthTracker(sdd_dir=sdd_dir)
        tracker.record_task_outcome(task_id, owned_files, outcome)
    # intentional-broad-except: per-file health is optional analytics and must
    # not propagate to the route.
    except Exception:
        logger.warning("file_health: update failed (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/tasks",
    status_code=201,
    responses={
        400: {"description": "Blocked by pre-create hook"},
        403: {"description": "Tenant access denied"},
        404: {"description": "Tenant not found"},
        429: {"description": "Tenant task quota exceeded"},
    },
)
async def create_task(body: TaskCreate, request: Request) -> TaskResponse:
    """Create a new task."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    effective_body = body.model_copy(update={"tenant_id": request_tenant_id(request)})

    # Auto-classify role if not specified
    if effective_body.role == "auto":
        effective_body.role = classify_role(effective_body.description)

    # Auto-estimate difficulty if minutes not provided
    if effective_body.estimated_minutes is None:
        score = estimate_difficulty(effective_body.description)
        effective_body.estimated_minutes = minutes_for_level(score.level)

    assessment = assess_task(effective_body)
    effective_body = effective_body.model_copy(
        update={
            "eu_ai_act_risk": merge_eu_ai_act_risk(effective_body.eu_ai_act_risk, assessment.risk_level).value,
            "approval_required": bool(effective_body.approval_required or assessment.approval_required),
            "risk_level": merge_bernstein_risk(effective_body.risk_level, assessment.bernstein_risk_level),
        }
    )

    with start_span("task.create", {"task.role": effective_body.role, "task.title": effective_body.title}):
        # Tenant quota enforcement
        from bernstein.core.tenant_isolation import TenantIsolationManager  # noqa: TC001

        tenant_mgr: TenantIsolationManager | None = getattr(
            request.app.state,
            "tenant_isolation_manager",
            None,
        )
        if tenant_mgr is not None:
            effective_tenant = request_tenant_id(request)
            current_count = store.count_by_status(tenant_id=effective_tenant).get("total", 0)
            allowed, reason = tenant_mgr.check_quota(effective_tenant, current_count)
            if not allowed:
                # The tenant quota is a hard cap, not a rolling window.  We
                # cannot promise a reset epoch, but we can advertise the
                # bucket capacity (max_tasks) and a remaining budget of zero
                # so the client back-off is informed.
                limit_value: int | None = None
                with suppress(Exception):
                    ctx = tenant_mgr.get_context(effective_tenant)
                    limit_value = int(ctx.quota.max_tasks)
                raise rate_limit_exception(
                    reason,
                    limit=limit_value,
                    remaining=0,
                )

        # Pre-create hook: may block via HookBlockingError (T719)
        try:
            pm = get_plugin_manager()
            pm.fire_pre_task_create(
                task_id="",  # ID not yet assigned - use empty string
                role=effective_body.role,
                title=effective_body.title,
                description=effective_body.description,
            )
        except HookBlockingError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Validate the requested role against the operator's configured
        # role_model_policy, when one is configured for this run. This is a
        # hard 400 (not just a log line) so the calling LLM gets an
        # immediate, actionable error instead of silently falling back to a
        # default provider/model at spawn time (see spawner_core.py's
        # _resolve_role_policy, which is exact-match only against
        # role_model_policy keys plus the "default" catch-all).
        #
        # "auto" is exempted because it is resolved to a concrete role via
        # classify_role() above, before this check ever runs -- but we still
        # guard it defensively in case that resolution is ever bypassed.
        raw_role = effective_body.role
        seed_config = getattr(request.app.state, "seed_config", None)
        role_model_policy: dict[str, Any] | None = getattr(seed_config, "role_model_policy", None)
        if role_model_policy:
            policy_keys = list(role_model_policy.keys())
            # "default" is a catch-all config entry, not an assignable role --
            # never advertise it as a valid choice to the calling LLM.
            valid_roles = [k for k in policy_keys if k != "default"]
            if raw_role != "auto" and raw_role not in role_model_policy:
                logger.warning(
                    "Rejecting task create: title=%r attempted role=%r is not a valid role. Valid roles=%s",
                    sanitize_log(str(effective_body.title)),
                    sanitize_log(str(raw_role)),
                    valid_roles,
                )
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Invalid role '{raw_role}'. Valid roles: "
                        f"[{', '.join(valid_roles)}]. Use one of these exact strings."
                    ),
                )
            logger.info(
                "Task created with role=%r matched against role_model_policy keys=%s",
                sanitize_log(str(raw_role)),
                policy_keys,
            )
        else:
            logger.info(
                "Task created with role=%r; no role_model_policy configured on this run "
                "(seed_config=%s) -- skipping role validation",
                sanitize_log(str(raw_role)),
                "present but empty" if seed_config is not None else "absent",
            )

        task = await store.create(effective_body)
        append_assessment_log(
            request.app.state.sdd_dir,
            build_log_record(task.id, task, assessment),
        )
        sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))
        get_plugin_manager().fire_task_created(task_id=task.id, role=task.role, title=task.title)
        return task_to_response(task)


@router.post(
    "/tasks/batch",
    status_code=201,
    responses={503: {"description": "Server is draining"}},
)
async def create_tasks_batch(body: BatchCreateRequest, request: Request) -> BatchCreateResponse:
    """Create multiple tasks atomically with title dedup."""
    if request.app.state.draining:  # type: ignore[attr-defined]
        raise HTTPException(
            status_code=503,
            detail=_DRAINING_DETAIL,
        )
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)

    prepared: list[TaskCreate] = []
    assessments: list[TaskRiskAssessment] = []
    for task_body in body.tasks:
        effective = task_body.model_copy(update={"tenant_id": request_tenant_id(request)})

        # Auto-classify role if not specified
        if effective.role == "auto":
            effective.role = classify_role(effective.description)

        # Auto-estimate difficulty if minutes not provided
        if effective.estimated_minutes is None:
            score = estimate_difficulty(effective.description)
            effective.estimated_minutes = minutes_for_level(score.level)

        assessment = assess_task(effective)
        effective = effective.model_copy(
            update={
                "eu_ai_act_risk": merge_eu_ai_act_risk(effective.eu_ai_act_risk, assessment.risk_level).value,
                "approval_required": bool(effective.approval_required or assessment.approval_required),
                "risk_level": merge_bernstein_risk(effective.risk_level, assessment.bernstein_risk_level),
            }
        )

        # Pre-create hook: skip individual task if blocked (don't fail entire batch)
        try:
            pm = get_plugin_manager()
            pm.fire_pre_task_create(
                task_id="",
                role=effective.role,
                title=effective.title,
                description=effective.description,
            )
        except HookBlockingError:
            logger.warning("Pre-create hook blocked task '%s' - skipping", sanitize_log(str(effective.title)))
            continue

        prepared.append(effective)
        assessments.append(assessment)

    created_tasks, skipped_titles = await store.create_batch(prepared, dedup_by_title=True)  # pyright: ignore[reportArgumentType]

    # Build a title->assessment lookup for created tasks (dedup may have dropped some)
    assessment_by_title = dict(zip([t.title for t in prepared], assessments, strict=False))
    for task in created_tasks:
        task_assessment = assessment_by_title.get(task.title)
        if task_assessment is not None:
            append_assessment_log(
                request.app.state.sdd_dir,
                build_log_record(task.id, task, task_assessment),
            )
        sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))
        get_plugin_manager().fire_task_created(task_id=task.id, role=task.role, title=task.title)

    return BatchCreateResponse(
        created=[task_to_response(t) for t in created_tasks],
        skipped_titles=skipped_titles,
    )


@router.post(
    "/tasks/self-create",
    status_code=201,
    responses={404: {"description": "Parent task not found"}},
)
async def self_create_subtask(body: TaskSelfCreate, request: Request) -> TaskResponse:
    """Create a subtask linked to a parent task.

    Agents call this to decompose work during execution.  The parent
    task is automatically transitioned to ``WAITING_FOR_SUBTASKS`` on
    the first subtask creation (if it is not already in that state).
    """
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)

    # Validate parent exists
    parent = store.get_task(body.parent_task_id)
    if parent is None:
        raise HTTPException(status_code=404, detail=f"Parent task '{body.parent_task_id}' not found")

    # Build a full TaskCreate from the self-create payload
    full_body = TaskCreate(
        title=body.title,
        description=body.description,
        role=body.role if body.role != "auto" else classify_role(body.description),
        priority=body.priority,
        scope=body.scope,
        complexity=body.complexity,
        estimated_minutes=body.estimated_minutes,
        depends_on=body.depends_on,
        parent_task_id=body.parent_task_id,
        owned_files=body.owned_files,
        tenant_id=request_tenant_id(request),
    )

    # Auto-estimate difficulty if minutes not provided
    if full_body.estimated_minutes is None:
        score = estimate_difficulty(full_body.description)
        full_body.estimated_minutes = minutes_for_level(score.level)

    with start_span("task.self_create", {"parent_task_id": body.parent_task_id}):
        task = await store.create(full_body)
        sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))

        # Auto-transition parent to waiting if not already
        if parent.status.value not in ("waiting_for_subtasks", "done", "failed", "closed"):
            subtask_count = store.count_subtasks(body.parent_task_id)
            with suppress(Exception):
                await store.wait_for_subtasks(body.parent_task_id, subtask_count)
                sse_bus.publish(
                    "task_update",
                    json.dumps({"id": parent.id, "status": "waiting_for_subtasks"}),
                )

        get_plugin_manager().fire_task_created(task_id=task.id, role=task.role, title=task.title)
        return task_to_response(task)


@router.get(
    "/tasks/next/{role}",
    responses=_TENANT_RESPONSES | {503: {"description": "Server is draining"}},
)
async def next_task(
    role: str,
    request: Request,
    claimed_by_session: str | None = None,
    parent_session_id: str | None = None,
) -> TaskResponse:
    """Claim the next available task for *role*.

    Pass ``claimed_by_session`` as a query param to record which parent
    orchestrator session owns the claim.

    Pass ``parent_session_id`` to restrict claiming to tasks that were
    created under that coordinator session.  Workers belonging to a
    coordinator should always pass their coordinator's session ID here
    to avoid stealing tasks from other namespaces.
    """
    if request.app.state.draining:  # type: ignore[attr-defined]
        raise HTTPException(
            status_code=503,
            detail=_DRAINING_DETAIL,
        )
    store = _get_store(request)
    task = await store.claim_next(
        role,
        tenant_id=_resolve_request_tenant_scope(request),
        claimed_by_session=claimed_by_session,
        parent_session_id=parent_session_id,
    )
    if task is None:
        logger.info(
            "task.next 404: role=%s claimed_by_session=%s parent_session_id=%s no open tasks",
            sanitize_log(role),
            sanitize_log(str(claimed_by_session)),
            sanitize_log(str(parent_session_id)),
        )
        raise HTTPException(status_code=404, detail=f"No open tasks for role '{role}'")
    return task_to_response(task)


@router.post("/tasks/claim-batch", responses={503: {"description": "Server is draining"}})
async def claim_batch(body: BatchClaimRequest, request: Request) -> BatchClaimResponse:
    """Atomically claim multiple tasks by ID for an agent."""
    if request.app.state.draining:  # type: ignore[attr-defined]
        raise HTTPException(
            status_code=503,
            detail=_DRAINING_DETAIL,
        )
    with start_span("task.claim_batch", {"agent_id": body.agent_id, "task_count": len(body.task_ids)}):
        store = _get_store(request)
        tenant_id = _resolve_request_tenant_scope(request)
        # Tenant authorization is enforced inside store.claim_batch under
        # the same lock that performs the claim, so the check cannot be
        # invalidated by a concurrent delete or tenant rewrite (TOCTOU).
        claimed, failed = await store.claim_batch(
            list(body.task_ids),
            body.agent_id,
            claimed_by_session=body.claimed_by_session,
            tenant_id=tenant_id,
        )
        if failed:
            logger.warning(
                "task.claim_batch partial failure: agent_id=%s requested=%d claimed=%d failed=%s",
                sanitize_log(body.agent_id),
                len(body.task_ids),
                len(claimed),
                sanitize_log(str(failed)),
            )
        return BatchClaimResponse(claimed=claimed, failed=failed)


@router.post(
    "/tasks/{task_id}/claim",
    responses={
        404: {"description": "Task not found"},
        409: {"description": "Version conflict or invalid state"},
        503: {"description": "Server is draining"},
    },
)
async def claim_task(
    task_id: str,
    request: Request,
    expected_version: int | None = None,
    claimed_by_session: str | None = None,
) -> TaskResponse:
    """Claim a specific task by ID.

    Pass ``expected_version`` as a query param for optimistic locking
    (CAS). If the task's version doesn't match, returns 409 Conflict.

    Pass ``claimed_by_session`` to record which parent orchestrator
    session owns this claim.
    """
    if request.app.state.draining:  # type: ignore[attr-defined]
        raise HTTPException(
            status_code=503,
            detail=_DRAINING_DETAIL,
        )
    with start_span("task.claim", {"task.id": task_id}):
        store = _get_store(request)
        sse_bus = _get_sse_bus(request)
        try:
            task = store.get_task(task_id)
            if task is None:
                raise KeyError
            _require_task_access(task, request)
            pre_claim_status = task.status.value
            pre_claim_version = task.version
            task = await store.claim_by_id(
                task_id,
                expected_version=expected_version,
                claimed_by_session=claimed_by_session,
            )
        except KeyError:
            logger.warning(
                "task.claim 404: task_id=%s not found (claimed_by_session=%s)",
                sanitize_log(task_id),
                sanitize_log(str(claimed_by_session)),
            )
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
        except ValueError as exc:
            # This is the single most important line for diagnosing claim-conflict
            # churn: log expected vs. actual version/status so a retry storm is
            # visible from the server log alone, without needing client-side traces.
            logger.warning(
                "task.claim 409: task_id=%s expected_version=%s actual_version=%s "
                "pre_claim_status=%s claimed_by_session=%s reason=%s",
                sanitize_log(task_id),
                sanitize_log(str(expected_version)),
                pre_claim_version,
                pre_claim_status,
                sanitize_log(str(claimed_by_session)),
                sanitize_log(str(exc)),
            )
            raise HTTPException(status_code=409, detail=str(exc)) from None
        sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "claimed"}))
        logger.info(
            "task.claim ok: task_id=%s new_version=%s claimed_by_session=%s",
            sanitize_log(task.id),
            task.version,
            sanitize_log(str(claimed_by_session)),
        )
        return task_to_response(task)


def _parse_terminal_body(body: TaskCompleteRequest) -> WorkerCompletion | WorkerRefusal | None:
    """Extract and validate a structured terminal payload from the body.

    Returns ``None`` for legacy prose summaries, which stay accepted
    unchanged. An explicit ``payload`` object, or a ``result_summary``
    that is itself a JSON object, is validated against the worker
    completion contract.

    Raises:
        ContractViolation: When the structured payload fails validation.
    """
    if body.payload is not None:
        return parse_terminal_payload(body.payload)
    if looks_like_contract_payload(body.result_summary):
        return parse_terminal_payload_text(body.result_summary)
    return None


async def _handle_contract_violation(
    request: Request,
    task_id: str,
    violation: ContractViolation,
    store: TaskStore,
    sse_bus: SSEBus,
) -> HTTPException:
    """Fail a task whose terminal payload violated the contract.

    The task is auto-failed with ``terminal_reason='contract_violation'``
    so the worker slot is released atomically, mirroring the empty-summary
    path. Returns the HTTPException for the caller to raise (422 with the
    schema error path, or 409 when the task is already terminal).
    """
    try:
        failed_task = await store.fail_contract_violation(task_id, violation)
    except IllegalTransitionError as exc:
        return HTTPException(status_code=409, detail=str(exc))
    logger.warning(
        "task.complete contract_violation: task_id=%s path=%s",
        sanitize_log(task_id),
        sanitize_log(violation.path),
    )
    sse_bus.publish("task_update", json.dumps({"id": failed_task.id, "status": failed_task.status.value}))
    get_plugin_manager().fire_task_failed(
        task_id=failed_task.id,
        role=failed_task.role,
        error=f"contract_violation: {violation.path}",
    )
    _update_file_health(request, failed_task.id, list(failed_task.owned_files), "failure")
    return HTTPException(
        status_code=422,
        detail={
            "error": "contract_violation",
            "message": violation.message,
            "schema_error_path": violation.path,
            "contract_version": _CONTRACT_VERSION,
            "task_id": task_id,
            "status": failed_task.status.value,
        },
    )


def _write_refusal_approval_item(request: Request, task: Task, refusal: WorkerRefusal) -> None:
    """Surface an ``awaiting_operator`` refusal as a pending approval item.

    Writes the same file shape the approvals routes list from
    ``.sdd/runtime/pending_approvals/``, so the operator sees the question
    in the existing approvals surface. Best-effort: an unwritable runtime
    directory must not fail the refusal that already landed.
    """
    pending_dir = _get_workdir(request) / ".sdd" / "runtime" / "pending_approvals"
    item = {
        "task_id": task.id,
        "task_title": task.title,
        "session_id": task.claimed_by_session or "",
        "diff": "",
        "test_summary": f"Operator input requested: {refusal.question}",
    }
    try:
        pending_dir.mkdir(parents=True, exist_ok=True)
        (pending_dir / f"{task.id}.json").write_text(json.dumps(item, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "Could not write pending approval item for refused task %s: %s",
            sanitize_log(task.id),
            exc,
        )


async def _finalize_refusal(
    request: Request,
    task: Task,
    refusal: WorkerRefusal,
    store: TaskStore,
    sse_bus: SSEBus,
) -> TaskResponse:
    """Post-process a refusal that already landed in the store.

    Routes the refusal kind deterministically: ``scope_exceeded`` feeds
    the follow-up task machinery (content-addressed ids, so redelivery
    cannot duplicate the split) and ``awaiting_operator`` surfaces as an
    operator approval item.
    """
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))
    if refusal.kind is RefusalKind.SCOPE_EXCEEDED:
        created = await store.create_refusal_follow_ups(task, refusal)
        for follow_up in created:
            sse_bus.publish("task_update", json.dumps({"id": follow_up.id, "status": "open"}))
    elif refusal.kind is RefusalKind.AWAITING_OPERATOR:
        _write_refusal_approval_item(request, task, refusal)
    # Evict session from the real-time monitor to free memory
    _evict_realtime_session(request, task.claimed_by_session)
    return task_to_response(task)


@router.post(
    "/tasks/{task_id}/complete",
    responses={
        404: {"description": "Task not found"},
        409: {"description": "Invalid state transition"},
        422: {"description": "Empty result_summary or contract violation - task auto-failed"},
    },
)
async def complete_task(task_id: str, body: TaskCompleteRequest, request: Request) -> TaskResponse:
    """Mark a task as done (or refused) from a worker terminal payload.

    Structured payloads (``body.payload`` or a JSON object embedded in
    ``result_summary``) are validated against the worker completion
    contract (#2244): an invalid payload is a typed ``contract_violation``
    failure carrying the schema error path, and a validated refusal lands
    the task in the terminal REFUSED state instead of DONE. Legacy prose
    summaries are accepted unchanged.

    If ``result_summary`` is empty the task is auto-transitioned to
    ``FAILED`` with ``reason='completion missing summary'`` and
    a 422 is returned with the failed task payload so the client knows the
    slot was released.
    """
    with start_span("task.complete", {"task.id": task_id}):
        store = _get_store(request)
        sse_bus = _get_sse_bus(request)
        refusal: WorkerRefusal | None = None
        result_summary = body.result_summary
        try:
            task = store.get_task(task_id)
            if task is None:
                raise KeyError
            _require_task_access(task, request)
            # Auto-claim if task reverted to "open" (e.g. after orchestrator
            # restart reconciliation).  Prevents agents from looping on 409.
            if task.status.value == "open":
                logger.info(
                    "task.complete auto-claim: task_id=%s reverted to open, re-claiming before complete",
                    sanitize_log(task_id),
                )
                await store.claim_by_id(task_id)
            structured = _parse_terminal_body(body)
            if isinstance(structured, WorkerRefusal):
                refusal = structured
                task = await store.refuse(task_id, refusal)
            else:
                if structured is not None:
                    result_summary = structured.summary
                # Defect 33: auto-commit the worker's uncommitted work BEFORE
                # the store transitions the task to done, so the commit message
                # is visible to the janitor (which reads ``git log`` after
                # /complete lands).  Failures are swallowed - the orchestrator
                # will catch a 0-diff task on /complete's branch and trigger
                # the bounded-reopen path.  Logging contract in
                # ``_run_auto_commit_pre_complete``.
                _run_auto_commit_pre_complete(request, task)
                task = await store.complete(task_id, result_summary, completion=structured)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
        except ContractViolation as exc:
            raise await _handle_contract_violation(request, task_id, exc, store, sse_bus) from None
        except EmptyCompletionError as exc:
            # empty summary is handled inside ``complete()``
            # (task is auto-failed under the lock).  Surface a structured
            # 422 so the client knows the slot was released.
            failed_task = exc.task
            detail: dict[str, Any] = {
                "error": "empty_result_summary",
                "message": str(exc),
                "task_id": task_id,
                "reason": "completion missing summary",
                "status": failed_task.status.value if failed_task is not None else "failed",
            }
            if failed_task is not None:
                sse_bus.publish(
                    "task_update",
                    json.dumps({"id": failed_task.id, "status": failed_task.status.value}),
                )
                get_plugin_manager().fire_task_failed(
                    task_id=failed_task.id,
                    role=failed_task.role,
                    error="completion missing summary",
                )
                _update_file_health(
                    request,
                    failed_task.id,
                    list(failed_task.owned_files),
                    "failure",
                )
            raise HTTPException(status_code=422, detail=detail) from None
        except IllegalTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        if refusal is not None:
            return await _finalize_refusal(request, task, refusal, store, sse_bus)
        sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "done"}))
        get_plugin_manager().fire_task_completed(task_id=task.id, role=task.role, result_summary=result_summary)

        # Sigstore/Ed25519 attestation for the task completion (fire-and-forget)
        _try_attest_task_completion(request, task.id, task.role, result_summary)

        # SBOM generation on task completion (fire-and-forget, opt-in via env/state)
        _try_generate_sbom(request)

        # Evict session from the real-time monitor to free memory
        _evict_realtime_session(request, task.claimed_by_session)

        # Update per-file health scores (fire-and-forget)
        _update_file_health(request, task.id, list(task.owned_files), "success")

        return task_to_response(task)


@router.post(
    "/tasks/{task_id}/wait-for-subtasks",
    responses={404: {"description": "Task not found"}, 409: {"description": "Invalid state transition"}},
)
async def wait_for_subtasks(task_id: str, body: TaskWaitForSubtasksRequest, request: Request) -> TaskResponse:
    """Mark a parent task as waiting until its generated subtasks complete."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.wait_for_subtasks(task_id, body.subtask_count)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except IllegalTransitionError as exc:
        logger.warning(
            "task.wait_for_subtasks 409: task_id=%s current_status=%s reason=%s",
            sanitize_log(task_id),
            existing_task.status.value if existing_task is not None else "unknown",
            sanitize_log(str(exc)),
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))
    return task_to_response(task)


@router.post(
    "/tasks/{task_id}/fail",
    responses={404: {"description": "Task not found"}, 409: {"description": "Invalid state transition"}},
)
async def fail_task(task_id: str, body: TaskFailRequest, request: Request) -> TaskResponse:
    """Mark a task as failed."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        # Auto-claim if task reverted to "open" (same rationale as /complete).
        if existing_task.status.value == "open":
            logger.info(
                "task.fail auto-claim: task_id=%s reverted to open, re-claiming before fail",
                sanitize_log(task_id),
            )
            await store.claim_by_id(task_id)
        task = await store.fail(task_id, body.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except IllegalTransitionError as exc:
        logger.warning(
            "task.fail 409: task_id=%s current_status=%s reason=%s",
            sanitize_log(task_id),
            existing_task.status.value if existing_task is not None else "unknown",
            sanitize_log(str(exc)),
        )
        raise HTTPException(status_code=409, detail=str(exc)) from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "failed"}))
    get_plugin_manager().fire_task_failed(task_id=task.id, role=task.role, error=body.reason)

    # Update per-file health scores with failure outcome (fire-and-forget)
    _update_file_health(request, task.id, list(task.owned_files), "failure")

    return task_to_response(task)


@router.post(
    "/tasks/{task_id}/reopen",
    responses={404: {"description": "Task not found"}, 409: {"description": "Invalid state transition"}},
)
async def reopen_task(task_id: str, body: TaskReopenRequest, request: Request) -> TaskResponse:
    """Reopen a done task that failed janitor verification (same task id).

    Transitions DONE -> OPEN and increments
    ``metadata['janitor_reopen_count']``. The orchestrator enforces the
    reopen budget; this endpoint only performs the state transition.
    """
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.reopen(task_id, body.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except IllegalTransitionError as exc:
        logger.warning(
            "task.reopen 409: task_id=%s current_status=%s reason=%s",
            sanitize_log(task_id),
            existing_task.status.value if existing_task is not None else "unknown",
            sanitize_log(str(exc)),
        )
        raise HTTPException(status_code=409, detail=str(exc)) from None
    logger.info(
        "task.reopen: task_id=%s reopen_count=%s reason=%s",
        sanitize_log(task_id),
        task.metadata.get("janitor_reopen_count"),
        sanitize_log(str(body.reason)),
    )
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "open"}))
    return task_to_response(task)


@router.post(
    "/tasks/{task_id}/close",
    responses={404: {"description": "Task not found"}, 409: {"description": "Invalid state transition"}},
)
async def close_task(task_id: str, request: Request) -> TaskResponse:
    """Mark a verified task as closed (terminal success state)."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.close(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except IllegalTransitionError as exc:
        logger.warning(
            "task.close 409: task_id=%s current_status=%s reason=%s",
            sanitize_log(task_id),
            existing_task.status.value if existing_task is not None else "unknown",
            sanitize_log(str(exc)),
        )
        raise HTTPException(status_code=409, detail=str(exc)) from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "closed"}))
    return task_to_response(task)


@router.post(
    "/tasks/{task_id}/cancel",
    responses={404: {"description": "Task not found"}, 409: {"description": "Invalid state transition"}},
)
async def cancel_task(task_id: str, body: TaskCancelRequest, request: Request) -> TaskResponse:
    """Cancel a task and cascade to all of its descendant subtasks.

    Walks the subtask tree (``parent_task_id`` references) via
    ``TaskStore.cancel_cascade`` so that children are not left running
    after the parent is aborted.  Returns the root task.
    """
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        # Preserve the legacy 409 for a root task that is already terminal -
        # ``cancel_cascade`` silently skips terminal tasks, so we check here.
        cancellable = {
            "open",
            "claimed",
            "in_progress",
            "blocked",
            "waiting_for_subtasks",
            "planned",
        }
        if existing_task.status.value not in cancellable:
            raise ValueError(
                f"Task '{task_id}' cannot be cancelled from status '{existing_task.status.value}'",
            )
        cancelled_tasks = await store.cancel_cascade(task_id, body.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except ValueError as exc:
        logger.warning(
            "task.cancel 409: task_id=%s current_status=%s reason=%s",
            sanitize_log(task_id),
            existing_task.status.value if existing_task is not None else "unknown",
            sanitize_log(str(exc)),
        )
        raise HTTPException(status_code=409, detail=str(exc)) from None

    # Publish an SSE event for every cancelled task (root + descendants) so
    # the dashboard and any waiting watchers can react immediately.
    root_task = next((t for t in cancelled_tasks if t.id == task_id), cancelled_tasks[0])
    for cancelled in cancelled_tasks:
        sse_bus.publish(
            "task_update",
            json.dumps({"id": cancelled.id, "status": cancelled.status.value}),
        )
    return task_to_response(root_task)


@router.post(
    "/tasks/{task_id}/block",
    responses={404: {"description": "Task not found"}, 409: {"description": "Invalid state transition"}},
)
async def block_task(task_id: str, body: TaskBlockRequest, request: Request) -> TaskResponse:
    """Mark a task as blocked -- requires human intervention to unblock."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.block(task_id, body.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except IllegalTransitionError as exc:
        logger.warning(
            "task.block 409: task_id=%s current_status=%s reason=%s",
            sanitize_log(task_id),
            existing_task.status.value if existing_task is not None else "unknown",
            sanitize_log(str(exc)),
        )
        raise HTTPException(status_code=409, detail=str(exc)) from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "blocked"}))
    return task_to_response(task)


def _store_progress_snapshot(store: Any, task_id: str, body: Any) -> None:
    """Store structured snapshot for stall detection when snapshot fields are present."""
    if body.files_changed is body.tests_passing is None:
        return
    store.add_snapshot(
        task_id,
        files_changed=body.files_changed if body.files_changed is not None else 0,
        tests_passing=body.tests_passing if body.tests_passing is not None else -1,
        errors=body.errors if body.errors is not None else 0,
        last_file=body.last_file,
    )


def _persist_lines_if_present(request: Request, task: Any, body: Any) -> None:
    """Persist lines_changed for the cost-efficiency metric endpoint."""
    if body.lines_changed is None or body.lines_changed <= 0:
        return
    agent_id = task.claimed_by_session or ""
    if agent_id:
        _persist_lines_changed(request, agent_id, body.lines_changed)


@router.post("/tasks/{task_id}/progress", responses={404: {"description": "Task not found"}})
async def progress_task(task_id: str, body: TaskProgressRequest, request: Request) -> TaskResponse:
    """Append an intermediate progress update to a task.

    Also stores a progress snapshot for stall detection when snapshot
    fields (files_changed, tests_passing, errors) are provided.
    """
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.add_progress(task_id, body.message, body.percent)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    _store_progress_snapshot(store, task_id, body)
    _persist_lines_if_present(request, task, body)

    # Real-time behavior anomaly detection - checks file access, commands,
    # network endpoints, output size, and file-change velocity against learned
    # baselines.  Kill signals are written automatically for KILL_AGENT severity.
    _try_check_realtime_anomaly(
        request,
        task_id,
        task.claimed_by_session,
        files_changed=body.files_changed or 0,
        last_file=body.last_file,
        last_command=body.last_command,
        message=body.message or "",
    )

    sse_bus.publish(
        "task_progress",
        json.dumps({"id": task.id, "message": body.message, "percent": body.percent}),
    )
    return task_to_response(task)


@router.post(
    "/tasks/{task_id}/partial-merge",
    responses={
        404: {"description": "Task not found"},
        409: {"description": "Task not in progress or has no active session"},
    },
)
async def partial_merge_task(
    task_id: str,
    body: PartialMergeRequest,
    request: Request,
) -> PartialMergeResponse:
    """Incrementally merge specific committed files from the agent's branch into main.

    Allows a long-running agent to push a completed subset of its work (e.g.
    the first 5 of 10 test files) while still writing the rest.  Reduces
    wall-clock time by making partial results available downstream earlier.

    Only files that are already **committed** in the agent's worktree branch
    (``agent/<session_id>``) are merged.  Uncommitted files are returned in
    ``uncommitted_files`` so the caller knows to commit them in the worktree
    first.  Files that were already merged by a prior call are skipped and
    returned in ``skipped_already_merged``.

    Requires the task to be ``in_progress`` with a ``claimed_by_session`` set.
    """
    from bernstein.core.incremental_merge import incremental_merge_files

    store = _get_store(request)
    sse_bus = _get_sse_bus(request)

    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)

    if task.status != "in_progress":  # pyright: ignore[reportUnnecessaryComparison]
        raise HTTPException(
            status_code=409,
            detail=f"Task '{task_id}' is not in_progress (status={task.status})",
        )
    session_id = task.claimed_by_session or ""
    if not session_id:
        raise HTTPException(
            status_code=409,
            detail=f"Task '{task_id}' has no active session (claimed_by_session is empty)",
        )

    workdir: Path = request.app.state.workdir
    runtime_dir = _get_runtime_dir(request)

    result = incremental_merge_files(
        workdir=workdir,
        runtime_dir=runtime_dir,
        session_id=session_id,
        files=body.files,
        message=body.message,
    )

    # Publish SSE event so the dashboard can show incremental progress
    if result.success and result.merged_files:
        sse_bus.publish(
            "task_partial_merge",
            json.dumps(
                {
                    "id": task_id,
                    "session_id": session_id,
                    "merged_files": result.merged_files,
                    "commit_sha": result.commit_sha,
                }
            ),
        )

    return PartialMergeResponse(
        success=result.success,
        merged_files=result.merged_files,
        skipped_already_merged=result.skipped_already_merged,
        uncommitted_files=result.uncommitted_files,
        conflicting_files=result.conflicting_files,
        commit_sha=result.commit_sha,
        error=result.error,
    )


@router.get("/tasks/{task_id}/partial-merge", responses={404: {"description": "Task not found"}})
def get_partial_merge_state(task_id: str, request: Request) -> PartialMergeResponse:
    """Return the cumulative incremental-merge state for a task's active session.

    Useful for monitoring how much of an in-progress task's output has already
    been merged into the main branch.
    """
    from bernstein.core.incremental_merge import get_incremental_merge_state

    task = _get_store(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)

    session_id = task.claimed_by_session or ""
    if not session_id:
        return PartialMergeResponse(
            success=True,
            merged_files=[],
            skipped_already_merged=[],
            uncommitted_files=[],
            conflicting_files=[],
            commit_sha="",
            error="",
        )

    runtime_dir = _get_runtime_dir(request)
    state = get_incremental_merge_state(runtime_dir, session_id)

    return PartialMergeResponse(
        success=True,
        merged_files=state.merged_files,
        skipped_already_merged=[],
        uncommitted_files=[],
        conflicting_files=[],
        commit_sha=state.merge_commits[-1] if state.merge_commits else "",
        error="",
    )


@router.get("/tasks/{task_id}/snapshots", responses={404: {"description": "Task not found"}})
def get_task_snapshots(task_id: str, request: Request) -> list[SnapshotEntry]:
    """Return stored progress snapshots for a task (oldest-first, up to 10)."""
    store = _get_store(request)
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)
    snapshots = store.get_snapshots(task_id)
    return [
        SnapshotEntry(
            timestamp=s.timestamp,
            files_changed=s.files_changed,
            tests_passing=s.tests_passing,
            errors=s.errors,
            last_file=s.last_file,
        )
        for s in snapshots
    ]


# Maximum number of tasks returned in a single GET /tasks response.
# Applies to both the paginated envelope and the legacy flat-list shape:
# the legacy path silently truncates to this cap and emits a deprecation
# header so callers can migrate to explicit pagination without an outage.
_LIST_TASKS_HARD_CAP = 500


@router.get("/tasks", response_model=None, responses=_TENANT_RESPONSES)
def list_tasks(
    request: Request,
    status: str | None = None,
    cell_id: str | None = None,
    tenant: str | None = None,
    claimed_by_session: str | None = None,
    parent_session_id: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> PaginatedTasksResponse | JSONResponse:
    """List tasks, optionally filtered by status, cell_id, and/or claim owner.

    When ``limit`` or ``offset`` query params are provided the response is a
    paginated envelope (``{tasks, total, limit, offset}``).  Without them,
    the legacy flat list is returned for backward compatibility, capped at
    ``_LIST_TASKS_HARD_CAP`` items and accompanied by a ``Deprecation``
    header asking callers to pass explicit pagination.

    Args:
        request: FastAPI request.
        status: If provided, only tasks with this status are returned.
        cell_id: If provided, only tasks in this cell are returned.
        tenant: Tenant scope override.
        claimed_by_session: If provided, only tasks claimed by this parent
            orchestrator session are returned.
        limit: Maximum number of tasks to return (max 500).  Triggers
            paginated response when present.
        offset: Number of tasks to skip.  Triggers paginated response
            when present.

    Returns:
        Paginated response **or** plain list of TaskResponse dicts (capped
        at ``_LIST_TASKS_HARD_CAP``).
    """
    store = _get_store(request)
    effective_tenant = _resolve_request_tenant_scope(request, tenant)
    all_tasks = store.list_tasks(
        status,
        cell_id,
        tenant_id=effective_tenant,
        claimed_by_session=claimed_by_session,
        parent_session_id=parent_session_id,
    )

    paginate = limit is not None or offset is not None
    if paginate:
        effective_limit = max(1, min(limit or 100, _LIST_TASKS_HARD_CAP))
        effective_offset = max(0, offset or 0)
        total = len(all_tasks)
        page = all_tasks[effective_offset : effective_offset + effective_limit]
        return PaginatedTasksResponse(
            tasks=[task_to_response(t) for t in page],
            total=total,
            limit=effective_limit,
            offset=effective_offset,
        )

    # Legacy: return a flat list capped at _LIST_TASKS_HARD_CAP for callers
    # that have not yet migrated to explicit pagination.  We always emit a
    # Deprecation header; when truncation occurs we also expose the true
    # total via X-Total-Count so clients can detect the cap and page.
    total = len(all_tasks)
    page = all_tasks[:_LIST_TASKS_HARD_CAP]
    body = [task_to_response(t).model_dump(mode="json") for t in page]
    headers = {
        "Deprecation": "true",
        "Link": '</tasks?limit=500&offset=0>; rel="successor-version"',
        "X-Total-Count": str(total),
    }
    if total > _LIST_TASKS_HARD_CAP:
        headers["Warning"] = (
            f'299 - "GET /tasks without limit/offset is capped at '
            f"{_LIST_TASKS_HARD_CAP}; pass limit/offset to page through "
            f'{total} tasks"'
        )
    return JSONResponse(content=body, headers=headers)


@router.get("/tasks/counts", responses=_TENANT_RESPONSES)
def task_counts(
    request: Request,
    tenant: str | None = None,
) -> TaskCountsResponse:
    """Return task counts per status without serialising task bodies.

    This is the lightweight alternative to GET /tasks for orchestrator
    tick summaries and dashboard polling.
    """
    store = _get_store(request)
    effective_tenant = _resolve_request_tenant_scope(request, tenant)
    counts = store.count_by_status(tenant_id=effective_tenant)
    # Expose every TaskStatus value so the GUI can render real numbers for
    # closed / in_progress / planned / pending_approval / waiting_for_subtasks
    # / orphaned instead of falling back to ``-``.  Missing keys default to 0
    # - back-compat for the six original buckets is preserved.
    return TaskCountsResponse(
        open=counts.get("open", 0),
        claimed=counts.get("claimed", 0),
        in_progress=counts.get("in_progress", 0),
        done=counts.get("done", 0),
        closed=counts.get("closed", 0),
        failed=counts.get("failed", 0),
        blocked=counts.get("blocked", 0),
        cancelled=counts.get("cancelled", 0),
        planned=counts.get("planned", 0),
        pending_approval=counts.get("pending_approval", 0),
        waiting_for_subtasks=counts.get("waiting_for_subtasks", 0),
        orphaned=counts.get("orphaned", 0),
        abandoned=counts.get("abandoned", 0),
        blocked_by_abandon=counts.get("blocked_by_abandon", 0),
        refused=counts.get("refused", 0),
        total=counts.get("total", 0),
    )


@router.get("/tasks/archive", responses=_TENANT_RESPONSES)
def get_archive(request: Request, limit: int = 50, tenant: str | None = None) -> list[ArchiveRecord]:
    """Return the last N archived (done/failed) task records."""
    return _get_store(request).read_archive(limit=limit, tenant_id=_resolve_request_tenant_scope(request, tenant))


@router.get("/tasks/graph", responses=_TENANT_RESPONSES)
def get_task_graph(request: Request) -> JSONResponse:
    """Return the task dependency graph as JSON (nodes + edges + critical path).

    Builds a DAG from all current tasks and returns:
    - ``nodes``: list of {id, role, status, estimated_minutes, title}
    - ``edges``: list of {from, to, type, semantic_type}
    - ``critical_path``: ordered list of task IDs on the longest chain
    - ``critical_path_minutes``: total estimated minutes on the critical path
    - ``parallel_width``: max tasks that can run concurrently
    - ``bottlenecks``: task IDs that block the most downstream work
    """
    from bernstein.core.knowledge.task_graph import TaskGraph

    tasks = _get_store(request).list_tasks(tenant_id=_resolve_request_tenant_scope(request))
    data = TaskGraph(tasks).to_dict()
    # Enrich nodes with title for CLI rendering
    task_map = {t.id: t for t in tasks}
    for node in data["nodes"]:
        node["title"] = task_map[node["id"]].title if node["id"] in task_map else ""
    return JSONResponse(content=data)


@router.get("/tasks/{task_id}", responses={404: {"description": "Task not found"}})
def get_task(task_id: str, request: Request) -> TaskResponse:
    """Get a single task by ID."""
    task = _get_store(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)
    return task_to_response(task)


@router.get(
    "/tasks/{task_id}/graph-neighbors",
    responses={404: {"description": "Task not found"}},
)
def get_task_graph_neighbors(task_id: str, request: Request) -> dict[str, Any]:
    """Return immediate dependency neighbours for a single task.

    Powers the dashboard Deps tab: upstream tasks the requested one waits
    on (its ``depends_on`` list) and downstream tasks that declare it as a
    dependency.  Depth is intentionally fixed at 1 - the panel renders two
    flat lists, not a transitive graph.
    """
    store = _get_store(request)
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)

    all_tasks = store.list_tasks()
    by_id = {t.id: t for t in all_tasks}

    def _neighbor(other: Task) -> dict[str, Any]:
        return {
            "id": other.id,
            "title": other.title,
            "status": other.status.value if hasattr(other.status, "value") else str(other.status),
            "role": other.role,
        }

    upstream: list[dict[str, Any]] = []
    seen_up: set[str] = set()
    for dep_id in task.depends_on:
        if dep_id in seen_up:
            continue
        seen_up.add(dep_id)
        dep = by_id.get(dep_id)
        if dep is None:
            # Missing dep - surface the ID so the operator can see the gap
            # without a hard 404.
            upstream.append({"id": dep_id, "title": None, "status": "missing", "role": None})
        else:
            upstream.append(_neighbor(dep))

    downstream: list[dict[str, Any]] = []
    seen_down: set[str] = set()
    for other in all_tasks:
        if other.id == task.id or other.id in seen_down:
            continue
        if task.id in other.depends_on:
            seen_down.add(other.id)
            downstream.append(_neighbor(other))

    return {
        "task_id": task.id,
        "depth": 1,
        "upstream": upstream,
        "downstream": downstream,
    }


@router.get(
    "/tasks/{task_id}/gates",
    responses={404: {"description": "Task or gate report not found"}, 500: {"description": "Gate report unreadable"}},
)
def get_task_gates(task_id: str, request: Request) -> JSONResponse:
    """Return the persisted quality-gate report for a task."""
    task = _get_store(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)

    report_path = _get_gate_report_path(request, task_id)
    if not report_path.exists():
        raise HTTPException(status_code=404, detail=f"Gate report for task '{task_id}' not found")
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Gate report for task '{task_id}' is unreadable") from exc

    # Annotate the response with a `generated_at` ISO-8601 UTC timestamp derived
    # from the report file mtime. The on-disk GateReport dataclass does not carry
    # its own timestamp, so the UI needs the file mtime to render "last run" /
    # relative-time strings. We only inject the field when missing to preserve
    # any future server-side overrides. Also surface the current task lifecycle
    # status so the client can stop polling for terminal tasks.
    if isinstance(payload, dict) and "generated_at" not in payload:
        with suppress(OSError):
            mtime = report_path.stat().st_mtime
            payload["generated_at"] = datetime.fromtimestamp(mtime, tz=UTC).isoformat().replace("+00:00", "Z")
    if isinstance(payload, dict) and "task_status" not in payload:
        payload["task_status"] = task.status.value
    return JSONResponse(content=payload)


@router.patch("/tasks/{task_id}", responses={404: {"description": "Task not found"}})
async def patch_task(task_id: str, body: TaskPatchRequest, request: Request) -> TaskResponse:
    """Update mutable task fields (role, priority, model) - manager corrections.

    Used by the manager agent or dashboard to correct mis-assigned tasks,
    adjust priority, or change model without interrupting the orchestrator.
    """
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.update(task_id, role=body.role, priority=body.priority, model=body.model)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))
    return task_to_response(task)


@router.post(
    "/tasks/{task_id}/prioritize",
    responses={404: {"description": "Task not found"}},
)
async def prioritize_task(task_id: str, request: Request) -> TaskResponse:
    """Bump a task to priority 0 so the orchestrator picks it up next."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.prioritize(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))
    return task_to_response(task)


@router.post(
    "/tasks/{task_id}/force-claim",
    responses={404: {"description": "Task not found"}, 409: {"description": "Cannot force-claim terminal task"}},
)
async def force_claim_task(task_id: str, request: Request) -> TaskResponse:
    """Force a task back to open with priority 0 for immediate pickup.

    Resets claimed/in_progress tasks back to open so the orchestrator's
    next tick will spawn a fresh agent for them.  Terminal tasks
    (done/failed/cancelled) are rejected with 409.
    """
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.force_claim(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "open"}))
    return task_to_response(task)


# ---------------------------------------------------------------------------
# Agent heartbeats and session management
# ---------------------------------------------------------------------------


@router.post("/agents/{agent_id}/heartbeat")
def agent_heartbeat(agent_id: str, body: HeartbeatRequest, request: Request) -> HeartbeatResponse:
    """Register an agent heartbeat."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    ts = store.heartbeat(agent_id, body.role, body.status)
    sse_bus.publish("agent_update", json.dumps({"agent_id": agent_id, "status": body.status}))
    return HeartbeatResponse(agent_id=agent_id, acknowledged=True, server_ts=ts)


@router.get(
    "/agents/{session_id}/logs",
    responses={404: {"description": "No log file for session"}},
)
def agent_logs(session_id: str, request: Request, tail_bytes: int = 0) -> AgentLogsResponse:
    """Return log file content for a session.

    Args:
        session_id: Agent session ID.
        tail_bytes: If > 0, return only the last N bytes of the log.
    """
    runtime_dir = _get_runtime_dir(request)
    log_path = runtime_dir / f"{session_id}.log"
    if not log_path.exists():
        raise HTTPException(status_code=404, detail=f"No log file for session '{session_id}'")
    size = log_path.stat().st_size
    offset = max(0, size - tail_bytes) if tail_bytes > 0 else 0
    content = read_log_tail(log_path, offset)
    return AgentLogsResponse(session_id=session_id, content=content, size=size)


@router.post("/agents/{session_id}/kill")
def agent_kill(session_id: str, request: Request) -> AgentKillResponse:
    """Request that an agent session be killed.

    Writes a ``.kill`` signal file that the orchestrator picks up on
    its next tick.
    """
    runtime_dir = _get_runtime_dir(request)
    sse_bus = _get_sse_bus(request)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / f"{session_id}.kill").write_text(str(time.time()))
    sse_bus.publish(
        "session_kill",
        json.dumps({"session_id": session_id}),
    )
    return AgentKillResponse(session_id=session_id, kill_requested=True)


@router.get("/agents/{session_id}/stream")
def agent_stream(session_id: str, request: Request) -> StreamingResponse:
    """SSE stream of live log output for a session."""
    runtime_dir = _get_runtime_dir(request)
    log_path = runtime_dir / f"{session_id}.log"

    async def _generate() -> AsyncGenerator[str, None]:
        yield f"data: {json.dumps({'connected': True, 'session_id': session_id})}\n\n"

        offset = 0
        idle_ticks = 0
        max_idle = 60

        while True:
            if await request.is_disconnected():
                return

            if not log_path.exists():
                idle_ticks += 1
                if idle_ticks >= max_idle:
                    yield f"data: {json.dumps({'done': True, 'reason': 'no_log_file'})}\n\n"
                    return
                await asyncio.sleep(0.5)
                continue

            size = log_path.stat().st_size
            if size <= offset:
                idle_ticks += 1
                if idle_ticks >= max_idle:
                    yield f"data: {json.dumps({'done': True, 'reason': 'idle'})}\n\n"
                    return
                await asyncio.sleep(0.5)
                continue

            chunk = read_log_tail(log_path, offset)
            offset = size
            idle_ticks = 0
            for line in chunk.splitlines():
                if line.strip():
                    yield f"data: {json.dumps({'line': line})}\n\n"

            await asyncio.sleep(0.5)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Bulletin board
# ---------------------------------------------------------------------------


@router.post("/bulletin", status_code=201)
def post_bulletin(body: BulletinPostRequest, request: Request) -> BulletinMessageResponse:
    """Append a message to the bulletin board."""
    bulletin = _get_bulletin(request)
    msg = BulletinMessage(
        agent_id=body.agent_id,
        type=body.type,
        content=body.content,
        cell_id=body.cell_id,
    )
    stored = bulletin.post(msg)

    # Broadcast to SSE bus
    _get_sse_bus(request).publish(
        "bulletin",
        json.dumps(
            {
                "agent_id": stored.agent_id,
                "type": stored.type,
                "content": stored.content,
                "timestamp": stored.timestamp,
                "cell_id": stored.cell_id,
            }
        ),
    )

    return BulletinMessageResponse(
        agent_id=stored.agent_id,
        type=stored.type,
        content=stored.content,
        timestamp=stored.timestamp,
        cell_id=stored.cell_id,
    )


@router.get("/bulletin")
def get_bulletin(request: Request, since: float = 0.0) -> list[BulletinMessageResponse]:
    """Get bulletin messages since a given timestamp."""
    messages = _get_bulletin(request).read_since(since)
    return [
        BulletinMessageResponse(
            agent_id=m.agent_id,
            type=m.type,
            content=m.content,
            timestamp=m.timestamp,
            cell_id=m.cell_id,
        )
        for m in messages
    ]


# ---------------------------------------------------------------------------
# Direct channel (agent-to-agent queries)
# ---------------------------------------------------------------------------


@router.post("/channel/query", status_code=201)
def post_channel_query(body: ChannelQueryRequest, request: Request) -> ChannelQueryResponse:
    """Post a coordination query targeted at an agent or role."""
    q = _get_direct_channel(request).post_query(
        sender_agent=body.sender_agent,
        topic=body.topic,
        content=body.content,
        target_agent=body.target_agent,
        target_role=body.target_role,
        ttl_seconds=body.ttl_seconds,
    )
    return ChannelQueryResponse(
        id=q.id,
        sender_agent=q.sender_agent,
        topic=q.topic,
        content=q.content,
        target_agent=q.target_agent,
        target_role=q.target_role,
        timestamp=q.timestamp,
        expires_at=q.expires_at,
        resolved=q.resolved,
    )


@router.post(
    "/channel/{query_id}/respond",
    status_code=201,
    responses={404: {"description": "Query not found"}},
)
def post_channel_response(query_id: str, body: ChannelResponseRequest, request: Request) -> ChannelResponseResponse:
    """Respond to a channel query."""
    r = _get_direct_channel(request).post_response(
        query_id=query_id,
        responder_agent=body.responder_agent,
        content=body.content,
    )
    if r is None:
        raise HTTPException(status_code=404, detail=f"Query '{query_id}' not found")
    return ChannelResponseResponse(
        id=r.id,
        query_id=r.query_id,
        responder_agent=r.responder_agent,
        content=r.content,
        timestamp=r.timestamp,
    )


@router.get("/channel/queries")
def get_channel_queries(
    request: Request, agent_id: str | None = None, role: str | None = None
) -> list[ChannelQueryResponse]:
    """Get pending queries, optionally filtered by agent_id or role."""
    queries = _get_direct_channel(request).get_pending_queries(agent_id=agent_id, role=role)
    return [
        ChannelQueryResponse(
            id=q.id,
            sender_agent=q.sender_agent,
            topic=q.topic,
            content=q.content,
            target_agent=q.target_agent,
            target_role=q.target_role,
            timestamp=q.timestamp,
            expires_at=q.expires_at,
            resolved=q.resolved,
        )
        for q in queries
    ]


@router.get(
    "/channel/{query_id}/responses",
)
def get_channel_responses(query_id: str, request: Request) -> list[ChannelResponseResponse]:
    """Get all responses for a channel query."""
    responses = _get_direct_channel(request).get_responses(query_id)
    return [
        ChannelResponseResponse(
            id=r.id,
            query_id=r.query_id,
            responder_agent=r.responder_agent,
            content=r.content,
            timestamp=r.timestamp,
        )
        for r in responses
    ]
