"""Browser worker: site checks and UI flows as replayable activities (#2523).

The typed activity boundary already ships a ``BROWSER`` modality and a
:class:`~bernstein.core.orchestration.activity_modalities.BrowserActivity` that
content-addresses an observation per decision step, but nothing drove a browser:
the caller had to arrive already holding snapshot bytes. This module is the
worker. It runs a site check or UI flow end to end -- allocate an isolated
profile, observe, act, anchor, tear down -- and hands a built
:class:`~bernstein.core.orchestration.activity.ActivityResult` to the same
:func:`~bernstein.core.orchestration.activity.dispatch_activity` path a coding
spawn uses, so a browser task is scheduled, budgeted, journalled, and audited
next to coding tasks with no separate control plane.

What makes the run replayable
-----------------------------
Around every action the worker captures the exact screenshot and DOM bytes it saw
*before* acting, stores both content-addressed, and folds them into that step's
anchor together with the action and the prior anchor. The result is a Merkle
chain (see :mod:`bernstein.core.orchestration.browser_check`) whose head is the
run identity, and whose per-step observation hashes are the activity's evidence
set. Two consequences the issue asks for follow directly:

* re-running the same flow over the same recorded observations reproduces a
  byte-identical action sequence, report, and verdict, because every value the
  worker writes is a pure function of the flow definition and the observed bytes
  -- no wall clock, no counter, no network ordering enters any hash; and
* altering one stored observation or one action receipt breaks that step's anchor
  and every anchor after it, so verification fails at an exact index.

The stochastic part -- what a live browser renders -- stays outside: the worker
drives an injected :class:`~bernstein.core.orchestration.browser_driver.BrowserDriver`
and never inspects how it reaches a page. Driver faults are normalised onto the
closed :class:`~bernstein.core.orchestration.activity.TerminalState` set, and a
partial flow is still anchored, because the steps that did run are exactly the
evidence a post-incident reader needs.

Isolation is structural: each run gets a profile directory derived from its flow
id, and the profile is torn down on every exit path, so two concurrent browser
tasks cannot share cookies even if they run against the same site.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.agents.computer_use import GENESIS_ANCHOR, Action, ActionKind
from bernstein.core.orchestration.activity import TerminalState
from bernstein.core.orchestration.activity_modalities import BrowserActivity
from bernstein.core.orchestration.browser_check import (
    BrowserCheckRecord,
    BrowserFlowReport,
    BrowserStepRecord,
    CheckKind,
    build_step_record,
    evaluate_check,
    report_to_canonical_bytes,
    validate_browser_flow_report,
)
from bernstein.core.orchestration.browser_driver import (
    BrowserDriverError,
    BrowserDriverUnavailable,
    BrowserProfile,
    BrowserStepTimeout,
    observe,
)
from bernstein.core.security.sanitize import sanitize_log

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from bernstein.core.orchestration.activity import ActivityResult
    from bernstein.core.orchestration.activity_modalities import ContentStore
    from bernstein.core.orchestration.browser_driver import BrowserDriver, PageState

logger = logging.getLogger(__name__)

__all__ = [
    "BrowserBudget",
    "BrowserBudgetExceeded",
    "BrowserRunResult",
    "BrowserWorker",
    "CheckSpec",
    "FlowStep",
]

#: Terminal capture appended after the last action so the post-action state is
#: anchored too. A pure capture with no side effect, so replaying it is a no-op.
_TERMINAL_ACTION = Action(kind=ActionKind.SCREENSHOT)

#: Maps a typed driver failure onto the closed terminal-state set. Ordered most
#: specific first, since the classes form a hierarchy.
_DRIVER_FAILURES: tuple[tuple[type[BrowserDriverError], TerminalState, str], ...] = (
    (BrowserStepTimeout, TerminalState.TIMED_OUT, "driver_timeout"),
    (BrowserDriverUnavailable, TerminalState.REFUSED, "driver_unavailable"),
    (BrowserDriverError, TerminalState.FAILED, "driver_error"),
)


class BrowserBudgetExceeded(RuntimeError):
    """A browser run was refused because a step would cross its cost cap.

    Raised *before* the step that would exceed the cap, so a budgeted browser
    task never spends past it. The message names the cap that was hit.
    """


@dataclass(frozen=True, slots=True)
class BrowserBudget:
    """The cost cap applied to a browser run.

    A run may anchor at most ``max_steps`` steps and store at most
    ``max_observation_bytes`` of observation bytes. Per-step screenshots are
    large, so the byte cap is the one that actually bounds a runaway flow; both
    are checked before the work happens.

    Attributes:
        max_steps: Maximum anchored steps (declared actions plus the terminal
            capture).
        max_observation_bytes: Maximum cumulative screenshot plus DOM bytes the
            run may store.
    """

    max_steps: int
    max_observation_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_steps < 0:
            raise ValueError("max_steps must be non-negative")
        if self.max_observation_bytes < 0:
            raise ValueError("max_observation_bytes must be non-negative")


@dataclass(frozen=True, slots=True)
class CheckSpec:
    """One site-check assertion to evaluate against a step's observation.

    A check attached to a :class:`FlowStep` is evaluated against the observation
    captured *before* that step's action; a check passed as ``final_checks`` is
    evaluated against the terminal post-action capture. That is what binds the
    verdict to anchored bytes: the assertion and the evidence are the same
    observation.

    Attributes:
        check_id: Stable id, unique within the flow.
        kind: The assertion kind.
        operand: The expected substring, or a ``sha256:`` screenshot hash.
    """

    check_id: str
    kind: CheckKind
    operand: str


@dataclass(frozen=True, slots=True)
class FlowStep:
    """One declared action plus the checks on the state that preceded it.

    Attributes:
        action: The canonicalised action to take. Only the *digest* of any typed
            value travels here, so a form-filling flow never hands the worker a
            secret to anchor.
        checks: Assertions evaluated against this step's pre-action observation.
    """

    action: Action
    checks: tuple[CheckSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class BrowserRunResult:
    """The outcome of a browser run.

    Attributes:
        report: The anchored flow report (the activity artifact).
        result: The built :class:`ActivityResult` ready for
            :func:`~bernstein.core.orchestration.activity.dispatch_activity`.
        profile_dir: The isolated profile the run used (already torn down).
        steps_executed: How many steps were anchored.
    """

    report: BrowserFlowReport
    result: ActivityResult
    profile_dir: Path
    steps_executed: int = field(default=0)


class BrowserWorker:
    """Runs a site check or UI flow as a typed activity under a cost cap.

    Args:
        store: The content-addressed store observation bytes and the report land
            in.
        budget: The cost cap the run must stay inside.
        profile_root: The directory per-task browser profiles are allocated under.
    """

    def __init__(self, *, store: ContentStore, budget: BrowserBudget, profile_root: Path) -> None:
        self._store = store
        self._budget = budget
        self._profile_root = profile_root

    def run(
        self,
        *,
        flow_id: str,
        start_url: str,
        steps: Sequence[FlowStep],
        driver_factory: Callable[[Path], BrowserDriver],
        final_checks: Sequence[CheckSpec] = (),
    ) -> BrowserRunResult:
        """Drive the flow, anchoring every observation and action.

        Allocates an isolated profile, builds the driver against it, then for each
        declared step captures the pre-action observation, performs the action,
        and anchors the step. After the last action it captures the post-action
        state as a terminal anchored step and evaluates ``final_checks`` against
        it. The profile is torn down and the driver closed on every exit path.

        Args:
            flow_id: Stable id for the flow (also the profile isolation key).
            start_url: The URL the flow starts from (provenance).
            steps: The declared actions and their per-step checks.
            driver_factory: Builds a driver bound to the isolated profile
                directory the worker allocates.
            final_checks: Assertions evaluated against the terminal capture.

        Returns:
            A :class:`BrowserRunResult` whose ``result`` is ready to dispatch.

        Raises:
            BrowserBudgetExceeded: When a step would cross the cost cap.
            ActivityRejected: When the assembled report is malformed (a duplicate
                check id, an empty operand, a broken chain), so a report that
                could not be verified later never reaches the journal.
        """
        profile = BrowserProfile.allocate(root=self._profile_root, task_id=flow_id)
        session = _FlowSession(store=self._store, budget=self._budget)
        driver: BrowserDriver | None = None
        terminal_state = TerminalState.COMPLETED
        reason_code = "ok"

        try:
            try:
                driver = driver_factory(profile.profile_dir)
                for step in steps:
                    state = observe(driver)
                    _perform(driver, step.action)
                    session.anchor(state=state, action=step.action, checks=step.checks)
                session.anchor(state=observe(driver), action=_TERMINAL_ACTION, checks=final_checks)
            except BrowserDriverError as exc:
                terminal_state, reason_code = _classify(exc)
                logger.warning(
                    "browser flow %s ended early: terminal=%s reason=%s",
                    sanitize_log(flow_id),
                    terminal_state.value,
                    reason_code,
                )
                session.anchor_terminal_capture(driver)

            report = validate_browser_flow_report(
                BrowserFlowReport(
                    flow_id=flow_id,
                    start_url=start_url,
                    steps=session.steps,
                    checks=session.checks,
                    head_anchor=session.head_anchor,
                )
            )
            if terminal_state is TerminalState.COMPLETED and any(not c.passed for c in report.checks):
                # A failing site check is a recorded verdict bound to anchored
                # bytes, not an exception: the evidence is the point.
                reason_code = "checks_failed"

            # The report's canonical bytes are stored content-addressed under the
            # anchored artifact_hash so an offline verifier reattaches it from the
            # run content store alone.
            self._store.put(report_to_canonical_bytes(report))
            result = session.finish(
                artifact=report.to_dict(),
                terminal_state=terminal_state,
                reason_code=reason_code,
            )
            return BrowserRunResult(
                report=report,
                result=result,
                profile_dir=profile.profile_dir,
                steps_executed=len(report.steps),
            )
        finally:
            if driver is not None:
                driver.close()
            profile.teardown()


def _perform(driver: BrowserDriver, action: Action) -> None:
    """Route an action to the driver verb that implements it."""
    if action.kind is ActionKind.NAVIGATE:
        driver.navigate(action.target)
        return
    driver.act(action)


def _classify(exc: BrowserDriverError) -> tuple[TerminalState, str]:
    """Map a typed driver failure onto the closed terminal-state set."""
    for failure_type, state, reason in _DRIVER_FAILURES:
        if isinstance(exc, failure_type):
            return state, reason
    return TerminalState.FAILED, "driver_error"


class _FlowSession:
    """Accumulates the anchored chain and the evidence set for one flow.

    Split out of :class:`BrowserWorker` so the anchoring rules live in one place
    and the worker body reads as the flow it drives.
    """

    def __init__(self, *, store: ContentStore, budget: BrowserBudget) -> None:
        self._activity = BrowserActivity(store=store)
        self._store = store
        self._budget = budget
        self._steps: list[BrowserStepRecord] = []
        self._checks: list[BrowserCheckRecord] = []
        self._head = GENESIS_ANCHOR
        self._bytes_stored = 0

    @property
    def steps(self) -> tuple[BrowserStepRecord, ...]:
        """The anchored steps, in execution order."""
        return tuple(self._steps)

    @property
    def checks(self) -> tuple[BrowserCheckRecord, ...]:
        """The evaluated checks, in declaration order."""
        return tuple(self._checks)

    @property
    def head_anchor(self) -> str:
        """The current head anchor -- the run's identity so far."""
        return self._head

    def anchor(self, *, state: PageState, action: Action, checks: Sequence[CheckSpec]) -> None:
        """Content-address one observation, anchor the action, evaluate its checks."""
        index = len(self._steps)
        if index + 1 > self._budget.max_steps:
            raise BrowserBudgetExceeded(f"browser flow exceeds max_steps={self._budget.max_steps} at step {index}")
        projected = self._bytes_stored + len(state.screenshot) + len(state.dom)
        if projected > self._budget.max_observation_bytes:
            raise BrowserBudgetExceeded(
                f"browser flow exceeds max_observation_bytes={self._budget.max_observation_bytes} at step {index}"
            )

        # Content-address both observation halves through the modality primitive,
        # so each hash joins the activity's evidence set.
        screenshot_obs = self._activity.observe(step=f"{index}:screenshot", snapshot=state.screenshot)
        dom_obs = self._activity.observe(step=f"{index}:dom", snapshot=state.dom)
        self._bytes_stored = projected

        record = build_step_record(
            index=index,
            prev_anchor=self._head,
            action=action,
            screenshot_content_hash=screenshot_obs.content_hash,
            dom_content_hash=dom_obs.content_hash,
            screenshot_bytes=state.screenshot,
            dom_bytes=state.dom,
        )
        # The action receipt is evidence in its own right: its bytes are the
        # anchor preimage, so the evidence set covers the decision, not just the
        # pixels behind it.
        self._activity.receipt(step=f"{index}:action", receipt=_receipt_bytes(record))
        self._steps.append(record)
        self._head = record.anchor

        for spec in checks:
            self._checks.append(
                BrowserCheckRecord(
                    check_id=spec.check_id,
                    kind=spec.kind,
                    operand=spec.operand,
                    step_index=index,
                    passed=evaluate_check(
                        kind=spec.kind,
                        operand=spec.operand,
                        dom_bytes=state.dom,
                        screenshot_content_hash=record.screenshot_content_hash,
                    ),
                )
            )

    def anchor_terminal_capture(self, driver: BrowserDriver | None) -> None:
        """Anchor the last observable state after a driver failure.

        The state a flow died in is the most useful thing a post-incident reader
        can have, so it is anchored as a pure capture (no side-effecting action is
        claimed). Best effort: a driver too broken to observe simply contributes
        nothing, and the partial chain still verifies.
        """
        if driver is None:
            return
        try:
            state = observe(driver)
        except BrowserDriverError:
            return
        try:
            self.anchor(state=state, action=_TERMINAL_ACTION, checks=())
        except BrowserBudgetExceeded:
            return

    def finish(self, *, artifact: object, terminal_state: TerminalState, reason_code: str) -> ActivityResult:
        """Build the typed activity result pinning the gathered evidence set."""
        return self._activity.finish(artifact=artifact, terminal_state=terminal_state, reason_code=reason_code)


def _receipt_bytes(record: BrowserStepRecord) -> bytes:
    """Return the action-receipt bytes stored as evidence for one step.

    These are the anchor preimage plus the anchor it produced, so the stored blob
    is self-checking: recomputing the anchor from the preimage must reproduce the
    recorded value.
    """
    from bernstein.core.agents.computer_use import action_anchor_preimage

    preimage = action_anchor_preimage(
        prev_anchor=record.prev_anchor,
        observation_hash=record.observation_hash,
        action=record.action(),
    )
    return preimage + b"\n" + record.anchor.encode("ascii")
