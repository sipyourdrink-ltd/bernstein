"""Base adapter for CLI coding agents."""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from bernstein.adapters.http_429_classifier import HTTP429Classification, classify_429
from bernstein.core.lineage.artifact_events import emit_production_event
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.platform_compat import (
    kill_process_group,
    process_alive,
    reap_process_group,
)
from bernstein.core.resource_limits import ResourceLimits, make_preexec_fn

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from bernstein.core.config.platform_compat import ProcessReapReceipt
    from bernstein.core.lineage.artifact_events import ArtifactProductionEvent
    from bernstein.core.lineage.identity import AgentCard
    from bernstein.core.models import AbortReason, ApiTierInfo, ModelConfig

logger = logging.getLogger(__name__)

# Default timeout for spawned agent processes (30 minutes).
DEFAULT_TIMEOUT_SECONDS: int = 1800

# Grace period between SIGTERM and SIGKILL (seconds).
_SIGTERM_GRACE_SECONDS: int = 30
#: First dotted-numeric token in a ``--version`` blob. Possessive quantifiers
#: plus the digit-run anchor keep the scan linear on untrusted subprocess
#: output; see the matching constant in ``adapters/security_floor.py``.
VERSION_TOKEN_RE = re.compile(r"(?<!\d)\d++(?:\.\d++){1,3}")


# ---------------------------------------------------------------------------
# Mutation-observability capability (issue #2507)
# ---------------------------------------------------------------------------
# Values mirror bernstein.core.replay.provider_state.CAPABILITY_* and are
# duplicated here (rather than imported) so the adapter layer stays free of
# replay-journal imports at module load time.

#: The adapter surfaces provider-side context-mutation signals (compaction
#: boundaries and similar opaque state markers) from its stream output.
MUTATION_OBSERVABILITY_OBSERVED = "observed"

#: The adapter has no observation surface for provider-side context
#: mutations. Recorded per run in the replay journal so an absence of
#: mutation entries stays distinguishable from an inability to see them.
MUTATION_OBSERVABILITY_DECLARED_BLIND = "declared-blind"


class SpawnError(RuntimeError):
    """Raised when an adapter process exits too early to be treated as spawned."""


class RateLimitError(SpawnError):
    """Raised when an adapter detects provider-side rate limiting on startup."""


class StandingCapError(SpawnError):
    """Raised when an adapter detects a standing account/key/session/spend cap.

    Unlike :class:`RateLimitError`, a standing cap will not clear within
    the run, so it must not consume the retry budget or trigger backoff.
    """

    def __init__(self, message: str, reason_code: str = "standing_cap_exceeded") -> None:
        super().__init__(message)
        self.reason_code = reason_code


# ---------------------------------------------------------------------------
# Rate-limit meter (per-adapter observability surface)
# ---------------------------------------------------------------------------

#: Default panel/window for rolling 429 counts, in seconds.
RATE_LIMIT_WINDOW_SECONDS: int = 300

#: Initial backoff after the first 429, in seconds.
_DEFAULT_INITIAL_BACKOFF_SECONDS: float = 1.0

#: Hard cap on exponential backoff growth, in seconds.
_DEFAULT_MAX_BACKOFF_SECONDS: float = 60.0


