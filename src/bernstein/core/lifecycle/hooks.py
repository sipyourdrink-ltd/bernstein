"""Lifecycle-hook registry, event enum, and context dataclass.

This module is self-contained; the pluggy bridge lives in a sibling
module so this file can be imported from contexts where pluggy is
unavailable or undesired.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.persistence.workspace import is_workspace_trusted

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import MappingProxyType

    from bernstein.core.lifecycle.hook_filter import HookFilter

log = logging.getLogger(__name__)

__all__ = [
    "DECISION_ALLOW",
    "DECISION_ANNOTATE",
    "DECISION_DENY",
    "DECISION_MUTATE",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_STDOUT_BYTES",
    "STANDARD_EVENTS",
    "HookDecision",
    "HookDenied",
    "HookFailure",
    "HookRegistry",
    "LifecycleContext",
    "LifecycleEvent",
    "UntrustedWorkspaceError",
    "bind_rate_limit_emit",
    "bind_retry_continuation_emit",
    "discover_default_hook_scripts",
    "parse_hook_decision",
]


DEFAULT_TIMEOUT_SECONDS: int = 30
"""Default subprocess timeout applied to script hooks."""

MAX_STDOUT_BYTES: int = 10 * 1024 * 1024
"""Maximum captured stdout for a script hook (10 MB). Output is truncated."""

# Whitelisted parent environment variables. Anything not listed here is
# stripped before launching a script hook, so secrets and unrelated
# settings do not leak into user-supplied processes.
_ENV_WHITELIST: tuple[str, ...] = ("PATH", "HOME", "USER")


class LifecycleEvent(StrEnum):
    """Named lifecycle events a hook may subscribe to.

    Two families of names are recognised:

    * Bernstein-native (snake_case): ``pre_task`` / ``post_task`` etc.
      These are the original event names and remain fully supported.
    * Cross-CLI (camelCase): ``sessionStart``, ``userPromptSubmitted``,
      ``preToolUse``, ``postToolUse``, ``errorOccurred``, ``idle`` and
      ``sessionEnd``. These match the de-facto event vocabulary used by
      neighbouring orchestrators, so a hook script written for another
      tool drops in unchanged. See ``docs/contributing/hooks.md`` for
      the payload contract per event.
    """

    # ------------------------------------------------------------------
    # Bernstein-native lifecycle events (pre-existing, snake_case).
    # ------------------------------------------------------------------
    PRE_TASK = "pre_task"
    POST_TASK = "post_task"
    PRE_MERGE = "pre_merge"
    POST_MERGE = "post_merge"
    PRE_SPAWN = "pre_spawn"
    POST_SPAWN = "post_spawn"
    PRE_ARCHIVE = "pre_archive"
    POST_ARCHIVE = "post_archive"
    # feat-resume-from-checkpoint: fires when ``bernstein resume <task-id>``
    # successfully loads a checkpoint and is about to re-spawn the task.
    # Plugins can react to track resume metrics, gate flaky tasks, etc.
    TASK_RESUME = "task.resume"
    # feat-cross-task-kb: fires when a task publishes a fact through the
    # cross-task knowledge-base facade. Payload carries the attribution
    # triple ``(producer_task_id, ts_ns, content_hash)`` plus ``tag``,
    # ``key``, and ``scope``.
    KB_FACT_PUBLISHED = "kb.fact_published"

    # ------------------------------------------------------------------
    # Cross-CLI lifecycle events (camelCase, T1323).
    # ------------------------------------------------------------------
    SESSION_START = "sessionStart"
    USER_PROMPT_SUBMITTED = "userPromptSubmitted"
    PRE_TOOL_USE = "preToolUse"
    POST_TOOL_USE = "postToolUse"
    ERROR_OCCURRED = "errorOccurred"
    IDLE = "idle"
    SESSION_END = "sessionEnd"

    # ------------------------------------------------------------------
    # Adapter rate-limit observability (feat/adapter-rate-limit-meter).
    # Emitted once per 429-class upstream signal, with the adapter name
    # and provider error code on ``context.data``.
    # ------------------------------------------------------------------
    RATE_LIMIT_HIT = "rate_limit.hit"

    # ------------------------------------------------------------------
    # Retry-with-continuation (feat/retry-with-continuation).
    # Fires when the orchestrator detects a "success without commit"
    # exit and launches a single continuation retry. Payload carries
    # ``session_id``, ``reason`` (the :class:`RetryDecision.reason`
    # tag), and ``attempt`` (1-indexed retry counter).
    # ------------------------------------------------------------------
    AGENT_RETRY_CONTINUATION = "agent.retry_continuation"

    # ------------------------------------------------------------------
    # ProgressWatch liveness probe
    # (feat-progress-watch-liveness-probe).
    # Emitted when the watcher detects that an agent's session log has
    # not grown for at least ``agents.progress_watch.inactivity_seconds``.
    # Payload keys on ``context.data``:
    #
    # * ``adapter``: adapter name (e.g. ``"claude"``).
    # * ``log_path``: stringified path of the watched log.
    # * ``last_log_growth_ts``: unix timestamp of the most recent
    #   observed growth (mtime or size move).
    # * ``detected_ts``: unix timestamp the stall was detected.
    # ------------------------------------------------------------------
    AGENT_PROGRESS_STALLED = "agent.progress_stalled"

    # ------------------------------------------------------------------
    # Respawn-budget exhaustion (feat/respawn-supervisor-budget).
    # Emitted once when an agent exhausts its bounded respawn budget and
    # the supervisor parks the session. Payload keys on ``context.data``:
    #
    # * ``reason``: machine-readable park reason (always
    #   ``"respawn_budget_exhausted"``).
    # * ``last_error``: stringified final spawn error, or empty string.
    # * ``attempts``: number of respawn attempts that were consumed
    #   (excludes the initial spawn).
    # * ``window_seconds``: the rolling window size the budget used.
    # * ``max_respawns``: the configured respawn ceiling.
    # ------------------------------------------------------------------
    AGENT_STARTUP_EXHAUSTED = "agent.startup_exhausted"

    # ------------------------------------------------------------------
    # Operator escalation (feat/operator-supervisor-surface).
    # Emitted when ``bernstein supervisor escalate <session_id>`` runs.
    # Payload keys on ``context.data``:
    #
    # * ``reason``: operator-supplied reason string.
    # * ``receipt_path``: path of the persisted escalation receipt.
    # * ``recommended_action``: receipt's recommended_action value.
    # * ``stall_reason``: receipt's stall_reason value.
    # * ``worker_id``: worker the operator escalated.
    # ------------------------------------------------------------------
    WORKER_ESCALATED = "worker.escalated"


#: The cross-CLI standardised event vocabulary introduced by issue #1323.
#: Pre-existing snake_case events remain supported but are not part of
#: this tuple.
STANDARD_EVENTS: tuple[LifecycleEvent, ...] = (
    LifecycleEvent.SESSION_START,
    LifecycleEvent.USER_PROMPT_SUBMITTED,
    LifecycleEvent.PRE_TOOL_USE,
    LifecycleEvent.POST_TOOL_USE,
    LifecycleEvent.ERROR_OCCURRED,
    LifecycleEvent.IDLE,
    LifecycleEvent.SESSION_END,
)


@dataclass(frozen=True, slots=True)
class LifecycleContext:
    """Immutable payload passed to every hook invocation.

    Attributes:
        event: The lifecycle event being dispatched.
        task: Task identifier, when the event is task-scoped.
        session_id: Agent session identifier, when relevant.
        workdir: Working directory the caller considers current.
        env: Extra environment variables to merge into script hooks
            (on top of the whitelisted parent env).
        timestamp: Unix timestamp when the context was built.
    """

    event: LifecycleEvent
    task: str | None = None
    session_id: str | None = None
    workdir: Path = field(default_factory=Path.cwd)
    env: dict[str, str] = field(default_factory=dict[str, str])
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict[str, Any])
    """Structured per-event payload. See ``docs/contributing/hooks.md``
    for the keys expected for each :class:`LifecycleEvent`."""

    def to_payload(self) -> dict[str, Any]:
        """Serialise the context for transport to a subprocess."""
        return {
            "event": self.event.value,
            "task": self.task,
            "session_id": self.session_id,
            "workdir": str(self.workdir),
            "env": self.env.copy(),
            "timestamp": self.timestamp,
            "data": self.data.copy(),
        }


@dataclass(frozen=True, slots=True)
class HookDecision:
    """Structured result returned by a hook subprocess on stdout.

    Hooks may emit a single-line JSON object on stdout to influence the
    pipeline:

    * ``{"decision": "allow"}`` - explicit allow (default if absent).
    * ``{"decision": "deny", "reason": "<text>"}`` - for ``preToolUse``,
      blocks the tool call and raises :class:`HookDenied`.
    * ``{"decision": "mutate", "data": {...}}`` - replaces the payload
      passed to subsequent hooks in the chain.
    * ``{"decision": "annotate", "data": {...}}`` - merges keys into the
      payload without replacing it.

    Any other key is preserved verbatim on ``HookDecision.raw`` for
    callers that want to inspect richer responses.
    """

    decision: str
    reason: str = ""
    data: dict[str, Any] = field(default_factory=dict[str, Any])
    raw: dict[str, Any] = field(default_factory=dict[str, Any])


# Recognised stdout decision verbs.
DECISION_ALLOW: str = "allow"
DECISION_DENY: str = "deny"
DECISION_MUTATE: str = "mutate"
DECISION_ANNOTATE: str = "annotate"

_VALID_DECISIONS: frozenset[str] = frozenset(
    {DECISION_ALLOW, DECISION_DENY, DECISION_MUTATE, DECISION_ANNOTATE},
)


class HookDenied(RuntimeError):
    """Raised when a hook emits ``{"decision": "deny"}``.

    For ``preToolUse`` the dispatcher raises this so the orchestrator
    can block the tool call and emit a structured audit-chain event.
    """

    def __init__(
        self,
        event: LifecycleEvent,
        hook: str,
        reason: str,
    ) -> None:
        super().__init__(f"hook '{hook}' denied {event.value}: {reason}")
        self.event = event
        self.hook = hook
        self.reason = reason


class HookFailure(RuntimeError):
    """Raised when a script hook exits non-zero or a callable raises.

    Attributes:
        event: The event whose dispatch failed.
        hook: Human-readable description of the failing hook.
        exit_code: Subprocess exit code, or ``None`` for callables.
        stderr: Captured standard error, when available.
    """

    def __init__(
        self,
        event: LifecycleEvent,
        hook: str,
        *,
        exit_code: int | None = None,
        stderr: str = "",
        cause: BaseException | None = None,
    ) -> None:
        detail = f"exit_code={exit_code}" if exit_code is not None else "raised"
        super().__init__(f"Lifecycle hook failed for {event.value}: {hook} ({detail})")
        self.event = event
        self.hook = hook
        self.exit_code = exit_code
        self.stderr = stderr
        self.__cause__ = cause


class UntrustedWorkspaceError(HookFailure):
    """Raised when a script hook would run in a workspace that is not trusted.

    Script hooks are discovered from, or declared in, the repository
    being worked on.  Like the plugin manager's committed hook scripts,
    they run only after the workspace has been explicitly trusted.
    """

    def __init__(self, event: LifecycleEvent, hook: str, workdir: Path) -> None:
        super().__init__(event, hook, stderr="workspace not trusted")
        self.args = (
            f"workspace {workdir} is not trusted; refusing to run {hook} for {event.value}. "
            "Run `bernstein trust` there (writes .sdd/runtime/workspace_trust.json) to enable script hooks.",
        )
        self.workdir = workdir


@dataclass(frozen=True, slots=True)
class _ScriptHook:
    path: Path
    timeout: int
    hook_filter: HookFilter | None = None


@dataclass(frozen=True, slots=True)
class _CallableHook:
    fn: Callable[[LifecycleContext], None]

    @property
    def label(self) -> str:
        name = getattr(self.fn, "__qualname__", None) or getattr(self.fn, "__name__", None) or repr(self.fn)
        return f"callable:{name}"


class HookRegistry:
    """Registers and dispatches lifecycle hooks.

    Hooks fire in registration order. The registry deliberately keeps
    pluggy integration in a bridge module so this class can be used
    standalone in tests and by callers that do not want to take a
    dependency on pluggy.
    """

    def __init__(self) -> None:
        self._scripts: dict[LifecycleEvent, list[_ScriptHook]] = {event: [] for event in LifecycleEvent}
        self._callables: dict[LifecycleEvent, list[_CallableHook]] = {event: [] for event in LifecycleEvent}
        # Insertion-order ledger so we can dispatch scripts and callables
        # in the exact order a user registered them.
        self._order: dict[LifecycleEvent, list[tuple[str, int]]] = {event: [] for event in LifecycleEvent}
        self._executor: ThreadPoolExecutor | None = None
        # The pluggy bridge attaches itself here when installed.
        self._pluggy_dispatcher: Callable[[LifecycleEvent, LifecycleContext], None] | None = None
        # Optional sink for ``hook.filtered`` metrics. Bound by the
        # orchestrator; defaults to a no-op so the registry stays usable
        # standalone in tests.
        self._filtered_metric_sink: Callable[[LifecycleEvent, str, str], None] | None = None

    def bind_filtered_metric_sink(
        self,
        sink: Callable[[LifecycleEvent, str, str], None],
    ) -> None:
        """Install a callback invoked when a hook is skipped by its filter.

        The callback receives ``(event, hook_label, reason)``. ``reason`` is
        the human-readable filter that did not match, e.g.
        ``filter 'Bash(git *)' did not match``. This is the
        ``hook.filtered{reason}`` metric described by issue #1628.
        """
        self._filtered_metric_sink = sink

    # ------------------------------------------------------------------ registration

    def register_script(
        self,
        event: LifecycleEvent,
        path: str | os.PathLike[str],
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        *,
        hook_filter: HookFilter | None = None,
    ) -> None:
        """Register a shell script to run when ``event`` fires.

        Args:
            event: Lifecycle event to subscribe to.
            path: Filesystem path to the script; need not exist yet.
            timeout: Maximum wall-clock seconds before the subprocess is killed.
            hook_filter: Optional parsed ``if:`` filter. When present, the
                runner evaluates it against the event payload before
                spawning the subprocess; a non-match skips the spawn and
                emits a ``hook.filtered`` metric. ``None`` always matches.
        """
        hook = _ScriptHook(path=Path(path), timeout=timeout, hook_filter=hook_filter)
        idx = len(self._scripts[event])
        self._scripts[event].append(hook)
        self._order[event].append(("script", idx))

    def register_callable(
        self,
        event: LifecycleEvent,
        fn: Callable[[LifecycleContext], None],
    ) -> None:
        """Register a Python callable for ``event``.

        The callable receives a single :class:`LifecycleContext` argument.
        Any exception it raises surfaces as :class:`HookFailure`.
        """
        hook = _CallableHook(fn=fn)
        idx = len(self._callables[event])
        self._callables[event].append(hook)
        self._order[event].append(("callable", idx))

    def attach_pluggy_dispatcher(
        self,
        dispatcher: Callable[[LifecycleEvent, LifecycleContext], None],
    ) -> None:
        """Install the pluggy bridge's dispatcher.

        Called by :mod:`bernstein.core.lifecycle.pluggy_bridge`. The
        dispatcher is invoked after callables and scripts so that
        plugin-supplied hookimpls see the same context.
        """
        self._pluggy_dispatcher = dispatcher

    # ------------------------------------------------------------------ introspection

    def registered(self, event: LifecycleEvent) -> list[str]:
        """Return labels of all hooks registered for ``event``, in order."""
        labels: list[str] = []
        for kind, idx in self._order[event]:
            if kind == "script":
                labels.append(f"script:{self._scripts[event][idx].path}")
            else:
                labels.append(self._callables[event][idx].label)
        return labels

    # ------------------------------------------------------------------ dispatch

    def run(self, event: LifecycleEvent, context: LifecycleContext) -> LifecycleContext:
        """Run all hooks for ``event`` synchronously, in registration order.

        For events in :data:`STANDARD_EVENTS`, scripts may emit a JSON
        decision on stdout. A ``deny`` decision raises
        :class:`HookDenied`; ``mutate`` and ``annotate`` produce a new
        :class:`LifecycleContext` whose ``data`` field is updated and
        is passed to subsequent hooks. The returned context is the
        final (possibly mutated) context.

        Pre-existing callers that ignore the return value continue to
        work - the signature change is forward-compatible.

        Raises:
            HookFailure: On the first failure; subsequent hooks are not run.
            HookDenied: When a hook explicitly denies the event.
        """
        current = context
        for kind, idx in self._order[event]:
            if kind == "script":
                hook = self._scripts[event][idx]
                if not self._filter_admits(event, hook, current):
                    continue
                decision = self._run_script(event, hook, current)
                current = _apply_decision(event, current, decision)
            else:
                self._run_callable(event, self._callables[event][idx], current)
        if self._pluggy_dispatcher is not None:
            try:
                self._pluggy_dispatcher(event, current)
            except (HookFailure, HookDenied):
                raise
            except Exception as exc:
                raise HookFailure(event, "pluggy", cause=exc) from exc
        return current

    def run_async(self, event: LifecycleEvent, context: LifecycleContext) -> Future[LifecycleContext]:
        """Schedule ``run`` on a background thread and return a Future.

        Use for post-events where the caller should not block on I/O.
        The caller owns the future; failures surface via ``future.exception()``.
        The future resolves to the final (possibly mutated) context.
        """
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bernstein-hooks")
        return self._executor.submit(self.run, event, context)

    def shutdown(self, wait: bool = True) -> None:
        """Tear down the background executor, if one was started."""
        if self._executor is not None:
            self._executor.shutdown(wait=wait)
            self._executor = None

    # ------------------------------------------------------------------ internals

    def _filter_admits(
        self,
        event: LifecycleEvent,
        hook: _ScriptHook,
        context: LifecycleContext,
    ) -> bool:
        """Return True if ``hook`` should run for this event payload.

        A hook with no filter always runs. A hook whose filter does not
        match the event payload is skipped before any subprocess spawn,
        and a ``hook.filtered`` metric is emitted via the bound sink.
        """
        if hook.hook_filter is None:
            return True
        if hook.hook_filter.matches(context.data):
            return True
        reason = f"filter '{hook.hook_filter.source}' did not match"
        log.debug("skipping hook %s for %s: %s", hook.path, event.value, reason)
        if self._filtered_metric_sink is not None:
            with contextlib.suppress(Exception):
                self._filtered_metric_sink(event, f"script:{hook.path}", reason)
        return False

    def _run_callable(
        self,
        event: LifecycleEvent,
        hook: _CallableHook,
        context: LifecycleContext,
    ) -> None:
        try:
            hook.fn(context)
        except Exception as exc:
            raise HookFailure(event, hook.label, cause=exc) from exc

    def _run_script(
        self,
        event: LifecycleEvent,
        hook: _ScriptHook,
        context: LifecycleContext,
    ) -> HookDecision | None:
        label = f"script:{hook.path}"
        # Fail closed at the spawn boundary: a script registered from the
        # repository must not run until the workspace is trusted.
        if not is_workspace_trusted(context.workdir):
            log.warning(
                "Hook execution gated: workspace is not trusted (%s). Refusing %s for %s.",
                context.workdir,
                label,
                event.value,
            )
            raise UntrustedWorkspaceError(event, label, context.workdir)
        env = _build_script_env(context)
        payload = json.dumps(context.to_payload()).encode("utf-8")
        try:
            proc = subprocess.run(
                [str(hook.path)],
                input=payload,
                env=env,
                cwd=str(context.workdir),
                capture_output=True,
                timeout=hook.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise HookFailure(event, label, cause=exc) from exc
        except subprocess.TimeoutExpired as exc:
            raise HookFailure(event, label, stderr="timeout", cause=exc) from exc

        stdout = proc.stdout or b""
        if len(stdout) > MAX_STDOUT_BYTES:
            truncated = stdout[:MAX_STDOUT_BYTES]
            log.warning(
                "hook stdout truncated from %d to %d bytes for %s",
                len(stdout),
                MAX_STDOUT_BYTES,
                hook.path,
            )
            stdout = truncated

        if proc.returncode != 0:
            stderr_text = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise HookFailure(event, label, exit_code=proc.returncode, stderr=stderr_text)

        return parse_hook_decision(stdout)


def _build_script_env(context: LifecycleContext) -> dict[str, str]:
    """Construct the environment for a script subprocess.

    Only whitelisted parent variables are forwarded. Anything callers
    want visible to hooks must live on ``context.env`` or be explicit
    ``BERNSTEIN_*`` values.
    """
    env: dict[str, str] = {}
    parent = _read_parent_env()
    for key in _ENV_WHITELIST:
        value = parent.get(key)
        if value is not None:
            env[key] = value
    # Forward all BERNSTEIN_* variables already on the parent.
    for key, value in parent.items():
        if key.startswith("BERNSTEIN_"):
            env[key] = value

    env["BERNSTEIN_EVENT"] = context.event.value
    if context.task is not None:
        env["BERNSTEIN_TASK_ID"] = context.task
    if context.session_id is not None:
        env["BERNSTEIN_SESSION_ID"] = context.session_id
    env["BERNSTEIN_WORKDIR"] = str(context.workdir)

    # Context.env wins over anything inherited so callers can override
    # a whitelisted value deliberately.
    env.update(context.env)
    return env


def _read_parent_env() -> MappingProxyType[str, str] | dict[str, str]:
    """Indirection point so tests can monkeypatch environment inheritance."""
    return os.environ.copy()


# ---------------------------------------------------------------------- decisions


def parse_hook_decision(stdout: bytes | str) -> HookDecision | None:
    """Parse a hook subprocess' stdout into a :class:`HookDecision`.

    The contract is intentionally narrow: hooks may emit either nothing,
    or a single JSON object with at least a ``decision`` key. Anything
    else is treated as "no decision" so legacy hooks that print log
    lines on stdout continue to behave as ``allow``.

    Args:
        stdout: Captured stdout from the hook subprocess.

    Returns:
        A :class:`HookDecision` if the stdout parses as a JSON object,
        otherwise ``None``.
    """
    text = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else stdout
    text = text.strip()
    if not text:
        return None
    try:
        parsed_raw: object = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed_raw, dict):
        return None
    parsed: dict[str, Any] = cast("dict[str, Any]", parsed_raw)
    decision_raw: Any = parsed.get("decision", DECISION_ALLOW)
    if not isinstance(decision_raw, str) or decision_raw not in _VALID_DECISIONS:
        # Unknown verb -> treat as allow but preserve the raw payload.
        decision_value: str = DECISION_ALLOW
    else:
        decision_value = decision_raw
    reason_raw: Any = parsed.get("reason", "")
    reason_value: str = reason_raw if isinstance(reason_raw, str) else str(reason_raw)
    data_raw: Any = parsed.get("data", {})
    data_value: dict[str, Any] = cast("dict[str, Any]", data_raw) if isinstance(data_raw, dict) else {}
    return HookDecision(
        decision=decision_value,
        reason=reason_value,
        data=data_value.copy(),
        raw=parsed.copy(),
    )


def _apply_decision(
    event: LifecycleEvent,
    context: LifecycleContext,
    decision: HookDecision | None,
) -> LifecycleContext:
    """Mutate ``context`` according to ``decision``.

    Raises:
        HookDenied: When ``decision.decision == "deny"``.
    """
    if decision is None or decision.decision == DECISION_ALLOW:
        return context
    if decision.decision == DECISION_DENY:
        raise HookDenied(event, hook="script", reason=decision.reason or "denied")
    if decision.decision == DECISION_MUTATE:
        return LifecycleContext(
            event=context.event,
            task=context.task,
            session_id=context.session_id,
            workdir=context.workdir,
            env=context.env.copy(),
            timestamp=context.timestamp,
            data=dict(decision.data),
        )
    if decision.decision == DECISION_ANNOTATE:
        merged = context.data.copy()
        merged.update(decision.data)
        return LifecycleContext(
            event=context.event,
            task=context.task,
            session_id=context.session_id,
            workdir=context.workdir,
            env=context.env.copy(),
            timestamp=context.timestamp,
            data=merged,
        )
    return context


# ---------------------------------------------------------------------- discovery

#: Default location where users drop ``<event>.{sh,py}`` scripts.
DEFAULT_HOOK_DIR_NAME: str = ".bernstein/hooks"

_DEFAULT_HOOK_SUFFIXES: tuple[str, ...] = (".sh", ".py")


def bind_retry_continuation_emit(registry: HookRegistry) -> None:
    """Wire the commit-completion retry path into ``registry``.

    After this call, every successful retry launch from
    :func:`bernstein.core.orchestration.commit_completion.maybe_retry_continuation`
    synchronously runs the registry for
    :attr:`LifecycleEvent.AGENT_RETRY_CONTINUATION` with a context
    whose ``data`` field carries ``session_id``, ``reason``, and
    ``attempt``.

    Mirrors :func:`bind_rate_limit_emit`. The orchestrator typically
    calls this once at startup alongside the rate-limit binding.
    """
    from bernstein.core.orchestration import commit_completion as _cc

    def _emit(payload: dict[str, Any]) -> None:
        ctx = LifecycleContext(
            event=LifecycleEvent.AGENT_RETRY_CONTINUATION,
            session_id=payload.get("session_id"),  # type: ignore[arg-type]
            data=payload,
        )
        registry.run(LifecycleEvent.AGENT_RETRY_CONTINUATION, ctx)

    _cc.set_retry_lifecycle_emitter(_emit)


def bind_rate_limit_emit(registry: HookRegistry) -> None:
    """Wire the adapter rate-limit meter into ``registry``.

    After this call, every ``record_rate_limit_hit(...)`` from
    :mod:`bernstein.adapters.base` synchronously runs the registry for
    :attr:`LifecycleEvent.RATE_LIMIT_HIT` with a context whose ``data``
    field carries ``adapter``, ``provider``, ``error_code`` and the
    meter snapshot.

    The orchestrator typically calls this once at startup.
    """
    # Import lazily so importing this module does not pull in the
    # adapters package eagerly (and so the test surface can replace the
    # callback without touching the import order).
    from bernstein.adapters import base as _adapters_base

    def _emit(meter: _adapters_base.RateLimitMeter, error_code: str) -> None:
        snapshot = meter.to_snapshot()
        payload: dict[str, Any] = {
            "adapter": meter.adapter_name,
            "provider": meter.provider,
            "error_code": error_code,
            "meter": snapshot,
        }
        ctx = LifecycleContext(event=LifecycleEvent.RATE_LIMIT_HIT, data=payload)
        registry.run(LifecycleEvent.RATE_LIMIT_HIT, ctx)

    _adapters_base.set_rate_limit_emit_callback(_emit)


def discover_default_hook_scripts(
    root: str | os.PathLike[str] | None = None,
) -> dict[LifecycleEvent, list[Path]]:
    """Discover scripts under ``<root>/.bernstein/hooks/<event>.{sh,py}``.

    The filename stem is matched against :class:`LifecycleEvent` values
    so a file named ``preToolUse.sh`` registers against
    :attr:`LifecycleEvent.PRE_TOOL_USE`. Files whose stem does not match
    a known event are silently ignored - that keeps the convention
    forgiving for ``README.md`` or other helpers operators drop into
    the directory.

    Args:
        root: Repository root. Defaults to the current working
            directory.

    Returns:
        A mapping from event to a list of script paths sorted by name.
    """
    base = Path(root) if root is not None else Path.cwd()
    hook_dir = base / DEFAULT_HOOK_DIR_NAME
    discovered: dict[LifecycleEvent, list[Path]] = {event: [] for event in LifecycleEvent}
    if not hook_dir.is_dir():
        return discovered
    valid_values = {event.value: event for event in LifecycleEvent}
    for entry in sorted(hook_dir.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix not in _DEFAULT_HOOK_SUFFIXES:
            continue
        event = valid_values.get(entry.stem)
        if event is None:
            continue
        discovered[event].append(entry)
    return discovered
