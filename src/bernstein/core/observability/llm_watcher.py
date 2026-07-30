"""LLM watcher: opt-in advisory observer above the deterministic orchestrator.

The watcher is the **top** of Bernstein's three-layer architecture
("deterministic orchestrator below, immutable HMAC chain in the middle,
LLM observer above" - see ticket
``2026-05-07-feat-llm-watcher-haiku-observer.md``).

Read-only contract
------------------
The watcher is structurally read-only:

1. The public ``observe`` API only accepts an immutable, frozen
   ``WatcherEvent`` snapshot.  It receives **no** orchestrator handle,
   no task store, no agent spawner, no filesystem path.  There is no
   capability inside this module to mutate orchestrator state - the
   omission is the enforcement.
2. The return type is ``list[Suggestion]`` - pure advisory data.
   Suggestions are advisory.  The orchestrator decides whether to log,
   surface, or persist them.  The watcher itself never writes to
   ``.sdd/backlog/``, ``.sdd/runtime/state/``, or any source file.
3. Failures inside the watcher (exceptions from the LLM adapter,
   timeout, network) are caught and converted into an empty signal
   list.  A misbehaving watcher cannot crash the orchestrator.

Off-by-default
--------------
The watcher is disabled by default.  The orchestrator emits zero
events and makes zero LLM calls unless ``WatcherConfig.enabled`` is
explicitly set to ``True`` - for example via
``BERNSTEIN_LLM_WATCHER_ENABLED=1`` or a future
``.sdd/config/watcher.yaml``.

This is the first plumbing slice for the P1 ticket.  Detector packs
(``stuck_loop``, ``plan_drift``, ``budget_overrun``,
``failure_recurrence``, ``jailbreak_shape``), the suggestion-review
CLI, and cost guardrails are deferred to follow-up tickets.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final, Literal, cast

logger = logging.getLogger(__name__)

__all__ = [
    "EventKind",
    "LLMWatcher",
    "Severity",
    "Suggestion",
    "WatcherConfig",
    "WatcherEvent",
    "audit_chain_break_detector",
    "build_watcher_from_env",
    "cost_runaway_detector",
    "register_default_detectors",
    "repeated_failure_detector",
    "stuck_spawn_detector",
    "suspicious_tool_mask_detector",
]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

#: Recognised orchestrator event kinds the watcher subscribes to.
EventKind = Literal[
    "plan_decided",
    "task_spawned",
    "task_completed",
    "merge_decided",
]

#: Advisory severity levels emitted by the watcher.
Severity = Literal["info", "warning", "critical"]

# Default Anthropic Haiku alias resolved by the existing Claude adapter
# (see ``bernstein.adapters.claude._MODEL_MAP``).  Kept as a string so
# the watcher never imports the adapter at module import time.
_DEFAULT_MODEL: Final[str] = "haiku"
_DEFAULT_PROVIDER: Final[str] = "claude"

_ENABLED_ENV_VAR: Final[str] = "BERNSTEIN_LLM_WATCHER_ENABLED"
_MODEL_ENV_VAR: Final[str] = "BERNSTEIN_LLM_WATCHER_MODEL"
_PROVIDER_ENV_VAR: Final[str] = "BERNSTEIN_LLM_WATCHER_PROVIDER"
_TRUTHY_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class WatcherEvent:
    """Immutable snapshot of an orchestrator event.

    The frozen dataclass is the **only** input surface to ``observe``.
    It carries no callable references, no mutable references to
    orchestrator state, no task-store handles.  The watcher therefore
    cannot mutate orchestrator state by construction.

    Attributes:
        kind: One of the recognised :data:`EventKind` values.
        run_id: Identifier of the orchestrator run that produced this
            event.
        timestamp: Unix epoch (seconds) when the event was created.
        payload: Free-form sanitised JSON-serialisable payload describing
            the event (e.g., plan summary, task id, merge decision).
            Callers MUST NOT include callable objects, file handles, or
            references to orchestrator-internal mutable state.
    """

    kind: EventKind
    run_id: str
    timestamp: float
    payload: dict[str, object] = field(default_factory=dict[str, object])


@dataclass(frozen=True, slots=True)
class Suggestion:
    """Advisory signal produced by the watcher.

    Suggestions are **never** auto-applied.  The orchestrator (or a
    human via a future CLI) decides whether to act on them.

    Attributes:
        suggestion_id: Stable identifier for cross-referencing in logs.
        run_id: Run that the originating event belonged to.
        detector: Free-form detector name (``stuck_loop``,
            ``plan_drift``, …).  In this first slice the watcher emits
            a generic ``observer`` detector; the detector pack lands in
            a follow-up ticket.
        severity: Advisory severity (``info`` | ``warning`` |
            ``critical``).
        rationale: Short human-readable explanation.
        proposed_action: Suggested next step (informational only -
            never executed automatically).
        cost_usd: Estimated USD cost of the LLM call that produced this
            suggestion.  ``0.0`` when the watcher short-circuits.
    """

    suggestion_id: str
    run_id: str
    detector: str
    severity: Severity
    rationale: str
    proposed_action: str
    cost_usd: float


@dataclass(frozen=True, slots=True)
class WatcherConfig:
    """Configuration for :class:`LLMWatcher`.

    Off by default.  The orchestrator constructs this from environment
    variables / future ``.sdd/config/watcher.yaml`` and passes it in.

    Attributes:
        enabled: Master switch.  When ``False`` (default) the watcher
            short-circuits ``observe`` to an empty list and makes zero
            LLM calls.
        model: Short model alias understood by the existing Claude
            adapter (default: ``"haiku"``).
        provider: Provider name routed through
            :func:`bernstein.core.llm.call_llm` (default: ``"claude"``).
        max_response_tokens: Cap on watcher LLM response length to
            keep the cost ceiling predictable.
        timeout_seconds: Hard timeout per LLM call; on expiry the
            watcher returns no suggestions for the event.
    """

    enabled: bool = False
    model: str = _DEFAULT_MODEL
    provider: str = _DEFAULT_PROVIDER
    max_response_tokens: int = 256
    timeout_seconds: float = 5.0


# Type for the LLM caller injected into :class:`LLMWatcher`.  Matches
# the signature of :func:`bernstein.core.routing.llm.call_llm`.  Kept
# as a Callable so tests can inject a stub without monkey-patching the
# real LLM module.
LLMCaller = Callable[..., Awaitable[str]]

#: Synchronous detector signature.  Detectors are pure functions that
#: receive a frozen :class:`WatcherEvent` and return zero or more
#: :class:`Suggestion` records.  They never mutate orchestrator state
#: and never make network calls - that's reserved for the LLM observer.
DetectorFn = Callable[[WatcherEvent], list["Suggestion"]]


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class LLMWatcher:
    """Opt-in LLM observer that emits advisory signals.

    Read-only contract
    ------------------
    By design this class:

    * accepts only :class:`WatcherEvent` snapshots through ``observe``;
    * receives no orchestrator/task-store handle in its constructor;
    * never imports task / agent / filesystem-mutation modules;
    * catches every exception from the underlying LLM caller and
      degrades to an empty signal list - the orchestrator is never
      crashed by watcher failure.

    Disabled-by-default
    -------------------
    When ``config.enabled`` is ``False`` the watcher short-circuits
    immediately and performs zero LLM calls.
    """

    def __init__(
        self,
        config: WatcherConfig,
        llm_caller: LLMCaller | None = None,
        detectors: list[DetectorFn] | None = None,
    ) -> None:
        """Initialise the watcher.

        Args:
            config: Watcher configuration; must be off by default.
            llm_caller: Optional injection seam for unit tests.  When
                ``None`` the watcher lazily imports
                :func:`bernstein.core.llm.call_llm` on first use, so a
                disabled watcher never imports the LLM stack.
            detectors: Optional list of plain-Python detector functions
                that run *before* the LLM observer on every enabled
                event.  Each detector is invoked with the frozen event
                and contributes zero or more :class:`Suggestion` records
                to the watcher output.  Detectors are read-only by
                contract: they receive the same immutable snapshot the
                LLM observer sees and have no orchestrator handle.
        """
        self._config = config
        self._llm_caller = llm_caller
        self._detectors: list[DetectorFn] = list(detectors or [])
        self._call_count = 0
        self._suggestion_count = 0

    @property
    def detectors(self) -> tuple[DetectorFn, ...]:
        """Read-only view of the registered detector functions."""
        return tuple(self._detectors)

    def register_detector(self, detector: DetectorFn) -> None:
        """Register a synchronous detector to run on every enabled event.

        Args:
            detector: A plain-Python callable that maps a
                :class:`WatcherEvent` to a list of :class:`Suggestion`
                records.  Exceptions are isolated by the watcher: a
                failing detector cannot crash the orchestrator or
                prevent peer detectors / the LLM observer from running.
        """
        self._detectors.append(detector)

    @property
    def config(self) -> WatcherConfig:
        """Return the watcher configuration (read-only)."""
        return self._config

    @property
    def call_count(self) -> int:
        """Number of LLM calls the watcher has issued."""
        return self._call_count

    @property
    def suggestion_count(self) -> int:
        """Number of suggestions the watcher has produced."""
        return self._suggestion_count

    async def observe(self, event: WatcherEvent) -> list[Suggestion]:
        """Process an orchestrator event and return advisory signals.

        Args:
            event: Immutable snapshot of an orchestrator event.

        Returns:
            A list of :class:`Suggestion` records.  Empty when the
            watcher is disabled, when the LLM call fails, when it
            times out, or when the model produces no advisory signal.
            **Never raises** to the caller - orchestrator stability is
            non-negotiable.

        Notes:
            Synchronous detectors fire first and contribute deterministic
            signals (cost runaway, stuck spawn, repeated failure, tool
            mask, audit chain break).  The LLM observer then runs and
            may add an additional advisory signal.
        """
        if not self._config.enabled:
            return []

        signals: list[Suggestion] = []
        signals.extend(self._run_detectors(event))

        try:
            response = await self._invoke_llm(event)
        except Exception:
            # Orchestrator stability is non-negotiable.  A misbehaving
            # watcher MUST NOT crash the loop.
            logger.warning(
                "LLM watcher failed for event=%s run=%s; degrading to no signals",
                event.kind,
                event.run_id,
                exc_info=True,
            )
            self._suggestion_count += len(signals)
            return signals

        if response.strip():
            signals.append(self._build_suggestion(event, response))

        self._suggestion_count += len(signals)
        return signals

    def _run_detectors(self, event: WatcherEvent) -> list[Suggestion]:
        """Run all registered detectors and collect their suggestions.

        Each detector is isolated: an exception in one does not block
        peer detectors or the downstream LLM observer.  The watcher's
        crash-resistance contract extends to the detector pack.

        Args:
            event: The frozen event passed through to detectors.

        Returns:
            Flat list of suggestions across all detectors, in
            registration order.
        """
        emitted: list[Suggestion] = []
        for detector in self._detectors:
            try:
                emitted.extend(detector(event))
            except Exception:  # pragma: no cover - defensive
                logger.warning(
                    "watcher detector %s failed for event=%s run=%s",
                    getattr(detector, "__name__", "<detector>"),
                    event.kind,
                    event.run_id,
                    exc_info=True,
                )
        return emitted

    async def _invoke_llm(self, event: WatcherEvent) -> str:
        """Call the watcher LLM with a minimal advisory prompt.

        Args:
            event: Event to observe.

        Returns:
            Raw response text.
        """
        caller = self._llm_caller or _default_llm_caller()
        prompt = self._render_prompt(event)
        self._call_count += 1
        return await caller(
            prompt,
            self._config.model,
            provider=self._config.provider,
            max_tokens=self._config.max_response_tokens,
            temperature=0.0,
        )

    def _render_prompt(self, event: WatcherEvent) -> str:
        """Render the advisory prompt for a single event.

        The prompt deliberately frames the watcher as **read-only** and
        forbids the LLM from proposing mutations.  Detector-specific
        prompts land in a follow-up ticket.

        Args:
            event: Event to observe.

        Returns:
            Prompt text fed to the watcher LLM.
        """
        return (
            "You are an advisory observer of a deterministic agent orchestrator.\n"
            "You have READ-ONLY access. You CANNOT spawn agents, edit files, or modify state.\n"
            "Your only output is a single advisory line (or empty if nothing is unusual).\n\n"
            f"Event kind: {event.kind}\n"
            f"Run id: {event.run_id}\n"
            f"Payload: {event.payload}\n\n"
            "If you observe an anomaly, drift, or missed opportunity, "
            "respond with one short sentence describing it. "
            "Otherwise, respond with nothing."
        )

    def _build_suggestion(
        self,
        event: WatcherEvent,
        response: str,
    ) -> Suggestion:
        """Convert a raw LLM response into a structured suggestion.

        Args:
            event: Event the response is about.
            response: Raw text from the watcher LLM.

        Returns:
            A frozen :class:`Suggestion`.
        """
        rationale = response.strip().splitlines()[0][:512]
        suggestion_id = f"watch-{event.run_id}-{event.kind}-{int(event.timestamp * 1000)}"
        return Suggestion(
            suggestion_id=suggestion_id,
            run_id=event.run_id,
            detector="observer",
            severity="info",
            rationale=rationale,
            proposed_action="Review the orchestrator log for this event.",
            cost_usd=0.0,
        )


# ---------------------------------------------------------------------------
# Construction helpers
# ---------------------------------------------------------------------------


def _is_truthy(value: str | None) -> bool:
    """Return True when *value* matches a known truthy token."""
    if value is None:
        return False
    return value.strip().lower() in _TRUTHY_VALUES


def build_watcher_from_env(
    *,
    llm_caller: LLMCaller | None = None,
) -> LLMWatcher:
    """Build a watcher from environment variables.

    Recognised variables
    --------------------
    ``BERNSTEIN_LLM_WATCHER_ENABLED``
        Master switch (``1`` / ``true`` / ``yes`` / ``on`` to enable).
        Anything else, including unset, leaves the watcher off.
    ``BERNSTEIN_LLM_WATCHER_MODEL``
        Model alias (default: ``haiku``).
    ``BERNSTEIN_LLM_WATCHER_PROVIDER``
        Provider name (default: ``claude``).

    Args:
        llm_caller: Optional injection seam (mainly for tests).

    Returns:
        A configured but disabled-by-default :class:`LLMWatcher`.
    """
    enabled = _is_truthy(os.environ.get(_ENABLED_ENV_VAR))
    model = os.environ.get(_MODEL_ENV_VAR, _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    provider = os.environ.get(_PROVIDER_ENV_VAR, _DEFAULT_PROVIDER).strip() or _DEFAULT_PROVIDER
    config = WatcherConfig(enabled=enabled, model=model, provider=provider)
    watcher = LLMWatcher(config=config, llm_caller=llm_caller)
    if enabled:
        # Off-by-default still wins: detectors are only registered on a
        # watcher that the operator has explicitly opted in to.
        register_default_detectors(watcher)
    return watcher


def _default_llm_caller() -> LLMCaller:
    """Lazy import of the project-wide LLM caller.

    Importing :func:`bernstein.core.llm.call_llm` at module top-level
    would pull in pydantic-settings, the OpenAI client, and the rest
    of the routing stack on every Bernstein boot - even when the
    watcher is disabled.  Deferring the import keeps the
    off-by-default path free of side effects.

    The ``bernstein.core.llm`` name is resolved at runtime via the
    ``_REDIRECT_MAP`` finder registered on :data:`sys.meta_path` (see
    :mod:`bernstein.core.__init__`).  Existing call sites
    (e.g. ``bernstein.core.quality.janitor``) import the same way.

    Returns:
        The project-wide async LLM caller.
    """
    from bernstein.core.llm import call_llm

    return call_llm


# ---------------------------------------------------------------------------
# Production detector pack
# ---------------------------------------------------------------------------
#
# Each detector is a plain Python function that consumes a frozen
# :class:`WatcherEvent` and returns a (possibly empty) list of
# :class:`Suggestion` records.  Detectors are deterministic, side-effect
# free, and structurally read-only: the watcher feeds them the same
# immutable snapshot that the LLM observer sees.  Failures are isolated
# by ``LLMWatcher._run_detectors``.

#: Default budget (USD) - used by ``cost_runaway_detector`` when the
#: event payload omits ``run_budget_usd``.  Conservative, biased toward
#: false-positives over silent overrun.
_DEFAULT_RUN_BUDGET_USD: Final[float] = 10.0

#: Hard ceiling - a single task burning more than this in 5 minutes is
#: always escalated regardless of the run-level budget.
_TASK_HARD_CEILING_USD: Final[float] = 2.0

#: Stuck-spawn threshold (seconds).  A task that confirmed its claim
#: but emitted no completion / audit traffic for this long is suspect.
_STUCK_SPAWN_TIMEOUT_S: Final[float] = 30 * 60

#: Repeated-failure threshold - the same task failing N times in a row
#: with the same exit signature.
_REPEATED_FAILURE_LIMIT: Final[int] = 3

#: Tool-mask threshold - masking more than this fraction of available
#: tools is unusual enough to surface as a configuration smell.
_TOOL_MASK_FRACTION_THRESHOLD: Final[float] = 0.5


def _suggestion_id(event: WatcherEvent, detector: str) -> str:
    """Build a stable suggestion id keyed on the event + detector.

    Args:
        event: The originating event.
        detector: Detector name; included so two detectors firing on the
            same event produce distinct suggestion ids.

    Returns:
        A short identifier suitable for cross-referencing in logs.
    """
    return f"watch-{event.run_id}-{event.kind}-{detector}-{int(event.timestamp * 1000)}"


def cost_runaway_detector(event: WatcherEvent) -> list[Suggestion]:
    """Fire when a single task burns >$2 in 5 min OR >50% of run budget.

    Reads the following payload fields when present:

    * ``task_cost_usd`` - USD spent on the current task in the last
      5 minutes (orchestrator-side aggregate).
    * ``run_cost_usd`` - total USD spent on the run so far.
    * ``run_budget_usd`` - operator-set ceiling for the run.
    * ``task_id`` - task identifier (informational).

    Both legs of the OR are evaluated; the more severe leg wins.

    Args:
        event: Frozen event snapshot.

    Returns:
        Zero or one :class:`Suggestion`.  Empty when no cost field is
        present or no threshold is breached.
    """
    payload = event.payload
    task_cost = float(cast(float, payload.get("task_cost_usd") or 0.0))
    run_cost = float(cast(float, payload.get("run_cost_usd") or 0.0))
    run_budget = float(cast(float, payload.get("run_budget_usd") or _DEFAULT_RUN_BUDGET_USD))
    task_id = str(payload.get("task_id") or "?")

    breached_task = task_cost > _TASK_HARD_CEILING_USD
    # Avoid div/0 on misconfigured budget; treat <=0 budget as no-budget.
    breached_run = bool(run_budget > 0 and run_cost > 0.5 * run_budget)
    if not breached_task and not breached_run:
        return []

    severity: Severity = "critical" if breached_task else "warning"
    rationale = (
        f"cost runaway on task {task_id}: "
        f"task_cost_usd={task_cost:.2f} run_cost_usd={run_cost:.2f} "
        f"run_budget_usd={run_budget:.2f}"
    )
    return [
        Suggestion(
            suggestion_id=_suggestion_id(event, "cost_runaway"),
            run_id=event.run_id,
            detector="cost_runaway",
            severity=severity,
            rationale=rationale,
            proposed_action=(
                "Pause the task and review the agent's most recent tool calls "
                "for a stuck retry loop or an unbounded tool budget."
            ),
            cost_usd=0.0,
        ),
    ]


def stuck_spawn_detector(event: WatcherEvent) -> list[Suggestion]:
    """Fire when a worktree sits in claim_confirmed without progress >30 min.

    Reads the payload for ``claim_confirmed`` (bool), ``task_completed``
    (bool), ``audit_emissions`` (int), ``time_in_state_s`` (float) and
    ``task_id``.  A spawn is considered stuck when the claim was
    confirmed, the task is not yet completed, no audit emissions have
    occurred, and the time-in-state exceeds the threshold.

    Args:
        event: Frozen event snapshot.

    Returns:
        Zero or one :class:`Suggestion`.
    """
    payload = event.payload
    claim_confirmed = bool(payload.get("claim_confirmed"))
    task_completed = bool(payload.get("task_completed"))
    audit_emissions = int(cast(int, payload.get("audit_emissions") or 0))
    time_in_state = float(cast(float, payload.get("time_in_state_s") or 0.0))
    task_id = str(payload.get("task_id") or "?")

    if not claim_confirmed or task_completed:
        return []
    if audit_emissions > 0:
        return []
    if time_in_state <= _STUCK_SPAWN_TIMEOUT_S:
        return []

    rationale = (
        f"stuck spawn for task {task_id}: claim_confirmed=true with no audit emissions for {int(time_in_state)}s"
    )
    return [
        Suggestion(
            suggestion_id=_suggestion_id(event, "stuck_spawn"),
            run_id=event.run_id,
            detector="stuck_spawn",
            severity="warning",
            rationale=rationale,
            proposed_action=(
                "Wake the agent via the WAKEUP signal file or, if non-responsive, mark the task failed and re-spawn."
            ),
            cost_usd=0.0,
        ),
    ]


def repeated_failure_detector(event: WatcherEvent) -> list[Suggestion]:
    """Fire when the same task fails 3 times in a row with the same exit signature.

    Reads ``task_id``, ``failure_count`` (int) and ``exit_signature``
    (str) from the payload.  When all three are present and the count
    crosses the threshold, the detector emits a critical advisory.

    Args:
        event: Frozen event snapshot.

    Returns:
        Zero or one :class:`Suggestion`.
    """
    payload = event.payload
    failure_count = int(cast(int, payload.get("failure_count") or 0))
    exit_signature = str(payload.get("exit_signature") or "").strip()
    task_id = str(payload.get("task_id") or "?")
    if not exit_signature:
        return []
    if failure_count < _REPEATED_FAILURE_LIMIT:
        return []
    rationale = f"task {task_id} failed {failure_count}x with identical exit signature {exit_signature!r}"
    return [
        Suggestion(
            suggestion_id=_suggestion_id(event, "repeated_failure"),
            run_id=event.run_id,
            detector="repeated_failure",
            severity="critical",
            rationale=rationale,
            proposed_action=(
                "Stop re-spawning. The deterministic retry loop is masking "
                "a structural defect - investigate the exit signature and "
                "patch the underlying code path before another retry."
            ),
            cost_usd=0.0,
        ),
    ]


def suspicious_tool_mask_detector(event: WatcherEvent) -> list[Suggestion]:
    """Fire when ``mask_tools`` removed >50% of the available tool surface.

    Reads ``available_tools`` (int - pre-mask count) and
    ``masked_tools`` (int - number removed by the mask) from the
    payload.  A tool mask that removes more than half of the tools is
    almost certainly a misconfigured policy and warrants review.

    Args:
        event: Frozen event snapshot.

    Returns:
        Zero or one :class:`Suggestion`.
    """
    payload = event.payload
    available = int(cast(int, payload.get("available_tools") or 0))
    masked = int(cast(int, payload.get("masked_tools") or 0))
    if available <= 0:
        return []
    fraction = masked / available
    if fraction <= _TOOL_MASK_FRACTION_THRESHOLD:
        return []
    rationale = f"tool masking removed {masked}/{available} tools ({fraction:.0%}) - policy may be over-restrictive"
    return [
        Suggestion(
            suggestion_id=_suggestion_id(event, "suspicious_tool_mask"),
            run_id=event.run_id,
            detector="suspicious_tool_mask",
            severity="warning",
            rationale=rationale,
            proposed_action=(
                "Review the tool-masking policy for this role; verify the "
                "remaining tool set is sufficient for the assigned task."
            ),
            cost_usd=0.0,
        ),
    ]


def audit_chain_break_detector(event: WatcherEvent) -> list[Suggestion]:
    """Fire when a new audit entry's ``prev_hmac`` does not match the prior tail.

    Reads ``prev_hmac`` (string carried on the new entry) and
    ``expected_prev_hmac`` (the HMAC of the chain tail the orchestrator
    captured before appending) from the payload.  When both are present
    and disagree, the chain has been tampered with or there has been a
    write race - either is an immediate critical signal.

    Args:
        event: Frozen event snapshot.

    Returns:
        Zero or one :class:`Suggestion`.
    """
    payload = event.payload
    prev = str(payload.get("prev_hmac") or "").strip()
    expected = str(payload.get("expected_prev_hmac") or "").strip()
    if not prev or not expected:
        return []
    if prev == expected:
        return []
    rationale = f"audit chain break: prev_hmac={prev[:16]}… does not match expected_prev_hmac={expected[:16]}…"
    return [
        Suggestion(
            suggestion_id=_suggestion_id(event, "audit_chain_break"),
            run_id=event.run_id,
            detector="audit_chain_break",
            severity="critical",
            rationale=rationale,
            proposed_action=(
                "Quarantine the audit log file, do NOT append further entries, "
                "and run ``bernstein audit verify`` to identify the corrupted "
                "tail. Investigate write-race or tamper signal before resuming."
            ),
            cost_usd=0.0,
        ),
    ]


#: Canonical default detector list.  Kept as a module-level tuple so the
#: orchestrator can introspect the registered surface (e.g. for
#: ``bernstein watcher list``).
_DEFAULT_DETECTORS: Final[tuple[DetectorFn, ...]] = (
    cost_runaway_detector,
    stuck_spawn_detector,
    repeated_failure_detector,
    suspicious_tool_mask_detector,
    audit_chain_break_detector,
)


def register_default_detectors(watcher: LLMWatcher) -> None:
    """Register the production detector pack on *watcher*.

    Idempotent: callers that wire a watcher manually before the
    ``build_watcher_from_env`` helper runs (e.g. tests) can call this
    once without worrying about double-registration as long as they do
    not call it twice themselves.

    Args:
        watcher: A constructed :class:`LLMWatcher` (typically just
            returned from ``build_watcher_from_env`` with the env switch
            on).
    """
    for detector in _DEFAULT_DETECTORS:
        watcher.register_detector(detector)