@dataclass
class RateLimitMeter:
    """Per-adapter rolling counters for upstream rate-limit pressure.

    The meter records, reports, and decays. It does not enforce: there
    is no token-bucket scheduler here. The intent is to give
    ``bernstein status`` and trace consumers a single place to read
    "how often is this adapter hitting 429 right now and how long is it
    waiting between retries".

    Attributes:
        adapter_name: Short adapter identifier (e.g. ``"claude"``).
        provider: Human-readable upstream provider label.
        requests_per_minute_target: Operator-declared RPM target, when
            known. ``0`` means "unset", and the meter just records
            429-related stats without an RPM denominator.
        last_429_ts: Unix timestamp of the most recent 429-class event,
            or ``0.0`` if none observed.
        consecutive_429_count: 429-class events observed since the last
            successful request. Reset by :meth:`record_success`.
        backoff_seconds_current: Current advisory backoff. Grows
            exponentially per consecutive 429, capped at
            ``_DEFAULT_MAX_BACKOFF_SECONDS``.
        window_hits: Timestamps of 429-class events within the active
            rolling window, used for the "x<n> in last <window>"
            summary line.
        last_error_code: Last observed provider-side error label, when
            the adapter could supply one (e.g. ``"anthropic_429"``).
    """

    adapter_name: str
    provider: str = ""
    requests_per_minute_target: int = 0
    last_429_ts: float = 0.0
    consecutive_429_count: int = 0
    backoff_seconds_current: float = 0.0
    window_hits: list[float] = field(default_factory=list[float])
    last_error_code: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record_hit(
        self,
        *,
        error_code: str = "",
        now: float | None = None,
        window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        """Register one 429-class event on this meter.

        Args:
            error_code: Provider-specific error label (optional).
            now: Override clock for tests; defaults to ``time.time()``.
            window_seconds: Rolling window for ``window_hits`` retention.
        """
        ts = time.time() if now is None else now
        with self._lock:
            self.last_429_ts = ts
            self.consecutive_429_count += 1
            self.last_error_code = error_code
            self.window_hits.append(ts)
            self._prune_locked(ts, window_seconds)
            # Exponential backoff: 1s, 2s, 4s, ... capped.
            prev = self.backoff_seconds_current
            if prev <= 0:
                self.backoff_seconds_current = _DEFAULT_INITIAL_BACKOFF_SECONDS
            else:
                self.backoff_seconds_current = min(prev * 2.0, _DEFAULT_MAX_BACKOFF_SECONDS)

    def record_success(self) -> None:
        """Reset the consecutive-failure counter after a clean request."""
        with self._lock:
            self.consecutive_429_count = 0
            self.backoff_seconds_current = 0.0

    def hits_in_window(
        self,
        *,
        now: float | None = None,
        window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
    ) -> int:
        """Return the number of 429-class events within the rolling window."""
        ts = time.time() if now is None else now
        with self._lock:
            self._prune_locked(ts, window_seconds)
            return len(self.window_hits)

    def is_active(
        self,
        *,
        now: float | None = None,
        window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
    ) -> bool:
        """Return True when at least one 429 fired inside the window."""
        return self.hits_in_window(now=now, window_seconds=window_seconds) > 0

    def to_snapshot(
        self,
        *,
        now: float | None = None,
        window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
    ) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot for status surfaces."""
        ts = time.time() if now is None else now
        with self._lock:
            self._prune_locked(ts, window_seconds)
            last_ago = (ts - self.last_429_ts) if self.last_429_ts > 0 else None
            return {
                "adapter": self.adapter_name,
                "provider": self.provider,
                "requests_per_minute_target": self.requests_per_minute_target,
                "last_429_ts": self.last_429_ts,
                "last_429_ago_seconds": last_ago,
                "consecutive_429_count": self.consecutive_429_count,
                "backoff_seconds_current": self.backoff_seconds_current,
                "window_seconds": window_seconds,
                "hits_in_window": len(self.window_hits),
                "last_error_code": self.last_error_code,
            }

    def _prune_locked(self, now: float, window_seconds: int) -> None:
        """Drop hits older than ``window_seconds``. Caller holds ``_lock``."""
        cutoff = now - window_seconds
        self.window_hits = [t for t in self.window_hits if t >= cutoff]


# ---------------------------------------------------------------------------
# Process-local meter registry
# ---------------------------------------------------------------------------

_METERS_LOCK: threading.Lock = threading.Lock()
_METERS: dict[str, RateLimitMeter] = {}

#: Optional emit callback. Bound by the orchestrator to a HookRegistry so
#: meter updates can fire ``rate_limit.hit`` lifecycle events without the
#: adapters taking a hard dependency on the lifecycle package.
_RATE_LIMIT_EMIT: Callable[[RateLimitMeter, str], None] | None = None


def register_rate_limit_meter(meter: RateLimitMeter) -> None:
    """Make ``meter`` visible to ``bernstein status`` and trace consumers.

    Safe to call repeatedly with the same meter: the registry keys on
    ``adapter_name`` so re-registration just refreshes the entry.
    """
    with _METERS_LOCK:
        _METERS[meter.adapter_name] = meter


def get_rate_limit_meters() -> dict[str, RateLimitMeter]:
    """Return a shallow copy of the currently-registered meter set."""
    with _METERS_LOCK:
        return _METERS.copy()


def reset_rate_limit_meters() -> None:
    """Drop every registered meter. For tests only."""
    with _METERS_LOCK:
        _METERS.clear()


def set_rate_limit_emit_callback(
    callback: Callable[[RateLimitMeter, str], None] | None,
) -> None:
    """Bind (or clear) the optional ``rate_limit.hit`` emit callback.

    The orchestrator owns its :class:`HookRegistry`; calling this with a
    bound emit lets adapters surface the event without importing the
    lifecycle subsystem directly. Passing ``None`` clears the binding -
    used by tests that want to assert no event was emitted.
    """
    global _RATE_LIMIT_EMIT
    _RATE_LIMIT_EMIT = callback


def fold_rate_limit_events(
    events: list[dict[str, Any]],
    *,
    window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
) -> list[str]:
    """Collapse a sequence of ``rate_limit.hit`` events into one line per adapter.

    Each input dict is expected to carry at least an ``adapter`` key - the
    standard payload emitted by :func:`record_rate_limit_hit`. Events
    missing an adapter label are grouped under ``"unknown"`` so they
    remain visible to operators rather than being silently dropped.

    Args:
        events: Ordered list of ``rate_limit.hit`` event payload dicts.
        window_seconds: Window length to mention in the folded summary.

    Returns:
        One human-readable line per adapter, sorted alphabetically:
        ``"<adapter> hit 429 x<n> in last <window>"``.
    """
    counts: dict[str, int] = {}
    for event in events:
        adapter_raw = event.get("adapter") if isinstance(event, dict) else None
        adapter = str(adapter_raw) if adapter_raw else "unknown"
        counts[adapter] = counts.get(adapter, 0) + 1
    window_label = _format_window_label(window_seconds)
    return [f"{adapter} hit 429 x{count} in last {window_label}" for adapter, count in sorted(counts.items())]


def _format_window_label(window_seconds: int) -> str:
    """Render a window length as the shortest natural-language label."""
    if window_seconds <= 0:
        return "0s"
    if window_seconds % 3600 == 0:
        hours = window_seconds // 3600
        return f"{hours}h"
    if window_seconds % 60 == 0:
        minutes = window_seconds // 60
        return f"{minutes}min"
    return f"{window_seconds}s"


def record_rate_limit_hit(
    meter: RateLimitMeter,
    *,
    error_code: str = "",
    now: float | None = None,
    window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
) -> None:
    """Update ``meter`` and fire ``rate_limit.hit`` if a callback is bound.

    Centralised so every touchpoint emits the same payload and so the
    meter registration stays in lockstep with the emit.
    """
    meter.record_hit(error_code=error_code, now=now, window_seconds=window_seconds)
    register_rate_limit_meter(meter)
    callback = _RATE_LIMIT_EMIT
    if callback is None:
        return
    try:
        callback(meter, error_code)
    except Exception as exc:
        # Observability must never break the spawn/spawn-probe path.
        logger.warning("rate_limit.hit emit failed for %s: %s", meter.adapter_name, exc)


@dataclass
class SpawnResult:
    """Result of spawning an agent process."""

    pid: int
    log_path: Path
    proc: object | None = None  # subprocess.Popen, kept for poll()-based alive check
    timeout_timer: threading.Timer | None = field(default=None, repr=False)
    #: Adapters that finish their bookkeeping after the process exits publish
    #: the worker here, so a caller that needs the artefacts can join it
    #: rather than poll for the files to appear.
    post_exit_thread: threading.Thread | None = field(default=None, repr=False)
    abort_reason: AbortReason | None = None
    abort_detail: str = ""
    finish_reason: str = ""


class WaitableProcess(Protocol):
    """Minimal process protocol for fast-exit probing."""

    def wait(self, timeout: float | None = None) -> object:
        """Wait for process completion and return its exit status."""


#: Process-environment channel carrying the orchestrator root's heartbeat
#: directory down to ``bernstein-worker``.
#:
#: Adapters derive every runtime path from the ``workdir`` they are spawned
#: into, which under worktree isolation is the agent's own worktree -- while
#: the orchestrator reads agent state from the project root. A heartbeat
#: written under the worktree is therefore never observed, and the agent is
#: killed at the stale threshold with nothing in the log pointing at why
#: (issue #4330). The root travels through the environment, the same channel
#: ``BERNSTEIN_RUN_ID`` uses to reach agent subprocesses, so the ~50 adapter
#: call sites do not each have to learn the difference.
HEARTBEAT_DIR_ENV = "BERNSTEIN_HEARTBEAT_DIR"


def build_worker_cmd(
    cmd: list[str],
    *,
    role: str,
    session_id: str,
    pid_dir: Path,
    workdir: Path,
    log_path: Path,
    model: str = "",
    heartbeat_dir: Path | None = None,
) -> list[str]:
    """Wrap a CLI command with bernstein-worker for process visibility.

    The worker sets the process title to "bernstein: <role> [<session>]"
    and writes a PID metadata file for ``bernstein ps``.

    Args:
        cmd: The original CLI command to wrap.
        role: Agent role (qa, backend, etc.).
        session_id: Unique session identifier.
        pid_dir: Directory for PID metadata JSON files.
        workdir: Project root directory.
        log_path: Path to the agent log file.
        model: Model name for metadata display.
        heartbeat_dir: Directory the orchestrator polls for heartbeats.
            Defaults to whatever the orchestrator exported in
            :data:`HEARTBEAT_DIR_ENV`; when neither is set the flag is
            omitted and the worker falls back to ``--workdir``, which is
            what a standalone ``bernstein-worker`` invocation wants.

    Returns:
        Wrapped command list.
    """
    resolved_heartbeat_dir = str(heartbeat_dir) if heartbeat_dir is not None else os.environ.get(HEARTBEAT_DIR_ENV, "")
    return [
        sys.executable,
        "-m",
        "bernstein.core.orchestration.worker",
        "--role",
        role,
        "--session",
        session_id,
        "--pid-dir",
        str(pid_dir),
        "--workdir",
        str(workdir),
        "--log-path",
        str(log_path),
        "--model",
        model,
        *(["--heartbeat-dir", resolved_heartbeat_dir] if resolved_heartbeat_dir else []),
        "--",
        *cmd,
    ]


# ---------------------------------------------------------------------------
# Spawn-time capability notices (issue #4256)
# ---------------------------------------------------------------------------
#
# A capability an adapter does not have must be reported at spawn, never
# silently dropped: a dropped instruction is indistinguishable from a model
# that ignored it, and that ambiguity is what makes the failure expensive to
# diagnose. Notices land in the run record next to the other per-run JSONL
# journals (``refused_merges.jsonl``, ``mailbox.jsonl``) so an operator can
# answer "how were this run's protocol instructions delivered?" from the run
# itself rather than from adapter source.

#: Run-record journal of spawn-time capability notices, relative to the agent
#: working directory. One JSON object per line.
CAPABILITY_NOTICES_RELPATH: tuple[str, ...] = (".sdd", "runtime", "capability_notices.jsonl")


def capability_notices_path(workdir: Path) -> Path:
    """Return the run-record path spawn-time capability notices append to.

    Args:
        workdir: Agent working directory (root of ``.sdd``).

    Returns:
        Path to the run's ``capability_notices.jsonl``.
    """
    return workdir.joinpath(*CAPABILITY_NOTICES_RELPATH)


def _iter_capability_notices(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the parsed capability notices already recorded in a run.

    Args:
        path: The run's ``capability_notices.jsonl``.

    Yields:
        One decoded notice per well-formed line; malformed lines are skipped
        so a corrupt journal cannot make a spawn re-report or crash.
    """
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            yield parsed


class CLIAdapter(ABC):
    """Interface for launching and monitoring CLI coding agents.

    Implement this for each supported CLI (Claude Code, Codex, Gemini, etc.).

    Adapters that inherently dial out to a known SaaS endpoint declare it
    via :attr:`external_endpoints` (host, port tuples). The base helper
    :meth:`enforce_network_policy` consults the active policy at spawn time
    and raises ``NetworkPolicyDenied`` when the destination is forbidden.
    """

    external_endpoints: tuple[tuple[str, int], ...] = ()

    #: Subclasses may override to declare the upstream provider label that
    #: shows up in the ``bernstein status`` rate-limit panel. Defaults to
    #: the adapter name when left blank.
    rate_limit_provider: str = ""

    #: Subclasses may override to declare an operator-visible RPM target.
    #: ``0`` keeps the column unset.
    rate_limit_target_rpm: int = 0

    #: Subclasses opt into the retry-with-continuation path by setting
    #: this to ``True`` and implementing :meth:`continuation_args`. The
    #: orchestrator consults this attribute via
    #: :func:`bernstein.core.orchestration.commit_completion.adapter_supports_continuation`
    #: after a "success without commit" exit and only launches a
    #: continuation retry when the adapter has opted in. Default
    #: ``False`` so unknown adapters never trigger the retry path.
    supports_session_continuation: bool = False

    #: Whether this adapter can supply a structured per-session log path
    #: for the ProgressWatch liveness probe (see
    #: :mod:`bernstein.core.observability.progress_watch`). Adapters that
    #: write to a deterministic on-disk log set this to ``True`` and
    #: override :meth:`session_log_path_for`. The default is ``False`` so
    #: the dispatch loop falls back to plain process-exit detection.
    supports_session_log_watch: bool = False

    #: Per-adapter strategy declaration across the four axes defined in
    #: :mod:`bernstein.adapters._contract` - resume, dangerous-mode,
    #: event-channel, and output-mode. Left ``None`` here so the canonical
    #: declaration lives
    #: in ``STRATEGY_MATRIX`` keyed by registry name; subclasses MAY override
    #: with an inline :class:`~bernstein.adapters._contract.AdapterStrategy`
    #: to keep the declaration next to the implementation. Read it through
    #: :meth:`strategy`, never directly - that resolver applies the matrix
    #: fallback so undeclared adapters still get a conservative default.
    strategy_override: Any = None

    def __init__(self) -> None:
        self._resource_limits: ResourceLimits | None = None
        self._rate_limit_meter: RateLimitMeter | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Wrap each subclass's ``spawn`` with the system-addendum preflight.

        The ``system_addendum`` delivery bucket is a declared contract axis
        (:meth:`system_addendum_channel`), and a declaration nothing enforces
        is prose. Enforcing it here - once, on the base class - means every
        adapter reports how it handled the addendum, including third-party
        adapters and adapters written after this code, without a per-adapter
        call anyone can forget to add.

        The wrapper forwards its arguments untouched: an adapter that delivers
        the addendum receives exactly what it received before.

        Args:
            **kwargs: Forwarded to :meth:`object.__init_subclass__`.
        """
        super().__init_subclass__(**kwargs)
        spawn = cls.__dict__.get("spawn")
        if not callable(spawn) or getattr(spawn, "__isabstractmethod__", False):
            return
        if getattr(spawn, "_bernstein_addendum_preflight", False):
            return

        @functools.wraps(spawn)
        def _spawn_with_addendum_preflight(self: CLIAdapter, *args: Any, **spawn_kwargs: Any) -> Any:
            self.report_system_addendum_delivery(
                system_addendum=str(spawn_kwargs.get("system_addendum") or ""),
                workdir=spawn_kwargs.get("workdir"),
                session_id=str(spawn_kwargs.get("session_id") or ""),
            )
            return spawn(self, *args, **spawn_kwargs)

        _spawn_with_addendum_preflight._bernstein_addendum_preflight = True  # type: ignore[attr-defined]
        cls.spawn = _spawn_with_addendum_preflight  # type: ignore[method-assign]

    @property
    def rate_limit_meter(self) -> RateLimitMeter:
        """Return the per-adapter meter, instantiating it on first read.

        The meter is created lazily so adapters that never see a 429 do
        not pay for an unused dataclass instance. The first access also
        registers the meter so ``bernstein status`` can find it even if
        no hit has yet been recorded.
        """
        if self._rate_limit_meter is None:
            try:
                adapter_name = self.name()
            except Exception:
                adapter_name = type(self).__name__.lower()
            provider = self.rate_limit_provider or adapter_name
            self._rate_limit_meter = RateLimitMeter(
                adapter_name=adapter_name,
                provider=provider,
                requests_per_minute_target=self.rate_limit_target_rpm,
            )
            register_rate_limit_meter(self._rate_limit_meter)
        return self._rate_limit_meter

    def record_rate_limit_hit(self, *, error_code: str = "") -> None:
        """Convenience hook for adapter HTTP error handlers.

        Concrete adapters call this from their 429 detection paths so
        the meter is updated and the lifecycle event fires through one
        well-known funnel.
        """
        record_rate_limit_hit(self.rate_limit_meter, error_code=error_code)

    def enforce_network_policy(self) -> None:
        """Refuse to spawn when the adapter's known endpoints are denied.

        No-op when ``external_endpoints`` is empty (the adapter is a pure
        local subprocess) or when the policy is unrestricted.
        """
        if not self.external_endpoints:
            return
        from bernstein.core.security.network_policy import policy_from_env

        policy = policy_from_env()
        for host, port in self.external_endpoints:
            policy.check(host, port, source=f"adapter:{self.name()}")

    def refuse_multimodal_if_needed(self, multimodal_context: Any | None) -> None:
        """Reject attachments for adapters that do not support multimodal input.

        Args:
            multimodal_context: Optional multimodal context from the worker
                launch path.

        Raises:
            CapabilityRefusal: When attachments are present and this adapter is
                not registered as multimodal-capable.
        """
        if multimodal_context is None:
            return

        inputs = getattr(multimodal_context, "inputs", ()) or ()
        attachments: list[str] = []
        for input_item in inputs:
            content_path = getattr(input_item, "content_path", None)
            if content_path is not None:
                attachments.append(str(content_path))
                continue
            description = getattr(input_item, "description", "") or "<inline attachment>"
            attachments.append(str(description))
        if not attachments:
            return

        from bernstein.core.agents.multimodal_attestation import refuse_when_incapable

        refuse_when_incapable(
            adapter_name=self._derive_session_namespace(),
            attachments=tuple(attachments),
        )

    def system_addendum_channel(self) -> Any:
        """Return this adapter's declared ``system_addendum`` delivery channel.

        Resolved from
        :data:`bernstein.adapters._contract.SYSTEM_ADDENDUM_CHANNEL_MATRIX` by
        registry namespace, the same way :meth:`strategy` resolves the other
        contract axes. An adapter with no declaration -- including a
        third-party one -- resolves to
        :attr:`~bernstein.adapters._contract.SystemAddendumChannel.IGNORED`, so
        the conservative assumption is that protocol instructions are dropped.

        Returns:
            The declared
            :class:`~bernstein.adapters._contract.SystemAddendumChannel`.
        """
        from bernstein.adapters._contract import system_addendum_channel

        return system_addendum_channel(self._derive_session_namespace())

    def report_system_addendum_delivery(
        self,
        *,
        system_addendum: str,
        workdir: Path | None,
        session_id: str,
    ) -> None:
        """Record how this spawn delivers ``system_addendum``, if at all.

        Called by the spawn preflight (:meth:`__init_subclass__`) before every
        adapter's ``spawn`` body runs, so no adapter can forget it. A non-empty
        addendum on an adapter declaring
        :attr:`~bernstein.adapters._contract.SystemAddendumChannel.IGNORED`
        logs a warning naming the consequence and writes the notice to the run
        record; a delivering adapter records the channel it used. An empty
        addendum records nothing - there is no delivery to report.

        Recording never fails a spawn: the notice is evidence, and a run that
        cannot write its evidence still has to start.

        Args:
            system_addendum: The protocol-critical text handed to ``spawn``.
            workdir: Agent working directory (root of ``.sdd``), or ``None``.
            session_id: The Bernstein session id this spawn runs under.
        """
        if not system_addendum or workdir is None:
            return
        try:
            from bernstein.adapters._contract import SystemAddendumChannel

            channel = self.system_addendum_channel()
            adapter = self._derive_session_namespace()
            digest = hashlib.sha256(system_addendum.encode("utf-8")).hexdigest()
            delivered = channel is not SystemAddendumChannel.IGNORED
            if not delivered:
                logger.warning(
                    "system_addendum not delivered: adapter %r declares no channel for it, so the "
                    "completion and heartbeat instructions (%d chars, sha256=%s) never reach session "
                    "%s. The agent can finish its work and never report done, and is then reaped as "
                    "stalled. Recorded in %s.",
                    adapter,
                    len(system_addendum),
                    digest[:12],
                    session_id or "<unknown>",
                    "/".join(CAPABILITY_NOTICES_RELPATH),
                )
            record = {
                "adapter": adapter,
                "capability": "system_addendum",
                "channel": str(channel),
                "delivered": delivered,
                "addendum_sha256": digest,
                "addendum_chars": len(system_addendum),
                "session_id": session_id,
            }
            path = capability_notices_path(workdir)
            key = (adapter, session_id, digest)
            if path.exists() and any(
                (
                    parsed.get("adapter"),
                    parsed.get("session_id"),
                    parsed.get("addendum_sha256"),
                )
                == key
                for parsed in _iter_capability_notices(path)
            ):
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        except Exception:
            logger.debug("system-addendum capability notice could not be recorded", exc_info=True)

    def set_resource_limits(self, limits: ResourceLimits | None) -> None:
        """Configure OS-level resource limits applied to spawned child processes.

        Must be called before :meth:`spawn`.  On POSIX, limits are enforced via
        ``resource.setrlimit`` in the child process ``preexec_fn``.  On other
        platforms the limits are recorded but not enforced.

        Args:
            limits: Resource limits to apply, or ``None`` to clear limits.
        """
        self._resource_limits = limits

    def _get_preexec_fn(self) -> Callable[[], None] | None:
        """Return a preexec_fn for subprocess.Popen based on configured limits.

        Returns:
            A zero-argument callable to pass as ``preexec_fn``, or ``None``
            when no limits are configured or the platform does not support it.
        """
        if self._resource_limits is None:
            return None
        return make_preexec_fn(self._resource_limits)

    @abstractmethod
    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        """Launch an agent process with the given prompt.

        Args:
            prompt: The task prompt for the agent.
            workdir: Working directory for the agent process.
            model_config: Model and effort configuration.
            session_id: Unique session identifier.
            mcp_config: Optional MCP server definitions.
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope ("small", "medium", "large") used by
                adapters that support per-task budget caps.
            budget_multiplier: Multiplier applied to the scope-based budget
                (e.g. 2.0 on retry after hitting the budget cap).
            system_addendum: Protocol-critical instructions (completion
                curl commands, heartbeat, signal-check) to inject via a
                system-prompt channel that survives prompt truncation.
                Adapters that support a separate system prompt (e.g. Claude
                Code's ``--append-system-prompt``) should use it; others
                may append to the user prompt as a fallback.
            multimodal_context: Optional
                :class:`bernstein.core.agents.multimodal.MultiModalContext`
                carrying base64-encoded attachments to be passed to the
                model API. Multimodal-capable adapters (Claude, Gemini)
                encode the attached bytes inline in the request body;
                other adapters MUST raise :class:`CapabilityRefusal`
                before any process is launched (see
                :func:`bernstein.core.agents.multimodal_attestation.refuse_when_incapable`).
        """
        ...

    def _start_timeout_watchdog(
        self,
        pid: int,
        timeout_seconds: int,
        session_id: str,
    ) -> threading.Timer:
        """Start a watchdog timer that kills the process on timeout.

        Sends SIGTERM first, waits 30s for graceful shutdown, then SIGKILL.

        Args:
            pid: Process ID to monitor.
            timeout_seconds: Seconds before triggering timeout.
            session_id: Session identifier for structured logging.

        Returns:
            The started Timer - caller should store it for cancellation.
            The extension path re-arms it via :meth:`extend_timeout` by
            cancelling and starting a fresh timer; a missed re-arm leaves the
            original timer in place, so the agent is never left unguarded
            (issue #4571).
        """

        def _kill_on_timeout() -> None:
            logger.warning(
                "Timeout after %ds: pid=%d session=%s - sending SIGTERM",
                timeout_seconds,
                pid,
                session_id,
            )
            if not kill_process_group(pid, signal.SIGTERM):
                return  # Already dead

            # Grace period for agent to commit partial work
            deadline = time.monotonic() + _SIGTERM_GRACE_SECONDS
            while time.monotonic() < deadline:
                if not process_alive(pid):
                    return  # Exited cleanly after SIGTERM
                time.sleep(1)

            logger.warning(
                "Agent did not exit after SIGTERM grace period: pid=%d session=%s - sending SIGKILL",
                pid,
                session_id,
            )
            kill_process_group(pid, signal.SIGKILL)

        timer = threading.Timer(timeout_seconds, _kill_on_timeout)
        timer.daemon = True
        timer.name = f"timeout-watchdog-{session_id}"
        timer.start()
        return timer

    def extend_timeout(
        self,
        timer: threading.Timer,
        pid: int,
        timeout_seconds: int,
        session_id: str,
    ) -> threading.Timer:
        """Re-arm an active watchdog with a new deadline (issue #4571).

        Cancels the current timer and starts a fresh one at ``timeout_seconds``
        from now. If the timer has already fired, cancelling is a no-op and the
        agent was already reaped; the caller should not extend a dead session.
        A caller that *never* reaches this method (e.g. the orchestrator
        stalled) leaves the original timer armed, which is the safe fallback:
        the agent is still killed at the old deadline, never left unguarded.

        Args:
            timer: The currently-armed watchdog timer.
            pid: Process ID the watchdog monitors.
            timeout_seconds: New deadline in seconds **from now** (a relative
                delay, not an absolute budget measured from spawn).
            session_id: Session identifier for structured logging.

        Returns:
            The newly-armed timer, which the caller should store back.
        """
        timer.cancel()
        return self._start_timeout_watchdog(pid, timeout_seconds, session_id)

    @staticmethod
    def _read_last_lines(log_path: Path, n: int = 10) -> list[str]:
        """Return the last *n* lines from ``log_path`` and its ``.stderr.log`` sibling.

        The Claude Code adapter pipes the upstream CLI's stdout through a
        wrapper that decodes stream-json into human-readable lines, but
        the wrapper drops anything that isn't valid NDJSON.  Rate-limit
        banners and startup errors from the CLI usually arrive on stderr
        (or as non-JSON stdout that the wrapper swallows), so they never
        reach ``log_path`` and the rate-limit probe returns ``False``
        even when the CLI clearly said "you've hit your limit".

        Reading both ``log_path`` and ``log_path.with_suffix(".stderr.log")``
        keeps the existing ``_is_rate_limit_error`` heuristic working for
        every adapter without changing call sites.  Adapters that don't
        write a separate stderr file are unaffected: the missing path is
        ignored.
        """
        lines: list[str] = []
        for candidate in (log_path, log_path.with_suffix(".stderr.log")):
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines.extend(text.splitlines())
        return lines[-n:] if lines else []

    @staticmethod
    def _is_rate_limit_error(lines: list[str]) -> bool:
        """Return True when log lines contain a provider rate-limit signal."""
        text = "\n".join(lines).lower()
        needles = (
            "rate limit",
            "usage limit",
            "quota exceeded",
            "too many requests",
            "429",
            "overloaded",
            "you've hit your limit",
            "hit your limit",
            "limit exceeded",
            "resets",  # "resets Apr 5 at 10pm" pattern from Claude Code
        )
        return any(needle in text for needle in needles)

    def _probe_fast_exit(
        self,
        proc: WaitableProcess,
        log_path: Path,
        *,
        provider_name: str,
        timeout_seconds: float = 8.0,
    ) -> None:
        """Treat early non-zero exits as spawn failures instead of live sessions.

        Args:
            proc: Subprocess-like object with ``wait(timeout=...)``.
            log_path: Runtime log path for tail inspection.
            provider_name: Human-readable provider/adapter label for errors.
            timeout_seconds: Probe window after spawn.

        Raises:
            RateLimitError: Provider immediately exited due to rate limiting.
            StandingCapError: Provider immediately exited due to a standing cap.
            SpawnError: Provider immediately exited for another reason.
        """
        try:
            exit_code = proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return
        except Exception as exc:
            logger.debug("Fast-exit probe failed for %s: %s", provider_name, exc)
            return

        if not isinstance(exit_code, int):
            logger.debug("Fast-exit probe for %s returned non-integer exit code %r; skipping", provider_name, exit_code)
            return

        if exit_code == 0:
            return

        tail_lines = self._read_last_lines(log_path, n=10)
        tail_text = tail_lines[-1] if tail_lines else "(no log output)"
        if self._is_rate_limit_error(tail_lines):
            # Distinguish a standing cap (won't clear this run) from a
            # transient request-rate limit before touching the meter.
            if classify_429(tail_text) is HTTP429Classification.STANDING:
                raise StandingCapError(f"{provider_name} hit a standing cap during startup: {tail_text}")
            # Tap the meter once before raising so the panel and the
            # ``rate_limit.hit`` event both see the spawn-time 429.
            try:
                self.record_rate_limit_hit(error_code=f"{provider_name}_fast_exit_429")
            except Exception as exc:
                logger.debug("rate-limit meter update failed for %s: %s", provider_name, exc)
            raise RateLimitError(f"{provider_name} rate-limited during startup: {tail_text}")
        raise SpawnError(f"{provider_name} exited early with code {exit_code}: {tail_text}")

    @staticmethod
    def cancel_timeout(result: SpawnResult) -> None:
        """Cancel the timeout watchdog for a completed process."""
        if result.timeout_timer is not None:
            result.timeout_timer.cancel()
            result.timeout_timer = None

    def is_alive(self, pid: int) -> bool:
        """Check if the agent process is still running."""
        return process_alive(pid)

    def kill(self, pid: int) -> ProcessReapReceipt:
        """Terminate the agent process and its entire process group.

        Processes are spawned with process-group isolation (POSIX
        ``start_new_session=True``), so the PID equals the PGID.  Using the
        PID directly avoids ``os.getpgid()`` failing when the wrapper
        process has already exited - this prevents orphan child processes
        from accumulating.  On Windows the same PID anchors a process-tree
        termination instead.

        Sends a graceful stop first, polls for exit for a short grace
        period, then escalates to a force-kill if the group is still alive.
        Without this escalation, agents that trap SIGTERM survive reap
        paths (wall-clock timeout and stale heartbeat) - see prior audit.

        Returns:
            A :class:`~bernstein.core.config.platform_compat.ProcessReapReceipt`
            describing how the reap was performed, so callers can mirror it
            into the audit chain.
        """
        return reap_process_group(pid)

    @abstractmethod
    def name(self) -> str:
        """Human-readable name of this CLI adapter."""
        ...

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect the current API tier and remaining quota.

        Returns:
            ApiTierInfo if tier detection is supported and successful, None otherwise.
            Subclasses should override this to return provider-specific tier info.
        """
        return None

    def is_rate_limited(self) -> bool:
        """Check if the provider is currently rate-limited.

        Subclasses should override this to probe the CLI for rate-limit
        signals before spawning.  Default returns False (no check).

        Returns:
            True if the provider is known to be rate-limited right now.
        """
        return False

    def cancel_tool_batch(self, _session_id: str, _batch_id: str) -> None:  # noqa: B027
        """Abort all pending tool calls in a batch.

        Optional: implemented by adapters that support concurrent tool execution.

        Args:
            _session_id: Agent session ID.
            _batch_id: The batch identifier to cancel.
        """

    def session_log_path_for(self, _session_id: str) -> Path | None:
        """Return the structured per-session log path, if any.

        Optional capability declared by :attr:`supports_session_log_watch`.
        Adapters whose upstream CLI writes a deterministic JSONL/text log
        per session override this method and return the absolute path
        Bernstein should watch. The default returns ``None``, meaning the
        ProgressWatch dispatch loop should skip this adapter and rely on
        plain process-exit detection.

        Args:
            _session_id: The Bernstein session id under which the agent
                was spawned. Adapters may translate this into the CLI's
                own session identifier as needed.

        Returns:
            Absolute :class:`~pathlib.Path` to the session log, or
            ``None`` when the adapter has no structured log to expose.
        """
        return None

    def resume(
        self,
        _session_id: str,
        _context: dict[str, Any],
    ) -> SpawnResult | None:
        """Reattach to a prior agent session for ``bernstein resume``.

        Optional capability declared in
        :mod:`bernstein.adapters._contract` (see
        ``RESUME_CAPABILITY_MATRIX``). Adapters that can stitch back into a
        provider-side session override this method and return a
        :class:`SpawnResult`. The default returns ``None`` to signal "I
        cannot resume natively - please fall back to a fresh spawn with
        scratchpad reinjection".

        Args:
            _session_id: The adapter session id captured in the
                checkpoint at the time the task was first spawned.
            _context: Adapter-opaque resume context. Typically contains
                ``{"prompt": str, "workdir": Path, "model_config": ...,
                "recovered_scratchpad": str}``. Adapters may consume any
                subset they understand.

        Returns:
            ``SpawnResult`` on a successful reattach, ``None`` to fall
            back to a fresh spawn.
        """
        return None

    #: Mutation-observability capability of this adapter (issue #2507).
    #: Declared-blind by default; adapters whose stream output surfaces
    #: provider-side context-mutation signals set
    #: :data:`MUTATION_OBSERVABILITY_OBSERVED` and override
    #: :meth:`observed_provider_mutations`. The declaration is recorded
    #: per run in the replay journal, so an absence of mutation entries
    #: is distinguishable from an inability to see them.
    provider_mutation_observability: str = MUTATION_OBSERVABILITY_DECLARED_BLIND

    def observed_provider_mutations(self, workdir: Path, session_id: str) -> list[dict[str, Any]]:
        """Return provider-side context-mutation signals observed for a session.

        Optional capability declared by
        :attr:`provider_mutation_observability`. Adapters that can parse
        mutation signals (compaction boundaries, context edits, stored-state
        references) out of their stream output override this method and
        return the signals in observation order, each as
        ``{"kind": str, "detail": dict}``. The orchestrator chains every
        returned signal into the run's replay journal as a content-addressed
        ``provider_state_mutation`` entry.

        The default returns an empty list: a declared-blind adapter has no
        observation surface, and that inability is itself recorded per run.

        Args:
            workdir: Agent working directory (root of ``.sdd``).
            session_id: The Bernstein session id the agent ran under.

        Returns:
            Observed mutation signals in stream order (empty when none were
            observed or the adapter cannot observe them).
        """
        del workdir, session_id
        return []

    def stream_signal_parser(self, line: str) -> object | None:
        """Map one line of adapter stdout to a canonical stream signal.

        The default implementation delegates to
        :func:`bernstein.core.protocols.stream_signals.parse_signal`,
        which recognises any line that follows the canonical
        ``BERNSTEIN:<KIND> [json]`` grammar.

        Adapters whose upstream CLI emits a different native protocol
        (Claude stream-json, Codex stream-json, etc.) override this
        method to translate their native event shape onto the canonical
        :class:`~bernstein.core.protocols.stream_signals.SignalKind`
        vocabulary, so the orchestrator can observe completion,
        question, plan-handoff, and blocked events through one
        interface regardless of upstream wire format.

        Args:
            line: One line of adapter stdout (newline-stripped or not).

        Returns:
            A
            :class:`~bernstein.core.protocols.stream_signals.StreamSignal`
            when the line carries a recognised signal, otherwise
            ``None``. The return type is declared as ``object`` so
            adapter subclasses are not forced to import the protocol
            module just to satisfy the signature.
        """
        from bernstein.core.protocols.stream_signals import parse_signal

        return parse_signal(line)

    def continuation_args(self, _session_id: str) -> list[str]:
        """Return CLI flags that re-enter the adapter's prior session.

        Adapters that opt into the retry-with-continuation path
        (``supports_session_continuation = True``) override this method
        and return the flag list that resumes the previous conversation
        without paying the full setup cost again. Typical
        implementations return ``["--resume", session_id]``,
        ``["--continue"]``, or an equivalent provider-specific switch.

        The default returns an empty list so adapters that have not
        opted in never accidentally feed corrupt arguments to the
        continuation spawn.

        Args:
            _session_id: The adapter session id from the prior launch.
        """
        return []

    #: Registry name of this adapter (for example ``"codex"``). Used to
    #: namespace the deterministic session id and to load the adapter's
    #: capability contract. Subclasses may override; when left blank the
    #: lower-cased :meth:`name` is used as a fallback.
    registry_name: str = ""

    #: Provider-identifier aliases this adapter answers to (for example
    #: ``("codex", "openai", "gpt")``). Consumed by
    #: :mod:`bernstein.adapters.registry` to build the
    #: provider-name -> adapter-name lookup table used by
    #: ``_infer_adapter_name_for_provider`` in ``spawner_core.py``. Empty by
    #: default: adapters that never need provider-string inference (most of
    #: the catalog) do not have to declare anything.
    provides: tuple[str, ...] = ()

    def _derive_session_namespace(self) -> str:
        """Return the namespace label used for deterministic session ids."""
        if self.registry_name:
            return self.registry_name
        return self.name().strip().lower() or type(self).__name__

    def strategy(self) -> Any:
        """Return this adapter's resolved :class:`AdapterStrategy`.

        Resolution order:

        1. An inline :attr:`strategy_override` set by the subclass, if any.
        2. The row in
           :data:`bernstein.adapters._contract.STRATEGY_MATRIX` keyed by the
           adapter's registry namespace (:meth:`_derive_session_namespace`,
           with a small alias table covering adapters whose ``name()`` does
           not match their registry key).
        3. The conservative
           :data:`bernstein.adapters._contract.DEFAULT_ADAPTER_STRATEGY`.

        The orchestrator dispatches off the returned enum fields (resume,
        dangerous-mode, event-channel) instead of branching on the adapter
        name. The return type is declared as ``object`` so subclasses are not
        forced to import the contract module just to read the attribute.

        Resolution stays inside :mod:`bernstein.adapters._contract` so this
        module never imports the registry: that would make every adapter
        transitively depend on every other adapter and break the
        ``adapters-independent`` import-linter contract.
        """
        from bernstein.adapters._contract import AdapterStrategy, strategy_for

        if isinstance(self.strategy_override, AdapterStrategy):
            return self.strategy_override
        return strategy_for(self._derive_session_namespace())

    def session_id_args(self, conversation_id: str) -> list[str]:
        """Return spawn-time argv for binding a deterministic session id.

        Derives a deterministic id from ``conversation_id`` (namespaced by
        this adapter) and pairs it with the CLI flag declared in the
        adapter's contract (``session_id_flag``). When the CLI exposes no
        such flag, the list is empty: callers should still record the
        derived id in orchestrator state for cross-reference, but pass no
        flag (see AC #3 of the deterministic-session-id binding).

        The derived id is stable across processes and runs, so a replay
        reaches the same conversation slot, and distinct adapters never
        collide because the adapter name is mixed into the namespace.

        Args:
            conversation_id: The orchestrator's conversation id.

        Returns:
            ``[flag, derived_id]`` when the contract declares a
            ``session_id_flag``, otherwise an empty list. Returns an empty
            list when no contract is on disk for this adapter.
        """
        from bernstein.adapters._contract import ContractSpec
        from bernstein.adapters.session_id import derive_session_id

        namespace = self._derive_session_namespace()
        try:
            spec = ContractSpec.load(namespace)
        except FileNotFoundError:
            return []
        if not spec.session_id_flag:
            return []
        derived = derive_session_id(conversation_id, namespace)
        return [spec.session_id_flag, str(derived)]


# ---------------------------------------------------------------------------
# Lineage spine write boundary (issue #2292)
# ---------------------------------------------------------------------------
#
# The spine is the single always-on Merkle+HMAC lineage store.
# ``record_artifact_write`` is the one write boundary for per-artifact
# provenance: in-process callers (the journal-head seal, schedule-fire
# records, the MCP tasks extension, checkpoint-retry) route through it.
# CLI-adapter runs spawn a subprocess (qwen, claude, ...) that writes files
# directly on disk without crossing this boundary, so those writes are not
# captured here; ``LineageSpine.verify`` reports a chain that carries only the
# journal seal as ``SEAL_ONLY`` rather than a clean pass (issue #2789).
# ``LineageSpine`` is bound at module scope so tests can patch it.

#: Env var that gates the spine write. ``BERNSTEIN_LINEAGE_ENABLED``
#: defaults to true and is a *hard* gate: when enabled, a failure to
#: record fails closed (raises) rather than silently dropping the entry.
#: Set to ``0`` / ``false`` / ``no`` / ``off`` to disable recording.
LINEAGE_ENABLED_ENV = "BERNSTEIN_LINEAGE_ENABLED"

#: W3C ``baggage`` member-key prefixes that mark a member as
#: orchestrator-controlled. Only members whose key carries one of these
#: prefixes survive into a sealed lineage entry; ambient members set by an
#: unrelated launching process (e.g. ``sentry-*`` from an OTEL/Sentry-
#: instrumented shell) are stripped before the value enters the entry hash or
#: HMAC body (issue #2787).
_LINEAGE_BAGGAGE_ALLOW_PREFIXES = ("bernstein-", "bernstein.")


def _filter_lineage_baggage(baggage: str | None) -> str | None:
    """Keep only orchestrator-controlled members of a W3C ``baggage`` value.

    ``baggage`` is a comma-separated list of ``key=value`` members (each value
    may carry ``;``-delimited properties). Ambient members inherited from an
    unrelated launching process must never be sealed into the lineage chain
    (issue #2787), so every member whose key is not orchestrator-controlled is
    dropped before the value reaches the entry hash and HMAC body.

    Returns:
        The filtered baggage string, or ``None`` when no member survives.
    """
    if not baggage:
        return None
    kept: list[str] = []
    for raw_member in baggage.split(","):
        member = raw_member.strip()
        if not member or "=" not in member:
            continue
        key = member.split("=", 1)[0].strip().lower()
        if key.startswith(_LINEAGE_BAGGAGE_ALLOW_PREFIXES):
            kept.append(member)
    return ",".join(kept) if kept else None


#: Process-wide sink for ``artifact.produced`` events (issue #2559). Left
#: ``None`` in a plain CLI run: the event is still journaled beside the spine,
#: so the fan-out stays replayable with no server attached. The task server
#: installs a publisher at startup to mirror events onto the SSE bus.
_ARTIFACT_EVENT_PUBLISHER: Callable[[ArtifactProductionEvent], None] | None = None


def set_artifact_event_publisher(publisher: Callable[[ArtifactProductionEvent], None] | None) -> None:
    """Install (or clear) the live sink for artifact production events.

    The publisher is best-effort and its exceptions are swallowed at the write
    boundary: a subscriber that cannot keep up must not fail the artifact write
    that fed it.
    """
    global _ARTIFACT_EVENT_PUBLISHER  # one process-wide sink, by design
    _ARTIFACT_EVENT_PUBLISHER = publisher


def _lineage_enabled() -> bool:
    """Return whether the lineage spine write boundary is active.

    Default is on; the flag flips off only when the env var is set to a
    recognisable falsey value. Anything else (including missing) keeps the
    boundary live so adapters cannot accidentally drop lineage by
    forgetting to set the variable.
    """
    raw = os.environ.get(LINEAGE_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def record_artifact_write(
    *,
    artifact_path: str,
    content: bytes,
    actor: str,
    step_id: str,
    model: str,
    lineage_root: Path,
    run_id: str,
    hmac_key: bytes,
    timestamp: int | None = None,
) -> str | None:
    """Record one artifact write into the run's lineage spine.

    The single write boundary for per-artifact provenance, used by the
    in-process callers listed in the module comment above. It appends exactly
    one Merkle-chained, HMAC-tagged entry per call. CLI-adapter subprocess
    file writes do not cross this boundary (issue #2789).

    Fail-closed gate: when ``BERNSTEIN_LINEAGE_ENABLED`` is truthy (the
    default), any failure inside the spine propagates -- provenance is a
    hard requirement, not best-effort. When the gate is disabled the
    call is a no-op returning ``None`` and the lineage root is never
    touched.

    Args:
        artifact_path: Repo-relative POSIX path of the artifact written.
        content: The bytes that landed on disk.
        actor: Producing agent / adapter identifier.
        step_id: Cross-link to the originating step / tool call.
        model: Model string recorded for provenance.
        lineage_root: ``.sdd/lineage`` root; per-run dirs live beneath it.
        run_id: Run identifier keying the spine.
        hmac_key: Audit-chain HMAC key used to tag entries.
        timestamp: Optional explicit timestamp; defaults to ``time_ns``.
            Passed explicitly by deterministic-replay callers.

    Returns:
        The entry hash on success, ``None`` when the gate is disabled.
    """
    if not _lineage_enabled():
        return None
    ts = timestamp if timestamp is not None else time.time_ns()

    # ``.lower()`` on the literal keeps the lowercase W3C spelling (OTEL SDKs
    # export lowercase ``traceparent``) without tripping the capitalised-env
    # lint rule.
    traceparent = os.environ.get("TRACEPARENT") or os.environ.get("traceparent".lower())
    tracestate = os.environ.get("TRACESTATE") or os.environ.get("tracestate".lower())
    raw_baggage = os.environ.get("BAGGAGE") or os.environ.get("baggage".lower())

    # Only orchestrator-controlled trace context is sealed into the chain
    # (issue #2787). Keep just the allowlisted baggage members; a surviving
    # bernstein-owned member is the signal that the ambient W3C context was set
    # by bernstein rather than inherited from an unrelated launching process,
    # so traceparent/tracestate are recorded only when such a member is present.
    # Otherwise all three are dropped before they enter the entry hash/HMAC body.
    baggage = _filter_lineage_baggage(raw_baggage)
    if baggage is None:
        traceparent = None
        tracestate = None

    entry = LineageSpine(lineage_root, run_id=run_id, hmac_key=hmac_key).record_entry(
        artifact_path=artifact_path,
        content=content,
        actor=actor,
        step_id=step_id,
        model=model,
        timestamp=ts,
        traceparent=traceparent,
        tracestate=tracestate,
        baggage=baggage,
    )

    # Issue #2559: exactly one production event per spine entry, emitted from
    # the same boundary the entry was written at, so there is no per-adapter
    # opt-in to forget and no path that records provenance without announcing
    # it. Fail-open by construction (see ``emit_production_event``): the entry
    # above is already durable, and the event set is re-derivable from it, so a
    # journal or bus failure must not undo a successful write.
    emit_production_event(
        lineage_root,
        run_id=run_id,
        entry=entry,
        publish=_ARTIFACT_EVENT_PUBLISHER,
    )
    return entry.entry_hash


def post_write_lineage_hook(
    *,
    artefact_path: str,
    new_content: bytes,
    agent_id: str,
    agent_card: AgentCard,
    private_key_pem: str,
    tool_call_id: str,
    span_id: str,
    lineage_root: Path,
    operator_hmac_key: bytes,
    artefact_kind: str = "file",
    run_id: str = "default",
) -> str | None:
    """Deprecated v1 shim -- routes writes through :func:`record_artifact_write`.

    Retained so existing importers keep a stable surface. The v1
    signature (Ed25519 agent card, JWS private key, artefact kind) is
    accepted but only the fields the spine records are forwarded: the
    spine is the single canonical store and no longer signs per-entry
    JWS. ``span_id`` maps onto the spine ``step_id`` and ``agent_id``
    onto ``actor``.

    Unlike v1 soft mode, the gate is now fail-closed via
    :func:`record_artifact_write` when lineage is enabled.

    Returns:
        The entry hash on success, ``None`` when the gate is disabled.
    """
    return record_artifact_write(
        artifact_path=artefact_path,
        content=new_content,
        actor=agent_id,
        step_id=tool_call_id or span_id,
        model=agent_card.kid,
        lineage_root=lineage_root,
        run_id=run_id,
        hmac_key=operator_hmac_key,
    )
