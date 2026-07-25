"""Commit-completion verification for the agent dispatch loop.

A common failure mode of CLI coding agents is exiting with success while
leaving the workspace untouched: the assistant claims it finished, but no
new commit landed. The agent's self-report cannot be trusted as a
completion signal - the workspace is the ground truth.

This module snapshots the workspace HEAD before launching an adapter and
compares it after the process exits. If the agent declared success
(exit code 0) but HEAD has not moved, the orchestrator launches a single
retry through the adapter's session-continuation primitive with a
corrective nudge. The recursion is capped at exactly one retry.

The check applies only to runs whose declared ``output_mode`` is
``git_diff`` (see :class:`bernstein.adapters._contract.OutputMode`). An
``artifact``-mode run completes on a signed lineage receipt instead of a
commit, so an unmoved HEAD is its contract rather than an anomaly and the
retry path is declined outright (issue #2608).

The check is intentionally adapter-agnostic: it only depends on the
adapter exposing two capabilities -- ``supports_session_continuation`` and
``continuation_args(session_id)`` -- both declared on
:class:`bernstein.adapters.base.CLIAdapter`. Adapters that do not support
a session-resume primitive are skipped: a fresh spawn with full prompt
reinjection would defeat the cost saving that motivates the retry in the
first place.

Public surface
--------------

* :class:`CommitCompletionCheck` -- stateless verifier with
  ``snapshot_before`` and ``verify_after`` methods.
* :data:`DEFAULT_CONTINUATION_NUDGE` -- the corrective prompt appended on
  retry.
* :data:`RETRY_LIMIT` -- the hard cap on continuation retries
  (``1`` by v1; the cap is non-configurable on purpose).
* :func:`maybe_retry_continuation` -- convenience helper that ties the
  snapshot, verification, retry decision, and lifecycle event together.

See ``docs/concepts/orchestrator-hardening.md`` for the operator-facing
overview.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from bernstein.adapters._contract import OutputMode
from bernstein.core.git.git_basic import rev_parse_head

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.adapters.base import CLIAdapter, SpawnResult


class ContinuationSpawnFn(Protocol):
    """Callable contract for the retry-spawn closure.

    The dispatch loop hands :func:`maybe_retry_continuation` a closure
    that knows how to forward into its own spawn machinery (worktree
    routing, container hand-off, lifecycle book-keeping). The protocol
    documents the two arguments the helper actually passes and keeps
    the return type narrow enough for mypy without leaking adapter
    internals.
    """

    def __call__(
        self,
        prompt: str,
        continuation_args: list[str],
    ) -> SpawnResult | None: ...


class RetryLifecycleEmitter(Protocol):
    """Callback contract for emitting ``agent.retry_continuation``.

    Wired by the orchestrator at startup (mirrors the
    :func:`bernstein.core.lifecycle.hooks.bind_rate_limit_emit`
    pattern). The default no-op keeps the module importable from
    contexts where the lifecycle registry is not configured (tests,
    CLI scripts, doc generators).
    """

    def __call__(self, payload: dict[str, object]) -> None: ...


def _noop_emitter(_payload: dict[str, object]) -> None:
    """Default emitter -- swallows the event so unit imports stay free of side effects."""


_lifecycle_emitter: RetryLifecycleEmitter = _noop_emitter


def set_retry_lifecycle_emitter(emitter: RetryLifecycleEmitter | None) -> None:
    """Install the callback that fires ``agent.retry_continuation``.

    Pass ``None`` to restore the no-op default. The orchestrator calls
    this once at startup with a closure that runs the lifecycle
    registry for :attr:`LifecycleEvent.AGENT_RETRY_CONTINUATION`.
    """
    global _lifecycle_emitter
    _lifecycle_emitter = emitter or _noop_emitter


logger = logging.getLogger(__name__)

#: Hard cap on continuation retries triggered by a missing commit. The
#: cap is one by design: a second retry would compound a flaky run and
#: blow past the "half a full run" cost budget that justifies the
#: retry path at all.
RETRY_LIMIT: int = 1

#: Default corrective nudge appended to the continuation prompt. Kept
#: short on purpose: the adapter already carries the original prompt
#: through its native session, so we only need to surface the
#: discrepancy.
DEFAULT_CONTINUATION_NUDGE: str = (
    "You exited successfully but the workspace has no new commit. "
    "Either commit your work or explain in plain prose why no commit "
    "was needed for this task."
)


class _ContinuationCapableAdapter(Protocol):
    """Structural type for adapters wired into the retry path.

    Adapters that opt in declare ``supports_session_continuation = True``
    on the class and implement :meth:`continuation_args` returning the
    CLI flags the spawn machinery should append for a continuation
    launch (typically something like ``["--resume", session_id]`` or
    ``["--continue"]``).

    The protocol is internal documentation only - the orchestrator
    consumes :class:`bernstein.adapters.base.CLIAdapter` directly.
    """

    supports_session_continuation: bool

    def continuation_args(self, session_id: str) -> list[str]:
        """Return adapter-specific CLI flags for a continuation launch."""
        ...


@dataclass(frozen=True, slots=True)
class CompletionVerdict:
    """Result of comparing the pre-spawn and post-exit HEAD snapshots.

    Attributes:
        committed: ``True`` if HEAD moved between snapshot and verify,
            or in the special "no baseline" case where one of the
            snapshots could not be read (see ``reason="head_unknown"``
            below). The "no baseline" case sets ``committed=True``
            defensively so the retry path stays off: we cannot make a
            confident statement about commit movement when the
            baseline is missing.
        before: HEAD SHA captured by :meth:`CommitCompletionCheck.snapshot_before`.
        after: HEAD SHA captured by :meth:`CommitCompletionCheck.verify_after`.
        reason: Short, operator-facing explanation. One of:
            * ``""`` -- normal commit landed (``committed=True``).
            * ``"head_did_not_move"`` -- agent exited but HEAD is
              unchanged (``committed=False``); this is the case that
              triggers the retry.
            * ``"head_unknown"`` -- one or both snapshots could not be
              read; ``committed=True`` defensively so no retry fires.
    """

    committed: bool
    before: str
    after: str
    reason: str = ""

    @property
    def needs_retry(self) -> bool:
        """``True`` when the agent declared success but HEAD did not move."""
        return not self.committed


class CommitCompletionCheck:
    """Stateless verifier that turns workspace HEAD into the completion signal.

    Usage::

        check = CommitCompletionCheck()
        before = check.snapshot_before(worktree)
        # ... spawn the adapter, wait for exit ...
        verdict = check.verify_after(worktree, before=before)
        if verdict.needs_retry:
            ...  # see ``maybe_retry_continuation``

    The class is intentionally stateless so it can be reused across
    sessions without re-instantiation. ``snapshot_before`` and
    ``verify_after`` are pure git reads and never mutate the worktree.
    """

    def snapshot_before(self, workdir: Path) -> str:
        """Return the SHA at HEAD inside ``workdir`` prior to spawning.

        Errors are caught and reported as the empty string so a missing
        HEAD (uninitialised repo, detached worktree, permission glitch)
        never blocks a spawn. The matching :meth:`verify_after` call
        also returns the empty string in that case and the verdict
        defaults to "no retry" -- we cannot make a confident statement
        about commit movement when the baseline is unknown.
        """
        return _safe_rev_parse(workdir)

    def verify_after(self, workdir: Path, *, before: str) -> CompletionVerdict:
        """Compare current HEAD against the pre-spawn snapshot.

        Args:
            workdir: Worktree path the adapter was launched into.
            before: SHA returned by :meth:`snapshot_before`.

        Returns:
            A :class:`CompletionVerdict`. ``needs_retry`` is ``True``
            only when both snapshots are valid (non-empty) *and* the
            second SHA equals the first.
        """
        after = _safe_rev_parse(workdir)
        if not before or not after:
            return CompletionVerdict(
                committed=True,  # unknown -> treat as committed, don't retry
                before=before,
                after=after,
                reason="head_unknown" if not before or not after else "",
            )
        if before == after:
            return CompletionVerdict(
                committed=False,
                before=before,
                after=after,
                reason="head_did_not_move",
            )
        return CompletionVerdict(committed=True, before=before, after=after)


def _safe_rev_parse(workdir: Path) -> str:
    """Best-effort HEAD read; never raises.

    Snapshot/verify must not abort an agent spawn. We log at debug
    level and return an empty string when git is unavailable, the
    worktree is missing, or the repo has no commits yet.
    """
    try:
        return rev_parse_head(workdir)
    except Exception as exc:  # pragma: no cover -- defensive
        logger.debug("commit-completion HEAD probe failed in %s: %s", workdir, exc)
        return ""


def _resolved_output_mode(adapter: CLIAdapter, override: OutputMode | None) -> OutputMode:
    """Return the effective output mode for a run.

    An explicit ``override`` (the task's declared artifact contract) wins over
    the adapter's declared axis, so one adapter can drive both a coding task
    and a report task. Absent both, the conservative default is ``git_diff``:
    the coding path stays the default and an undeclared adapter never silently
    skips the commit check.
    """
    if override is not None:
        return override
    try:
        return adapter.strategy().output_mode
    except Exception:  # pragma: no cover -- defensive: a stub adapter in tests
        return OutputMode.GIT_DIFF


def adapter_supports_continuation(adapter: CLIAdapter) -> bool:
    """Return ``True`` if the adapter advertises session continuation.

    Read off the class attribute :attr:`CLIAdapter.supports_session_continuation`.
    Adapters that have not opted in default to ``False`` -- the retry
    path is skipped and the agent's "success-without-commit" exit
    surfaces to the normal failure-handling path.
    """
    return bool(getattr(adapter, "supports_session_continuation", False))


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """Outcome of the retry-decision step.

    Attributes:
        should_retry: ``True`` when a continuation spawn is warranted.
        reason: Short tag for tracing. One of ``"committed"``,
            ``"head_unknown"``, ``"adapter_unsupported"``,
            ``"non_zero_exit"``, ``"retry_cap_reached"``,
            ``"artifact_output_mode"``, or ``"needs_retry"``.
    """

    should_retry: bool
    reason: str


def decide_retry(
    *,
    adapter: CLIAdapter,
    verdict: CompletionVerdict,
    exit_code: int,
    attempts: int,
    output_mode: OutputMode | None = None,
) -> RetryDecision:
    """Decide whether to launch a continuation retry.

    Inputs are pure values; the decision is auditable and trivially
    unit-tested. Used by :func:`maybe_retry_continuation` and exposed
    so adapter-specific callers can compose their own retry flow.

    Args:
        adapter: The adapter that just exited.
        verdict: Result of :meth:`CommitCompletionCheck.verify_after`.
        exit_code: Adapter process exit code. Only ``0`` is eligible
            for retry -- non-zero exits are handled by the normal
            failure path.
        attempts: Number of continuation retries already performed for
            this session. Must be ``0`` on the first call.
        output_mode: The run's declared output mode. ``artifact``
            completes on a signed lineage receipt rather than a commit
            (issue #2608), so an unmoved HEAD is the expected outcome
            and must never trigger the "you forgot to commit" nudge.
            Defaults to the adapter's declared axis when omitted.

    Returns:
        A :class:`RetryDecision` documenting the call.
    """
    if exit_code != 0:
        return RetryDecision(should_retry=False, reason="non_zero_exit")
    # An artifact-mode run has no commit to check: HEAD not moving is the
    # contract, not a failure. Decided before the verdict is consulted so a
    # non-coding task can never be nudged to commit work that has none.
    if _resolved_output_mode(adapter, output_mode) is not OutputMode.GIT_DIFF:
        return RetryDecision(should_retry=False, reason="artifact_output_mode")
    # Surface the "no baseline" case before the committed shortcut: an
    # unknown HEAD reports ``committed=True`` defensively, but the
    # trace-level reason "head_unknown" is the more useful tag.
    if verdict.reason == "head_unknown":
        return RetryDecision(should_retry=False, reason="head_unknown")
    if verdict.committed:
        return RetryDecision(should_retry=False, reason="committed")
    if not adapter_supports_continuation(adapter):
        return RetryDecision(should_retry=False, reason="adapter_unsupported")
    if attempts >= RETRY_LIMIT:
        return RetryDecision(should_retry=False, reason="retry_cap_reached")
    return RetryDecision(should_retry=True, reason="needs_retry")


def build_continuation_prompt(
    *,
    original_prompt: str,
    nudge: str = DEFAULT_CONTINUATION_NUDGE,
) -> str:
    """Compose the continuation prompt.

    The nudge is appended after a blank line so it reads as a follow-up
    user turn inside the adapter's session view. The original prompt
    is preserved verbatim -- leading/trailing whitespace and final
    newlines stay intact so transcript replay matches the original
    spawn byte-for-byte.
    """
    if not nudge:
        return original_prompt
    if not original_prompt:
        return nudge
    return f"{original_prompt}\n\n{nudge}"


def maybe_retry_continuation(
    *,
    adapter: CLIAdapter,
    workdir: Path,
    before: str,
    session_id: str,
    exit_code: int,
    original_prompt: str,
    attempts: int = 0,
    check: CommitCompletionCheck | None = None,
    nudge: str = DEFAULT_CONTINUATION_NUDGE,
    spawn_fn: ContinuationSpawnFn | None = None,
    output_mode: OutputMode | None = None,
) -> tuple[RetryDecision, CompletionVerdict, SpawnResult | None]:
    """Convenience wrapper around verify + decide + (optional) spawn.

    Designed for the agent dispatch loop. Callers that already have a
    spawn callable hand it in as ``spawn_fn``; the helper invokes it
    with the continuation prompt and the adapter's continuation args.
    Pass ``spawn_fn=None`` to get the decision without actually
    re-launching.

    Args:
        adapter: The adapter that just exited.
        workdir: Worktree the adapter was launched into.
        before: HEAD SHA captured by :meth:`snapshot_before`.
        session_id: Adapter session id, fed to ``continuation_args``.
        exit_code: Adapter process exit code.
        original_prompt: The prompt rendered for the first attempt.
        attempts: Existing retry count for this session.
        check: Optional pre-built verifier; defaults to a fresh
            :class:`CommitCompletionCheck`.
        nudge: Override the corrective message tail.
        spawn_fn: Optional callable
            ``spawn_fn(prompt: str, continuation_args: list[str]) -> SpawnResult``.
            When ``None`` the helper does not spawn; callers wiring
            into a heavier dispatch layer should pass a closure that
            forwards into their own spawn machinery.
        output_mode: The run's declared output mode; forwarded to
            :func:`decide_retry`. Pass ``artifact`` for a task that
            completes on a signed lineage receipt instead of a commit.

    Returns:
        A 3-tuple of ``(decision, verdict, retry_result)``. The third
        element is ``None`` when no retry was attempted or
        ``spawn_fn`` was omitted.
    """
    check = check or CommitCompletionCheck()
    verdict = check.verify_after(workdir, before=before)
    decision = decide_retry(
        adapter=adapter,
        verdict=verdict,
        exit_code=exit_code,
        attempts=attempts,
        output_mode=output_mode,
    )
    if not decision.should_retry or spawn_fn is None:
        return decision, verdict, None

    continuation_args = adapter.continuation_args(session_id)
    prompt = build_continuation_prompt(original_prompt=original_prompt, nudge=nudge)
    attempt = attempts + 1
    logger.info(
        "retry-with-continuation: session=%s reason=%s attempt=%d args=%s",
        session_id,
        decision.reason,
        attempt,
        continuation_args,
    )
    # Fire ``agent.retry_continuation`` so downstream consumers
    # (metrics, dashboards, audit log) can observe the retry. The
    # emitter is a module-level callback installed by the orchestrator
    # at startup; the unit-test default is a no-op so this call does
    # not require a configured registry to be safe.
    try:
        _lifecycle_emitter(
            {
                "session_id": session_id,
                "reason": decision.reason,
                "attempt": attempt,
            }
        )
    except Exception as exc:  # pragma: no cover -- emitter must never mask the spawn
        logger.debug("retry-continuation lifecycle emitter failed: %s", exc)
    retry_result = spawn_fn(prompt, continuation_args)
    return decision, verdict, retry_result


__all__ = [
    "DEFAULT_CONTINUATION_NUDGE",
    "RETRY_LIMIT",
    "CommitCompletionCheck",
    "CompletionVerdict",
    "ContinuationSpawnFn",
    "RetryDecision",
    "adapter_supports_continuation",
    "build_continuation_prompt",
    "decide_retry",
    "maybe_retry_continuation",
]
