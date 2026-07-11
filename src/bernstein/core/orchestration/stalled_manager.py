"""Stalled-manager detection - actionable error when manager never creates children.

The manager agent is responsible for decomposing the seed goal into child tasks
by POSTing to the task server. When the manager process is alive (heartbeats
present, hook events being recorded) but never makes a successful ``POST /tasks``
call - typically because of an auth-token issue between the manager worktree
and the task server - the generic watchdog eventually kills the task server
with a misleading "Server unresponsive" message after roughly three minutes.

This module detects that specific failure mode directly:

* A manager-role session has been alive for ``STALL_THRESHOLD_S`` seconds.
* No child tasks exist on the server (only the manager's own seed task).

When that combination holds, we

1. log a clear single-line orchestrator message (no false "unresponsive" claim),
2. write a structured failure record to ``.sdd/runtime/failures/`` with the
   manager session id, hook event count, last few bash commands, redacted env,
   and a remediation pointer,
3. request a clean orchestrator shutdown (``_running = False``) so the parent
   ``bernstein run`` invocation exits non-zero with the diagnostic visible.

The detector is purely observability + UX; it never restarts the server,
never kills the manager, and never modifies the auth flow itself.
"""

from __future__ import annotations

import json
import logging
import math
import operator
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bernstein.core import defaults as _defaults

logger = logging.getLogger(__name__)

# How long the manager may run with zero child tasks before we declare a stall.
# Chosen to comfortably exceed the manager's own LLM-call latency (typically
# 30-60 s). The generic watchdog here is `AGENT.idle_log_age_threshold_s`
# (src/bernstein/core/defaults.py, also 180.0 s) which kills any agent -
# including the manager - whose log hasn't grown in 3 minutes. 180.0 would
# race that idle-kill instead of firing before it, so this stays a few
# seconds under it to guarantee the stalled-manager detector reports the
# real cause first.
#
# This is only reached when ``orch._config`` is unset entirely (bare test
# doubles) - production always carries a value via
# ``OrchestratorConfig.stalled_manager_threshold_s``'s ``default_factory``.
# Derived from ``core.defaults.ORCHESTRATOR`` rather than a second hardcoded
# literal so there is exactly one authoritative default to tune.
STALL_THRESHOLD_S: float = _defaults.ORCHESTRATOR.stalled_manager_threshold_s

# Env var checked ahead of ``OrchestratorConfig.stalled_manager_threshold_s``
# (which itself defaults from ``tuning.orchestrator.stalled_manager_threshold_s``
# in bernstein.yaml, see core.defaults.OrchestratorDefaults). Precedence:
# env var > yaml-tunable config field > the module constant above.
STALL_THRESHOLD_ENV_VAR: str = "BERNSTEIN_STALL_THRESHOLD_S"

# Path to the operator-facing remediation doc. A sibling effort owns the
# actual document; if it already lives elsewhere we still emit a pointer so
# operators can grep for it.
REMEDIATION_DOC: str = "docs/architecture/manager-auth.md"

# Env vars whose names we surface in the failure record (values redacted).
# Anything that looks like a secret is redacted regardless of being on the
# allowlist - see ``_redact_env``.
_TRACKED_ENV_PREFIXES: tuple[str, ...] = ("BERNSTEIN_", "ANTHROPIC_", "OPENAI_", "OPENROUTER_")

# Substrings used to redact env values whose names suggest secrets.
_SENSITIVE_NAME_HINTS: tuple[str, ...] = (
    "TOKEN",
    "SECRET",
    "KEY",
    "PASSWORD",
    "AUTH",
    "CREDENTIAL",
)


