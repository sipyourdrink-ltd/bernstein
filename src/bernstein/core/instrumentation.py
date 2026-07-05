"""Wave-3 per-agent instrumentation: LLM calls, tool calls, and conversation history.

Wave 2 (see ``.sdd/runs/<run_id>/summary.json`` and
:mod:`bernstein.core.orchestration.run_report`) added phase/task-level timing
that the top-level orchestrator can see directly. This module adds the layer
below it: individual LLM API calls, individual tool invocations, and the
growing message history *inside* a single agent process - things the
orchestrator cannot see because they happen inside an agent subprocess (or,
for in-process runners, inside a single call to an SDK).

This is a strictly additive, observe-only instrumentation layer. Every public
method is wrapped in a broad ``try/except`` and degrades to a logged warning
on any failure - instrumentation must NEVER crash or block the agent run it
is observing (see ``.claude/rules/lessons.md`` rule 2: "logging IS the
debugging interface", and the corollary that observability code itself must
be maximally defensive).

Output layout (one directory per agent, created on first write)::

    .sdd/runs/<run_id>/tasks/<task_id>/agents/<agent_id>/llm-calls.jsonl
    .sdd/runs/<run_id>/tasks/<task_id>/agents/<agent_id>/tool-calls.jsonl
    .sdd/runs/<run_id>/tasks/<task_id>/agents/<agent_id>/conversation.jsonl

Design choice - module-level singleton, not a class threaded through every
call site: the two hook points wired up in wave 3
(:mod:`bernstein.adapters.openai_agents_runner` and
:mod:`bernstein.adapters.openai_agents_builtins`) are built from free
functions and module-level state (``emit_event``, the builtin tool
closures), not a class with a natural place to stash ``self``. Each of those
processes is already a dedicated one-agent-per-process runner (the adapter
spawns ``python -m bernstein.adapters.openai_agents_runner`` once per agent
session), so "one instance per agent process" and "one module-level
singleton" are the same thing here. ``init_instrumenter``/``get_instrumenter``
avoid plumbing an instrumenter parameter through a dozen existing function
signatures. A caller that needs multiple independent instrumenters in one
process (e.g. a test) can still construct :class:`RunInstrumenter` directly
and never touch the singleton.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from bernstein.core.security.sanitize import sanitize_log

logger = logging.getLogger(__name__)

# Env var carrying the orchestrator's run id down into agent subprocesses.
# Must be added to the adapter env-isolation allowlist
# (``bernstein.adapters.env_isolation._BASE_ALLOWLIST``) or a filtered
# subprocess env silently drops it and every agent falls back to "unknown".
RUN_ID_ENV_VAR = "BERNSTEIN_RUN_ID"

# Truncation cap for individual string values inside logged tool-call args.
# Prevents a single huge string argument (e.g. a full file body passed to
# write_file) from bloating tool-calls.jsonl - only metadata/preview is
# wanted here, never full payloads (see module docstring + task spec).
_ARG_VALUE_TRUNCATE_CHARS = 500
_TRUNCATE_MARKER = "...[truncated]"

# Filesystem-safe shape for a single directory-name component. run_id arrives
# via an environment variable and task_id/agent_id via the runner manifest,
# so :func:`resolve_agent_dir` treats all three as untrusted before joining
# them into a path (see :func:`_sanitize_path_component`).
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]")
_MAX_COMPONENT_CHARS = 128
_FALLBACK_COMPONENT = "unknown"


def _sanitize_path_component(value: str) -> str:
    """Reduce an untrusted id to a single safe directory-name component.

    Keeps only the final path component (dropping separators, absolute
    prefixes, and ``..`` segments before it), replaces every character
    outside ``[A-Za-z0-9._-]`` with ``_``, caps the length, and collapses
    anything that could still escape the directory (empty string, ``.``,
    ``..``, all-dot names) to ``"unknown"``. Never raises - a hostile or
    malformed id degrades to a wrong-but-contained directory name, matching
    this module's observe-only contract.
    """
    cleaned = value.replace("\x00", "").strip()
    # Normalize backslashes so a Windows-style separator cannot survive as a
    # literal character on POSIX, then keep only the last path component.
    cleaned = PurePosixPath(cleaned.replace("\\", "/")).name
    cleaned = _SAFE_COMPONENT_RE.sub("_", cleaned)[:_MAX_COMPONENT_CHARS]
    if not cleaned or cleaned.strip(".") == "":
        return _FALLBACK_COMPONENT
    return cleaned


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with millisecond precision."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _truncate_value(value: Any, *, max_chars: int = _ARG_VALUE_TRUNCATE_CHARS) -> Any:
    """Recursively truncate long strings inside an args structure.

    Only strings are truncated; other JSON-safe scalar types pass through
    unchanged. Dicts and lists are walked recursively so nested large
    payloads (e.g. ``{"content": "<huge file body>"}``) are capped without
    losing the surrounding structure. Non-JSON-safe objects are stringified
    first so a stray non-serializable arg never breaks the write.
    """
    if isinstance(value, str):
        if len(value) > max_chars:
            return value[:max_chars] + _TRUNCATE_MARKER
        return value
    if isinstance(value, dict):
        return {str(k): _truncate_value(v, max_chars=max_chars) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_truncate_value(v, max_chars=max_chars) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # Fallback for anything else JSON can't natively encode (e.g. custom
    # objects some adapter passed through as a tool arg).
    try:
        text = str(value)
    except Exception:  # pragma: no cover - defensive, str() essentially never raises
        return "<unrepr-able value>"
    return _truncate_value(text, max_chars=max_chars)


@dataclass
class RunInstrumenter:
    """Appends JSONL instrumentation records for one agent's run.

    One instance is expected per (run_id, task_id, agent_id) triple - i.e.
    per agent process/session. All three JSONL files live under
    ``base_dir`` (already resolved to
    ``.../agents/<agent_id>/`` by the caller - see :func:`init_instrumenter`
    and :func:`for_agent`).

    Every public method is defensive: a failure to create the directory or
    write a line is logged at WARNING and swallowed, never raised, so a full
    disk / permissions problem / serialization bug can never take down the
    agent run being observed.
    """

    run_id: str
    task_id: str
    agent_id: str
    base_dir: Path

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._dir_ready = False
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            self._dir_ready = True
            logger.info(
                "RunInstrumenter initialized run_id=%s task_id=%s agent_id=%s -> "
                "llm_calls=%s tool_calls=%s conversation=%s",
                sanitize_log(self.run_id),
                sanitize_log(self.task_id),
                sanitize_log(self.agent_id),
                self._llm_calls_path(),
                self._tool_calls_path(),
                self._conversation_path(),
            )
        except OSError as exc:
            logger.warning(
                "RunInstrumenter: failed to create instrumentation dir %s (run_id=%s "
                "task_id=%s agent_id=%s): %s - instrumentation for this agent is "
                "DISABLED, the agent run itself is unaffected",
                self.base_dir,
                sanitize_log(self.run_id),
                sanitize_log(self.task_id),
                sanitize_log(self.agent_id),
                exc,
            )

    # -- path helpers ---------------------------------------------------

    def _llm_calls_path(self) -> Path:
        return self.base_dir / "llm-calls.jsonl"

    def _tool_calls_path(self) -> Path:
        return self.base_dir / "tool-calls.jsonl"

    def _conversation_path(self) -> Path:
        return self.base_dir / "conversation.jsonl"

    def _append_line(self, path: Path, record: dict[str, Any], *, kind: str, key: str) -> None:
        """Serialize *record* and append it as a single line, single write() call.

        Building the full line before calling ``write()`` once (rather than
        writing pieces incrementally) is what keeps concurrent writers from
        the same process from interleaving partial lines - see module
        docstring. A ``threading.Lock`` additionally protects against two
        threads in the SAME process racing on the SAME file (e.g. a
        heartbeat thread and the main thread); separate agent PROCESSES
        never share a base_dir so no cross-process lock is needed.
        """
        if not self._dir_ready:
            return
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "RunInstrumenter: failed to serialize %s record %s=%s: %s", kind, kind, sanitize_log(key), exc
            )
            return
        try:
            with self._lock, path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            logger.debug("RunInstrumenter: wrote %s record %s=%s to %s", kind, kind, sanitize_log(key), path)
        except OSError as exc:
            logger.warning(
                "RunInstrumenter: failed to write %s record %s=%s to %s: %s", kind, kind, sanitize_log(key), path, exc
            )

    # -- public API -------------------------------------------------------

    def log_llm_call(
        self,
        *,
        call_id: str,
        ts_start: str,
        ts_end: str,
        model: str,
        endpoint: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        wall_ms: float | None = None,
        ts_ttft: str | None = None,
        tokens_per_sec: float | None = None,
        status: str = "ok",
        error: str | None = None,
    ) -> None:
        """Append one record to ``llm-calls.jsonl`` for a single LLM API call.

        ``wall_ms`` is computed from ``ts_start``/``ts_end`` when not given
        explicitly. ``tokens_per_sec`` is only ever computed by the CALLER
        from real streamed first-token timestamps - this method never
        fabricates TTFT/throughput when the call did not stream (per task
        spec: "If it doesn't stream ... do NOT fake TTFT").
        """
        try:
            if wall_ms is None:
                wall_ms = _iso_delta_ms(ts_start, ts_end)
            if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
                total_tokens = prompt_tokens + completion_tokens
            record: dict[str, Any] = {
                "call_id": call_id,
                "ts_start": ts_start,
                "ts_end": ts_end,
                "wall_ms": wall_ms,
                "model": model,
                "endpoint": endpoint,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "status": status,
                "error": error,
            }
            if ts_ttft is not None:
                record["ts_ttft"] = ts_ttft
            if tokens_per_sec is not None:
                record["tokens_per_sec"] = tokens_per_sec
            self._append_line(self._llm_calls_path(), record, kind="llm_call", key=call_id)
        except Exception as exc:  # intentional-broad-except: instrumentation must never raise
            logger.warning("RunInstrumenter.log_llm_call failed for call_id=%s: %s", sanitize_log(call_id), exc)

    def log_tool_call(
        self,
        *,
        call_id: str,
        ts_start: str,
        ts_end: str,
        tool: str,
        args: dict[str, Any] | None = None,
        success: bool,
        error: str | None = None,
        wall_ms: float | None = None,
    ) -> None:
        """Append one record to ``tool-calls.jsonl`` for a single tool invocation.

        ``args`` is truncated (see :func:`_truncate_value`) before being
        written - never the full tool result, only the call's own
        arguments.
        """
        try:
            if wall_ms is None:
                wall_ms = _iso_delta_ms(ts_start, ts_end)
            record: dict[str, Any] = {
                "call_id": call_id,
                "ts_start": ts_start,
                "ts_end": ts_end,
                "wall_ms": wall_ms,
                "tool": tool,
                "args": _truncate_value(args or {}),
                "success": success,
                "error": error,
            }
            self._append_line(self._tool_calls_path(), record, kind="tool_call", key=call_id)
        except Exception as exc:  # intentional-broad-except: instrumentation must never raise
            logger.warning("RunInstrumenter.log_tool_call failed for call_id=%s: %s", sanitize_log(call_id), exc)

    def log_message(
        self,
        *,
        idx: int,
        role: str,
        content_length: int,
        tool_calls: list[str] | None = None,
        ts: str | None = None,
    ) -> None:
        """Append one record to ``conversation.jsonl`` for a new message.

        Only shape metadata is recorded - never message content - per the
        task spec's privacy/size constraint.
        """
        try:
            record: dict[str, Any] = {
                "idx": idx,
                "role": role,
                "content_length": content_length,
                "ts": ts or _now_iso(),
            }
            if tool_calls:
                record["tool_calls"] = list(tool_calls)
            self._append_line(self._conversation_path(), record, kind="message", key=str(idx))
        except Exception as exc:  # intentional-broad-except: instrumentation must never raise
            logger.warning("RunInstrumenter.log_message failed for idx=%s: %s", idx, exc)


def _iso_delta_ms(ts_start: str, ts_end: str) -> float | None:
    """Best-effort millisecond delta between two ISO-8601 timestamps.

    Returns ``None`` (never raises, never fabricates a value) when either
    timestamp fails to parse.
    """
    try:
        start = datetime.fromisoformat(ts_start)
        end = datetime.fromisoformat(ts_end)
        return (end - start).total_seconds() * 1000.0
    except (ValueError, TypeError):
        return None


class _NullInstrumenter(RunInstrumenter):
    """No-op stand-in returned by :func:`get_instrumenter` before init.

    Lets call sites do ``get_instrumenter().log_llm_call(...)`` unconditionally
    without an ``if instrumenter is not None`` guard at every call site, while
    guaranteeing zero disk I/O (and zero directory creation) until a real
    instrumenter is explicitly initialized via :func:`init_instrumenter`.
    """

    def __init__(self) -> None:  # intentionally skips __post_init__ dir creation
        self.run_id = "uninitialized"
        self.task_id = "uninitialized"
        self.agent_id = "uninitialized"
        self.base_dir = Path()
        self._lock = threading.Lock()
        self._dir_ready = False


_instrumenter_lock = threading.Lock()
_instrumenter: RunInstrumenter | None = None
_null_instrumenter = _NullInstrumenter()


def resolve_agent_dir(workdir: Path, run_id: str, task_id: str, agent_id: str) -> Path:
    """Return the per-agent instrumentation directory for the given ids.

    Mirrors the wave-2 run layout (``.sdd/runs/<run_id>/...``) documented in
    :mod:`bernstein.core.orchestration.run_report`.

    Each id is sanitized to a single directory-name component first (see
    :func:`_sanitize_path_component`): ``run_id`` comes from the
    ``BERNSTEIN_RUN_ID`` environment variable and ``task_id``/``agent_id``
    from the runner manifest, so a value carrying path separators or ``..``
    segments must not be able to point the instrumentation tree outside
    ``<workdir>/.sdd/runs/``.
    """
    return (
        workdir
        / ".sdd"
        / "runs"
        / _sanitize_path_component(run_id)
        / "tasks"
        / _sanitize_path_component(task_id)
        / "agents"
        / _sanitize_path_component(agent_id)
    )


def init_instrumenter(*, run_id: str, task_id: str, agent_id: str, base_dir: Path) -> RunInstrumenter:
    """Create and install the process-wide :class:`RunInstrumenter` singleton.

    Safe to call more than once (e.g. a test re-initializing between cases);
    each call replaces the previous singleton. Never raises - construction
    failures are caught inside :meth:`RunInstrumenter.__post_init__` and
    degrade to a disabled-but-non-crashing instance.
    """
    global _instrumenter
    instrumenter = RunInstrumenter(run_id=run_id, task_id=task_id, agent_id=agent_id, base_dir=base_dir)
    with _instrumenter_lock:
        _instrumenter = instrumenter
    return instrumenter


def get_instrumenter() -> RunInstrumenter:
    """Return the process-wide instrumenter, or a silent no-op if uninitialized.

    Callers should prefer this over reaching into module state directly so
    an un-instrumented context (e.g. a unit test that imports a hooked
    module without calling :func:`init_instrumenter`) degrades to "nothing
    is written" instead of an ``AttributeError``.
    """
    with _instrumenter_lock:
        return _instrumenter if _instrumenter is not None else _null_instrumenter
