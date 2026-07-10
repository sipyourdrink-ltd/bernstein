"""Core AgentSpawner class and prompt rendering utilities."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import logging
import re
import shutil
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bernstein.adapters.base import RateLimitError, SpawnError, SpawnResult
from bernstein.adapters.plugin_sdk import (
    SAMPLING_PARAM_KEYS,
    SamplingParamsRefusal,
    ensure_sampling_params_supported,
)
from bernstein.adapters.registry import adapter_name_for_provider, get_adapter
from bernstein.adapters.skills_injector import inject_skills
from bernstein.agents.registry import AgentRegistry, get_registry
from bernstein.bridges.base import AgentState, BridgeError, RuntimeBridge, SpawnRequest
from bernstein.core.agents.adapter_health import AdapterHealthMonitor
from bernstein.core.agents.container import ContainerConfig, ContainerError, ContainerManager
from bernstein.core.agents.heartbeat import HeartbeatMonitor
from bernstein.core.agents.in_process_agent import InProcessAgent
from bernstein.core.agents.response_style import (
    ResponseStyleTemplateError,
    addendum_sha256,
    render_style_addendum,
    resolve_response_style,
)
from bernstein.core.agents.spawn_errors import (
    ModelNotConfiguredError,
    RetryStrategy,
    classify_spawn_error,
)
from bernstein.core.agents.spawn_rate_limiter import SpawnRateLimiter, SpawnRateLimitExceeded

# Import sub-module functions
from bernstein.core.agents.spawner_merge import (
    finalize_agent_trace,
    merge_and_cleanup_worktree,
    merge_worktree_branch,
    reap_container,
    reap_in_process,
    reap_openclaw,
    reap_subprocess,
)
from bernstein.core.agents.spawner_merge import (
    reap_completed_agent as _reap_completed_agent,
)
from bernstein.core.agents.spawner_merge import (
    update_trace_outcome as _update_trace_outcome,
)
from bernstein.core.agents.spawner_prompt_cache import mark_cacheable_prefix
from bernstein.core.agents.spawner_sandbox_session import (
    SandboxExecHandle,
    cancel_session_exec,
    submit_session_exec,
    write_prompt_to_session,
)
from bernstein.core.agents.spawner_warm_pool import (
    _CLAUDE_TIER_MODELS,
    _coerce_model_for_non_claude_adapter,
    _select_batch_config,
    _should_use_router,
)
from bernstein.core.agents.spawner_worktree import (
    cleanup_worktree as _cleanup_worktree,
)
from bernstein.core.agents.spawner_worktree import (
    prune_orphan_worktrees as _prune_orphan_worktrees,
)
from bernstein.core.agents.spawner_worktree import (
    release_warm_pool_slot,
    worktree_manager_for_repo,
)
from bernstein.core.context import TaskContextBuilder
from bernstein.core.context_recommendations import RecommendationEngine
from bernstein.core.defaults import SPAWN
from bernstein.core.lessons import gather_lessons_for_context
from bernstein.core.lifecycle import transition_agent
from bernstein.core.models import (
    AbortReason,
    AgentBackend,
    AgentSession,
    IsolationMode,
    ModelConfig,
    Task,
    TransitionReason,
)
from bernstein.core.orchestrator import ShutdownInProgress
from bernstein.core.prometheus import (
    agent_spawn_duration,
    sandbox_exec_count_total,
    sandbox_session_created_total,
)
from bernstein.core.router import ProviderHealthStatus, RouterError, TierAwareRouter
from bernstein.core.sandbox import DockerSandbox, spawn_in_sandbox
from bernstein.core.team_state import TeamStateStore
from bernstein.core.traces import AgentTrace, TraceStore, new_trace
from bernstein.core.worktree import WorktreeError, WorktreeManager, WorktreeSetupConfig
from bernstein.core.worktree_claude_md import write_claude_md
from bernstein.plugins.manager import get_plugin_manager
from bernstein.templates.renderer import TemplateError, render_role_prompt

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable

    from bernstein.adapters.base import CLIAdapter
    from bernstein.agents.catalog import CatalogAgent, CatalogRegistry
    from bernstein.core.agency_loader import AgencyAgent
    from bernstein.core.agents.warm_pool import PoolSlot, WarmPool
    from bernstein.core.bulletin import BulletinBoard
    from bernstein.core.git_ops import MergeResult
    from bernstein.core.knowledge.task_graph import TaskGraph
    from bernstein.core.mcp_manager import MCPManager
    from bernstein.core.mcp_registry import MCPRegistry
    from bernstein.core.resource_limits import ResourceLimits
    from bernstein.core.sandbox.backend import SandboxBackend, SandboxSession
    from bernstein.core.sandbox.manifest import WorkspaceManifest
    from bernstein.core.workspace import Workspace

# ---------------------------------------------------------------------------
# Module-level file cache (mtime-keyed, automatically invalidates on change)
# ---------------------------------------------------------------------------
_FILE_CACHE: dict[str, tuple[float, str]] = {}
_DIR_CACHE: dict[str, tuple[float, list[str]]] = {}

# Serializes every sandbox lifecycle audit append across threads.
#
# AuditLog has no internal locking: each instance recovers the chain tail
# from disk in __init__ and appends with that prev_hmac. Sandbox events are
# emitted concurrently - session_create/exec_start on the spawn thread,
# exec_end/session_destroy on per-agent exec-done callback threads - so
# unserialized appends let two writers recover the same tail and write
# sibling records, forking the HMAC chain and breaking verify() for the
# whole daily log. Module-level (not per-spawner) so multiple spawner
# instances in one process share the same critical section.
_SANDBOX_AUDIT_LOCK = threading.Lock()


def _read_cached(path: Path) -> str:
    """Return file contents, re-reading only when mtime changes.

    Args:
        path: File to read.

    Returns:
        File contents, or empty string if the file does not exist.
    """
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _FILE_CACHE.pop(key, None)
        return ""
    cached = _FILE_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    content = path.read_text(encoding="utf-8")
    _FILE_CACHE[key] = (mtime, content)
    return content


def _list_subdirs_cached(path: Path) -> list[str]:
    """Return sorted list of immediate subdirectory names, cached by mtime.

    Args:
        path: Directory to list.

    Returns:
        Sorted subdirectory names, or empty list if path is not a directory.
    """
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _DIR_CACHE.pop(key, None)
        return []
    cached = _DIR_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    names = sorted(d.name for d in path.iterdir() if d.is_dir())
    _DIR_CACHE[key] = (mtime, names)
    return names


logger = logging.getLogger(__name__)


def _sanitise_for_log(value: str) -> str:
    """Strip CR/LF from ``value`` so attacker-controlled input cannot
    inject fake log lines.

    Used at every log site that touches data read out of the pending
    pushes file or subprocess stderr (CodeQL/Sonar py/log-injection
    S5145). Keep this function cheap and side-effect-free - it is
    called inside the spawner hot path.
    """
    return value.replace("\r", "").replace("\n", "") if value else value


# ---------------------------------------------------------------------------
# Error-aware spawn-failure extraction
# ---------------------------------------------------------------------------
# Ground truth: work/bernstein/proofs/d2/minimax/FAIL-NOTE.md. Adapter
# fast-exit probes (``CLIAdapter._probe_fast_exit`` in adapters/base.py)
# raise a ``SpawnError``/``RateLimitError`` whose message embeds only the
# LAST LINE of the runner's log (``tail_lines[-1]``). In the D2 MiniMax
# incident, the openai_agents runner actually died on
# ``BadRequestError: 400 ... does not support max tokens > 196608``, but
# the log's last line was a benign, unrelated SDK tracing warning
# (``OPENAI_API_KEY is not set, skipping trace export``) - the real error
# sat further up in the per-session runtime log. That masking happened
# across 7 run attempts before the real defect was found by hand.
#
# Fixing the extraction inside adapters/base.py is out of scope for this
# change (file-ownership boundary - see PR description), so this
# re-derives a full, error-aware failure reason downstream, in the
# spawner's own exception handler, by independently re-reading the same
# per-session log the adapter wrote (``<spawn_cwd>/.sdd/runtime/
# <session_id>.log`` - see e.g. adapters/openai_agents.py's ``log_path``
# construction) rather than trusting the already-truncated exception
# message.
_TRACEBACK_HEADER = "Traceback (most recent call last):"
_ERROR_LEVEL_RE = re.compile(r"\b(ERROR|CRITICAL)\b")
_EXCEPTION_CLASS_RE = re.compile(r"\b\w+(?:Error|Exception)\b")
_HTTP_STATUS_RE = re.compile(r"\b[45]\d{2}\b")
_SPAWN_EXIT_CODE_RE = re.compile(r"exited early with code (-?\d+)")
_FAILURE_REASON_MAX_CHARS = 4000
_FAILURE_REASON_FALLBACK_LINES = 10


def extract_error_aware_reason(log_text: str, max_chars: int = _FAILURE_REASON_MAX_CHARS) -> str:
    """Extract the LAST genuine error record from a runner's log text.

    Scans (in priority order) for: the last ``Traceback (most recent call
    last):`` block through to its final exception line; failing that, the
    last line matching an ERROR/CRITICAL log level, an exception-class
    pattern (``\\w+Error``/``Exception``), or an HTTP 4xx/5xx status code
    mention. This deliberately does NOT just grab the log's last line -
    that naive approach is the exact masking bug this function replaces
    (see module docstring above and FAIL-NOTE.md).

    Args:
        log_text: Full contents of the runner's log (stdout/stderr
            concatenated, or a per-session ``.sdd/runtime/<id>.log``).
        max_chars: Cap on the returned text, measured from the start of
            the matched error record (not a truncation of the message
            body - it's a generous ceiling so pathological logs can't
            balloon a caller's log line without limit).

    Returns:
        The full matched error text (traceback or multi-line block from
        the last matching error line to end of log), capped at
        ``max_chars``. When no error pattern is found anywhere in the
        log, returns the last ``_FAILURE_REASON_FALLBACK_LINES`` lines,
        clearly prefixed with "(no error pattern found, showing last N
        lines)" so callers can tell a fallback from a real match.
    """
    if not log_text or not log_text.strip():
        return "(no error pattern found, showing last 10 lines): <log empty or unavailable>"

    lines = log_text.splitlines()

    # 1. Traceback blocks are the most authoritative signal - prefer the
    #    LAST one (a runner may log an earlier, recovered exception too).
    traceback_starts = [i for i, line in enumerate(lines) if line.strip() == _TRACEBACK_HEADER]
    if traceback_starts:
        start = traceback_starts[-1]
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].strip() == "":
                end = j
                break
        block = "\n".join(lines[start:end]).strip()
        if block:
            return block[:max_chars]

    # 2. Otherwise, find the LAST line matching an ERROR/CRITICAL level,
    #    an exception-class name, or an HTTP 4xx/5xx status code, and
    #    return everything from there to the end of the log (full
    #    multi-line error body, e.g. an HTTP error response payload that
    #    follows the status-code line).
    match_idx = None
    for i, line in enumerate(lines):
        if _ERROR_LEVEL_RE.search(line) or _EXCEPTION_CLASS_RE.search(line) or _HTTP_STATUS_RE.search(line):
            match_idx = i
    if match_idx is not None:
        block = "\n".join(lines[match_idx:]).strip()
        if block:
            return block[:max_chars]

    # 3. No error pattern anywhere in the log - fall back to the last N
    #    lines, clearly labeled as a fallback (never silently equal to
    #    just the last line, which is the bug being fixed here).
    tail = "\n".join(lines[-_FAILURE_REASON_FALLBACK_LINES:]).strip()
    return f"(no error pattern found, showing last {_FAILURE_REASON_FALLBACK_LINES} lines)\n{tail}"[:max_chars]


def _diagnose_spawn_failure(
    session_id: str,
    spawn_cwd: Path,
    adapter_name: str,
    exc: Exception,
) -> str:
    """Re-derive a full, error-aware failure reason for a failed spawn attempt.

    Independently re-reads the runner's per-session log
    (``<spawn_cwd>/.sdd/runtime/<session_id>.log`` and its
    ``.stderr.log`` sibling, when present) and runs
    :func:`extract_error_aware_reason` over it, instead of trusting
    ``str(exc)`` - which, for adapters that raise via
    ``CLIAdapter._probe_fast_exit`` (adapters/base.py), only ever embeds
    the log's last line. Emits a WARNING with the agent id, exit context,
    the extracted reason, and the log file path so a human can jump
    straight to the full session log.

    Args:
        session_id: Agent session id - also the per-session log's stem.
        spawn_cwd: Worktree cwd the adapter spawned into.
        adapter_name: Adapter name, for the warning log line.
        exc: The exception raised by the failed spawn attempt.

    Returns:
        The error-aware failure reason, or ``str(exc)`` when no
        per-session log file can be found on disk.
    """
    log_path = spawn_cwd / ".sdd" / "runtime" / f"{session_id}.log"
    stderr_path = log_path.with_suffix(".stderr.log")

    log_text_parts: list[str] = []
    found_path: Path | None = None
    for candidate in (log_path, stderr_path):
        try:
            log_text_parts.append(candidate.read_text(encoding="utf-8", errors="replace"))
            found_path = found_path or candidate
        except OSError:
            continue

    if not log_text_parts:
        return str(exc)

    reason = extract_error_aware_reason("\n".join(log_text_parts))

    exit_code_match = _SPAWN_EXIT_CODE_RE.search(str(exc))
    exit_context = f"exit_code={exit_code_match.group(1)}" if exit_code_match else "exit_code=unknown"

    logger.warning(
        "Spawn failure reason extracted for agent %s (adapter=%s, %s): %s | log=%s",
        session_id,
        adapter_name,
        exit_context,
        reason[:2000],
        found_path,
    )

    return reason


def _render_signal_check(session_id: str) -> str:
    """Return signal-check instructions to append to every agent's system prompt.

    Args:
        session_id: The session ID assigned to this agent.

    Returns:
        Markdown block instructing the agent to poll signal files.
    """
    return (
        "\n## Signal files (check periodically)\n"
        "Every 60 seconds, check for orchestrator signals:\n"
        "```bash\n"
        f"cat .sdd/runtime/signals/{session_id}/WAKEUP 2>/dev/null\n"
        f"cat .sdd/runtime/signals/{session_id}/SHUTDOWN 2>/dev/null\n"
        "```\n"
        "If **SHUTDOWN** exists:\n"
        "```bash\n"
        'git add -A && git commit -m "[WIP] <task title>" 2>/dev/null || true\n'
        "exit 0\n"
        "```\n"
        "If **WAKEUP** exists: read it, address the concern, then continue working.\n"
    )


def _render_auth_section(token_path: Path) -> str:
    """Return authentication instructions to inject into every agent's prompt.

    The token file path is referenced by path rather than embedding the raw
    token so that credentials do not appear in prompt logs.

    The path is coerced to absolute form so the ``cat`` examples resolve
    correctly even when the agent's spawn cwd differs from the orchestrator
    workdir (the worktree case - see #1261). ``resolve(strict=False)``
    keeps the call cheap when the file has not yet been written and never
    fails on missing intermediates.

    Args:
        token_path: Path to the session-scoped JWT token file (mode 0600).

    Returns:
        Markdown block instructing the agent to authenticate all requests.
    """
    absolute = token_path if token_path.is_absolute() else token_path.resolve(strict=False)
    return (
        "\n## Task Server Authentication\n"
        "Your agent token is stored at this absolute path (do NOT print or "
        "log its contents):\n"
        f"```\n{absolute}\n```\n"
        "Include this header in **all** task server requests - the path is "
        "absolute, so it works regardless of your current shell directory:\n"
        "```bash\n"
        f'-H "Authorization: Bearer $(cat {absolute})"\n'
        "```\n"
        "**Command-form contract - read this before your first request.** Your "
        "`run_command` tool accepts two call forms:\n"
        "- a single command **STRING** (e.g. "
        f'`run_command("curl ... -H \\"Authorization: Bearer $(cat {absolute})\\" ...")`)'
        "\n  → this runs via a shell, so `$(...)`, `$VAR`, pipes, and `&&` all expand normally.\n"
        "- an **argv LIST** (e.g. "
        f'`run_command(["curl", "-H", "Authorization: Bearer $(cat {absolute})", ...])`)'
        "\n  → this execs the process directly with NO shell involved, so `$(...)` and "
        "`$VAR` are never expanded. The literal text (including the dollar sign, "
        "parens, and path) is sent as-is, curl still exits 0, and the task server "
        "returns 401. There is no visible error other than the HTTP status - it "
        "looks like success unless you check it.\n\n"
        "**Every curl below MUST be invoked with `run_command` in the single-STRING "
        "form whenever it uses `$(...)`, `$VAR`, a pipe, or `&&`.** If you are not "
        "sure which form your tool call used, re-issue the request as one string "
        "and re-check the status code.\n\n"
        "**Do not use the `read_file` tool to obtain your token.** `read_file` is "
        "confined to your own worktree, and the token file lives outside it - the "
        "call will fail with a workdir-escape error every time, regardless of the "
        "token's validity. The only supported way to read the token is through "
        "`run_command` in string form running `cat <token-path>` (or interpolating "
        "it into the curl command directly, as shown below).\n\n"
        "**Always check the HTTP status, not just the command's exit code.** curl "
        "exits 0 even on a 401 or 500 - the failure is only visible in the response "
        "body/status line. Add `-w '\\n%{http_code}'` to every call and treat any "
        "status outside 200-299 as a failure: stop, re-verify you used the string "
        "form and the correct token path, and retry. Do not report a task as done, "
        "or give up, based solely on a non-2xx response without first confirming "
        "the command form was correct.\n"
        "Example - creating a subtask (pass the whole line to `run_command` as ONE string):\n"
        "```bash\n"
        f"curl -sS -w '\\n%{{http_code}}' -X POST http://127.0.0.1:8052/tasks \\\n"
        f'  -H "Authorization: Bearer $(cat {absolute})" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        '  -d \'{"title": "...", "role": "backend", "description": "..."}\'\n'
        "```\n"
        "Example - marking a task complete (pass the whole line to `run_command` as ONE string):\n"
        "```bash\n"
        f"curl -sS -w '\\n%{{http_code}}' -X POST http://127.0.0.1:8052/tasks/<TASK_ID>/complete \\\n"
        f'  -H "Authorization: Bearer $(cat {absolute})" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        '  -d \'{"result_summary": "Done"}\'\n'
        "```\n"
        "If the token file is unreadable for any reason, fall back to the\n"
        "`BERNSTEIN_AUTH_TOKEN` environment variable, which is exported into\n"
        "your shell:\n"
        "```bash\n"
        '-H "Authorization: Bearer $BERNSTEIN_AUTH_TOKEN"\n'
        "```\n"
    )


def _health_check_interval(tasks: list[Task]) -> int:
    """Derive health-check cron interval (minutes) from task batch duration.

    Maps estimated_minutes to a polling frequency:

    - ``< 15`` min (simple tasks): check every **3** minutes
    - ``> 60`` min (complex tasks): check every **10** minutes
    - Otherwise: check every **5** minutes

    Args:
        tasks: Batch of tasks assigned to the agent.

    Returns:
        Cron interval in minutes.
    """
    if not tasks:
        return 5
    max_est = max((t.estimated_minutes for t in tasks), default=30)
    if max_est > 60:
        return 10
    if max_est < 15:
        return 3
    return 5


def _inject_scheduled_tasks(
    workdir: Path,
    session_id: str,
    health_interval_minutes: int = 5,
) -> None:
    """Write ``.claude/scheduled_tasks.json`` with a recurring health-check cron task.

    Claude Code's scheduled-task system fires the cron prompt on the given
    interval inside a running agent session.  This enables agent-internal
    monitoring: the agent self-evaluates its progress and reports via MCP
    rather than the orchestrator guessing from external heartbeat signals.

    The cron task survives context compaction - Claude Code re-fires it even
    after the context window is compressed.

    Args:
        workdir: Working directory for the agent (worktree root).
        session_id: Agent session identifier (used as the cron task ID prefix).
        health_interval_minutes: Cron interval in minutes (1-59).
    """
    tasks_path = workdir / ".claude" / "scheduled_tasks.json"
    tasks_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "tasks": [
            {
                "id": f"hc-{session_id[:8]}",
                "cron": f"*/{health_interval_minutes} * * * *",
                "prompt": (
                    "Self-check: Are you making progress on your assigned tasks? "
                    "If stuck for >2 minutes, use the bernstein MCP tool to report your status. "
                    "If token budget is >80% consumed, commit your work and wrap up."
                ),
                "createdAt": int(time.time() * 1000),
                "recurring": True,
            }
        ]
    }
    try:
        tasks_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        logger.debug(
            "Injected scheduled health-check task (interval=%dm) → %s",
            health_interval_minutes,
            tasks_path,
        )
    except OSError as exc:
        logger.debug("Failed to write scheduled_tasks.json for %s: %s", session_id, exc)


def _extract_tags_from_tasks(tasks: list[Task]) -> list[str]:
    """Derive lesson-retrieval tags from a batch of tasks.

    Uses the role and significant title words as tags.

    Args:
        tasks: Batch of tasks.

    Returns:
        List of lowercase tags for lesson lookup.
    """
    stop_words = {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "not",
        "no",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "than",
        "too",
        "very",
        "just",
        "into",
        "out",
        "up",
        "down",
        "over",
        "this",
        "that",
        "it",
        "its",
    }
    tags: set[str] = set()
    for task in tasks:
        tags.add(task.role.lower())
        for word in task.title.lower().split():
            cleaned = word.strip("-_.,;:!?()[]{}\"'`#")
            if len(cleaned) > 2 and cleaned not in stop_words:
                tags.add(cleaned)
    return sorted(tags)


def _render_predecessor_context(tasks: list[Task], task_graph: TaskGraph | None) -> str:
    """Build a context section from INFORMS/TRANSFORMS predecessor outputs.

    Args:
        tasks: Batch of tasks being assigned.
        task_graph: Optional task graph for looking up typed edges.

    Returns:
        Markdown section with predecessor results, or empty string.
    """
    if task_graph is None:
        return ""

    lines: list[str] = []
    for task in tasks:
        pred_ctx = task_graph.predecessor_context(task.id)
        for item in pred_ctx:
            summary = item["result_summary"]
            if not summary:
                continue
            edge_label = "informed by" if item["edge_type"] == "informs" else "transforms output of"
            lines.append(f"- **{item['title']}** ({edge_label}): {summary}")

    if not lines:
        return ""
    return (
        "\n## Predecessor context\n"
        "The following completed tasks provide context for your work:\n" + "\n".join(lines) + "\n"
    )


def _render_batch_prompt(task: Task) -> str:
    """Build a /batch prompt for homogeneous large-scale refactors.

    When a task declares ``execution_mode: batch``, Bernstein spawns a single
    Claude Code agent with this prompt.  Claude Code's built-in ``/batch``
    skill handles decomposition into 5-30 independent units, spawns worktree
    subagents in parallel, runs tests and opens a PR per unit, and tracks
    progress internally.  This is far more efficient than Bernstein spawning
    N separate agents for mechanical changes (renames, migrations, API updates).

    The outer agent needs ``--max-turns 200`` (set by the caller) to cover the
    full research -> decompose -> spawn -> track lifecycle.

    Args:
        task: The batch-mode task to delegate.

    Returns:
        Prompt string starting with ``/batch`` that triggers the batch skill.
    """
    lines: list[str] = [f"/batch {task.description}"]
    if task.owned_files:
        lines.append(f"\nAffected paths: {', '.join(task.owned_files)}")
    lines.extend(
        (
            f"\nTask ID for completion reporting: {task.id}",
            "\nAfter all batch units are complete, run:\n"
            f"curl -sS -X POST http://127.0.0.1:8052/tasks/{task.id}/complete "
            f'-H "Content-Type: application/json" '
            f'-d \'{{"result_summary": "Batch complete: {task.title}"}}\'',
        )
    )
    return "\n".join(lines)


def _load_persistent_memory(sdd_dir: Path, lesson_tags: list[str]) -> str:
    """Load persistent memory from SQLite store."""
    db_path = sdd_dir / "memory" / "memory.db"
    if not db_path.exists():
        return ""
    try:
        from bernstein.core.memory.sqlite_store import SQLiteMemoryStore

        store = SQLiteMemoryStore(db_path)
        memories = store.get_relevant(lesson_tags, limit=10)
        if not memories:
            return ""
        lines = ["## Persistent Memory\nRelevant conventions and architectural decisions:"]
        for m in memories:
            lines.append(f"- [{m.type.upper()}] {m.content}")
        return "\n".join(lines) + "\n"
    except Exception as mem_exc:
        logger.debug("Failed to fetch persistent memory: %s", mem_exc)
        return ""


def _build_rag_context(tasks: list[Task], workdir: Path, spawner_config: Any | None) -> str:
    """Build RAG-based smart context injection."""
    try:
        from bernstein.core.rag import CodebaseIndexer
        from bernstein.core.section_dedup import deduplicate_section

        indexer = CodebaseIndexer(workdir)
        if indexer.file_count() == 0:
            return ""
        query = " ".join(t.title for t in tasks)
        rag_cfg = getattr(spawner_config, "rag", None)
        max_files = rag_cfg.max_files if rag_cfg else 5
        max_chars = (rag_cfg.max_tokens if rag_cfg else 50000) * 4

        results = indexer.search(query, limit=max_files)
        if not results:
            return ""
        lines = ["## Relevant Code Context\nAutomatically identified relevant files via RAG:"]
        total_chars = 0
        for res in results:
            if total_chars >= max_chars:
                break
            path = Path(res["path"])
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            remaining = max_chars - total_chars
            if len(content) > remaining:
                content = content[:remaining] + "\n... (truncated)"
            lines.append(f"### {res['path']} (score: {res['score']:.2f})\n```\n{content}\n```")
            total_chars += len(content)
        return deduplicate_section("\n".join(lines) + "\n")
    except Exception as rag_exc:
        logger.debug("Smart context injection failed: %s", rag_exc)
        return ""


def _build_file_scope_context(tasks: list[Task]) -> str:
    """Build file-scope context based on owned files."""
    try:
        from bernstein.core.context_activation import activate_context_for_task

        all_owned: list[str] = []
        for t in tasks:
            all_owned.extend(t.owned_files)
        return activate_context_for_task(all_owned)
    except Exception as exc:
        logger.debug("File-scope context activation failed: %s", exc)
        return ""


def _render_prompt(
    tasks: list[Task],
    templates_dir: Path,
    workdir: Path,
    agency_catalog: dict[str, AgencyAgent] | None = None,
    spawner_config: Any | None = None,
    catalog_system_prompt: str | None = None,
    context_builder: TaskContextBuilder | None = None,
    session_id: str = "",
    bulletin_summary: str = "",
    task_graph: TaskGraph | None = None,
    token_budget: int = 0,
    meta_messages: list[str] | None = None,
    max_turns: int | None = None,
    mailbox_section: str = "",
) -> str:
    """Build the full agent prompt from role template + tasks + context.

    Uses the Jinja2-style template renderer for proper variable substitution.
    Falls back to simple string concatenation if rendering fails.  When the
    template renderer fallback is used, the agency catalog is checked for
    roles not covered by templates/roles/.

    If *catalog_system_prompt* is provided it replaces the built-in role
    template entirely, so the spawner can inject catalog-defined personas.

    Args:
        tasks: Batch of 1-3 tasks (all same role).
        templates_dir: Root of templates/roles/ directory.
        workdir: Project working directory.
        agency_catalog: Optional Agency agent catalog for extended roles.
        spawner_config: Optional spawner config used for prompt-side limits.
        catalog_system_prompt: Optional system prompt from a catalog agent.
            When set, this replaces the template/role-based role prompt.
        context_builder: Optional TaskContextBuilder for rich context injection.
        bulletin_summary: Optional recent bulletin activity to inject as a
            team-awareness section. Empty string means no section is added.
        task_graph: Optional task graph for injecting typed-edge predecessor
            context (INFORMS / TRANSFORMS outputs).
        max_turns: Optional best-effort resolution of the agent's tool-use
            turn cap, known at the spawn call site (see
            ``AgentSpawner.spawn_for_tasks``'s resolution logic just before
            this function is called). When present, renders a static
            "## Turn budget" section so the model self-polices instead of
            exploring until ``MaxTurnsExceeded`` fires with zero output
            (see work/bernstein/m27-nudge-plan.md, Approach C MINIMAL).
            ``None`` means the caller could not resolve a value at
            prompt-build time (e.g. SDK default applies, or the resolved
            adapter doesn't use a turn-capped runner) - the section is
            skipped in that case, not rendered with a placeholder.

    Returns:
        Complete prompt string ready for the CLI adapter.
        Cache block annotation is available via mark_cacheable_prefix()
        vs dynamic, so adapters can apply provider-specific caching.
    """
    role = tasks[0].role

    # Build task descriptions block
    task_lines: list[str] = []
    for i, task in enumerate(tasks, 1):
        task_lines.extend((f"### Task {i}: {task.title} (id={task.id})", task.description))
        if task.owned_files:
            task_lines.append(f"Files: {', '.join(task.owned_files)}")
        task_lines.append("")
    task_block = "\n".join(task_lines)

    # Project context from .sdd/project.md if it exists
    project_md = workdir / ".sdd" / "project.md"
    project_context = _read_cached(project_md)

    # Completion instructions with concrete curl commands and retry logic.
    # The server may briefly restart during hot-reload (evolve mode), so
    # agents must retry on transient connection errors (--retry-connrefused).
    # Do NOT use --retry-all-errors: it retries 4xx (e.g. 409 Conflict),
    # causing infinite loops when task state has changed.
    completion_cmds = "\n".join(
        f"curl -s -w '\\n%{{http_code}}' --retry 3 --retry-delay 2 --retry-connrefused "
        f"-X POST http://127.0.0.1:8052/tasks/{t.id}/complete "
        f'-H "Content-Type: application/json" '
        f'-d \'{{"result_summary": "Completed: {t.title}"}}\''
        for t in tasks
    )
    instructions = (
        f"Complete these tasks. When ALL are done:\n\n"
        f"**Step 1: Commit your changes**\n"
        f"```bash\n"
        f'git add -A && git commit -m "feat: <brief summary of what you did>"\n'
        f"```\n\n"
        f"**Step 2: Mark tasks complete on the task server**\n"
        f"```bash\n{completion_cmds}\n```\n\n"
        f"**Important:** Only retry on connection refused / network errors. "
        f"If the server returns HTTP 409 or any other 4xx error, do NOT retry - "
        f"the task state has changed and retrying will not help. Just exit.\n\n"
        f"**Step 3: Exit**"
    )

    # Available roles from templates directory
    available_roles = ""
    if templates_dir.is_dir():
        available_roles = ", ".join(_list_subdirs_cached(templates_dir))

    # Specialist agents from agency catalog
    specialist_block = ""
    if agency_catalog and role == "manager":
        specialists: list[str] = [
            f"- **{agent.name}** ({agent.role}): {agent.description}"
            for agent in sorted(agency_catalog.values(), key=lambda a: a.role)
        ]
        if specialists:
            specialist_block = (
                "\n\n## Available specialist agents (from Agency catalog)\n"
                "When creating tasks, prefer assigning to a specialist role if one matches.\n"
                "Fall back to generic roles (backend, qa, etc.) if no specialist fits.\n\n" + "\n".join(specialists)
            )

    # Build rich task context via TaskContextBuilder
    rich_context = ""
    if context_builder is not None:
        try:
            rich_context = context_builder.build_context(tasks)
        except Exception as exc:
            logger.warning("TaskContextBuilder failed, skipping rich context: %s", exc)

    # Build template context for renderer
    context = {
        "GOAL": tasks[0].title,
        "TASK_DESCRIPTION": task_block,
        "PROJECT_STATE": project_context,
        "AVAILABLE_ROLES": available_roles,
        "INSTRUCTIONS": instructions,
        "SPECIALISTS": specialist_block,
    }

    # Use catalog system prompt when available (Agency specialist prompt),
    # otherwise fall back to role template or built-in default.
    #
    # The manager role is exempt from this substitution even if a catalog
    # system prompt is set: templates/roles/manager.md carries the
    # task-server task-creation instructions (POST /tasks schema, decomposition
    # steps) that no catalog persona defines. Letting a catalog prompt replace
    # the manager template silently breaks decomposition - the manager agent
    # would have a persona but no idea how to create child tasks.
    if catalog_system_prompt and role != "manager":
        role_prompt = catalog_system_prompt
    else:
        try:
            role_prompt = render_role_prompt(role, context, templates_dir=templates_dir)
        except (FileNotFoundError, TemplateError) as exc:
            logger.warning(
                "Template render failed for role %s (templates_dir=%s), using fallback: %s",
                role,
                templates_dir,
                exc,
            )
            role_prompt = _render_fallback(role, templates_dir, agency_catalog)

    sdd_dir = workdir / ".sdd"
    lesson_tags = _extract_tags_from_tasks(tasks)
    lesson_context = gather_lessons_for_context(sdd_dir, lesson_tags)
    persistent_memory_context = _load_persistent_memory(sdd_dir, lesson_tags)
    smart_context = _build_rag_context(tasks, workdir, spawner_config)
    file_scope_context = _build_file_scope_context(tasks)

    # Assemble final prompt
    from bernstein.core.section_dedup import deduplicate_section

    sections = [role_prompt]
    if specialist_block:
        sections.append(specialist_block)
    sections.append(f"\n## Assigned tasks\n{task_block}")
    if lesson_context:
        sections.append(f"\n{lesson_context}\n")
    if persistent_memory_context:
        sections.append(deduplicate_section(f"\n{persistent_memory_context}\n"))
    if smart_context:
        sections.append(f"\n{smart_context}\n")
    if rich_context:
        sections.append(f"\n{rich_context}\n")
    if file_scope_context:
        sections.append(deduplicate_section(f"\n## File-scope context\n{file_scope_context}\n"))
    # Parent context inheritance: inject parent's context summary
    # when a task was created from decomposing a larger parent task.
    parent_ctx_parts = [t.parent_context for t in tasks if t.parent_context]
    if parent_ctx_parts:
        sections.append(
            "\n## Parent context (inherited)\n"
            "This task was decomposed from a parent task. The parent agent gathered "
            "the following context:\n" + "\n".join(parent_ctx_parts) + "\n"
        )
    predecessor_ctx = _render_predecessor_context(tasks, task_graph)
    if predecessor_ctx:
        sections.append(predecessor_ctx)
    if bulletin_summary:
        sections.append(
            deduplicate_section(
                f"\n## Team awareness\n"
                f"Other agents are working in parallel. Recent activity:\n{bulletin_summary}\n\n"
                f"If you need to create a shared utility, check if it already exists first.\n"
                f"If you define an API endpoint, use consistent naming with existing endpoints.\n"
            )
        )
    # Coordination mailbox (#2357): typed messages other workers addressed to
    # these tasks, rendered deterministically from the mailbox journal so
    # every adapter type receives byte-identical context.
    if mailbox_section and mailbox_section.strip():
        sections.append(deduplicate_section(mailbox_section))
    try:
        rec_engine = RecommendationEngine(workdir)
        rec_engine.build()
        rec_section = rec_engine.render_for_prompt(role, max_chars=2000)
        if rec_section:
            sections.append(f"\n{rec_section}\n")
    except Exception as exc:
        logger.debug("Recommendation rendering failed: %s", exc)
    if project_context:
        sections.append(deduplicate_section(f"\n## Project context\n{project_context}\n"))
    if token_budget > 0:
        if token_budget >= 1_000_000:
            budget_hint = f"~{token_budget // 1_000_000}M"
        elif token_budget >= 1_000:
            budget_hint = f"~{token_budget // 1_000}K"
        else:
            budget_hint = str(token_budget)
        sections.append(
            deduplicate_section(
                f"\n## Token budget\n"
                f"You have {budget_hint} tokens for this task. Plan your work accordingly - "
                f"focus on the task, avoid unnecessary exploration, and wrap up promptly.\n"
            )
        )
    sections.append(deduplicate_section(f"\n## Instructions\n{instructions}\n"))
    if session_id:
        try:
            heartbeat_instructions = HeartbeatMonitor(workdir).inject_heartbeat_instructions(session_id)
            sections.append(
                deduplicate_section(
                    "\n## Heartbeat (background)\n"
                    "Run this in the background to report progress:\n"
                    f"```bash\n{heartbeat_instructions}\n```\n"
                )
            )
        except Exception as exc:
            logger.debug("Heartbeat instructions unavailable: %s", exc)
    if session_id:
        sections.append(deduplicate_section(_render_signal_check(session_id)))

    if meta_messages:
        nudges_block = "\n## Operational nudges\n" + "\n".join(f"- {m}" for m in meta_messages) + "\n"
        sections.append(nudges_block)

    # Turn-budget nudge (work/bernstein/m27-nudge-plan.md, Approach C
    # MINIMAL): models spawned in tool-use loops (observed worst on MiniMax
    # M2.7-highspeed) burn their whole turn cap reading/re-verifying and
    # never write output, then hit MaxTurnsExceeded with nothing to show.
    # Since Bernstein has no live mid-run injection channel into the
    # openai-agents SDK's internal Runner.run_sync loop (see the plan doc's
    # feasibility analysis), the only buildable fix today is a STATIC
    # budget baked into the prompt at spawn time from whatever max_turns
    # value the caller could resolve before this render call. Only render
    # when a real positive value is known - a placeholder/guessed value
    # would be actively misleading.
    if max_turns is not None and max_turns > 0:
        halfway_turn = max(1, max_turns // 2)
        # near_end heuristic: 3 turns before the cap, but never below/at
        # halfway_turn (tiny caps like max_turns=4 would otherwise put
        # near_end before halfway) and never past max_turns itself (the
        # outer min enforces the cap; without it max_turns=1 rendered
        # "By turn 2" against a 1-turn budget).
        near_end_turn = min(max_turns, max(halfway_turn + 1, max_turns - 3))
        turn_budget_block = (
            "\n## Turn budget\n"
            f"You have a hard budget of {max_turns} tool-use turns for this task.\n\n"
            f"- By turn {halfway_turn} (roughly halfway): if the core task is already "
            "done, STOP - write your final summary now. Do not spend remaining turns "
            "re-reading files you've already read or re-verifying work that already "
            "passed.\n"
            f"- By turn {near_end_turn} (near your limit): if you have not yet written "
            "any code/output, you are out of time for further exploration - write "
            "SOMETHING now, even a partial/best-effort change, rather than continuing "
            "to read.\n"
            "- On your FINAL turn: your last message must be plain text summarizing "
            "what you accomplished, what remains unfinished, and any risks. Do not "
            "attempt further tool calls.\n\n"
            "STOP CONDITIONS - if any of these are true, stop immediately and write "
            "your summary:\n"
            "- All requested changes are implemented and tests pass\n"
            "- You have verified your work is correct\n"
            "- You are re-reading files you already read with no new information to "
            "gain\n"
        )
        sections.append(turn_budget_block)
        logger.info(
            "Turn budget nudge injected for session=%s: max_turns=%d halfway=%d near_end=%d",
            session_id,
            max_turns,
            halfway_turn,
            near_end_turn,
        )
    else:
        logger.info(
            "Turn budget nudge skipped for session=%s: max_turns not available at "
            "prompt-build time (resolved value=%r) - agent will not receive a turn-budget "
            "self-check section",
            session_id,
            max_turns,
        )

    # Annotate prompt sections with cache hints so adapters can apply
    # provider-specific caching (e.g. Anthropic's cache_control).
    cache_blocks = mark_cacheable_prefix(sections)

    # Cache blocks are computed but the function returns the flat string
    # for backward compatibility.  Callers that need cache hints can call
    # mark_cacheable_prefix(sections) separately.
    _ = cache_blocks  # computed for future use
    return "".join(sections)


def _render_fallback(
    role: str,
    templates_dir: Path,
    agency_catalog: dict[str, AgencyAgent] | None = None,
) -> str:
    """Fallback: read raw template, check agency catalog, or generate default.

    Args:
        role: Role name.
        templates_dir: Root of templates/roles/ directory.
        agency_catalog: Optional Agency agent catalog to check for roles
            not found in templates/roles/.

    Returns:
        Raw role prompt string without variable substitution.
    """
    template_path = templates_dir / role / "system_prompt.md"
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")

    # Check agency catalog: look for an agent whose name or role matches.
    if agency_catalog:
        agent = agency_catalog.get(role)
        if agent is None:
            # Try matching by mapped role name.
            for a in agency_catalog.values():
                if a.role == role:
                    agent = a
                    break
        if agent and agent.prompt_body:
            logger.info("Using Agency agent '%s' for role '%s'", agent.name, role)
            return agent.prompt_body

    return f"You are a {role} specialist."


class AgentSpawner:
    """Spawns short-lived CLI agents for task batches.

    Agents are spawned per-batch and expected to exit after completion.
    No long-running sessions -- see ADR-001.

    Args:
        adapter: CLI adapter for launching agent processes.
        templates_dir: Path to templates/roles/ directory.
        workdir: Project working directory.
        agent_registry: Optional agent registry for dynamic agent types.
    """

    def __init__(
        self,
        adapter: CLIAdapter,
        templates_dir: Path,
        workdir: Path,
        agent_registry: AgentRegistry | None = None,
        agency_catalog: dict[str, AgencyAgent] | None = None,
        router: TierAwareRouter | None = None,
        mcp_config: dict[str, Any] | None = None,
        mcp_registry: MCPRegistry | None = None,
        mcp_manager: MCPManager | None = None,
        catalog: CatalogRegistry | None = None,
        use_worktrees: bool = True,
        worktree_setup_config: WorktreeSetupConfig | None = None,
        workspace: Workspace | None = None,
        bulletin: BulletinBoard | None = None,
        enable_caching: bool = False,
        container_config: ContainerConfig | None = None,
        sandbox: DockerSandbox | None = None,
        max_tokens_per_task: dict[str, int] | None = None,
        role_model_policy: dict[str, dict[str, str]] | None = None,
        runtime_bridge: RuntimeBridge | None = None,
        backend: AgentBackend = AgentBackend.SUBPROCESS,
        resource_limits: ResourceLimits | None = None,
        warm_pool: WarmPool | None = None,
        spawn_rate_limiter: SpawnRateLimiter | None = None,
        sandbox_session: SandboxSession | None = None,
        sandbox_backend: SandboxBackend | None = None,
        sandbox_manifest_factory: Callable[[], WorkspaceManifest] | None = None,
        sandbox_options: dict[str, Any] | None = None,
        sandbox_server_port: int | None = None,
        default_model: str | None = None,
    ) -> None:
        self._enable_caching = enable_caching
        # Run-level model (e.g. from ``bernstein run --model``), threaded in by
        # the orchestrator from the CLI flag / seed config. Used to coerce
        # Claude tier names (opus/sonnet/haiku) emitted by the heuristic
        # selector into a model the active non-Claude adapter actually
        # understands - see ``_coerce_model_for_non_claude_adapter``.
        self._default_model = default_model
        self._resource_limits = resource_limits
        self._adapter_cache: dict[str, CLIAdapter] = {}
        if enable_caching:
            from bernstein.adapters.caching_adapter import CachingAdapter

            adapter = CachingAdapter(adapter, workdir)
        self._adapter = adapter
        self._adapter_cache[self._adapter.name()] = self._adapter
        self._templates_dir = templates_dir
        self._workdir = workdir
        self._registry = agent_registry or get_registry(
            definitions_dir=workdir / ".sdd" / "agents" / "definitions",
            auto_reload=True,
        )
        self._agency_catalog = agency_catalog
        self._router = router
        self._mcp_config = mcp_config
        self._mcp_registry = mcp_registry
        self._mcp_manager = mcp_manager
        self._catalog = catalog
        self._max_tokens_per_task = max_tokens_per_task or {}
        self._role_model_policy = role_model_policy or {}
        self._workspace = workspace
        self._bulletin = bulletin
        self._context_builder = TaskContextBuilder(workdir)
        self._procs: dict[str, subprocess.Popen[bytes] | None] = {}
        self._shutdown_event: threading.Event | None = None
        self._agent_failure_timestamps: dict[str, float] = {}  # adapter_name -> last failure ts
        self._adapter_health = AdapterHealthMonitor()
        self._use_worktrees = use_worktrees
        self._worktree_setup_config = worktree_setup_config
        self._worktree_mgr: WorktreeManager | None = None
        self._worktree_managers: dict[Path, WorktreeManager] = {}
        if use_worktrees:
            self._worktree_mgr = WorktreeManager(workdir, setup_config=worktree_setup_config)
            self._worktree_managers[workdir.resolve()] = self._worktree_mgr
            # Clean stale worktrees from prior crashed/stopped runs
            cleaned = self._worktree_mgr.cleanup_all_stale()
            if cleaned:
                logger.info("Cleaned %d stale worktree(s) from prior run", cleaned)
        self._worktree_paths: dict[str, Path] = {}
        self._worktree_roots: dict[str, Path] = {}
        self._warm_pool = warm_pool
        self._warm_pool_entries: dict[str, PoolSlot] = {}
        # Per-repo lock to serialize pushes and prevent non-fast-forward races
        self._push_locks: dict[Path, threading.Lock] = {}
        # Per-repo lock to serialize merges and prevent concurrent index corruption.
        # Used as a fallback when no :class:`MergeQueue` has been wired in via
        # :meth:`set_merge_queue` (e.g. in unit tests that construct a bare
        # spawner).  Production callers should route through the merge queue
        # injected by the orchestrator.
        self._merge_locks: dict[Path, threading.Lock] = {}
        # Set by the orchestrator via :meth:`set_merge_queue` after construction.
        # When present, merges are serialised through the FIFO queue so the
        # dashboard can observe pending jobs and so merge-tree conflict checks
        # can be inserted on the queue's boundary ( fix).
        self._merge_queue: Any = None
        self._traces: dict[str, AgentTrace] = {}
        self._trace_store = TraceStore(workdir / ".sdd" / "traces")
        self._runtime_bridge = runtime_bridge
        self._sandbox = sandbox if sandbox is not None and sandbox.enabled else None
        self._sandbox_managers: dict[str, ContainerManager] = {}
        # oai-002 phase 1: optional SandboxBackend-issued session.
        # Phase 2 (oai-002b) routes adapter exec through the session
        # via :mod:`spawner_sandbox_session` when the backend is not
        # the local worktree. Worktree-backed sessions still go through
        # the existing direct-subprocess path so the worker wrapper,
        # process-group bookkeeping, and timeout watchdog stay intact.
        self._sandbox_session: SandboxSession | None = sandbox_session
        if sandbox_session is not None:
            sandbox_session_created_total.labels(backend=getattr(sandbox_session, "backend_name", "unknown")).inc()
        # Issue #2162: per-agent sandbox sessions. When a backend plus a
        # manifest factory are attached (instead of a single pre-built
        # session), _spawn_via_sandbox_session provisions ONE session per
        # spawn and destroys it when the exec future resolves, so an exec
        # timeout that kills a container only kills that agent and
        # concurrent agents never share a single workspace clone. The
        # sandbox_session parameter above keeps working unchanged for
        # callers that pass a shared session (tests, back-compat).
        self._sandbox_backend = sandbox_backend
        self._sandbox_manifest_factory = sandbox_manifest_factory
        self._sandbox_options: dict[str, Any] = dict(sandbox_options or {})
        self._sandbox_server_port = sandbox_server_port
        # session_id -> per-spawn SandboxSession owned (and destroyed)
        # by this spawner.  Popped exactly once by _destroy_sandbox_session
        # so the exec-done callback and kill() cannot double-destroy.
        self._sandbox_owned_sessions: dict[str, SandboxSession] = {}
        # One reachability probe per spawner instance is enough - the
        # answer is a property of the Docker daemon, not of the session.
        self._sandbox_reachability_checked = False
        # session_id -> SandboxExecHandle for agents whose exec went
        # through SandboxSession.exec.  Consulted by check_alive / kill
        # so the orchestrator's lifecycle paths keep working without a
        # local subprocess PID.
        self._sandbox_exec_handles: dict[str, SandboxExecHandle] = {}
        # Container isolation
        self._container_mgr: ContainerManager | None = None
        if container_config is not None:
            try:
                self._container_mgr = ContainerManager(container_config, workdir)
            except ContainerError as exc:
                logger.warning("Container runtime unavailable, falling back to subprocess: %s", exc)

        # Backend selection
        self._backend = backend
        self._in_process: InProcessAgent | None = None
        if backend == AgentBackend.IN_PROCESS:
            pid_dir = workdir / ".sdd" / "runtime" / "pids"
            self._in_process = InProcessAgent(adapter, workdir, pid_dir=pid_dir)
            logger.info("In-process agent backend enabled (wrapping %s)", adapter.name())
        self._spawn_rate_limiter = spawn_rate_limiter or SpawnRateLimiter()

        # Zero-trust: lazy agent identity store - loaded on first use.
        # Stored as a cached property so the auth directory is not created
        # until the first agent is spawned.
        self._identity_store_instance: Any = None
        # Map session_id → token file path for cleanup on reap.
        self._agent_token_files: dict[str, Path] = {}
        # Rate-limit tracker is optionally injected by the orchestrator.
        self._rate_limit_tracker: Any = None

    @property
    def role_model_policy(self) -> dict[str, dict[str, Any]]:
        """Read-only view of the configured ``role_model_policy``.

        Exposed so callers outside this module (task_lifecycle.py's retry
        escalation, in particular) can determine whether a role has been
        pinned to a non-Claude provider/model *before* stamping a Claude
        tier name ("opus"/"sonnet") onto a retried task - see
        ``_choose_retry_escalation`` for why that matters. Returns a
        shallow copy; mutating it does not affect spawn behavior.
        """
        return self._role_model_policy.copy()

    @property
    def default_adapter_name(self) -> str:
        """Name of the spawner's default (run-level) adapter, e.g. ``claude``."""
        return self._adapter.name()

    @property
    def _identity_store(self) -> Any:
        """Return the AgentIdentityStore, creating it on first access."""
        if self._identity_store_instance is None:
            from bernstein.core.agents.agent_identity import AgentIdentityStore

            auth_dir = self._workdir / ".sdd" / "auth"
            self._identity_store_instance = AgentIdentityStore(auth_dir)
        return self._identity_store_instance

    def _issue_agent_token(self, session_id: str, role: str, task_ids: list[str]) -> Path:
        """Issue a short-lived task-scoped JWT and write it to a 0600 token file.

        The token file path is recorded in ``_agent_token_files`` for cleanup
        when the agent is reaped.

        The returned path is resolved to an absolute path (#1261) - agents
        spawn with cwd set to a git worktree under
        ``.sdd/worktrees/<session>/``, so a relative path here would resolve
        against the worktree at ``cat`` time and miss the real token that
        lives under the orchestrator's project root. The auth section
        injected into the prompt by :func:`_render_auth_section` then ends
        up pointing at a non-existent file, the agent loops on
        ``find ... -name "*.token"``, and every ``POST /tasks`` returns 401.

        Args:
            session_id: The agent session ID (used as identity ID).
            role: The agent's role.
            task_ids: Task IDs the agent is authorised to act on.

        Returns:
            Absolute path to the written token file.
        """
        import os

        _, raw_token = self._identity_store.create_identity(
            session_id,
            role,
            task_ids=task_ids,
            metadata={"source": "spawner"},
        )

        # ``resolve(strict=False)`` returns an absolute path even when the
        # directory does not yet exist on disk, so the prompt injection
        # always references the canonical project-root location regardless
        # of the agent's spawn cwd (worktree, container, sandbox).
        tokens_dir = (self._workdir / ".sdd" / "runtime" / "agent_tokens").resolve(strict=False)
        tokens_dir.mkdir(parents=True, exist_ok=True)
        token_path = tokens_dir / f"{session_id}.token"

        fd = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, raw_token.encode("utf-8"))
        finally:
            os.close(fd)

        self._agent_token_files[session_id] = token_path
        # Only the session_id and task list (non-secret) are logged; the
        # token itself stays in the on-disk file referenced by token_path.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
        logger.info("Issued zero-trust token for session %s (tasks=%s)", session_id, task_ids or "unrestricted")
        return token_path

    def _revoke_agent_token(self, session_id: str) -> None:
        """Revoke the agent identity and delete the token file on reap.

        Args:
            session_id: The agent session ID whose token should be revoked.
        """
        try:
            self._identity_store.revoke(session_id, reason="agent reaped", actor="spawner")
        except Exception as exc:
            logger.debug("Could not revoke identity %s: %s", session_id, exc)

        token_path = self._agent_token_files.pop(session_id, None)
        if token_path is not None and token_path.exists():
            try:
                token_path.unlink()
            except OSError as exc:
                # Only the file path (not its contents) is logged.
                # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure  # noqa: E501
                logger.debug("Could not delete token file %s: %s", token_path, exc)

    def set_shutdown_event(self, shutdown_event: threading.Event | None) -> None:
        """Attach the orchestrator shutdown event for spawn/worktree guards."""
        self._shutdown_event = shutdown_event
        for manager in self._worktree_managers.values():
            manager.set_shutdown_event(shutdown_event)

    def _render_mailbox_section(self, tasks: list[Task]) -> str:
        """Render pending coordination-mailbox messages for *tasks* (#2357).

        A deterministic projection of the task-server mailbox journal at
        ``.sdd/runtime/mailbox.jsonl``: reading requires no key, the render
        is a pure function of the journal, and every adapter type receives
        the identical bytes. Best-effort - a missing or unreadable journal
        renders nothing and never blocks a spawn.
        """
        try:
            from bernstein.core.communication.task_mailbox import (
                TaskMailbox,
                render_mailbox_section,
            )

            journal = self._workdir / ".sdd" / "runtime" / "mailbox.jsonl"
            if not journal.is_file():
                return ""
            mailbox = TaskMailbox(journal)
            pending = [message for task in tasks for message in mailbox.pending(task.id)]
            return render_mailbox_section(pending)
        except Exception as exc:
            logger.debug("Mailbox section rendering skipped: %s", type(exc).__name__)
            return ""

    # -- Worktree lifecycle (delegated to spawner_worktree) --------------------

    def _worktree_manager_for_repo(self, repo_root: Path) -> WorktreeManager | None:
        return worktree_manager_for_repo(
            repo_root,
            use_worktrees=self._use_worktrees,
            worktree_managers=self._worktree_managers,
            worktree_setup_config=self._worktree_setup_config,
            shutdown_event=self._shutdown_event,
        )

    def get_worktree_path(self, session_id: str) -> Path | None:
        """Return the worktree path for *session_id*, or None if not registered."""
        return self._worktree_paths.get(session_id)

    @property
    def sandbox_session(self) -> SandboxSession | None:
        """Return the optional :class:`SandboxSession` attached to this spawner.

        Phase 1 (oai-002) keeps this purely informational - adapters
        continue to run as local subprocesses against the worktree
        path. The session is exposed so the orchestrator and the
        ``bernstein agents --sandbox-backends`` CLI can report which
        backend the spawner was wired against. Phase 2 (oai-002b)
        routes adapter exec through ``sandbox_session.exec``.
        """
        return self._sandbox_session

    def _sandbox_session_routing_active(self) -> bool:
        """Return True when spawns must route through a sandbox session.

        Two wiring shapes activate the routing seam:

        1. A shared non-worktree :class:`SandboxSession` attached at
           construction (oai-002 phase 2 back-compat).
        2. A :class:`SandboxBackend` plus manifest factory attached at
           construction, which makes ``_spawn_via_sandbox_session``
           provision one session per spawn (issue #2162).
        """
        if self._sandbox_session is not None:
            return getattr(self._sandbox_session, "backend_name", "worktree") != "worktree"
        return self._sandbox_backend is not None and self._sandbox_manifest_factory is not None

    def cleanup_worktree(self, session_id: str) -> None:
        """Remove the worktree and branch for a dead agent session."""
        _cleanup_worktree(
            session_id,
            worktree_roots=self._worktree_roots,
            worktree_paths=self._worktree_paths,
            worktree_managers=self._worktree_managers,
            worktree_mgr=self._worktree_mgr,
            workdir=self._workdir,
        )

    def prune_orphan_worktrees(self, active_session_ids: set[str]) -> int:
        """Remove orphan worktree directories that don't correspond to active sessions."""
        return _prune_orphan_worktrees(
            active_session_ids,
            worktree_managers=self._worktree_managers,
            worktree_paths=self._worktree_paths,
            worktree_roots=self._worktree_roots,
        )

    def _release_warm_pool_slot(self, session_id: str) -> None:
        """Release a claimed warm pool slot for *session_id*, if any."""
        release_warm_pool_slot(
            session_id,
            warm_pool_entries=self._warm_pool_entries,
            warm_pool=self._warm_pool,
        )

    # -- Merge and reap (delegated to spawner_merge) ---------------------------

    def set_merge_queue(self, merge_queue: Any) -> None:
        """Wire in the orchestrator's :class:`MergeQueue` for FIFO merges.

        Called after construction because the orchestrator owns the queue
        and constructs the spawner before itself.  When set, all agent
        merges enqueue through this queue instead of using the ad-hoc
        per-repo lock dict -- fixing.
        """
        self._merge_queue = merge_queue

    def _merge_and_cleanup_worktree(
        self,
        session: AgentSession,
        skip_merge: bool,
        defer_cleanup: bool = False,
    ) -> MergeResult | None:
        """Merge worktree branch back and optionally clean up."""
        return merge_and_cleanup_worktree(
            session,
            skip_merge,
            defer_cleanup=defer_cleanup,
            worktree_paths=self._worktree_paths,
            worktree_roots=self._worktree_roots,
            worktree_managers=self._worktree_managers,
            merge_locks=self._merge_locks,
            warm_pool_entries=self._warm_pool_entries,
            warm_pool=self._warm_pool,
            workdir=self._workdir,
            merge_worktree_branch_fn=self._merge_worktree_branch,
            merge_queue=self._merge_queue,
        )

    def _pending_pushes_path(self) -> Path:
        """Return the path to the pending-pushes JSONL file."""
        from bernstein.core.agents.spawner_merge import pending_pushes_path

        return pending_pushes_path(self._workdir)

    def _record_pending_push(self, session_id: str, branch: str, repo_root: Path) -> None:
        """Append a failed push to the retry queue on disk."""
        from bernstein.core.agents.spawner_merge import record_pending_push

        record_pending_push(self._workdir, session_id, branch, repo_root)

    def _validate_pending_push_entry(self, line: str, safe_base: Path) -> tuple[Path, str, str] | None:
        """Parse and validate a single pending-push entry line."""
        from bernstein.core.agents.spawner_merge import validate_pending_push_entry

        return validate_pending_push_entry(line, safe_base)

    def retry_pending_pushes(self) -> int:
        """Retry any pushes recorded in the pending-pushes file."""
        from bernstein.core.agents.spawner_merge import retry_pending_pushes

        return retry_pending_pushes(self._workdir)

    def _finalize_trace(self, session: AgentSession) -> None:
        """Write the finalized trace for a reaped session."""
        finalize_agent_trace(session, self._traces, self._trace_store)

    def reap_completed_agent(
        self,
        session: AgentSession,
        skip_merge: bool = False,
        defer_cleanup: bool = False,
    ) -> MergeResult | None:
        """Terminate and wait on the subprocess for a completed agent."""
        return _reap_completed_agent(
            session,
            skip_merge=skip_merge,
            defer_cleanup=defer_cleanup,
            runtime_bridge=self._runtime_bridge,
            run_bridge_call_fn=self._run_bridge_call,
            container_mgr=self._container_mgr,
            sandbox_managers=self._sandbox_managers,
            in_process=self._in_process,
            backend=self._backend,
            procs=self._procs,
            worktree_paths=self._worktree_paths,
            worktree_roots=self._worktree_roots,
            worktree_managers=self._worktree_managers,
            merge_locks=self._merge_locks,
            warm_pool_entries=self._warm_pool_entries,
            warm_pool=self._warm_pool,
            workdir=self._workdir,
            merge_worktree_branch_fn=self._merge_worktree_branch,
            traces=self._traces,
            trace_store=self._trace_store,
            merge_queue=self._merge_queue,
        )

    def update_trace_outcome(self, session_id: str, outcome: str) -> None:
        """Update the stored trace outcome for a session."""
        _update_trace_outcome(session_id, outcome, self._traces, self._trace_store)

    def _merge_worktree_branch(self, session_id: str, repo_root: Path | None = None) -> MergeResult:
        """Merge the agent's worktree branch with conflict detection."""
        return merge_worktree_branch(session_id, self._workdir, repo_root=repo_root)

    def _enforce_lethal_trifecta(
        self,
        session_id: str,
        role: str,
        catalog_agent: CatalogAgent | None,
    ) -> None:
        """Refuse spawns whose configured tool chain trips the full trifecta.

        Records a capability manifest under
        ``.sdd/runtime/spawn_capabilities/`` and raises :class:`SpawnError`
        when enforcement is on.  Warn / off modes only persist the
        manifest.

        The adapter envelope is recorded for traceability but the
        evaluation only considers the catalog-declared tool list - the
        adapter alone is fine-grained-scoped via the worker tool
        allowlist (T578) at runtime.  Once an operator opts into a
        specific tool combination that unions the trifecta, the spawn is
        refused.

        Aliasing defence: the registry is the source of truth.  If an
        operator registers an alias name with the original tool's caps
        (a structural choice they own), that alias contributes to the
        trifecta calculation just like the canonical name.  If they
        register an alias with empty caps to *strip* protections, the
        registry's default-deny semantics take over the moment the
        original (now unknown) tool name is used elsewhere.

        On refusal we additionally emit a ``capability_matrix_refusal``
        event into the HMAC-chained audit log so that SOC2/Dream-Security
        auditors can replay every blocked spawn attempt without parsing
        log lines.  Audit-emission failures degrade gracefully - a missing
        audit log must never silently mask the refusal raise.
        """
        import json
        from datetime import UTC, datetime

        from bernstein.core.defaults import SECURITY
        from bernstein.core.security.capability_matrix import (
            CapabilityRegistry,
            EnforcementMode,
            LethalTrifectaError,
        )

        try:
            mode = EnforcementMode(SECURITY.lethal_trifecta_enforcement)
        except ValueError:
            mode = EnforcementMode.ENFORCE
        registry = CapabilityRegistry.load_default(workdir=self._workdir, mode=mode)

        adapter_token = f"adapter.{self._adapter.name()}"
        catalog_tools = list(catalog_agent.tools) if catalog_agent is not None else []
        chain: list[str] = [adapter_token, *catalog_tools]

        # Spawn-time enforcement only refuses chains where the trifecta is
        # reached via *declared* tool tags - undeclared catalog tools
        # default to all-three at the registry level (so the audit CLI
        # surfaces them as warnings) but a single undeclared tool should
        # not block a spawn.  Once any operator-declared chain unions all
        # three, we deny.
        declared_only = [t for t in catalog_tools if t in registry.tools]
        decision = registry.evaluate_chain(declared_only)

        runtime_dir = self._workdir / ".sdd" / "runtime" / "spawn_capabilities"
        try:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            manifest = {
                "agent_id": session_id,
                "role": role,
                "timestamp": datetime.now(UTC).isoformat(),
                "tools": chain,
                "evaluated": catalog_tools,
                "triggered": sorted(c.value for c in decision.triggered),
                "allowed": decision.allowed,
                "reason": decision.reason,
                "mode": decision.mode.value,
                "unknown_tools": list(decision.unknown_tools),
                "offending_tools": list(decision.offending_tools),
            }
            (runtime_dir / f"{session_id}.json").write_text(
                json.dumps(manifest, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Could not persist capability manifest for %s: %s", session_id, exc)

        if not decision.allowed:
            err = LethalTrifectaError(decision)
            logger.error(
                "Refusing spawn %s (role=%s): %s - chain=%s",
                session_id,
                role,
                decision.reason,
                chain,
            )
            self._emit_capability_matrix_refusal_audit_event(
                session_id=session_id,
                role=role,
                chain=chain,
                catalog_tools=catalog_tools,
                decision=decision,
            )
            raise SpawnError(f"lethal trifecta: {decision.reason}") from err

    def _emit_capability_matrix_refusal_audit_event(
        self,
        *,
        session_id: str,
        role: str,
        chain: list[str],
        catalog_tools: list[str],
        decision: Any,
    ) -> None:
        """Append a ``capability_matrix_refusal`` event to the HMAC audit chain.

        Persists the structural decision to ``<workdir>/.sdd/audit/`` so
        auditors can verify that no trifecta-prone agent ever spawned
        without a matching deny event.  Failures (key permission, disk
        full) are caught and logged - they must never mask the underlying
        refusal raise.

        Args:
            session_id: Spawn session identifier (becomes the audit
                ``resource_id``).
            role: Agent role being refused.
            chain: The full evaluated tool chain including the adapter
                envelope.
            catalog_tools: The catalog-declared tool list (subset of
                *chain* used for the trifecta evaluation).
            decision: The :class:`ChainDecision` produced by the
                capability registry.
        """
        try:
            from bernstein.core.security.audit import AuditLog

            audit = AuditLog(audit_dir=self._workdir / ".sdd" / "audit")
            audit.log(
                event_type="capability_matrix_refusal",
                actor="spawner",
                resource_type="agent_session",
                resource_id=session_id,
                details={
                    "role": role,
                    "reason": decision.reason,
                    "chain": chain.copy(),
                    "catalog_tools": catalog_tools.copy(),
                    "triggered": sorted(c.value for c in decision.triggered),
                    "offending_tools": list(decision.offending_tools),
                    "unknown_tools": list(decision.unknown_tools),
                    "mode": decision.mode.value,
                },
            )
        except Exception as exc:  # audit must never mask deny - log and move on
            logger.warning(
                "Could not emit capability_matrix_refusal audit event for %s: %s",
                session_id,
                exc,
            )

    @staticmethod
    def _is_fresh_restart_retry(task: Task) -> bool:
        """Return True when this spawn must run as a fresh-context retry.

        Issue #1109: a task opts into fresh-context retries by setting
        ``agent_restart_between_retries=True``.  The flag only takes effect
        on retry attempts (``retry_count > 0``); the very first attempt is
        always a fresh spawn anyway.

        Args:
            task: The task being spawned.

        Returns:
            True when the spawn must drop accumulated state and be audited.
        """
        return bool(task.agent_restart_between_retries) and task.retry_count > 0

    def _strip_failure_context_for_fresh_retry(self, task: Task) -> tuple[str, list[str]]:
        """Return ``(description, meta_messages)`` with failure-context replay removed.

        ``maybe_retry_task`` and ``retry_or_fail_task`` annotate retry tasks
        with the prior failure summary so the next agent learns from it.
        For fresh-context retries that replay is exactly what we want to
        suppress: the agent must start as if this were attempt #1.

        Args:
            task: The task whose carry-over context is being stripped.

        Returns:
            Tuple of the cleaned description and a list of meta-messages
            with any ``Retry N: Previous attempt failed*`` entries removed.
        """
        # Drop the "## Previous attempt failed" section appended by the
        # retry helpers.  Everything before that header is the canonical
        # description; everything after is failure replay.
        description = task.description
        marker = "\n\n## Previous attempt failed\n"
        idx = description.find(marker)
        if idx != -1:
            description = description[:idx]

        # Drop replay messages but keep operator-supplied nudges intact.
        cleaned_messages = [
            msg for msg in task.meta_messages if not msg.startswith("Retry ") or "Previous attempt failed" not in msg
        ]
        return description, cleaned_messages

    def _emit_fresh_restart_on_retry_audit(
        self,
        *,
        task_id: str,
        retry_n: int,
        reason: str,
    ) -> None:
        """Append an ``agent_fresh_restart_on_retry`` event to the audit chain.

        Issue #1109 - every fresh-context retry must leave a trace so
        operators can correlate the restart with the prior failure.  Audit
        failures (key permission, disk full) must never mask the spawn:
        they are logged and swallowed.

        Args:
            task_id: ID of the task being retried (audit ``resource_id``).
            retry_n: Retry attempt number (1, 2, ...).
            reason: Free-form reason string from the prior failure.
        """
        try:
            from bernstein.core.security.audit import (
                AGENT_FRESH_RESTART_ON_RETRY,
                AuditLog,
            )

            audit = AuditLog(audit_dir=self._workdir / ".sdd" / "audit")
            audit.log(
                event_type=AGENT_FRESH_RESTART_ON_RETRY,
                actor="spawner",
                resource_type="task",
                resource_id=task_id,
                details={
                    "task_id": task_id,
                    "retry_n": retry_n,
                    "reason": reason,
                },
            )
        except Exception as exc:  # audit must never block the spawn
            logger.warning(
                "Could not emit agent_fresh_restart_on_retry audit event for task %s: %s",
                task_id,
                exc,
            )

    def _emit_response_profile_audit(
        self,
        *,
        task_ids: list[str],
        style: str,
        source: str,
        profile_content_sha256: str,
    ) -> None:
        """Append a ``task_response_profile`` event to the audit chain.

        Every spawn declares a response-style profile; recording the profile
        name and the rendered-addendum hash per task keeps the audit trail
        aligned with the cost ledger entry written at completion. Audit
        failures (key permission, disk full) never mask the spawn: they are
        logged and swallowed.

        Args:
            task_ids: IDs of the tasks in this spawn batch.
            style: Resolved response style (``verbose``/``balanced``/``terse``).
            source: Which input supplied the style (resolution provenance).
            profile_content_sha256: SHA-256 of the rendered style addendum.
        """
        try:
            from bernstein.core.security.audit import (
                TASK_RESPONSE_PROFILE,
                AuditLog,
            )

            audit = AuditLog(audit_dir=self._workdir / ".sdd" / "audit")
            for task_id in task_ids:
                audit.log(
                    event_type=TASK_RESPONSE_PROFILE,
                    actor="spawner",
                    resource_type="task",
                    resource_id=task_id,
                    details={
                        "task_id": task_id,
                        "response_profile": style,
                        "style_source": source,
                        "profile_content_sha256": profile_content_sha256,
                    },
                )
        except Exception as exc:  # audit must never block the spawn
            logger.warning(
                "Could not emit task_response_profile audit event for tasks %s: %s",
                task_ids,
                exc,
            )

    def _maybe_record_profile_transition(
        self,
        *,
        task_id: str,
        session_id: str,
        prev_profile: str,
        prev_sha: str,
        new_profile: str,
        new_sha: str,
    ) -> None:
        """Record a ``profile_transition`` event when a re-spawn changes profile.

        A task re-spawned under a different response-style profile (for
        example after a role-policy edit between attempts) accumulates
        ledger entries under two profiles. Per-profile cost attribution
        must exclude such tasks rather than split their tokens, so the
        change is recorded to ``.sdd/cost/profile_transitions.jsonl``
        before the new profile overwrites the stamp on task metadata.
        First spawns (no previous stamp) and same-profile re-spawns
        record nothing. Failures are logged and swallowed - attribution
        metadata must never block the spawn.

        Args:
            task_id: The task being re-spawned.
            session_id: The new session's id (recorded as the agent).
            prev_profile: Profile previously stamped on task metadata
                (empty on first spawn).
            prev_sha: Previously stamped addendum hash.
            new_profile: Profile resolved for this spawn.
            new_sha: Rendered-addendum hash for this spawn.
        """
        if not prev_profile or prev_profile == new_profile:
            return
        try:
            from bernstein.core.cost.profile_attribution import (
                default_transitions_path,
                record_profile_transition,
            )

            record_profile_transition(
                default_transitions_path(self._workdir / ".sdd"),
                task_id=task_id,
                agent_id=session_id,
                from_profile=prev_profile,
                to_profile=new_profile,
                from_sha256=prev_sha,
                to_sha256=new_sha,
            )
            logger.info(
                "Profile transition recorded for task %s: %s -> %s",
                task_id,
                prev_profile,
                new_profile,
            )
        except Exception as exc:  # attribution must never block the spawn
            logger.warning(
                "Could not record profile_transition for task %s: %s",
                task_id,
                exc,
            )

    def _reap_openclaw(self, session: AgentSession) -> None:
        """Sync logs from the remote bridge for an OpenClaw session."""
        reap_openclaw(session, self._runtime_bridge, self._run_bridge_call)

    def _reap_container(self, session: AgentSession) -> None:
        """Destroy the container for a containerized agent session."""
        reap_container(session, self._container_mgr, self._sandbox_managers)

    def _reap_in_process(self, session: AgentSession) -> bool:
        """Wait on and clean up an in-process agent. Returns True if reaped."""
        return reap_in_process(session, self._in_process, self._backend)

    def _reap_subprocess(self, session: AgentSession) -> None:
        """Terminate and wait on the OS subprocess."""
        reap_subprocess(session, self._procs)

    def _infer_adapter_name_for_provider(self, provider_name: str | None, model: str) -> str:
        """Resolve adapter name from provider/model identifiers via the adapter registry.

        Delegates to :func:`bernstein.adapters.registry.adapter_name_for_provider`,
        which looks the pair up against the ``provider_name -> adapter_name``
        table built from every adapter's ``provides`` declaration. This
        replaces the old hand-ordered substring `if`/`elif` chain (Root
        Cause A of the provider/adapter routing bug ladder): there is no
        longer any hardcoded branch order to get wrong, and
        :func:`bernstein.adapters.registry._register_provider_alias` raises
        loudly at table-build time if two adapters ever claim the same
        alias, instead of silently misrouting at spawn time.

        Unrecognized provider/model combinations still fall back to
        ``self._adapter.name()`` -- the currently-active adapter -- exactly
        as before, so Claude-only / unrecognized-provider operators are
        unaffected.
        """
        logger.debug(
            "_infer_adapter_name_for_provider: provider_name=%r model=%r current_adapter=%r",
            provider_name,
            model,
            self._adapter.name(),
        )
        resolved = adapter_name_for_provider(provider_name, model)
        if resolved is not None:
            logger.info(
                "_infer_adapter_name_for_provider: resolved provider_name=%r model=%r -> adapter=%r",
                provider_name,
                model,
                resolved,
            )
            return resolved
        fallback = self._adapter.name()
        logger.info(
            "_infer_adapter_name_for_provider: no registry match for provider_name=%r model=%r; "
            "falling back to current adapter %r",
            provider_name,
            model,
            fallback,
        )
        return fallback

    def _get_adapter_by_name(self, adapter_name: str, *, role: str | None = None) -> CLIAdapter:
        """Return cached adapter instance, creating one when needed.

        When *role* is supplied, the per-role adapter deny-list
        (``role_adapter_policy``) is consulted before instantiation. An
        empty allow-list for a role is back-compat: the spawn proceeds.
        A non-empty allow-list rejects spawns whose adapter is not on
        it, raising :exc:`bernstein.core.security.role_adapter_policy.
        RoleAdapterDenied` and emitting a structured ``role.adapter.
        denied`` event into the HMAC audit chain.

        Args:
            adapter_name: Adapter id (``claude``, ``aider``, …).
            role: Effective role of the spawn site (taken from the
                primary task's ``role`` field). Optional so legacy
                call sites that have no role still work.
        """
        if role is not None:
            from bernstein.core.security.audit import AuditLog as _AuditLog
            from bernstein.core.security.role_adapter_policy import enforce as _enforce_role_adapter

            audit_log: _AuditLog | None = None
            try:
                audit_log = _AuditLog(audit_dir=self._workdir / ".sdd" / "audit")
            except Exception as exc:
                logger.debug("role_adapter_policy: audit ctor failed (%s); deny will not be logged", exc)
            _enforce_role_adapter(role, adapter_name, audit_log=audit_log)

        cached = self._adapter_cache.get(adapter_name)
        if cached is not None:
            return cached

        adapter = get_adapter(adapter_name)
        if self._enable_caching:
            from bernstein.adapters.caching_adapter import CachingAdapter

            adapter = CachingAdapter(adapter, self._workdir)
        self._adapter_cache[adapter_name] = adapter
        return adapter

    def _run_bridge_call(self, awaitable: Any) -> Any:
        """Run a bridge coroutine from the sync orchestration path."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, awaitable).result()

    def _mcp_config_for_adapter(
        self,
        adapter: CLIAdapter,
        mcp_config: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Attach adapter-specific extras to the per-spawn MCP config.

        Adapters that opt in via a truthy ``consumes_heartbeat_dir``
        attribute (currently ``openai_agents``) receive the orchestrator
        root's heartbeat directory as a ``heartbeat_dir`` key.  Their
        runner processes write heartbeats themselves, but they execute
        inside a per-session worktree and cannot derive the project root
        the ``HeartbeatMonitor`` polls - without this injection the
        heartbeat would land in the worktree and never be observed.

        The SAME attribute gates injection of ``instrumentation_root``:
        the orchestrator's wave-2 phase/task timing (``write_summary_json``)
        lives under ``self._workdir / ".sdd" / "runs" / <run_id>`` (the
        project root), but the runner subprocess only knows its own
        per-session worktree path (``manifest.workdir``) under default
        worktree isolation (``use_worktrees=True``). Without this
        injection ``RunInstrumenter`` writes its llm-calls/tool-calls/
        conversation JSONL under ``<worktree>/.sdd/runs/...`` instead -
        a directory that (a) nobody looks in, since the run report lives
        at the project root, and (b) is deleted outright when the
        worktree is merged/cleaned up after the task finishes, so the
        JSONL files vanish even on a fully successful run. This exactly
        mirrors the pre-existing ``heartbeat_dir`` bug this docstring
        describes above, just for wave-3 instrumentation instead of
        wave-2 heartbeats.

        Adapters without the attribute get ``mcp_config`` back unchanged
        so their MCP config files stay byte-identical.

        Logged at INFO on every call (bug #11): the previous silent
        skip made a ``CachingAdapter``-wrapped adapter's dropped
        ``consumes_heartbeat_dir`` flag invisible - workers wrote
        heartbeats into the worktree, the monitor polled the
        orchestrator root, and every spawn was killed at the stale
        threshold with no log line pointing at the cause.
        """
        consumes = getattr(adapter, "consumes_heartbeat_dir", False)
        injected = bool(consumes)
        if injected:
            heartbeat_dir = str(self._workdir / ".sdd" / "runtime" / "heartbeats")
            instrumentation_root = str(self._workdir)
        logger.info(
            "heartbeat_dir/instrumentation_root injection check: adapter=%s consumes_heartbeat_dir=%s injected=%s",
            adapter.name() if hasattr(adapter, "name") else type(adapter).__name__,
            consumes,
            injected,
        )
        if not injected:
            return mcp_config
        return {
            **(mcp_config or {}),
            "heartbeat_dir": heartbeat_dir,
            "instrumentation_root": instrumentation_root,
        }

    def _primary_adapter_supports_sampling(
        self, model_config: ModelConfig, *, provider_name: str | None = None
    ) -> bool:
        """Best-effort probe: does the adapter for this spawn honour sampling?

        Used to decide whether mode-profile sampling params may be folded
        into the per-spawn config. Only adapters that declare
        :attr:`AdapterCapability.SUPPORTS_SAMPLING_PARAMS` accept them; for
        any other adapter the spawn path's
        :func:`ensure_sampling_params_supported` gate would refuse the
        spawn, so injecting profile defaults there would break otherwise
        valid runs.

        ``provider_name`` is the per-role/per-spawn provider resolved by the
        caller (``_apply_sampling_overrides`` passes the same ``provider_name``
        that ``spawn_for_tasks`` computed from ``role_model_policy``/task
        ``cli`` and fed into ``_resolve_routing``). Passing it through to
        :meth:`_infer_adapter_name_for_provider` is what makes this probe
        target the adapter that will ACTUALLY spawn the role - e.g.
        ``openai_agents`` for a role pinned via ``role_model_policy.<role>.
        provider: openai_agents`` - instead of always resolving from
        ``provider_name=None``, which silently falls back to the *primary*
        adapter (``self._adapter``, e.g. ``claude`` from ``cli: auto``).
        That primary-adapter fallback was the root cause of the mode-profile
        sampling fold (and, before PR3's unconditional role-policy fold, any
        role-scoped sampling override) being silently skipped whenever the
        primary adapter did not declare ``SUPPORTS_SAMPLING_PARAMS`` even
        though the per-role adapter did (see the D2 OpenRouter KILL-NOTE).
        ``provider_name=None`` (the default) preserves the previous
        primary-adapter behavior for call sites that have no role context.

        The probe prefers an already-known adapter instance - the default
        adapter or an already-cached one - to avoid perturbing the failover
        loop's own adapter resolution/caching. But the per-role adapter is
        frequently NOT yet cached at this point in ``spawn_for_tasks``
        (this gate runs before the spawn loop's own
        :meth:`_get_adapter_by_name` call), which is exactly the scenario
        that silently starved the mode-profile fold: a cache-miss used to
        fall straight through to ``self._adapter`` (the primary adapter),
        never actually checking the per-role adapter's capability at all.
        To fix that without perturbing the failover loop, an uncached probe
        instantiates the resolved adapter class directly via
        :func:`bernstein.adapters.registry.get_adapter` - a plain
        constructor call, not :meth:`_get_adapter_by_name` (which enforces
        ``role_adapter_policy`` and writes an audit-log entry as a side
        effect) - checks its capability, and discards the instance without
        adding it to ``self._adapter_cache``. Any failure to construct the
        probe instance (unknown adapter name, missing optional dependency,
        etc.) is swallowed and treated as "does not support sampling", the
        conservative choice that preserves today's behavior.
        """

        def _supports(adapter: object) -> bool:
            from bernstein.adapters.plugin_sdk import AdapterCapability, PluginAdapter

            if not isinstance(adapter, PluginAdapter):
                return False
            try:
                return AdapterCapability.SUPPORTS_SAMPLING_PARAMS in adapter.plugin_info().capabilities
            except Exception:  # pragma: no cover - defensive against bad plugins
                return False

        adapter_name = self._infer_adapter_name_for_provider(provider_name, model_config.model)
        cached = self._adapter_cache.get(adapter_name)
        if cached is not None:
            result = _supports(cached)
            logger.info(
                "_primary_adapter_supports_sampling: provider_name=%r model=%r -> "
                "adapter=%r (cached) supports_sampling=%s",
                provider_name,
                model_config.model,
                adapter_name,
                result,
            )
            return result

        if adapter_name == self._adapter.name():
            result = _supports(self._adapter)
            logger.info(
                "_primary_adapter_supports_sampling: provider_name=%r model=%r -> "
                "adapter=%r (== primary self._adapter) supports_sampling=%s",
                provider_name,
                model_config.model,
                adapter_name,
                result,
            )
            return result

        # Uncached, non-primary adapter (the common case for a role pinned
        # to a different provider than the run's primary adapter, e.g.
        # ``cli: auto`` -> claude primary with a role_model_policy
        # ``provider: openai_agents`` override): probe it directly via the
        # registry factory, read-only, without caching or role-policy
        # enforcement.
        try:
            from bernstein.adapters.registry import get_adapter

            probe_adapter = get_adapter(adapter_name)
        except Exception as exc:
            logger.info(
                "_primary_adapter_supports_sampling: provider_name=%r model=%r -> "
                "adapter=%r could not be probed (%s: %s); treating as supports_sampling=False",
                provider_name,
                model_config.model,
                adapter_name,
                type(exc).__name__,
                exc,
            )
            return False

        result = _supports(probe_adapter)
        logger.info(
            "_primary_adapter_supports_sampling: provider_name=%r model=%r -> "
            "adapter=%r (uncached probe, primary=%r) supports_sampling=%s",
            provider_name,
            model_config.model,
            adapter_name,
            self._adapter.name() if hasattr(self._adapter, "name") else type(self._adapter).__name__,
            result,
        )
        return result

    def _apply_sampling_overrides(
        self,
        mcp_config: dict[str, Any] | None,
        *,
        role_policy: dict[str, Any],
        model_config: ModelConfig,
        tasks: list[Task],
        provider_name: str | None = None,
    ) -> dict[str, Any] | None:
        """Fold per-role endpoint/sampling and mode-profile sampling params into config.

        Two opt-in sources feed the per-spawn ``mcp_config`` slots the
        adapter manifest reads (see :data:`SAMPLING_PARAM_KEYS`):

        1. ``role_model_policy[role]`` - the per-role
           :class:`~bernstein.core.config.config_schema.RoleModelPolicyEntry`
           parsed from ``bernstein.yaml``: ``base_url``/``api_key_env`` (the
           OpenAI-compatible endpoint override; ``api_key_env`` was already
           validated at parse time against the fail-closed credential
           allowlist) AND, since PR3, ``temperature``/``top_p``/``top_k``/
           ``max_tokens``/``extra_params``. All of these are explicit
           operator config, so they forward unconditionally; the spawn
           path's capability gate (:func:`ensure_sampling_params_supported`)
           still guards whether the target adapter actually honours them.
        2. The resolved :class:`ModeProfile`'s deterministic sampling params
           (``temperature``, ``top_p``, ``top_k``, ``max_tokens``) via
           :func:`apply_mode_to_spawn`. These are implicit defaults, so they
           are folded in only when the target adapter declares
           ``SUPPORTS_SAMPLING_PARAMS`` - otherwise the capability gate
           would refuse an otherwise valid spawn.

        Precedence is: an explicit value already present in ``mcp_config``
        (operator-set) wins over a role-policy value, which wins over a
        mode-profile value. Absent config leaves ``mcp_config`` unchanged,
        so a run without any of these keys is byte-identical to before.
        (PR3 note: this is the function the design doc referred to as
        ``_fold_role_and_mode_sampling_params_into_mcp_config`` - it was
        already implemented and named ``_apply_sampling_overrides`` when
        this PR started; see the PR3 report for that drift.)

        The merge is deterministic: it reads only the parsed config, the
        selected model id, and the task metadata - no wall-clock or random
        input - so two operators with identical state build identical
        manifests.
        """
        role = tasks[0].role if tasks else None
        logger.debug(
            "_apply_sampling_overrides: entry role=%r model=%r provider_name=%r mcp_config_keys=%s role_policy_keys=%s",
            role,
            model_config.model,
            provider_name,
            sorted((mcp_config or {}).keys()),
            sorted(role_policy.keys()),
        )
        derived: dict[str, Any] = {}

        # Mode-profile sampling params (lowest precedence). Wiring these here
        # is what makes a ModeProfile's sampling params actually reach the
        # adapter manifest; the profile object defined them but nothing
        # forwarded them before. Guarded by the target adapter's capability
        # so a default profile temperature never breaks a spawn on an
        # adapter that cannot honour sampling params. ``provider_name`` is
        # forwarded so the gate probes the adapter that will ACTUALLY spawn
        # this role (e.g. openai_agents pinned via role_model_policy), not
        # always the run's primary adapter (e.g. claude from cli: auto) -
        # see _primary_adapter_supports_sampling's docstring / the D2
        # OpenRouter KILL-NOTE this fixes.
        if self._primary_adapter_supports_sampling(model_config, provider_name=provider_name):
            from bernstein.core.agents.spawner_prompt import apply_mode_to_spawn

            bundle = apply_mode_to_spawn(
                model_id=model_config.model,
                prompt="",
                tools=None,
                task=tasks[0] if tasks else None,
                workdir=self._workdir,
            )
            profile = bundle.profile
            if profile.temperature is not None:
                derived["temperature"] = profile.temperature
            if profile.top_p is not None:
                derived["top_p"] = profile.top_p
            if profile.top_k is not None:
                derived["top_k"] = profile.top_k
            if profile.max_tokens is not None:
                derived["max_tokens"] = profile.max_tokens
            logger.debug(
                "_apply_sampling_overrides: mode-profile %r contributed sampling keys=%s",
                profile.name,
                derived.copy(),
            )
        else:
            logger.debug(
                "_apply_sampling_overrides: adapter for model=%r does not declare "
                "SUPPORTS_SAMPLING_PARAMS, skipping mode-profile sampling defaults",
                model_config.model,
            )

        # Per-role endpoint override (higher precedence than the profile).
        for key in ("base_url", "api_key_env"):
            value = role_policy.get(key)
            if isinstance(value, str) and value:
                derived[key] = value

        # PR3: per-role sampling overrides (RoleModelPolicyEntry.temperature/
        # top_p/top_k/max_tokens/extra_params). Same precedence tier as the
        # endpoint override above - explicit per-role operator config beats
        # the mode-profile default for the same key. Each field is validated
        # for type before folding in so a malformed role_policy entry cannot
        # inject an unexpected type into the manifest.
        role_sampling_before = derived.copy()
        temperature = role_policy.get("temperature")
        if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
            derived["temperature"] = float(temperature)
        top_p = role_policy.get("top_p")
        if isinstance(top_p, (int, float)) and not isinstance(top_p, bool):
            derived["top_p"] = float(top_p)
        top_k = role_policy.get("top_k")
        if isinstance(top_k, int) and not isinstance(top_k, bool):
            derived["top_k"] = top_k
        max_tokens = role_policy.get("max_tokens")
        if isinstance(max_tokens, int) and not isinstance(max_tokens, bool):
            derived["max_tokens"] = max_tokens
        extra_params = role_policy.get("extra_params")
        if isinstance(extra_params, dict) and extra_params:
            derived["extra_params"] = extra_params
        role_overrode = {
            k: v for k, v in derived.items() if k not in role_sampling_before or role_sampling_before[k] != v
        }
        if role_overrode:
            logger.info(
                "_apply_sampling_overrides: role=%r role_model_policy sampling fields=%s take "
                "precedence over mode-profile defaults for the same key(s)",
                role,
                role_overrode,
            )

        gate_adapter_name = self._infer_adapter_name_for_provider(provider_name, model_config.model)

        if not derived:
            logger.info(
                "_apply_sampling_overrides: role=%r gate_adapter=%r (provider_name=%r) - no derived "
                "sampling/endpoint keys, mcp_config unchanged (reason: neither role_model_policy nor "
                "the resolved mode profile contributed any sampling/endpoint keys for this role)",
                role,
                gate_adapter_name,
                provider_name,
            )
            return mcp_config

        # Operator-set values in ``mcp_config`` always win: only fill slots
        # the caller did not already set.
        merged = dict(mcp_config or {})
        filled: dict[str, Any] = {}
        skipped_operator_set: dict[str, Any] = {}
        for key, value in derived.items():
            if merged.get(key) is None:
                merged[key] = value
                filled[key] = value
            else:
                skipped_operator_set[key] = merged[key]
        logger.info(
            "_apply_sampling_overrides: role=%r gate_adapter=%r (provider_name=%r) folded_keys=%s "
            "into runner manifest%s",
            role,
            gate_adapter_name,
            provider_name,
            filled,
            f" (skipped, operator mcp_config already set: {skipped_operator_set})" if skipped_operator_set else "",
        )
        return merged

    def _spawn_via_runtime_bridge(
        self,
        *,
        session: AgentSession,
        prompt: str,
        spawn_cwd: Path,
        model_config: ModelConfig,
        preferred_log_path: Path,
    ) -> bool:
        """Attempt to spawn via the configured runtime bridge.

        Returns:
            True when the remote run was accepted and ``session`` was populated.

        Raises:
            BridgeError: If the bridge rejects the spawn before acceptance.
        """
        if self._runtime_bridge is None:
            return False
        bridge_status = self._run_bridge_call(
            self._runtime_bridge.spawn(
                SpawnRequest(
                    agent_id=session.id,
                    image="openclaw-agent",
                    command=[],
                    prompt=prompt,
                    workdir=str(spawn_cwd),
                    timeout_seconds=session.timeout_s or 1800,
                    log_path=str(preferred_log_path),
                    role=session.role,
                    model=model_config.model,
                    effort=model_config.effort,
                    labels={"session_id": session.id},
                )
            )
        )
        if not isinstance(bridge_status, object):
            return False
        session.runtime_backend = self._runtime_bridge.name()
        session.pid = None
        session.log_path = str(preferred_log_path)
        session.provider = session.provider or self._runtime_bridge.name()
        session.bridge_session_key = bridge_status.metadata.get("session_key") or None
        session.bridge_run_id = bridge_status.metadata.get("run_id") or None
        transition_agent(session, "working", actor="spawner", reason="remote bridge run accepted")
        return True

    def _bridge_status(self, session: AgentSession) -> Any:
        """Fetch the latest remote runtime status for a bridge-backed session."""
        if self._runtime_bridge is None:
            raise BridgeError("No runtime bridge configured", agent_id=session.id)
        return self._run_bridge_call(self._runtime_bridge.status(session.id))

    def _bridge_cancel(self, session: AgentSession) -> None:
        """Best-effort cancellation for a bridge-backed session."""
        if self._runtime_bridge is None:
            raise BridgeError("No runtime bridge configured", agent_id=session.id)
        self._run_bridge_call(self._runtime_bridge.cancel(session.id))

    def spawn_for_tasks(self, tasks: list[Task], model_override: str | None = None) -> AgentSession:
        """Route, render prompt, and spawn an agent for a task batch."""
        from bernstein.core.telemetry import start_span

        if not tasks:
            raise ValueError("Cannot spawn agent with empty task list")

        with start_span(
            "agent.spawn",
            attributes={
                "role": tasks[0].role,
                "task_count": len(tasks),
                "model_override": model_override,
            },
        ):
            return self._spawn_for_tasks_internal(tasks, model_override=model_override)

    def _resolve_routing(
        self,
        tasks: list[Task],
        model_config: ModelConfig,
        role_policy: dict[str, Any],
        preferred_provider: str | None,
    ) -> tuple[ModelConfig, str | None, str]:
        """Select provider and model via router or operator config."""
        provider_name: str | None = None
        # Per-step `cli:` is treated as a synthetic pinned adapter so the
        # router-skip decision matches the role_model_policy cli case.
        effective_role_policy: dict[str, Any] = role_policy.copy()
        if tasks[0].cli and "cli" not in effective_role_policy:
            effective_role_policy["cli"] = tasks[0].cli
        use_router = _should_use_router(
            role_policy=effective_role_policy,
            adapter_name=self._adapter.name(),
            has_router=self._router is not None and bool(self._router.state.providers),
        )
        if not use_router:
            if preferred_provider:
                provider_name = preferred_provider
            routing_source = "operator-config" if role_policy.get("model") else "heuristic"
            logger.info(
                "Router skipped for role=%s (adapter=%s): using %s/%s (source=%s)",
                tasks[0].role,
                role_policy.get("cli", self._adapter.name()),
                model_config.model,
                model_config.effort,
                routing_source,
            )
            return model_config, provider_name, routing_source

        assert self._router is not None
        try:
            decision = self._router.select_provider_for_task(
                tasks[0],
                base_config=model_config,
                preferred_provider=preferred_provider,
            )
            logger.info(
                "Router selected provider for role=%s: provider=%s model=%s/%s (preferred_provider=%s)",
                tasks[0].role,
                decision.provider,
                decision.model_config.model,
                decision.model_config.effort,
                preferred_provider,
            )
            return decision.model_config, decision.provider, "router"
        except RouterError as exc:
            if preferred_provider:
                logger.warning(
                    "Role policy provider override for role=%s could not be honored (%s); "
                    "falling back to normal routing",
                    tasks[0].role,
                    exc,
                )
                try:
                    decision = self._router.select_provider_for_task(tasks[0], base_config=model_config)
                    return decision.model_config, decision.provider, "router-fallback"
                except RouterError as fallback_exc:
                    logger.warning("Router failed to select provider, using fallback: %s", fallback_exc)
            else:
                logger.warning("Router failed to select provider, using fallback: %s", exc)
        return model_config, provider_name, "heuristic"

    def _spawn_for_tasks_internal(self, tasks: list[Task], model_override: str | None = None) -> AgentSession:
        """Actual spawn implementation."""
        if self._shutdown_event is not None and self._shutdown_event.is_set():
            logger.info(
                "spawn refused: shutdown_event is set - role=%s task_count=%d task_ids=%s",
                tasks[0].role if tasks else "<empty>",
                len(tasks),
                [t.id for t in tasks],
            )
            raise ShutdownInProgress("Orchestrator shutting down - refusing new spawn")

        # Disk space check: refuse to spawn if less than 1 GB free.
        # Worktree creation + agent output can consume significant disk.
        try:
            usage = shutil.disk_usage(self._workdir)
            free_gb = usage.free / (1024**3)
            if free_gb < SPAWN.disk_free_threshold_gb:
                logger.error("Disk space critical: %.1f GB free, skipping spawn", free_gb)
                threshold = SPAWN.disk_free_threshold_gb
                raise SpawnError(f"Disk space critical: {free_gb:.1f} GB free (need >= {threshold} GB)")
        except OSError as exc:
            logger.warning("Could not check disk space: %s", exc)

        # 5min cooldown check (legacy) + per-adapter health monitor
        now = time.time()
        adapter_name = self._adapter.name()
        last_fail = self._agent_failure_timestamps.get(adapter_name, 0.0)
        if now - last_fail < SPAWN.spawn_failure_cooldown_s:
            logger.info(
                "Agent %s in cooldown (%.1fs remaining) - skipping spawn",
                adapter_name,
                SPAWN.spawn_failure_cooldown_s - (now - last_fail),
            )
            raise SpawnError(f"Agent {adapter_name} is in cooldown after recent failure")
        if not self._adapter_health.is_healthy(adapter_name):
            stats = self._adapter_health.get_stats(adapter_name)
            rate = stats.failure_rate if stats is not None else 0.0
            logger.info(
                "Adapter %s disabled by health monitor (failure_rate=%.0f%%) - skipping spawn",
                adapter_name,
                rate * 100,
            )
            raise SpawnError(f"Adapter {adapter_name} is disabled by health monitor (failure rate {rate:.0%})")

        if not tasks:
            raise ValueError("Cannot spawn agent with empty task list")

        roles = {t.role for t in tasks}
        if len(roles) > 1:
            raise ValueError(f"All tasks in a batch must share the same role, got: {roles}")

        # Issue #1109: opt-in fresh-context retry.  When a task carries
        # ``agent_restart_between_retries=True`` AND ``retry_count > 0``
        # we must spawn a brand-new agent with no log carryover and no
        # failure-context replay.  Strip the carry-over annotations from
        # the in-memory task so prompt rendering treats it like attempt #1,
        # disable warm-pool reuse, and audit the restart for traceability.
        primary_task = tasks[0]
        fresh_restart_on_retry = self._is_fresh_restart_retry(primary_task)
        if fresh_restart_on_retry:
            cleaned_description, cleaned_meta_messages = self._strip_failure_context_for_fresh_retry(primary_task)
            primary_task.description = cleaned_description
            primary_task.meta_messages = cleaned_meta_messages
            self._emit_fresh_restart_on_retry_audit(
                task_id=primary_task.id,
                retry_n=primary_task.retry_count,
                reason=primary_task.terminal_reason or "",
            )
            logger.info(
                "Fresh-context retry for task %s (retry_n=%d): dropped failure replay, skipping warm pool",
                primary_task.id,
                primary_task.retry_count,
            )

        # ---------------------------------------------------------------
        # Model selection precedence (highest wins):
        #
        #   1. Operator config: role_model_policy has cli+model for this role
        #      → use exactly that adapter and model.  The router's
        #      arms (haiku/sonnet/opus) are Claude-specific and meaningless
        #      for non-Claude adapters like qwen, gemini, codex, etc.
        #
        #   2. Router suggestion: bandit/cascade router picks a model from
        #      its Claude-specific arm set.  Only consulted when the adapter
        #      is Claude-compatible (i.e. the router's arms match the
        #      adapter's model names).
        #
        #   3. Default heuristic: _select_batch_config picks model/effort
        #      based on task complexity, scope, and role templates.
        # ---------------------------------------------------------------
        metrics_dir = self._workdir / ".sdd" / "metrics"
        # role_model_policy may pin this role's model below; feed that pin to
        # the heuristic selector as the default so a role-policy-only config
        # (no run-level default_model) does not fail heuristic routing before
        # the pin is applied.
        _policy_preview = self._role_model_policy.get(tasks[0].role) or self._role_model_policy.get("default") or {}
        base_config = _select_batch_config(
            tasks,
            templates_dir=self._templates_dir,
            metrics_dir=metrics_dir if metrics_dir.exists() else None,
            workdir=self._workdir,
            default_model=self._default_model or _policy_preview.get("model"),
        )
        if model_override:
            base_config = ModelConfig(
                model=model_override,
                effort=base_config.effort,
                max_tokens=base_config.max_tokens,
                is_batch=base_config.is_batch,
            )
        model_config = base_config
        provider_name: str | None = None
        role_name = tasks[0].role
        role_policy = self._role_model_policy.get(role_name)
        role_policy_match = "exact"
        if role_policy is None:
            role_policy = self._role_model_policy.get("default")
            role_policy_match = "default"
        if role_policy is None:
            if self._role_model_policy:
                # role_model_policy IS configured (non-empty) but neither this
                # role nor a "default" key exists in it - this is an operator
                # misconfiguration, not "no policy at all". Fail loudly rather
                # than silently falling through to code-level defaults.
                role_policy_match = "HARD FAIL"
                logger.info(
                    "role_model_policy resolution for role=%r: match=%s, resolved=None, available_keys=%s",
                    role_name,
                    role_policy_match,
                    sorted(self._role_model_policy.keys()),
                )
                raise ModelNotConfiguredError(
                    f"No model configured for role={role_name!r}: role_model_policy is "
                    f"non-empty but has neither an entry for {role_name!r} nor a 'default' "
                    f"entry. Available role_model_policy keys: "
                    f"{sorted(self._role_model_policy.keys())}. Add a role entry or a "
                    "'default' entry to role_model_policy in the YAML config."
                )
            # role_model_policy itself is empty/not configured at all - other
            # mechanisms downstream (router, adapter defaults, seed config)
            # may still supply a model, so it's OK to fall through with {}.
            role_policy = {}
            role_policy_match = "none"
        logger.info(
            "role_model_policy resolution for role=%r: match=%s, resolved=%s, available_keys=%s",
            role_name,
            role_policy_match,
            role_policy,
            sorted(self._role_model_policy.keys()),
        )
        # Per-step CLI override (plan-file `cli:` field) wins over role-level
        # role_model_policy.provider, which in turn wins over the default
        # adapter. The string is treated as a provider/adapter identifier and
        # resolved via _infer_adapter_name_for_provider downstream.
        preferred_provider = tasks[0].cli or role_policy.get("provider")

        # Retry escalation (task_lifecycle._choose_retry_escalation) stamps
        # Claude tier names ("opus"/"sonnet"/"haiku") onto ``task.model``.
        # Those are escalation labels, NOT operator pins - when the operator
        # pinned this role's model via role_model_policy, a tier-stamped
        # retry model must not shadow the pin, or the retry is spawned with
        # e.g. model="opus" against a MiniMax endpoint (400 "unknown model
        # 'opus'", run-9 attempt-8). The ab-test escape hatch
        # (metadata["pinned_model"]) still marks a tier name as a genuine
        # pin. ``metadata`` may be ``None`` on older/partial constructions.
        task_metadata = tasks[0].metadata or {}
        task_model_is_pinned = bool(task_metadata.get("pinned_model"))
        task_model_is_tier_name = tasks[0].model in _CLAUDE_TIER_MODELS
        task_model_blocks_role_policy = bool(tasks[0].model) and (task_model_is_pinned or not task_model_is_tier_name)

        if not task_model_blocks_role_policy and role_policy.get("model"):
            if tasks[0].model and tasks[0].model != role_policy["model"]:
                logger.info(
                    "Retry model decision for task %s (role=%s, retry_count=%s): "
                    "keeping operator role_model_policy model=%r, ignoring "
                    "tier-stamped task.model=%r (escalation label, not an operator pin)",
                    tasks[0].id,
                    tasks[0].role,
                    getattr(tasks[0], "retry_count", None),
                    role_policy["model"],
                    tasks[0].model,
                )
            model_config = ModelConfig(
                model=role_policy["model"],
                effort=role_policy.get("effort", base_config.effort),
                max_tokens=base_config.max_tokens,
                is_batch=base_config.is_batch,
            )
        elif not tasks[0].effort and role_policy.get("effort"):
            model_config = ModelConfig(
                model=base_config.model,
                effort=role_policy["effort"],
                max_tokens=base_config.max_tokens,
                is_batch=base_config.is_batch,
            )

        model_config, provider_name, routing_source = self._resolve_routing(
            tasks,
            model_config,
            role_policy,
            preferred_provider,
        )

        # When the run-level adapter is non-Claude and no model was pinned by the
        # operator, the heuristic/batch selector may still have produced a Claude
        # tier name (opus/sonnet/haiku). Substitute the adapter's own default so
        # the model recorded here matches what the adapter actually runs (e.g.
        # Codex gets gpt-5.4, not `codex exec -m opus`). Claude-compatible
        # adapters are returned unchanged.
        #
        # ``tasks[0].model`` is normally an operator pin and must be left
        # alone. But retry escalation (see defaults.py's escalation map) and
        # manager-created child tasks both stamp internal Claude tier names
        # ("opus"/"sonnet"/"haiku") onto ``task.model`` - those are not
        # operator pins, they're meaningless tier labels for a non-Claude
        # adapter. Treat a tier-named ``tasks[0].model`` as coercible too;
        # any other value (e.g. "MiniMax-M3") is a genuine pin and is left
        # untouched.
        #
        # Exception: callers that explicitly pin a tier name as a genuine
        # comparison point (e.g. ``bernstein ab-test --model-a opus
        # --model-b sonnet``) stamp ``metadata["pinned_model"] = True`` on
        # the task. Coercing both sides of an A/B test to the same adapter
        # default would silently collapse the comparison into A-vs-A, so
        # honor the pin and skip coercion. (``task_model_is_pinned`` /
        # ``task_model_is_tier_name`` are computed above, before the
        # role-policy model application.)
        if (
            provider_name is None
            and not role_policy.get("model")
            and (not tasks[0].model or task_model_is_tier_name)
            and not task_model_is_pinned
        ):
            model_config = _coerce_model_for_non_claude_adapter(
                model_config,
                adapter_name=self._adapter.name(),
                adapter_default_model=self._default_model or getattr(self._adapter, "default_model", None),
            )
        elif (
            provider_name is not None
            and not role_policy.get("model")
            and task_model_is_tier_name
            and not task_model_is_pinned
        ):
            # role_model_policy pinned a *provider* for this role but no
            # *model* (e.g. ``role_model_policy: {backend: {provider:
            # qwen}}``). ``provider_name`` is therefore non-None here, which
            # made the branch above a no-op - the tier name stamped by the
            # heuristic selector or retry escalation (task_lifecycle stamps
            # "opus"/"sonnet" unconditionally, see task_lifecycle.py's retry
            # escalation) would otherwise reach a non-Claude adapter
            # literally (e.g. ``qwen -m opus``). Resolve which adapter this
            # provider actually maps to (read-only name lookup, no adapter
            # instantiation / role-policy enforcement side effects - see
            # ``_primary_adapter_supports_sampling``'s docstring for why
            # ``_get_adapter_by_name`` is avoided at this point in the spawn
            # path) and coerce against ITS default model, not
            # ``self._adapter``'s (the two can differ, e.g.
            # ``self._adapter=claude``, ``role_policy.provider=qwen``).
            resolved_adapter_name = self._infer_adapter_name_for_provider(provider_name, model_config.model)
            before_model = model_config.model
            model_config = _coerce_model_for_non_claude_adapter(
                model_config,
                adapter_name=resolved_adapter_name,
                adapter_default_model=self._default_model,
            )
            logger.info(
                "Provider-only role_policy coercion for role=%s: provider=%s -> "
                "resolved_adapter=%s, tier-stamped model=%r %s (task_model=%r, "
                "role_policy_provider=%r, role_policy_model=%r)",
                tasks[0].role,
                provider_name,
                resolved_adapter_name,
                before_model,
                f"coerced to {model_config.model!r}" if model_config.model != before_model else "left unchanged",
                tasks[0].model,
                role_policy.get("provider"),
                role_policy.get("model"),
            )

        logger.info(
            "Model selection for role=%s: model=%s effort=%s provider=%s source=%s "
            "role_policy_model=%s task_model=%s base_config_model=%s",
            tasks[0].role,
            model_config.model,
            model_config.effort,
            provider_name or self._adapter.name(),
            routing_source,
            role_policy.get("model"),
            tasks[0].model,
            base_config.model,
        )

        provider_for_rate_limit = provider_name or self._adapter.name()
        try:
            self._spawn_rate_limiter.acquire(provider_for_rate_limit)
        except SpawnRateLimitExceeded as exc:
            logger.warning(
                "Spawn rate limit exceeded for provider '%s' -- retry in %.1fs",
                exc.provider,
                exc.retry_after_s,
            )
            raise SpawnError(
                f"Spawn rate limit exceeded for provider '{exc.provider}'. Retry after {exc.retry_after_s:.1f}s."
            ) from exc

        # Check catalog for a specialist agent before building from templates
        role = tasks[0].role
        task_description = " ".join(t.description for t in tasks)
        catalog_agent: CatalogAgent | None = None
        if self._catalog is not None:
            catalog_agent = self._catalog.match(role, task_description)

        # Build session ID early so we can inject it into the prompt for signal checks
        session_id = f"{role}-{uuid.uuid4().hex[:8]}"

        # Lethal-trifecta structural check (orchestration-time).  See
        # bernstein.core.security.capability_matrix.  The adapter envelope
        # plus any catalog-declared tools form the chain we evaluate.
        self._enforce_lethal_trifecta(session_id, role, catalog_agent)

        # Build catalog system prompt, appending tool preferences when present
        catalog_system_prompt: str | None = None
        if catalog_agent and catalog_agent.system_prompt:
            catalog_system_prompt = catalog_agent.system_prompt
            if catalog_agent.tools:
                tools_hint = "\n\n## Preferred tools\nUse these tools when available: " + ", ".join(
                    f"`{t}`" for t in catalog_agent.tools
                )
                catalog_system_prompt = catalog_system_prompt + tools_hint

        # Compute per-task token budget from scope (use highest scope in batch)
        _scope_order = {"small": 0, "medium": 1, "large": 2}
        max_scope = max((t.scope.value for t in tasks), key=lambda s: _scope_order.get(s, 1))
        task_token_budget = self._max_tokens_per_task.get(max_scope, 0)

        # Batch execution mode: single task delegates to Claude Code /batch skill.
        # The outer agent handles decomposition, parallel subagent spawning, and
        # PR-per-unit creation internally, so Bernstein only needs one process.
        is_batch_mode = any(t.execution_mode == "batch" for t in tasks)

        # Render prompt (catalog system_prompt replaces role template when matched)
        bulletin_summary = self._bulletin.summary() if self._bulletin is not None else ""
        meta_messages = list(tasks[0].meta_messages)
        mailbox_section = self._render_mailbox_section(tasks)

        # Best-effort max_turns resolution for the turn-budget prompt nudge
        # (work/bernstein/m27-nudge-plan.md, Approach C MINIMAL). The
        # AUTHORITATIVE value for openai_agents spawns is resolved later,
        # inside OpenAIAgentsAdapter._build_manifest() (mcp_config override >
        # _resolve_max_turns()), which runs after this prompt is already
        # built - there is no Bernstein-owned hook to inject text into an
        # already-rendered prompt from there. So we mirror that same
        # precedence HERE, at prompt-build time, using a plain read (no
        # mutation of task/adapter state): explicit per-task override first,
        # then the same env-var/tuning-default resolver the runner itself
        # uses. This can diverge from the adapter's final value only if a
        # per-spawn mcp_config override is injected between here and the
        # adapter call (not done anywhere in this codebase today - see grep
        # for "mcp_config...max_turns" - so in practice they match for every
        # current call path).
        #
        # Explicit values follow the same max-over-tasks rule as the
        # explicit_max_turns threading in the spawn loop below, so the
        # prompt describes the same cap the adapter is handed. The
        # env/tuning fallback mirrors a resolver that ONLY the
        # openai_agents runner enforces; other adapters compute their own
        # turn budgets (e.g. the claude adapter's effort/scope-based
        # computation in _build_command), so applying the fallback there
        # would state a cap the adapter never enforces and would add the
        # budget section to every default spawn's prompt. Gate the
        # fallback to spawns resolved to the openai_agents adapter;
        # everything else renders the section only for an explicit
        # Task.max_turns. Default spawns on other adapters keep a
        # byte-identical prompt.
        _effective_max_turns = max((t.max_turns for t in tasks if t.max_turns is not None), default=None)
        _max_turns_source = "task.max_turns (explicit per-task override)"
        if _effective_max_turns is None:
            _budget_adapter_name = adapter_name_for_provider(provider_name, model_config.model)
            if _budget_adapter_name is None:
                from bernstein.adapters.openai_agents import OpenAIAgentsAdapter

                _spawns_turn_capped_runner = isinstance(self._adapter, OpenAIAgentsAdapter)
            else:
                _spawns_turn_capped_runner = _budget_adapter_name == "openai_agents"
            if _spawns_turn_capped_runner:
                try:
                    from bernstein.adapters.openai_agents_runner import _resolve_max_turns

                    _effective_max_turns = _resolve_max_turns()
                    _max_turns_source = (
                        "openai_agents_runner._resolve_max_turns "
                        "(env BERNSTEIN_MAX_TURNS / tuning.agent.max_turns / SDK default)"
                    )
                except Exception as exc:
                    logger.debug(
                        "Turn-budget prompt injection: _resolve_max_turns() unavailable for session=%s (%s); "
                        "prompt will omit the turn-budget section",
                        session_id,
                        exc,
                    )
                    _effective_max_turns = None
                    _max_turns_source = "unresolved (import/call failed)"
            else:
                _max_turns_source = "skipped (adapter does not enforce the openai_agents turn-cap resolver)"
        logger.info(
            "Turn-budget max_turns resolution for session=%s: value=%r source=%s",
            session_id,
            _effective_max_turns,
            _max_turns_source,
        )

        if is_batch_mode:
            # Use the first batch task as the primary task for the /batch prompt.
            # Multi-task batches with mode=batch are unusual but we handle them by
            # using the first task's goal as the primary directive.
            prompt = _render_batch_prompt(tasks[0])
            logger.info(
                "Batch execution mode: spawning single agent with /batch prompt for task %s",
                tasks[0].id,
            )
        else:
            prompt = _render_prompt(
                tasks,
                self._templates_dir,
                self._workdir,
                self._agency_catalog,
                spawner_config=getattr(self, "_config", None),
                catalog_system_prompt=catalog_system_prompt,
                context_builder=self._context_builder,
                session_id=session_id,
                bulletin_summary=bulletin_summary,
                token_budget=task_token_budget,
                meta_messages=meta_messages,
                max_turns=_effective_max_turns,
                mailbox_section=mailbox_section,
            )

        agent_source = catalog_agent.source if catalog_agent else "built-in"
        if catalog_agent:
            logger.info(
                "Catalog agent '%s' (source=%s) selected for role '%s'",
                catalog_agent.name,
                catalog_agent.source,
                role,
            )
        # Determine isolation mode
        isolation_mode = IsolationMode.NONE
        if self._container_mgr is not None:
            isolation_mode = IsolationMode.CONTAINER
        elif self._use_worktrees:
            isolation_mode = IsolationMode.WORKTREE

        # Resolve the per-spawn response-style profile.
        # Resolution is deterministic (task metadata > role policy > seed
        # default > "balanced") and the rendered addendum flows to the
        # adapter via ``system_addendum`` - the rendered prompt itself is
        # untouched, so a spawn whose resolution lands on the neutral
        # "balanced" style (empty addendum) is byte-identical to a
        # pre-change spawn. The profile name and addendum hash are stamped
        # on the session and task metadata so the completion-time cost
        # ledger entry and the audit trail can attribute spend per profile.
        style_resolution = resolve_response_style(
            task_metadata=task_metadata,
            role_policy=role_policy,
            default_policy=self._role_model_policy.get("default") or {},
        )
        try:
            style_addendum = render_style_addendum(style_resolution.style, workdir=self._workdir)
        except ResponseStyleTemplateError as exc:
            raise SpawnError(
                f"Response-style profile {style_resolution.style!r} for role {role!r} "
                f"(source={style_resolution.source}) cannot be rendered: {exc}"
            ) from exc
        profile_content_sha = addendum_sha256(style_addendum)
        for _t in tasks:
            if isinstance(_t.metadata, dict):
                self._maybe_record_profile_transition(
                    task_id=_t.id,
                    session_id=session_id,
                    prev_profile=str(_t.metadata.get("response_profile") or ""),
                    prev_sha=str(_t.metadata.get("profile_content_sha256") or ""),
                    new_profile=style_resolution.style,
                    new_sha=profile_content_sha,
                )
                _t.metadata["response_profile"] = style_resolution.style
                _t.metadata["profile_content_sha256"] = profile_content_sha
        logger.info(
            "Response-style profile for role=%s: style=%s source=%s addendum_sha256=%s",
            role,
            style_resolution.style,
            style_resolution.source,
            profile_content_sha,
        )
        self._emit_response_profile_audit(
            task_ids=[t.id for t in tasks],
            style=style_resolution.style,
            source=style_resolution.source,
            profile_content_sha256=profile_content_sha,
        )

        session = AgentSession(
            id=session_id,
            role=role,
            task_ids=[t.id for t in tasks],
            model_config=model_config,
            status="starting",
            provider=provider_name,
            agent_source=agent_source,
            isolation=isolation_mode.value,
            token_budget=task_token_budget,
            meta_messages=meta_messages,
            response_profile=style_resolution.style,
            profile_content_sha256=profile_content_sha,
        )

        # Zero-trust: issue a short-lived, task-scoped JWT for this agent.
        # The token is written to a 0600 file and its path is injected into
        # the prompt so the agent can include it in task server requests.
        # We wrap in try/except so auth failures never block spawning.
        try:
            task_ids_for_scope = [t.id for t in tasks]
            _token_path = self._issue_agent_token(session_id, role, task_ids_for_scope)
            prompt = prompt + _render_auth_section(_token_path)
        except Exception as _token_exc:
            # Only the session_id and exception are logged.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.warning("Zero-trust token issuance failed for %s: %s", session_id, _token_exc)

        # Prompt size pre-check: estimate token count and reject or
        # truncate before spending a worktree + adapter spawn on an oversized prompt.
        from bernstein.core.prompt_precheck import PromptAction, check_prompt_size, truncate_prompt

        _precheck = check_prompt_size(prompt, model=model_config.model)
        if _precheck.action == PromptAction.REJECT:
            logger.error("Prompt too large for session %s: %s", session_id, _precheck.message)
            raise SpawnError(f"Prompt size pre-check failed: {_precheck.message}")
        elif _precheck.action == PromptAction.TRUNCATE:
            logger.warning(
                "Prompt exceeds 80%% of context window for session %s; truncating. %s",
                session_id,
                _precheck.message,
            )
            prompt = truncate_prompt(prompt, _precheck.safe_char_limit)

        # Determine working directory: repo-specific > worktree > shared workdir
        spawn_cwd = self._workdir
        worktree_repo_root = self._workdir.resolve()

        # If the task targets a specific repo in a multi-repo workspace,
        # use that repo's path as the working directory.
        task_repo = tasks[0].repo
        if task_repo is not None and self._workspace is not None:
            try:
                spawn_cwd = self._workspace.resolve_repo(task_repo)
                worktree_repo_root = spawn_cwd.resolve()
                logger.info("Task targets repo '%s', spawn cwd: %s", task_repo, spawn_cwd)
            except KeyError:
                logger.warning(
                    "Task repo '%s' not found in workspace, falling back to workdir",
                    task_repo,
                )

        worktree_mgr = self._worktree_manager_for_repo(worktree_repo_root)
        if self._use_worktrees and worktree_mgr is not None:
            # Try acquiring a pre-provisioned worktree from the warm pool first.
            # This avoids the 5-15s ``git worktree add`` overhead on hot paths.
            #
            # Issue #1109: fresh-context retries bypass the warm pool so any
            # state baked into a pre-warmed worktree (cached prompt prefixes,
            # half-installed deps, leftover indexes) cannot leak across the
            # restart boundary.
            warm_entry = (
                self._warm_pool.claim_slot(role) if self._warm_pool is not None and not fresh_restart_on_retry else None
            )
            if warm_entry is not None:
                spawn_cwd = Path(warm_entry.worktree_path)
                self._worktree_paths[session_id] = spawn_cwd
                self._worktree_roots[session_id] = worktree_repo_root
                self._warm_pool_entries[session_id] = warm_entry
                logger.info(
                    "Using warm pool slot %s for session %s (role=%s)",
                    warm_entry.slot_id,
                    session_id,
                    role,
                )
            else:
                try:
                    spawn_cwd = worktree_mgr.create(session_id)
                    self._worktree_paths[session_id] = spawn_cwd
                    self._worktree_roots[session_id] = worktree_repo_root
                except WorktreeError as exc:
                    raise SpawnError(
                        f"Cannot create workspace for agent {session_id}: {exc}. "
                        "Fix: run 'bernstein stop' then restart, or delete .sdd/worktrees/ manually"
                    ) from exc

        # Build per-task MCP config: auto-detected servers merged with base config
        effective_mcp = self._mcp_config
        if self._mcp_registry is not None:
            effective_mcp = self._mcp_registry.resolve_for_tasks(tasks, base_config=self._mcp_config)

        # Layer MCPManager servers on top (task-requested MCP servers)
        if self._mcp_manager is not None:
            # Collect MCP server names requested by tasks in this batch
            task_server_names: list[str] = []
            for t in tasks:
                task_server_names.extend(t.mcp_servers)
            # Deduplicate while preserving order
            seen: set[str] = set()
            unique_names: list[str] = []
            for n in task_server_names:
                if n not in seen:
                    seen.add(n)
                    unique_names.append(n)
            # Pass None to get all servers when no specific ones requested
            requested = unique_names or None
            effective_mcp = self._mcp_manager.build_mcp_config_for_task(
                task_mcp_servers=requested,
                base_config=effective_mcp,
            )
            # Validate that MCP servers are ready before spawning the agent.
            # A non-ready server is logged as a warning but does not block spawn
            # so that a single failing optional server does not halt all work.
            try:
                from bernstein.core.mcp_readiness import validate_mcp_readiness

                validate_mcp_readiness(
                    self._mcp_manager,
                    server_names=unique_names or None,
                    fail_on_error=False,
                )
            except Exception:
                logger.warning("MCP readiness probe raised unexpectedly (non-fatal)", exc_info=True)

        # Layer per-role endpoint overrides and mode-profile sampling params
        # onto the per-spawn config. Both feed the same ``SAMPLING_PARAM_KEYS``
        # slots the adapter manifest reads, and both are opt-in: absent config
        # leaves ``effective_mcp`` byte-identical to today. An explicit value
        # already present in ``mcp_config`` always wins over these derived
        # defaults, so operator-set overrides are never silently replaced.
        effective_mcp = self._apply_sampling_overrides(
            effective_mcp,
            role_policy=role_policy,
            model_config=model_config,
            tasks=tasks,
            provider_name=provider_name,
        )

        log_dir = spawn_cwd / ".sdd" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        preferred_log_path = log_dir / f"{session_id}.log"

        # Write a task-specific CLAUDE.md at the worktree root so the agent
        # inherits its assigned tasks, role constraints, owned file paths,
        # and context files instead of only the generic project CLAUDE.md
        # . The helper also marks the file as skip-worktree so
        # the override never lands in merge commits.
        _task_context_files: list[str] = []
        for _t in tasks:
            _cfs = _t.metadata.get("context_files") if isinstance(_t.metadata, dict) else None
            if isinstance(_cfs, list):
                for _cf in _cfs:
                    if isinstance(_cf, str) and _cf not in _task_context_files:
                        _task_context_files.append(_cf)
        try:
            write_claude_md(
                spawn_cwd,
                tasks,
                session_id=session_id,
                role=role,
                workdir=self._workdir,
                context_files=_task_context_files or None,
            )
        except Exception as exc:  # pragma: no cover - best-effort, never blocks spawn
            logger.warning("Failed to write task-specific CLAUDE.md for %s: %s", session_id, exc)

        # Inject role-specific skills into the worktree before spawn so the
        # agent picks up orchestration protocol and role-specific instructions.
        # Skills survive context compaction and reduce prompt boilerplate.
        inject_skills(
            workdir=spawn_cwd,
            role=role,
            tasks=tasks,
            session_id=session_id,
            templates_dir=self._templates_dir,
        )
        _inject_scheduled_tasks(
            workdir=spawn_cwd,
            session_id=session_id,
            health_interval_minutes=_health_check_interval(tasks),
        )

        remote_spawned = False
        if self._runtime_bridge is not None:
            # Same capability gate as the local adapter loop below: the
            # bridge spawn request has no way to carry sampling/endpoint
            # overrides, so requesting them alongside a configured bridge
            # must fail loudly instead of running the task remotely with
            # provider defaults.
            _bridge_sampling_keys = tuple(
                key for key in SAMPLING_PARAM_KEYS if effective_mcp is not None and effective_mcp.get(key) is not None
            )
            if _bridge_sampling_keys:
                raise SamplingParamsRefusal(self._runtime_bridge.name(), _bridge_sampling_keys)
            try:
                remote_spawned = self._spawn_via_runtime_bridge(
                    session=session,
                    prompt=prompt,
                    spawn_cwd=spawn_cwd,
                    model_config=model_config,
                    preferred_log_path=preferred_log_path,
                )
            except BridgeError as exc:
                fallback_allowed = bool(self._runtime_bridge.config.extra.get("fallback_to_local", True))
                if not fallback_allowed:
                    raise SpawnError(f"OpenClaw bridge rejected spawn for {session_id}: {exc}") from exc
                logger.warning(
                    "OpenClaw bridge failed before acceptance for %s, falling back to local adapter: %s",
                    session_id,
                    exc,
                )

        # Spawn via adapter with runtime provider/adapter failover.
        # This is critical for real-world rate-limit handling where a chosen
        # provider may fail at process-start time.
        #
        # In unattended mode, wrap the spawn with persistent retry
        # (exponential backoff + heartbeats) for rate-limit errors.
        from bernstein.core.rate_limit_tracker import (
            UnattendedRetryPolicy,
            is_unattended_mode,
        )

        _unattended_policy: UnattendedRetryPolicy | None = None
        if is_unattended_mode():
            _unattended_policy = UnattendedRetryPolicy()
            logger.info("Unattended mode: retry rate-limit errors with backoff")

        _unattended_max = _unattended_policy.max_retries if _unattended_policy is not None else 1
        _unattended_attempt = 0
        result: SpawnResult | None = None

        # Touch heartbeat file BEFORE spawn so the watchdog sees the agent as
        # alive from the moment it starts - avoids a race window where the
        # process is running but no heartbeat file exists yet.
        with suppress(OSError):
            hb_dir = self._workdir / ".sdd" / "runtime" / "heartbeats"
            hb_dir.mkdir(parents=True, exist_ok=True)
            hb_file = hb_dir / f"{session_id}.json"
            hb_file.write_text(json.dumps({"timestamp": time.time(), "status": "starting"}))

        while True:
            # Remote spawn already succeeded - skip the local adapter loop entirely
            if remote_spawned:
                break
            if not remote_spawned:
                attempt_errors: list[str] = []
                disabled_providers: dict[str, bool] = {}
                attempted: set[tuple[str | None, str, str]] = set()
                max_attempts = max(1, len(self._router.state.providers) if self._router is not None else 1) + 2
                while len(attempted) < max_attempts:
                    adapter_name = self._infer_adapter_name_for_provider(provider_name, model_config.model)
                    attempt_key = (provider_name, adapter_name, model_config.model)
                    if attempt_key in attempted:
                        break
                    attempted.add(attempt_key)

                    try:
                        target_adapter = self._get_adapter_by_name(adapter_name, role=session.role)
                    except Exception as exc:
                        attempt_errors.append(f"{adapter_name}: {exc}")
                        break

                    # Fail loudly when sampling/endpoint overrides are
                    # requested for an adapter that does not declare the
                    # SUPPORTS_SAMPLING_PARAMS capability.  Silently
                    # dropping them would run the task with parameters the
                    # operator did not ask for, so this raises instead of
                    # falling through to provider failover.
                    ensure_sampling_params_supported(target_adapter, effective_mcp)

                    # Per-attempt config so a failover to a different
                    # adapter never inherits another adapter's extras.
                    attempt_mcp = self._mcp_config_for_adapter(target_adapter, effective_mcp)

                    # Wave 3 (per-agent instrumentation): tell the
                    # openai_agents runner subprocess which task it is
                    # working so its RunInstrumenter writes to
                    # .sdd/runs/<run_id>/tasks/<task_id>/agents/<agent_id>/
                    # instead of an "unknown" task bucket. Scoped to the
                    # openai_agents adapter only: other adapters pass
                    # mcp_config through to their own CLI flags verbatim,
                    # and a stray top-level "task_id" key there is an
                    # unnecessary risk for no benefit (those adapters are
                    # not instrumented in this wave).
                    if "openai_agents" in adapter_name and tasks:
                        attempt_mcp = dict(attempt_mcp or {})
                        attempt_mcp.setdefault("task_id", tasks[0].id)
                        # Bug fix (instrumentation audit, bug 3 - "4 of 9
                        # implement tasks have zero instrumentation"): this
                        # spawn can carry MULTIPLE tasks in one agent
                        # process (role-batched spawns / spawn_for_resume
                        # with a multi-task batch). Only tagging tasks[0].id
                        # meant every OTHER task in the batch got no
                        # instrumentation directory at all - the runner's
                        # singleton RunInstrumenter only ever knew about the
                        # first task. Pass the FULL id list so the runner
                        # can fan its JSONL writes out to every task's own
                        # agents/<agent_id>/ directory, not just the first.
                        all_task_ids = [t.id for t in tasks if getattr(t, "id", None)]
                        if len(all_task_ids) > 1:
                            attempt_mcp.setdefault("task_ids", all_task_ids)
                        logger.info(
                            "instrumentation task-id injection: adapter=%s primary_task_id=%s "
                            "batch_size=%d all_task_ids=%s",
                            adapter_name,
                            tasks[0].id,
                            len(tasks),
                            all_task_ids,
                        )

                    # Inline per-role council block
                    # (``role_model_policy.<role>.council``, parsed and
                    # validated by ``seed_parser._parse_council``): forward
                    # it so the runner manifest gets the same ``council``
                    # payload the ``model: councils/<name>.yaml`` file
                    # convention produces via ``_load_council_config``.
                    # Scoped to the openai_agents adapter only - its runner
                    # is the sole consumer of ``manifest.council``, and
                    # other adapters treat unknown top-level mcp_config
                    # keys as MCP server entries (see claude.py's
                    # bare-servers fallback). An operator-set
                    # ``mcp_config["council"]`` always wins (setdefault).
                    if "openai_agents" in adapter_name:
                        role_council = role_policy.get("council")
                        if isinstance(role_council, dict) and role_council:
                            attempt_mcp = dict(attempt_mcp or {})
                            attempt_mcp.setdefault("council", role_council)
                            logger.info(
                                "spawn_for_tasks: role=%r inline role_model_policy council block "
                                "forwarded into the runner manifest (candidates=%d)",
                                tasks[0].role if tasks else None,
                                len(role_council.get("candidates") or ()),
                            )

                    try:
                        # Apply OS-level resource limits to non-sandboxed spawns.
                        target_adapter.set_resource_limits(self._resource_limits)
                        spawn_start = time.perf_counter()
                        if self._in_process is not None and self._backend == AgentBackend.IN_PROCESS:
                            # In-process: run the adapter's subprocess via
                            # a thread inside the current Python process
                            fake_pid, actual_log_path = self._in_process.run(
                                prompt=prompt,
                                workdir=spawn_cwd,
                                model_config=model_config,
                                session_id=session_id,
                                mcp_config=attempt_mcp,
                            )
                            result = SpawnResult(pid=fake_pid, log_path=actual_log_path)
                        elif self._sandbox_session_routing_active():
                            # oai-002 phase 2: route exec through a
                            # SandboxSession (Docker, E2B, Modal,
                            # plugin) - either the shared session
                            # attached at construction or a per-spawn
                            # session provisioned from the attached
                            # backend (issue #2162).  The local-worktree
                            # backend is intentionally excluded so the
                            # existing direct-subprocess path keeps
                            # worker-wrapper / PID semantics intact.
                            result = self._spawn_via_sandbox_session(
                                session_id=session_id,
                                prompt=prompt,
                                spawn_cwd=spawn_cwd,
                                model_config=model_config,
                                mcp_config=attempt_mcp,
                                session=session,
                                adapter=target_adapter,
                            )
                        elif self._sandbox is not None:
                            result = self._spawn_in_sandbox(
                                session_id=session_id,
                                prompt=prompt,
                                spawn_cwd=spawn_cwd,
                                model_config=model_config,
                                mcp_config=attempt_mcp,
                                session=session,
                                adapter=target_adapter,
                                task_scope=max_scope,
                            )
                        elif self._container_mgr is not None:
                            result = self._spawn_in_container(
                                session_id=session_id,
                                prompt=prompt,
                                spawn_cwd=spawn_cwd,
                                model_config=model_config,
                                mcp_config=attempt_mcp,
                                session=session,
                                adapter=target_adapter,
                                task_scope=max_scope,
                            )
                        else:
                            # Extract budget_multiplier from task metadata
                            # (set by retry logic when previous attempt hit budget cap).
                            _budget_mult = max(float(t.metadata.get("budget_multiplier", 1.0)) for t in tasks)
                            # Explicit per-task max_turns override (Task.max_turns):
                            # thread it to the adapter as explicit_max_turns, but
                            # only when its spawn() signature accepts the
                            # parameter. Adapters without support keep their own
                            # auto-computed turn budget; warn so the operator
                            # knows the cap was not applied. When several grouped
                            # tasks carry a value the largest wins, mirroring
                            # budget_multiplier above.
                            _extra_spawn_kwargs: dict[str, Any] = {}
                            _explicit_turns = max((t.max_turns for t in tasks if t.max_turns is not None), default=None)
                            if _explicit_turns is not None:
                                if "explicit_max_turns" in inspect.signature(target_adapter.spawn).parameters:
                                    _extra_spawn_kwargs["explicit_max_turns"] = _explicit_turns
                                else:
                                    logger.warning(
                                        "Adapter %s spawn() does not accept explicit_max_turns; "
                                        "task max_turns=%d ignored, falling back to adapter-computed turns",
                                        adapter_name,
                                        _explicit_turns,
                                    )
                            # Cacheable prefix extraction is deferred to adapters
                            # that support provider-specific caching.
                            result = target_adapter.spawn(
                                prompt=prompt,
                                workdir=spawn_cwd,
                                model_config=model_config,
                                session_id=session_id,
                                mcp_config=attempt_mcp,
                                task_scope=max_scope,
                                budget_multiplier=_budget_mult,
                                system_addendum=style_addendum,
                                **_extra_spawn_kwargs,
                            )
                        spawn_duration = time.perf_counter() - spawn_start
                        agent_spawn_duration.labels(adapter=provider_name or adapter_name).observe(spawn_duration)
                        self._adapter_health.record_success(adapter_name, latency_ms=spawn_duration * 1000)
                        if provider_name is not None:
                            session.provider = provider_name
                        elif self._router and self._router.state.providers:
                            session.provider = adapter_name
                        else:
                            session.provider = None
                        session.model_config = model_config
                        break
                    except RateLimitError as exc:
                        attempt_errors.append(f"{adapter_name}: {exc}")
                        self._adapter_health.record_failure(adapter_name)
                        logger.warning(
                            "Rate-limit detected for provider=%s adapter=%s; retrying with alternate provider",
                            provider_name or adapter_name,
                            adapter_name,
                        )
                        if self._router is None or provider_name is None:
                            continue
                        provider_cfg = self._router.state.providers.get(provider_name)
                        if provider_cfg is not None:
                            provider_cfg.health.status = ProviderHealthStatus.RATE_LIMITED
                            if provider_name not in disabled_providers:
                                disabled_providers[provider_name] = provider_cfg.available
                            provider_cfg.available = False
                        try:
                            decision = self._router.select_provider_for_task(tasks[0], base_config=model_config)
                            provider_name = decision.provider
                            model_config = decision.model_config
                        except RouterError:
                            provider_name = None
                    except Exception as exc:
                        categorized = classify_spawn_error(exc, provider=provider_name)
                        # Re-derive the failure reason from the runner's own
                        # per-session log instead of trusting str(exc), which
                        # for fast-exit-probe failures (adapters/base.py)
                        # only ever embeds the log's LAST LINE - see
                        # extract_error_aware_reason()'s module docstring
                        # and work/bernstein/proofs/d2/minimax/FAIL-NOTE.md.
                        diagnosed_reason = _diagnose_spawn_failure(session_id, spawn_cwd, adapter_name, exc)
                        attempt_errors.append(f"{adapter_name}: {diagnosed_reason}")

                        # Fail-fast for permanent and operator-fix errors - no
                        # point trying alternate providers when the binary is
                        # missing or credentials are invalid.
                        if categorized.retry_strategy in (
                            RetryStrategy.NO_RETRY,
                            RetryStrategy.RETRY_AFTER_FIX,
                        ):
                            logger.warning(
                                "Spawn failure is non-retryable (strategy=%s session=%s adapter=%s): %s",
                                categorized.retry_strategy.value,
                                session_id,
                                adapter_name,
                                diagnosed_reason,
                            )
                            self._adapter_health.record_failure(adapter_name)
                            break

                        self._adapter_health.record_failure(adapter_name)
                        logger.warning(
                            "Agent spawn failed (session=%s provider=%s adapter=%s strategy=%s): %s",
                            session_id,
                            provider_name,
                            adapter_name,
                            categorized.retry_strategy.value,
                            diagnosed_reason,
                        )
                        if self._router is None or provider_name is None:
                            continue
                        provider_cfg = self._router.state.providers.get(provider_name)
                        if provider_cfg is not None:
                            self._router.update_provider_health(provider_name, success=False)
                            if provider_name not in disabled_providers:
                                disabled_providers[provider_name] = provider_cfg.available
                            provider_cfg.available = False
                        try:
                            decision = self._router.select_provider_for_task(tasks[0], base_config=model_config)
                            provider_name = decision.provider
                            model_config = decision.model_config
                        except RouterError:
                            provider_name = None

                for prov, was_available in disabled_providers.items():
                    provider_cfg = self._router.state.providers.get(prov) if self._router is not None else None
                    if provider_cfg is not None:
                        provider_cfg.available = was_available

                if result is None:
                    error_text = "; ".join(attempt_errors) or "no viable spawn attempts"
                    if _unattended_policy is not None:
                        _unattended_attempt += 1
                        if _unattended_attempt < _unattended_max:
                            delay = _unattended_policy.next_delay(_unattended_attempt)
                            signals_dir = spawn_cwd / ".sdd" / "runtime" / "signals"
                            logger.warning(
                                "Unattended retry: cycle %d/%d, sleeping %.0fs",
                                _unattended_attempt,
                                _unattended_max,
                                delay,
                            )
                            _unattended_policy.wait_with_heartbeats(
                                session_id,
                                _unattended_attempt,
                                f"429 rate limit ({error_text})",
                                signals_dir=signals_dir,
                            )
                            # Reset provider availability for the retry
                            if self._router is not None:
                                for _p, _was_available in disabled_providers.items():
                                    _pcfg = self._router.state.providers.get(_p)
                                    if _pcfg is not None:
                                        _pcfg.available = _was_available
                            # Re-select provider for the retry
                            if self._router is not None and self._router.state.providers:
                                with suppress(RouterError):
                                    _decision = self._router.select_provider_for_task(
                                        tasks[0], base_config=model_config
                                    )
                                    provider_name = _decision.provider
                                    model_config = _decision.model_config
                            continue
                    # Release warm pool slot before raising so the pre-provisioned
                    # worktree is not permanently leaked (BUG-19).
                    self._release_warm_pool_slot(session_id)
                    raise RuntimeError(f"All spawn attempts failed for session {session_id}: {error_text}")
                # Success - exit the retry loop
                break

        # Post-spawn session setup
        if result is not None:
            session.pid = result.pid
            session.abort_reason = result.abort_reason
            session.abort_detail = result.abort_detail
            session.finish_reason = result.finish_reason
            if result.log_path:
                session.log_path = str(result.log_path)

        if session.status != "working":
            transition_agent(
                session,
                "working",
                actor="spawner",
                reason="agent process started",
            )
        if result is not None and result.proc is not None:
            self._procs[session_id] = result.proc  # type: ignore[assignment]
            # Register stdin pipe for real-time IPC (if available)
            proc_stdin = getattr(result.proc, "stdin", None)
            if proc_stdin is not None:
                from bernstein.core.agents.agent_ipc import register_stdin_pipe

                register_stdin_pipe(session_id, proc_stdin)

        # Create and persist the initial trace
        # Serialize task fields to JSON-safe types (convert Enums to their values)
        import dataclasses

        def _task_to_dict(t: Task) -> dict[str, Any]:
            d: dict[str, Any] = {}
            for fld in dataclasses.fields(t):
                val: Any = getattr(t, fld.name)
                if hasattr(val, "value"):  # Enum
                    val = val.value
                elif isinstance(val, list):
                    val = [v.value if hasattr(v, "value") else v for v in cast("list[Any]", val)]
                d[fld.name] = val
            return d

        task_snapshots: list[dict[str, Any]] = [_task_to_dict(t) for t in tasks]
        trace = new_trace(
            session_id=session_id,
            task_ids=[t.id for t in tasks],
            role=role,
            model=model_config.model,
            effort=model_config.effort,
            log_path=session.log_path,
            task_snapshots=task_snapshots,
        )
        self._traces[session_id] = trace
        try:
            self._trace_store.write(trace)
        except Exception as exc:
            logger.warning("Failed to write initial trace for %s: %s", session_id, exc)

        get_plugin_manager().fire_agent_spawned(
            session_id=session.id, role=session.role, model=session.model_config.model
        )
        return session

    def spawn_for_resume(
        self,
        tasks: list[Task],
        *,
        worktree_path: Path,
        changed_files: list[str],
    ) -> AgentSession:
        """Spawn a new agent to resume work in a crashed agent's worktree.

        Builds a prompt that includes context about the previous crash and the
        files already modified, then spawns the agent in the preserved worktree
        directory instead of creating a new one.

        Args:
            tasks: Batch of tasks (same role) to resume.
            worktree_path: Path to the preserved worktree from the crashed agent.
            changed_files: Files already modified by the crashed agent.

        Returns:
            AgentSession with PID and metadata populated.
        """
        if not tasks:
            raise ValueError("Cannot resume with empty task list")

        # Build resume context prefix
        files_list = "\n".join(f"  - {f}" for f in changed_files) if changed_files else "  (none)"
        resume_header = (
            "## Crash recovery\n"
            "The previous agent assigned to this task crashed. "
            "Continue from where it left off.\n"
            f"Files already modified by the previous agent:\n{files_list}\n\n"
        )

        metrics_dir = self._workdir / ".sdd" / "metrics"
        # Same role-policy fallback as the main spawn path: a role-policy-only
        # config (no run-level default_model) must not fail heuristic routing.
        _policy_preview = self._role_model_policy.get(tasks[0].role) or self._role_model_policy.get("default") or {}
        model_config = _select_batch_config(
            tasks,
            templates_dir=self._templates_dir,
            metrics_dir=metrics_dir if metrics_dir.exists() else None,
            workdir=self._workdir,
            default_model=self._default_model or _policy_preview.get("model"),
        )
        role = tasks[0].role
        session_id = f"{role}-resume-{uuid.uuid4().hex[:8]}"

        meta_messages = ["This is a crash recovery session. Continue from where the previous agent left off."]

        # Same best-effort max_turns resolution as spawn_for_tasks() above
        # (work/bernstein/m27-nudge-plan.md) - crash-recovery sessions are
        # exactly the kind of short, tightly-budgeted resume where a model
        # exploring instead of finishing is most costly.
        #
        # Resume spawns go straight to ``self._adapter`` (no provider
        # routing below), so the env/tuning fallback - which only the
        # openai_agents runner enforces - applies only when that adapter
        # is the openai_agents one. See the matching gate and rationale in
        # spawn_for_tasks() above.
        _resume_max_turns = max((t.max_turns for t in tasks if t.max_turns is not None), default=None)
        if _resume_max_turns is None:
            from bernstein.adapters.openai_agents import OpenAIAgentsAdapter

            if isinstance(self._adapter, OpenAIAgentsAdapter):
                try:
                    from bernstein.adapters.openai_agents_runner import _resolve_max_turns

                    _resume_max_turns = _resolve_max_turns()
                except Exception as exc:
                    logger.debug(
                        "Turn-budget prompt injection: _resolve_max_turns() unavailable for resume session=%s (%s)",
                        session_id,
                        exc,
                    )
                    _resume_max_turns = None
        logger.info(
            "Turn-budget max_turns resolution for resume session=%s: value=%r",
            session_id,
            _resume_max_turns,
        )

        prompt = _render_prompt(
            tasks,
            self._templates_dir,
            self._workdir,
            self._agency_catalog,
            spawner_config=getattr(self, "_config", None),
            context_builder=self._context_builder,
            session_id=session_id,
            meta_messages=meta_messages,
            max_turns=_resume_max_turns,
            mailbox_section=self._render_mailbox_section(tasks),
        )
        # Prepend crash recovery context
        prompt = resume_header + prompt

        session = AgentSession(
            id=session_id,
            role=role,
            task_ids=[t.id for t in tasks],
            model_config=model_config,
            status="starting",
        )

        _scope_order = {"small": 0, "medium": 1, "large": 2}
        resume_scope = max((t.scope.value for t in tasks), key=lambda s: _scope_order.get(s, 1))
        result = self._adapter.spawn(
            prompt=prompt,
            workdir=worktree_path,
            model_config=model_config,
            session_id=session_id,
            task_scope=resume_scope,
        )
        session.pid = result.pid
        session.abort_reason = result.abort_reason
        session.abort_detail = result.abort_detail
        session.finish_reason = result.finish_reason

        # Touch heartbeat on resume spawn (same rationale as main spawn path)
        with suppress(OSError):
            hb_dir = self._workdir / ".sdd" / "runtime" / "heartbeats"
            hb_dir.mkdir(parents=True, exist_ok=True)
            hb_file = hb_dir / f"{session_id}.json"
            hb_file.write_text(json.dumps({"timestamp": time.time(), "status": "starting"}))

        transition_agent(session, "working", actor="spawner", reason="agent process started in worktree")
        if result.log_path:
            session.log_path = str(result.log_path)
        if result.proc is not None:
            self._procs[session_id] = result.proc  # type: ignore[assignment]

        # Track worktree so reap_completed_agent can merge+clean up
        self._worktree_paths[session_id] = worktree_path

        return session

    def _spawn_in_container(
        self,
        *,
        session_id: str,
        prompt: str,
        spawn_cwd: Path,
        model_config: ModelConfig,
        mcp_config: dict[str, Any] | None,
        session: AgentSession,
        adapter: CLIAdapter,
        task_scope: str = "medium",
    ) -> SpawnResult:
        """Spawn an agent inside a container.

        Builds the adapter command, then runs it inside a container
        managed by the ContainerManager.  Falls back to direct subprocess
        spawn if container creation fails.

        Args:
            session_id: Agent session ID.
            prompt: Rendered agent prompt.
            spawn_cwd: Working directory for the agent.
            model_config: Model and effort configuration.
            mcp_config: MCP server configuration.
            session: AgentSession to update with container metadata.
            adapter: Adapter selected for this spawn attempt.
            task_scope: Task scope for max_turns scaling.

        Returns:
            SpawnResult with PID and log path.
        """
        assert self._container_mgr is not None

        # Build environment for the container from the adapter's filtered env
        from bernstein.adapters.env_isolation import build_filtered_env

        adapter_name = adapter.name().lower()
        extra_keys: list[str] = []
        if "claude" in adapter_name:
            extra_keys.append("ANTHROPIC_API_KEY")
        elif "gemini" in adapter_name:
            extra_keys.extend(["GOOGLE_API_KEY", "GEMINI_API_KEY"])
        elif "codex" in adapter_name:
            extra_keys.append("OPENAI_API_KEY")
        container_env = build_filtered_env(extra_keys)

        # Write the prompt to a temp file inside the workspace so the
        # container can read it
        prompt_file = spawn_cwd / ".sdd" / "runtime" / "prompts" / f"{session_id}.md"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt, encoding="utf-8")

        # Build the CLI command the adapter would normally run
        log_dir = spawn_cwd / ".sdd" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{session_id}.log"

        # --- Two-phase sandbox (Codex-style) ---
        # Phase 1: run dependency installation with network access.
        # Phase 2: run the agent with network disabled.
        from bernstein.core.agents.container import NetworkMode, _detect_setup_commands

        two_phase_cfg = self._container_mgr.config.two_phase_sandbox
        phase2_network_override: NetworkMode | None = None

        if two_phase_cfg is not None:
            setup_cmds = list(two_phase_cfg.setup_commands) or _detect_setup_commands(spawn_cwd)
            if setup_cmds:
                ok = self._container_mgr.run_phase1_setup(
                    session_id=session_id,
                    setup_cmds=setup_cmds,
                    env=container_env,
                    workspace_override=spawn_cwd,
                    timeout_s=two_phase_cfg.phase1_timeout_s,
                )
                if not ok:
                    logger.warning(
                        "Phase 1 setup failed for %s - proceeding to Phase 2 anyway",
                        session_id,
                    )
            phase2_network_override = two_phase_cfg.phase2_network_mode

        try:
            handle = self._container_mgr.spawn_in_container(
                session_id=session_id,
                cmd=self._adapter_cmd_for_container(
                    prompt_file=prompt_file,
                    model_config=model_config,
                    session_id=session_id,
                    mcp_config=mcp_config,
                    adapter=adapter,
                ),
                env=container_env,
                workspace_override=spawn_cwd,
                log_path=log_path,
                network_mode_override=phase2_network_override,
            )
            session.container_id = handle.container_id
            session.isolation = IsolationMode.CONTAINER.value
            return SpawnResult(pid=handle.pid or 0, log_path=log_path)
        except ContainerError as exc:
            logger.warning(
                "Container spawn failed for %s, falling back to subprocess: %s",
                session_id,
                exc,
            )
            session.isolation = IsolationMode.NONE.value
            return adapter.spawn(
                prompt=prompt,
                workdir=spawn_cwd,
                model_config=model_config,
                session_id=session_id,
                mcp_config=mcp_config,
                task_scope=task_scope,
            )

    def _spawn_in_sandbox(
        self,
        *,
        session_id: str,
        prompt: str,
        spawn_cwd: Path,
        model_config: ModelConfig,
        mcp_config: dict[str, Any] | None,
        session: AgentSession,
        adapter: CLIAdapter,
        task_scope: str = "medium",
    ) -> SpawnResult:
        """Spawn an agent in a per-session Docker or Podman sandbox.

        Args:
            session_id: Agent session identifier.
            prompt: Rendered system prompt.
            spawn_cwd: Worktree or workspace path mounted into the sandbox.
            model_config: Model and effort configuration.
            mcp_config: Optional MCP configuration for the adapter.
            session: Mutable session record to update.
            adapter: Adapter selected for this spawn attempt.
            task_scope: Task scope for max_turns scaling.

        Returns:
            Spawn result for the sandboxed process.
        """
        assert self._sandbox is not None

        from bernstein.adapters.env_isolation import build_filtered_env

        adapter_name = adapter.name().lower()
        extra_keys: list[str] = []
        if "claude" in adapter_name:
            extra_keys.append("ANTHROPIC_API_KEY")
        elif "gemini" in adapter_name:
            extra_keys.extend(["GOOGLE_API_KEY", "GEMINI_API_KEY"])
        elif "codex" in adapter_name:
            extra_keys.append("OPENAI_API_KEY")
        sandbox_env = build_filtered_env(extra_keys)

        prompt_file = spawn_cwd / ".sdd" / "runtime" / "prompts" / f"{session_id}.md"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt, encoding="utf-8")

        log_dir = spawn_cwd / ".sdd" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{session_id}.log"

        try:
            manager, handle = spawn_in_sandbox(
                sandbox=self._sandbox,
                session_id=session_id,
                adapter_name=adapter_name,
                cmd=self._adapter_cmd_for_container(
                    prompt_file=prompt_file,
                    model_config=model_config,
                    session_id=session_id,
                    mcp_config=mcp_config,
                    adapter=adapter,
                ),
                env=sandbox_env,
                workdir=spawn_cwd,
                log_path=log_path,
            )
        except ContainerError as exc:
            logger.warning(
                "Sandbox runtime unavailable for %s, falling back to worktree isolation: %s",
                session_id,
                exc,
            )
            session.isolation = IsolationMode.WORKTREE.value if self._use_worktrees else IsolationMode.NONE.value
            return adapter.spawn(
                prompt=prompt,
                workdir=spawn_cwd,
                model_config=model_config,
                session_id=session_id,
                mcp_config=mcp_config,
                task_scope=task_scope,
            )

        self._sandbox_managers[session_id] = manager
        session.container_id = handle.container_id
        session.isolation = IsolationMode.CONTAINER.value
        return SpawnResult(pid=handle.pid or 0, log_path=log_path)

    def _spawn_via_sandbox_session(
        self,
        *,
        session_id: str,
        prompt: str,
        spawn_cwd: Path,
        model_config: ModelConfig,
        mcp_config: dict[str, Any] | None,
        session: AgentSession,
        adapter: CLIAdapter,
    ) -> SpawnResult:
        """Route adapter exec through a :class:`SandboxSession`.

        Phase 2 of ``oai-002``. When the spawner has been wired with a
        non-worktree :class:`SandboxBackend` (Docker, E2B, Modal,
        custom plugin), the adapter command is run via
        :meth:`SandboxSession.exec` and the prompt is injected via
        :meth:`SandboxSession.write` instead of mutating the host
        worktree directly. Issue #2162: when a backend plus manifest
        factory are attached (production wiring), a dedicated session
        is provisioned for this spawn and destroyed when the exec
        future resolves; a shared session attached at construction is
        used as-is for back-compat. The local-worktree backend is intentionally
        excluded: keeping it on the legacy direct-subprocess path
        preserves the worker-wrapper, process-group, and timeout-watchdog
        bookkeeping that production tooling depends on, and matches the
        ticket's "byte-identical behaviour for worktree-only configs"
        acceptance criterion.

        Args:
            session_id: Agent session identifier.
            prompt: Rendered system prompt.
            spawn_cwd: Worktree path on the host. Reserved for log
                output and for adapters that still read host paths.
            model_config: Model and effort configuration.
            mcp_config: Optional MCP configuration.
            session: Mutable session record updated with isolation
                metadata.
            adapter: The adapter selected for this spawn attempt.

        Returns:
            A :class:`SpawnResult`. ``pid`` is ``0`` because the
            command lives inside the backend; liveness is tracked via
            the :class:`SandboxExecHandle` stored in
            ``_sandbox_exec_handles``.
        """
        sbx_session: SandboxSession
        owned = False
        if self._sandbox_session is not None:
            # Back-compat: a single shared session attached at
            # construction. Its lifecycle belongs to whoever built it.
            sbx_session = self._sandbox_session
        else:
            # Issue #2162: one session per spawn. Provisioning failure
            # falls back to the direct adapter spawn, mirroring the
            # ContainerError fallback in _spawn_in_sandbox.
            try:
                sbx_session = self._provision_sandbox_session(session_id)
            except Exception as exc:
                logger.warning(
                    "Sandbox session provisioning failed for %s, falling back to direct spawn: %s",
                    session_id,
                    exc,
                )
                session.isolation = IsolationMode.WORKTREE.value if self._use_worktrees else IsolationMode.NONE.value
                return adapter.spawn(
                    prompt=prompt,
                    workdir=spawn_cwd,
                    model_config=model_config,
                    session_id=session_id,
                    mcp_config=mcp_config,
                )
            owned = True
            self._sandbox_owned_sessions[session_id] = sbx_session

        log_dir = spawn_cwd / ".sdd" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{session_id}.log"

        # 1) Inject the prompt through the session's file primitive.
        write_prompt_to_session(
            session=sbx_session,
            prompt=prompt,
            session_id=session_id,
        )

        # 2) Build the command using the existing container-shaped
        #    helper.  It reads the prompt from a relative path inside
        #    the workspace, which is exactly what session.exec needs.
        prompt_file = spawn_cwd / ".sdd" / "runtime" / "prompts" / f"{session_id}.md"
        cmd = self._adapter_cmd_for_container(
            prompt_file=prompt_file,
            model_config=model_config,
            session_id=session_id,
            mcp_config=mcp_config,
            adapter=adapter,
        )

        # 2b) Forward API keys to the sandbox so adapters can authenticate.
        #     IMPORTANT: do NOT use build_filtered_env() here -- it copies
        #     PATH and other host-specific vars that OVERRIDE the container's
        #     own env when passed to Docker exec_run(environment=...).
        #     Only forward the specific API keys the adapter needs.
        import os as _os

        adapter_name_lc = adapter.name().lower()
        _env_keys: list[str] = []
        if "claude" in adapter_name_lc:
            _env_keys.append("ANTHROPIC_API_KEY")
        elif "gemini" in adapter_name_lc:
            _env_keys.extend(["GOOGLE_API_KEY", "GEMINI_API_KEY"])
        else:
            # OpenAI-compatible adapters (codex, qwen, generic) only.
            # Claude/Gemini sandboxes must not receive OpenAI credentials
            # they never use (least-privilege, same per-adapter gating as
            # the legacy container env allowlists).
            _env_keys.extend(["OPENAI_API_KEY", "OPENAI_BASE_URL"])
        sandbox_env = {k: v for k in _env_keys if (v := _os.environ.get(k)) is not None}

        # 2c) Audit the exec submission (issue #2162). The argv embeds
        #     prompt paths and model names, so only its hash is chained.
        import hashlib as _hashlib

        from bernstein.core.security.audit import SANDBOX_EXEC_START

        self._emit_sandbox_audit(
            SANDBOX_EXEC_START,
            resource_id=sbx_session.session_id,
            details={
                "session_id": sbx_session.session_id,
                "adapter": adapter.name(),
                "cmd_hash": _hashlib.sha256(" ".join(cmd).encode("utf-8")).hexdigest(),
                "agent_session_id": session_id,
            },
        )

        # 3) Submit the exec on a dedicated thread; the future drives
        #    liveness checks via SandboxSession-aware paths.
        handle = submit_session_exec(
            session=sbx_session,
            cmd=cmd,
            session_id=session_id,
            log_path=log_path,
            env=sandbox_env,
            workdir=self._workdir,
        )
        self._sandbox_exec_handles[session_id] = handle

        # When the future resolves we increment the per-exit-code
        # counter, chain the exec_end audit event, sync committed work
        # back to the host, and (for per-spawn sessions) destroy the
        # session so no container outlives its agent (issue #2162).
        def _record_exit(_h: SandboxExecHandle = handle, _owned: bool = owned) -> None:
            try:
                if _h.future.cancelled():
                    code = "cancelled"
                elif _h.future.exception() is not None:
                    code = "error"
                else:
                    code = str(_h.future.result().exit_code)
            except Exception:  # pragma: no cover - defensive
                code = "error"
            sandbox_exec_count_total.labels(backend=_h.backend_name, exit_code=code).inc()
            from bernstein.core.security.audit import SANDBOX_EXEC_END

            self._emit_sandbox_audit(
                SANDBOX_EXEC_END,
                resource_id=sbx_session.session_id,
                details={
                    "session_id": sbx_session.session_id,
                    "exit_code": code,
                    "agent_session_id": session_id,
                },
            )
            # Retrieve committed work from the sandbox-local clone
            # before the session goes away. Skipped for cancelled or
            # crashed execs where the container state is undefined.
            if code not in ("cancelled", "error"):
                self._sync_back_sandbox_work(sbx_session, session_id)
            if _owned:
                self._destroy_sandbox_session(session_id)

        handle.future.add_done_callback(lambda _f: _record_exit())

        session.isolation = IsolationMode.CONTAINER.value
        session.runtime_backend = handle.backend_name
        return SpawnResult(pid=0, log_path=log_path)

    def _provision_sandbox_session(self, session_id: str) -> SandboxSession:
        """Provision a dedicated sandbox session for one spawn (issue #2162).

        One session per agent means one container per agent: an exec
        timeout that kills a container only kills that agent, and
        concurrent agents no longer share a single workspace clone.

        Args:
            session_id: Agent session identifier, recorded in the audit
                event for correlation. The backend allocates its own
                sandbox session id.

        Returns:
            The freshly created :class:`SandboxSession`.

        Raises:
            Exception: Whatever the backend raised; the caller falls
                back to a direct adapter spawn.
        """
        assert self._sandbox_backend is not None
        assert self._sandbox_manifest_factory is not None
        manifest = self._sandbox_manifest_factory()
        sbx_session = asyncio.run(self._sandbox_backend.create(manifest, options=self._sandbox_options.copy()))
        backend_name = getattr(sbx_session, "backend_name", "unknown")
        sandbox_session_created_total.labels(backend=backend_name).inc()
        logger.info(
            "Provisioned sandbox session %s for agent %s (backend=%s)",
            sbx_session.session_id,
            session_id,
            backend_name,
        )
        from bernstein.core.security.audit import SANDBOX_SESSION_CREATE

        self._emit_sandbox_audit(
            SANDBOX_SESSION_CREATE,
            resource_id=sbx_session.session_id,
            details={
                "session_id": sbx_session.session_id,
                "image": self._sandbox_options.get("image"),
                "backend": backend_name,
                "agent_session_id": session_id,
            },
        )
        self._check_task_server_reachability(sbx_session)
        return sbx_session

    def _destroy_sandbox_session(self, session_id: str) -> None:
        """Destroy the per-spawn sandbox session owned by *session_id*.

        Idempotent and race-safe: the owned-session map is popped
        first, so a :meth:`kill` racing the exec-done callback destroys
        the session exactly once. Failures log a warning, never raise.

        Args:
            session_id: Agent session whose sandbox session should go.
        """
        sbx_session = self._sandbox_owned_sessions.pop(session_id, None)
        if sbx_session is None:
            return
        try:
            if self._sandbox_backend is not None:
                asyncio.run(self._sandbox_backend.destroy(sbx_session))
            else:  # pragma: no cover - owned sessions always have a backend
                asyncio.run(sbx_session.shutdown())
        except Exception as exc:
            logger.warning(
                "Failed to destroy sandbox session %s for agent %s: %s",
                sbx_session.session_id,
                session_id,
                exc,
            )
            return
        logger.info("Destroyed sandbox session %s for agent %s", sbx_session.session_id, session_id)
        from bernstein.core.security.audit import SANDBOX_SESSION_DESTROY

        self._emit_sandbox_audit(
            SANDBOX_SESSION_DESTROY,
            resource_id=sbx_session.session_id,
            details={"session_id": sbx_session.session_id, "agent_session_id": session_id},
        )

    def _sync_back_sandbox_work(self, sbx_session: SandboxSession, session_id: str) -> None:
        """Best-effort sync of sandbox-local commits back to the host.

        Agent commits land in the sandbox's own clone (e.g.
        ``/workspace`` inside a Docker container) and would vanish with
        the session. Bundle every ref inside the sandbox, copy the
        bundle to ``.sdd/runtime/sandbox/<session_id>.bundle`` on the
        host, then fetch it into ``refs/remotes/sandbox/<session_id>/*``
        so the work stays inspectable after the run (issue #2162).

        Failures log a warning and never crash the run.

        Args:
            sbx_session: The session holding the agent's clone.
            session_id: Agent session identifier; used as the bundle
                basename and the remote-ref namespace.
        """
        import subprocess as _subprocess

        bundle_in_sandbox = f"/tmp/{session_id}.bundle"
        try:
            bundle_result = asyncio.run(
                sbx_session.exec(["git", "bundle", "create", bundle_in_sandbox, "--all"], timeout=120)
            )
            if bundle_result.exit_code != 0:
                logger.warning(
                    "Sandbox sync-back for %s: git bundle create failed (exit %d): %s",
                    session_id,
                    bundle_result.exit_code,
                    bundle_result.stderr[:500].decode("utf-8", errors="replace"),
                )
                return
            bundle_bytes = asyncio.run(sbx_session.read(bundle_in_sandbox))
            bundle_dir = self._workdir / ".sdd" / "runtime" / "sandbox"
            bundle_dir.mkdir(parents=True, exist_ok=True)
            bundle_path = bundle_dir / f"{session_id}.bundle"
            bundle_path.write_bytes(bundle_bytes)

            refspec = f"refs/heads/*:refs/remotes/sandbox/{session_id}/*"
            fetch = _subprocess.run(
                ["git", "fetch", str(bundle_path), refspec],
                cwd=self._workdir,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if fetch.returncode != 0:
                logger.warning(
                    "Sandbox sync-back for %s: git fetch from bundle failed: %s",
                    session_id,
                    fetch.stderr.strip()[:500],
                )
                return
            refs_result = _subprocess.run(
                ["git", "for-each-ref", "--format=%(refname)", f"refs/remotes/sandbox/{session_id}/"],
                cwd=self._workdir,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            fetched_refs = [line for line in refs_result.stdout.splitlines() if line]
            logger.info(
                "Synced sandbox work for %s: bundle at %s, fetched refs: %s",
                session_id,
                bundle_path,
                ", ".join(fetched_refs) or "(none)",
            )
        except Exception as exc:
            logger.warning("Sandbox sync-back for %s failed: %s", session_id, exc)

    def _check_task_server_reachability(self, sbx_session: SandboxSession) -> None:
        """Warn once when the sandbox cannot reach the host task server.

        Some Docker Desktop configurations do not support host
        networking, so agents inside the container cannot POST to the
        task server on 127.0.0.1. This probe surfaces the condition as
        an explicit warning instead of a silent run stall; the run
        proceeds and relies on the legacy path behavior (issue #2162).
        Never fails the run.

        Args:
            sbx_session: A freshly provisioned session to probe from.
        """
        port = self._sandbox_server_port
        if port is None or self._sandbox_reachability_checked:
            return
        self._sandbox_reachability_checked = True
        probe = f'import socket; socket.create_connection(("127.0.0.1", {int(port)}), timeout=3).close()'
        try:
            result = asyncio.run(sbx_session.exec(["python3", "-c", probe], timeout=15))
        except Exception as exc:
            logger.warning(
                "Could not probe task server reachability from sandbox session %s: %s",
                sbx_session.session_id,
                exc,
            )
            return
        if result.exit_code != 0:
            logger.warning(
                "Sandbox session %s cannot reach the task server on 127.0.0.1:%d; "
                "agents inside containers on this Docker daemon will not reach the "
                "task server (some Docker Desktop configurations do not support "
                "host networking). The run will rely on the legacy path behavior.",
                sbx_session.session_id,
                port,
            )

    def _emit_sandbox_audit(self, event_type: str, *, resource_id: str, details: dict[str, Any]) -> None:
        """Append a sandbox lifecycle event to the HMAC-chained audit log.

        Best-effort by design (issue #2162): audit failures (key
        permission, disk full) are logged at warning level and never
        block the spawn or teardown paths that emit them.

        All emissions are serialized through ``_SANDBOX_AUDIT_LOCK``:
        exec_end/session_destroy fire from exec-done callback threads
        while the spawn thread emits session_create/exec_start for other
        agents, and AuditLog's tail-recover-then-append sequence is not
        concurrency-safe (overlapping writers fork the HMAC chain).

        Args:
            event_type: One of the ``sandbox.*`` event-type constants
                from :mod:`bernstein.core.security.audit`.
            resource_id: Audit resource identifier (sandbox session id).
            details: Structured event payload.
        """
        try:
            from bernstein.core.security.audit import AuditLog

            with _SANDBOX_AUDIT_LOCK:
                audit = AuditLog(audit_dir=self._workdir / ".sdd" / "audit")
                audit.log(
                    event_type=event_type,
                    actor="spawner",
                    resource_type="sandbox_session",
                    resource_id=resource_id,
                    details=details,
                )
        except Exception as exc:  # audit must never block execution
            logger.warning("Could not emit %s audit event for %s: %s", event_type, resource_id, exc)

    def _adapter_cmd_for_container(
        self,
        *,
        prompt_file: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None,
        adapter: CLIAdapter,
    ) -> list[str]:
        """Build the CLI command to run inside the container.

        Reads the prompt from the prompt file instead of passing it as
        a command-line argument (which can hit ARG_MAX limits).

        Args:
            _prompt_file: Path to the prompt file (part of interface;
                the container path is reconstructed from session_id).
            model_config: Model and effort config.
            session_id: Session ID for the worker wrapper.
            _mcp_config: MCP configuration dict (part of interface).

        Returns:
            Command argument list.
        """
        import shlex

        _ = prompt_file  # Part of interface; container path is reconstructed from session_id
        _ = mcp_config  # Part of interface; not used in container command
        # Map container path: host workspace is mounted at /workspace
        container_prompt = f"/workspace/.sdd/runtime/prompts/{session_id}.md"

        # Build a generic shell command that reads the prompt and pipes it
        # to the adapter CLI. This works across all adapters.
        adapter_name = adapter.name().lower()

        # Resolve the actual CLI binary name. adapter.name() may return a
        # display name like "Qwen CLI" which is not a valid command. Map
        # known adapters to their binary names.
        _ADAPTER_BINARY_MAP: dict[str, str] = {
            "qwen cli": "qwen",
            "claude code": "claude",
            "codex cli": "codex",
            "gemini cli": "gemini",
            "aider": "aider",
        }
        cli_binary = _ADAPTER_BINARY_MAP.get(adapter_name, adapter_name.split()[0])

        # Shell-quote every interpolated value. ``model`` and the role
        # segment of ``session_id`` originate from task-server payloads
        # (length-checked only), so unquoted interpolation into ``sh -c``
        # would let a crafted task run arbitrary commands at container
        # startup, outside the adapter's own tool-approval gate.
        q_prompt = shlex.quote(container_prompt)
        q_model = shlex.quote(str(model_config.model))
        q_effort = shlex.quote(str(model_config.effort))
        q_binary = shlex.quote(cli_binary)

        if "claude" in adapter_name:
            cmd = [
                "sh",
                "-c",
                f"claude --model {q_model} "
                f"--effort {q_effort} "
                f"--max-turns 50 "
                f"--dangerously-skip-permissions "
                f"--output-format stream-json "
                f'-p "$(cat {q_prompt})"',
            ]
        elif "qwen" in adapter_name:
            # Qwen CLI uses positional arg for prompt, -y for auto-approve.
            # Inside containers, --auth-type openai is required because the
            # default qwen auth config is not present.
            cmd = [
                "sh",
                "-c",
                f'{q_binary} -y --auth-type openai --model {q_model} "$(cat {q_prompt})"',
            ]
        else:
            # Generic: assume the adapter CLI reads from stdin or -p flag
            cmd = [
                "sh",
                "-c",
                f'cat {q_prompt} | {q_binary} -p "$(cat {q_prompt})"',
            ]
        return cmd

    def _container_manager_for_session(self, session_id: str) -> ContainerManager | None:
        """Return the container manager responsible for a session."""
        return self._sandbox_managers.get(session_id, self._container_mgr)

    def _check_alive_openclaw(self, session: AgentSession) -> bool:
        """Check liveness for an OpenClaw remote-bridge session."""
        try:
            bridge_status = self._bridge_status(session)
        except BridgeError as exc:
            logger.warning("OpenClaw status check failed for %s, treating as still alive: %s", session.id, exc)
            return True
        session.exit_code = bridge_status.exit_code
        session.bridge_session_key = bridge_status.metadata.get("session_key") or session.bridge_session_key
        session.bridge_run_id = bridge_status.metadata.get("run_id") or session.bridge_run_id
        return bridge_status.state in {AgentState.PENDING, AgentState.RUNNING}

    def _check_alive_container(self, session: AgentSession) -> bool | None:
        """Check liveness via container manager. Returns None if not container-based."""
        container_mgr = self._container_manager_for_session(session.id)
        if not (session.container_id and container_mgr is not None):
            return None
        handle = container_mgr.get_handle(session.id)
        if handle is None:
            return False
        alive = container_mgr.is_alive(handle)
        if not alive:
            session.exit_code = container_mgr.get_exit_code(handle)
        return alive

    def _check_alive_process(self, session: AgentSession) -> bool | None:
        """Check liveness via stored subprocess. Returns None if no proc stored."""
        proc = self._procs.get(session.id)
        if proc is None:
            return None
        exit_code = proc.poll()
        if exit_code is not None:
            session.exit_code = exit_code
            return False
        return True

    def _check_alive_sandbox_session(self, session: AgentSession) -> bool | None:
        """Liveness for agents whose exec runs via :meth:`SandboxSession.exec`.

        Returns ``None`` when the session was not routed through a
        sandbox session (so the next checker in the chain runs).
        """
        handle = self._sandbox_exec_handles.get(session.id)
        if handle is None:
            return None
        if not handle.future.done():
            return True
        if handle.future.cancelled():
            session.exit_code = -1
            return False
        exc = handle.future.exception()
        if exc is not None:
            session.exit_code = -1
            return False
        try:
            session.exit_code = handle.future.result().exit_code
        except Exception:  # pragma: no cover - already inspected above
            session.exit_code = -1
        return False

    def _check_alive_in_process(self, session: AgentSession) -> bool | None:
        """Check liveness via InProcessAgent. Returns None if not applicable."""
        if self._in_process is None:
            return None
        alive = self._in_process.is_alive(session.id)
        if not alive:
            exit_code_val = self._in_process.wait(session.id, timeout=0.1)
            if exit_code_val is not None:
                session.exit_code = exit_code_val
        return alive

    def check_alive(self, session: AgentSession) -> bool:
        """Check if the agent process is still running.

        Args:
            session: Agent session to check.

        Returns:
            True if the process is alive, False otherwise.
        """
        if session.runtime_backend == "openclaw":
            return self._check_alive_openclaw(session)

        for checker in (
            self._check_alive_sandbox_session,
            self._check_alive_container,
            self._check_alive_process,
            self._check_alive_in_process,
        ):
            result = checker(session)
            if result is not None:
                return result

        if session.pid is None:
            return False
        return self._adapter.is_alive(session.pid)

    def kill(self, session: AgentSession) -> None:
        """Terminate the agent process and mark session dead.

        Args:
            session: Agent session to kill.
        """
        if session.runtime_backend == "openclaw":
            self._kill_openclaw(session)
            return

        self._kill_local(session)

    def _kill_openclaw(self, session: AgentSession) -> None:
        """Kill an agent running on the OpenClaw remote bridge."""
        try:
            self._bridge_cancel(session)
        except BridgeError as exc:
            logger.warning("OpenClaw cancellation failed for %s: %s", session.id, exc)
        self._transition_to_dead(
            session, "remote bridge kill requested", "remote runtime cancellation requested by orchestrator"
        )

    def _kill_local(self, session: AgentSession) -> None:
        """Kill a locally-running agent (container, in-process, or PID)."""
        # Sandbox-session-routed agents have no local PID; cancel the
        # future on its owning loop instead.
        sbx_handle = self._sandbox_exec_handles.get(session.id)
        if sbx_handle is not None:
            cancel_session_exec(sbx_handle)
            self._sandbox_exec_handles.pop(session.id, None)
            # Issue #2162: per-spawn sessions are destroyed on kill so a
            # cancelled agent never leaves its container behind. No-op
            # when the exec-done callback already destroyed it.
            self._destroy_sandbox_session(session.id)
            self._transition_to_dead(
                session,
                "kill requested",
                "sandbox session exec cancellation requested by orchestrator",
            )
            return
        container_mgr = self._container_manager_for_session(session.id)
        if session.container_id and container_mgr is not None:
            handle = container_mgr.get_handle(session.id)
            if handle is not None:
                container_mgr.destroy(handle)
            self._sandbox_managers.pop(session.id, None)
        elif self._in_process is not None and self._backend == AgentBackend.IN_PROCESS:
            self._in_process.stop(session.id)
            exit_code_val = self._in_process.wait(session.id, timeout=5.0)
            if exit_code_val is not None:
                session.exit_code = exit_code_val
            self._in_process.cleanup(session.id)
        elif session.pid is not None:
            self._adapter.kill(session.pid)
        self._transition_to_dead(session, "kill requested", "local process kill requested by orchestrator")

    def _transition_to_dead(self, session: AgentSession, reason: str, detail: str) -> None:
        """Transition session to dead and update team state."""
        if session.status != "dead":
            transition_agent(
                session,
                "dead",
                actor="spawner",
                reason=reason,
                transition_reason=TransitionReason.ABORTED,
                abort_reason=AbortReason.SHUTDOWN_SIGNAL,
                abort_detail=detail,
                finish_reason="kill_requested",
            )
        try:
            TeamStateStore(self._workdir / ".sdd").on_kill(session.id)
        except Exception as _ts_exc:
            logger.debug("Team state on_kill failed: %s", _ts_exc)