@dataclass(frozen=True)
class StalledManagerDiagnostic:
    """Structured diagnostic for a stalled-manager incident."""

    session_id: str
    manager_task_id: str
    runtime_s: float
    hook_event_count: int
    last_bash_commands: list[str] = field(default_factory=list)
    env_seen: dict[str, str] = field(default_factory=dict)
    remediation: str = REMEDIATION_DOC

    def message(self) -> str:
        """Return the one-line operator-facing console message.

        ``hook_event_count`` is sourced from the hook sidecar file
        (``.sdd/runtime/hooks/{session_id}.jsonl``), a different telemetry
        channel from the manager's own per-session activity log. A manager
        can be genuinely alive and working (reading files, planning) for
        the full stall window while that sidecar still reads 0 events, so
        ``hook_event_count == 0`` does not by itself prove the manager
        never authenticated to the task server - only that this specific
        channel saw nothing. Only assert an auth failure when there is
        corroborating evidence (a nonzero hook-event count).
        """
        if self.hook_event_count == 0 and not self.last_bash_commands:
            explanation = (
                "manager active but no child tasks yet - NOT confirmed as an auth failure; "
                "hook_event_count is a separate telemetry channel from the manager's own "
                "activity log and can read 0 even while the manager is working. The stall "
                "threshold may simply be too low for this codebase's decomposition time - see "
                f"{STALL_THRESHOLD_ENV_VAR} / tuning.orchestrator.stalled_manager_threshold_s"
            )
        else:
            explanation = (
                f"this is NOT a generic server timeout - the manager likely cannot authenticate "
                f"to the task server ({self.hook_event_count} hook event(s) recorded)"
            )
        return (
            f"Manager session {self.session_id} ran for {self.runtime_s:.0f}s without "
            f"creating any child tasks ({self.hook_event_count} hook event(s) recorded). "
            f"{explanation}. See {self.remediation} for remediation."
        )

    def to_record(self) -> dict[str, Any]:
        """Serializable failure record for ``.sdd/runtime/failures/``."""
        return {
            "kind": "stalled_manager",
            "session_id": self.session_id,
            "manager_task_id": self.manager_task_id,
            "runtime_s": round(self.runtime_s, 2),
            "hook_event_count": self.hook_event_count,
            "last_bash_commands": self.last_bash_commands.copy(),
            "env_seen": self.env_seen.copy(),
            "remediation": self.remediation,
            "detected_at": time.time(),
        }


def _is_sensitive_name(name: str) -> bool:
    upper = name.upper()
    return any(hint in upper for hint in _SENSITIVE_NAME_HINTS)


def _redact_env(env: dict[str, str]) -> dict[str, str]:
    """Filter to tracked prefixes and redact obviously sensitive values."""
    out: dict[str, str] = {}
    for name, value in env.items():
        if not any(name.startswith(p) for p in _TRACKED_ENV_PREFIXES):
            continue
        if _is_sensitive_name(name):
            out[name] = "<redacted>" if value else "<unset>"
        else:
            # Even non-sensitive entries are truncated to keep the record small.
            out[name] = value[:120]
    return out


def _read_hook_events(workdir: Path, session_id: str, *, tail: int = 200) -> list[dict[str, Any]]:
    """Read the last ``tail`` JSONL records from the hook sidecar for a session."""
    hooks_path = workdir / ".sdd" / "runtime" / "hooks" / f"{session_id}.jsonl"
    if not hooks_path.exists():
        return []
    try:
        raw = hooks_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in raw[-tail:]:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _extract_last_bash_commands(events: list[dict[str, Any]], *, limit: int = 5) -> list[str]:
    """Pull the last ``limit`` Bash tool inputs from a hook event stream."""
    bashes: list[str] = []
    for event in events:
        if event.get("tool_name") == "Bash":
            cmd = str(event.get("tool_input", ""))[:200]
            if cmd:
                bashes.append(cmd)
    return bashes[-limit:]


def _find_manager_session(agents: dict[str, Any], now: float) -> Any | None:
    """Return the oldest live ``role == "manager"`` session, if any."""
    candidates: list[tuple[float, Any]] = []
    for session in agents.values():
        if getattr(session, "role", "") != "manager":
            continue
        if getattr(session, "status", "") == "dead":
            continue
        runtime = max(now - float(getattr(session, "spawn_ts", now)), 0.0)
        candidates.append((runtime, session))
    if not candidates:
        return None
    # Oldest session first - that's the one we judge against the threshold.
    candidates.sort(key=operator.itemgetter(0), reverse=True)
    return candidates[0][1]


def _has_child_tasks(latest_tasks: dict[str, Any], manager_task_id: str) -> bool:
    """Return True if any task other than the manager's seed task exists."""
    return any(task_id != manager_task_id for task_id in latest_tasks)


def _write_failure_record(workdir: Path, diagnostic: StalledManagerDiagnostic) -> Path | None:
    """Persist the diagnostic to ``.sdd/runtime/failures/`` if writable."""
    failures_dir = workdir / ".sdd" / "runtime" / "failures"
    try:
        failures_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    ts = time.strftime("%Y%m%dT%H%M%S")
    path = failures_dir / f"manager-stalled-{ts}-{diagnostic.session_id}.json"
    try:
        path.write_text(json.dumps(diagnostic.to_record(), indent=2), encoding="utf-8")
    except OSError:
        return None
    return path


def _append_orchestrator_log(workdir: Path, line: str) -> None:
    """Append a single line to ``.sdd/runtime/orchestrator.log``, best-effort."""
    log_dir = workdir / ".sdd" / "runtime"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    log_path = log_dir / "orchestrator.log"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with suppress(OSError), log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{ts}] {line}\n")


def build_diagnostic(
    workdir: Path,
    session: Any,
    *,
    now: float,
    manager_env: dict[str, str] | None = None,
) -> StalledManagerDiagnostic:
    """Assemble a diagnostic record from filesystem + session state."""
    session_id = str(getattr(session, "id", ""))
    task_ids = getattr(session, "task_ids", []) or []
    manager_task_id = str(task_ids[0]) if task_ids else ""
    runtime = max(now - float(getattr(session, "spawn_ts", now)), 0.0)
    events = _read_hook_events(workdir, session_id)
    bashes = _extract_last_bash_commands(events)
    env_seen = _redact_env(manager_env or {})
    return StalledManagerDiagnostic(
        session_id=session_id,
        manager_task_id=manager_task_id,
        runtime_s=runtime,
        hook_event_count=len(events),
        last_bash_commands=bashes,
        env_seen=env_seen,
    )


def _is_sane_threshold(value: float) -> bool:
    """A stall threshold must be a positive, finite number of seconds.

    ``0``/negative would fire the watchdog on the very first tick after
    spawn for every manager session. ``nan``/``inf`` would make the
    ``runtime < threshold`` comparison in :func:`detect_stalled_manager`
    permanently false/true, silently disabling or permanently no-op'ing the
    watchdog with no operator-visible signal - a real risk once this value
    can come from an operator-typable env var.
    """
    return math.isfinite(value) and value > 0


def _resolve_stall_threshold_s(orch: Any) -> float:
    """Resolve the effective stall threshold, cached once per orchestrator.

    Precedence: ``BERNSTEIN_STALL_THRESHOLD_S`` env var > ``OrchestratorConfig.
    stalled_manager_threshold_s`` (yaml-tunable via ``tuning.orchestrator.
    stalled_manager_threshold_s``, see ``core.defaults.OrchestratorDefaults``)
    > the ``STALL_THRESHOLD_S`` module default. An unparseable, non-positive,
    or non-finite value at any tier falls through to the next tier with a
    warning rather than crashing or silently disabling the watchdog.

    The resolved value is cached on ``orch`` so env/config are only
    re-parsed - and the resolution + race-check logged - once per
    orchestrator instance, instead of on every tick for the lifetime of the
    manager session (``detect_stalled_manager`` is called every tick while
    the manager is alive and below threshold).
    """
    cached = getattr(orch, "_stalled_manager_threshold_cache", None)
    if cached is not None:
        return cached

    resolved: float | None = None
    raw_env = os.environ.get(STALL_THRESHOLD_ENV_VAR)
    if raw_env is not None and raw_env.strip():
        try:
            env_value = float(raw_env)
        except ValueError:
            logger.warning(
                "%s=%r is not a valid float; falling back to config/default stall threshold",
                STALL_THRESHOLD_ENV_VAR,
                raw_env,
            )
        else:
            if _is_sane_threshold(env_value):
                logger.info(
                    "stalled_manager: threshold resolved to %.1fs from env %s=%r",
                    env_value,
                    STALL_THRESHOLD_ENV_VAR,
                    raw_env,
                )
                resolved = env_value
            else:
                logger.warning(
                    "%s=%r must be a positive, finite number of seconds; "
                    "falling back to config/default stall threshold",
                    STALL_THRESHOLD_ENV_VAR,
                    raw_env,
                )

    if resolved is None:
        config_value = getattr(getattr(orch, "_config", None), "stalled_manager_threshold_s", None)
        if config_value is not None and _is_sane_threshold(float(config_value)):
            resolved = float(config_value)
        else:
            if config_value is not None:
                logger.warning(
                    "tuning.orchestrator.stalled_manager_threshold_s=%r must be a positive, "
                    "finite number of seconds; falling back to the %.1fs default",
                    config_value,
                    STALL_THRESHOLD_S,
                )
            resolved = STALL_THRESHOLD_S

    # Race-check runs for every resolution path (env, config, and default),
    # not just the config/default fallback - env is the primary way an
    # operator raises the threshold, so it's the path that most needs this.
    _warn_if_races_idle_kill(resolved)
    orch._stalled_manager_threshold_cache = resolved
    return resolved


def _warn_if_races_idle_kill(threshold_s: float) -> None:
    """Warn when the stall threshold has been raised past the idle-agent deadline.

    ``detect_idle_agents`` (``core.agents.heartbeat``) has no call site in the
    orchestrator tick loop today, so this is not a live race - but the two
    deadlines' 170-vs-180s spacing is intentional, and a raised threshold
    should still surface if that watchdog is ever wired up later.
    """
    idle_threshold = float(_defaults.AGENT.idle_log_age_threshold_s)
    if threshold_s >= idle_threshold:
        logger.warning(
            "stalled_manager_threshold_s (%.0fs) >= AGENT.idle_log_age_threshold_s (%.0fs); "
            "if idle-log-kill is ever wired into the tick loop it may fire before the "
            "stalled-manager diagnostic can produce its more specific message",
            threshold_s,
            idle_threshold,
        )


def detect_stalled_manager(orch: Any) -> StalledManagerDiagnostic | None:
    """Return a diagnostic if a manager session has stalled, else ``None``.

    Pure detection - never mutates orchestrator state. The caller decides
    whether to log, write the failure record, or abort the run.
    """
    workdir = getattr(orch, "_workdir", None)
    if not isinstance(workdir, Path):
        return None

    agents_raw = getattr(orch, "_agents", {})
    if not isinstance(agents_raw, dict):
        return None
    agents: dict[str, Any] = dict(agents_raw)

    now = time.time()
    session = _find_manager_session(agents, now)
    if session is None:
        return None

    runtime = max(now - float(getattr(session, "spawn_ts", now)), 0.0)
    threshold = _resolve_stall_threshold_s(orch)
    if runtime < threshold:
        return None

    task_ids = getattr(session, "task_ids", []) or []
    manager_task_id = str(task_ids[0]) if task_ids else ""

    latest_tasks_raw = getattr(orch, "_latest_tasks_by_id", {})
    if not isinstance(latest_tasks_raw, dict):
        return None
    latest_tasks: dict[str, Any] = dict(latest_tasks_raw)

    if _has_child_tasks(latest_tasks, manager_task_id):
        return None

    manager_env = getattr(orch, "_manager_env_snapshot", None)
    return build_diagnostic(workdir, session, now=now, manager_env=manager_env)


def handle_stalled_manager(orch: Any) -> StalledManagerDiagnostic | None:
    """Detect, surface, and request shutdown when the manager is stalled.

    Returns the diagnostic on detection (also written to logs + failures dir);
    returns ``None`` when no stall is present. Setting ``_running = False`` on
    the orchestrator triggers the existing clean-drain path in
    ``orchestrator_run._run_loop`` - no generic-watchdog kill is required.
    """
    # Avoid emitting the same diagnostic on every tick.
    if getattr(orch, "_stalled_manager_emitted", False):
        return None

    diagnostic = detect_stalled_manager(orch)
    if diagnostic is None:
        return None

    workdir = getattr(orch, "_workdir", None)
    message = diagnostic.message()
    logger.error("%s", message)

    if isinstance(workdir, Path):
        _append_orchestrator_log(workdir, f"stalled_manager: {message}")
        record_path = _write_failure_record(workdir, diagnostic)
        if record_path is not None:
            logger.error("Stalled-manager diagnostic written to %s", record_path)

    # Best-effort console surface for operators running ``bernstein run``
    # in the foreground.  The orchestrator does not assume a Rich console
    # is present; ``print`` reaches stdout/stderr regardless.
    print(f"[bernstein] {message}")

    # Bulletin / notification channel if the orchestrator wires one in.
    bulletin = getattr(orch, "_post_bulletin", None)
    if callable(bulletin):
        try:
            bulletin("alert", f"stalled_manager: {message}")
        except Exception:
            logger.debug("post_bulletin raised while reporting stalled manager", exc_info=True)

    # Request clean shutdown so the run aborts with a non-zero exit *and* the
    # operator sees the actionable diagnostic instead of the generic
    # "Server unresponsive" message that the supervisor would emit later.
    orch._running = False
    orch._stalled_manager_emitted = True
    orch._stalled_manager_diagnostic = diagnostic
    return diagnostic
