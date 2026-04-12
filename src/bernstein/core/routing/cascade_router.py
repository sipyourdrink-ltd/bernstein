"""Cost-aware model cascading router.

Implements a two-phase routing strategy:

1. **Initial selection**: pick the cheapest viable model based on task metadata
   and bandit history (haiku for simple, sonnet for medium/complex, opus for
   critical).  Skips a tier proactively when bandit data shows its success rate
   is below the quality threshold for this (role, complexity) class.

2. **Confidence-based escalation**: after execution, inspect signals — janitor
   result, agent output text, exit code — to decide whether to escalate to the
   next tier or accept the result.

Cascade order (cheapest → most expensive):
    Standard tasks:    haiku  →  sonnet  →  opus
    High-stakes tasks: sonnet →  opus

Escalation signals (evaluated in priority order):
1. Hard failure (task marked failed, non-zero exit) → always escalate
2. Low-confidence markers in agent output text → escalate
3. Janitor verification failure → escalate
4. Bandit proactive skip: if current tier's success rate < threshold and
   N ≥ MIN_OBSERVATIONS → jump to next tier immediately

Cost tracking:
    Each ``CascadeAttempt`` records model, cost, latency, and outcome.
    ``CascadeChainReport`` summarises the full chain: cheap-attempt cost,
    escalation overhead, and savings vs. a hypothetical all-opus baseline.

Integration:
    Call ``cascade_router.select(task)`` to get the initial ``CascadeDecision``.
    After the agent completes, call ``cascade_router.record_and_escalate()``
    to record the outcome and retrieve the next decision (or ``None`` if done).
    Persist results with ``cascade_router.save_chain()``.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.cost import (
    CASCADE,
    MIN_OBSERVATIONS,
    QUALITY_THRESHOLD,
    EpsilonGreedyBandit,
    _model_cost,
)
from bernstein.core.models import Complexity, Scope, Task

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Patterns in agent output that signal low confidence or incomplete work.
# We scan the tail of the output (last 2 000 chars) to keep it fast.
_LOW_CONFIDENCE_PATTERN: re.Pattern[str] = re.compile(
    r"\b("
    r"i(?:'m| am) not sure"
    r"|i(?:'m| am) unsure"
    r"|i cannot determine"
    r"|i can'?t determine"
    r"|i don'?t know how"
    r"|this is beyond (?:my|the)"
    r"|i lack(?:ed)? (?:the )?context"
    r"|partial(?:ly)? (?:complete|implemented)"
    r"|unable to (?:fully|completely)"
    r"|not (?:fully )?confident"
    r"|need(?:s)? more (?:context|information|info)"
    r"|couldn'?t (?:fully )?complete"
    r"|could not (?:fully )?complete"
    r"|incomplete implementation"
    r"|left (?:as )?(?:a )?placeholder"
    r"|TODO:.*escalat"
    r")\b",
    re.IGNORECASE,
)

# Number of output chars from the tail to scan for confidence signals.
_CONFIDENCE_SCAN_TAIL = 2_000

# Cost per 1k tokens for Opus — used to compute savings vs all-opus baseline.
_OPUS_COST_PER_1K: float = 0.015

# Approximate average token usage per task (blended input + output).
_AVG_TASK_TOKENS: int = 80_000


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class CascadeAttempt:
    """One attempt within a cascade chain.

    Attributes:
        task_id: Task this attempt belongs to.
        chain_id: Cascade chain identifier.
        model: Model used for this attempt (e.g. "haiku", "sonnet").
        effort: Effort level (e.g. "low", "high", "max").
        attempt_number: 0-based index within the chain (0 = cheapest tier).
        started_at: Unix timestamp when the attempt was started.
        cost_usd: Actual USD cost incurred (0 until recorded).
        latency_s: Wall-clock seconds for the attempt (0 until recorded).
        tokens_used: Total input + output tokens.
        success: Whether the attempt succeeded (task complete + janitor pass).
        escalated: True when this attempt was escalated past.
        escalation_reason: Human-readable reason for escalation (if escalated).
    """

    task_id: str
    chain_id: str
    model: str
    effort: str
    attempt_number: int
    started_at: float = field(default_factory=time.time)
    cost_usd: float = 0.0
    latency_s: float = 0.0
    tokens_used: int = 0
    success: bool = False
    escalated: bool = False
    escalation_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "task_id": self.task_id,
            "chain_id": self.chain_id,
            "model": self.model,
            "effort": self.effort,
            "attempt_number": self.attempt_number,
            "started_at": self.started_at,
            "cost_usd": self.cost_usd,
            "latency_s": self.latency_s,
            "tokens_used": self.tokens_used,
            "success": self.success,
            "escalated": self.escalated,
            "escalation_reason": self.escalation_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CascadeAttempt:
        """Deserialise from a dict."""
        return cls(
            task_id=str(d["task_id"]),
            chain_id=str(d["chain_id"]),
            model=str(d["model"]),
            effort=str(d["effort"]),
            attempt_number=int(d["attempt_number"]),
            started_at=float(d.get("started_at", 0.0)),
            cost_usd=float(d.get("cost_usd", 0.0)),
            latency_s=float(d.get("latency_s", 0.0)),
            tokens_used=int(d.get("tokens_used", 0)),
            success=bool(d.get("success", False)),
            escalated=bool(d.get("escalated", False)),
            escalation_reason=d.get("escalation_reason"),
        )


@dataclass
class CascadeDecision:
    """Model selection decision produced by the cascade router.

    Attributes:
        model: Model name (e.g. "haiku", "sonnet", "opus").
        effort: Effort level (e.g. "low", "high", "max").
        attempt_number: 0-based position in the cascade chain.
        is_escalated: True when this is not the first attempt.
        reason: Human-readable explanation of the routing decision.
        estimated_cost_usd: Rough per-task cost estimate at this model tier.
        chain_id: Opaque identifier for this task's cascade chain; pass back
            to ``record_and_escalate()`` after execution.
    """

    model: str
    effort: str
    attempt_number: int
    is_escalated: bool
    reason: str
    estimated_cost_usd: float
    chain_id: str


@dataclass
class CascadeChainReport:
    """Cost summary for a completed cascade chain.

    Attributes:
        chain_id: Cascade chain identifier.
        task_id: Task this chain belongs to.
        role: Task role.
        attempts: Ordered list of all attempts (cheapest first).
        final_model: Model that produced the accepted result.
        succeeded: Whether the chain ended in success.
        total_cost_usd: Sum of all attempt costs.
        first_attempt_cost_usd: Cost of the cheapest-tier attempt.
        escalation_overhead_usd: Extra cost paid for escalated attempts.
        saved_vs_direct_opus_usd: Savings vs. routing directly to Opus
            (0 when first attempt used Opus or chain failed).
    """

    chain_id: str
    task_id: str
    role: str
    attempts: list[CascadeAttempt]
    final_model: str
    succeeded: bool
    total_cost_usd: float
    first_attempt_cost_usd: float
    escalation_overhead_usd: float
    saved_vs_direct_opus_usd: float

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "chain_id": self.chain_id,
            "task_id": self.task_id,
            "role": self.role,
            "attempts": [a.to_dict() for a in self.attempts],
            "final_model": self.final_model,
            "succeeded": self.succeeded,
            "total_cost_usd": self.total_cost_usd,
            "first_attempt_cost_usd": self.first_attempt_cost_usd,
            "escalation_overhead_usd": self.escalation_overhead_usd,
            "saved_vs_direct_opus_usd": self.saved_vs_direct_opus_usd,
        }


# ---------------------------------------------------------------------------
# CascadeRouter
# ---------------------------------------------------------------------------


class CascadeRouter:
    """Cost-aware cascading model router.

    Selects the cheapest viable model for a task's first attempt and escalates
    to more capable tiers only when confidence signals warrant it.  Integrates
    with ``EpsilonGreedyBandit`` to learn which model is cheapest-while-meeting-
    quality-thresholds for each (role, complexity) class over time.

    Important: The cascade arms (haiku/sonnet/opus) are Claude-specific model
    names.  Use ``router_applicable()`` to check whether this router should be
    consulted for a given adapter before calling ``select()``.

    Usage::

        router = CascadeRouter(bandit_metrics_dir=workdir / ".sdd" / "metrics")

        # Before spawning agent:
        decision = router.select(task)
        # spawn agent with decision.model / decision.effort …

        # After agent finishes (cost_usd and latency_s from metrics collector):
        next_decision = router.record_and_escalate(
            chain_id=decision.chain_id,
            task=task,
            attempt=CascadeAttempt(
                task_id=task.id,
                chain_id=decision.chain_id,
                model=decision.model,
                effort=decision.effort,
                attempt_number=decision.attempt_number,
                cost_usd=0.05,
                latency_s=120.0,
                success=True,
            ),
            janitor_passed=True,
        )
        # next_decision is None → done, or CascadeDecision → spawn again

        # Persist chain report:
        router.save_chain(decision.chain_id, task, workdir / ".sdd" / "metrics")
    """

    CHAIN_FILE = "cascade_chains.jsonl"

    # Adapters whose model names match the cascade arms (haiku/sonnet/opus).
    _CLAUDE_COMPATIBLE_ADAPTERS: frozenset[str] = frozenset({"claude", "claude code", "claude_code", "claude-code"})

    @staticmethod
    def router_applicable(adapter_name: str) -> bool:
        """Return whether this router's arms are valid for the given adapter.

        The cascade router's arms (haiku/sonnet/opus) are Claude-specific.
        For non-Claude adapters the router cannot produce meaningful model
        selections and should be skipped.

        Args:
            adapter_name: Name returned by ``adapter.name()`` or the ``cli``
                value from ``role_model_policy``.

        Returns:
            ``True`` when the router can route for this adapter.
        """
        return adapter_name.lower().strip() in CascadeRouter._CLAUDE_COMPATIBLE_ADAPTERS

    def __init__(
        self,
        bandit_metrics_dir: Path | None = None,
        quality_threshold: float = QUALITY_THRESHOLD,
        min_observations: int = MIN_OBSERVATIONS,
    ) -> None:
        self._bandit_metrics_dir = bandit_metrics_dir
        self._quality_threshold = quality_threshold
        self._min_observations = min_observations

        # chain_id → list of attempts (chronological order)
        self._chains: dict[str, list[CascadeAttempt]] = {}

        # Lazy-loaded bandit instance (initialised on first use)
        self._bandit_instance: EpsilonGreedyBandit | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select(self, task: Task, chain_id: str | None = None) -> CascadeDecision:
        """Select the model for the next attempt in a cascade chain.

        For a new task (no existing chain), picks the cheapest viable model
        using bandit history.  For an escalating chain, advances to the next
        tier in the cascade.

        Args:
            task: Task to route.
            chain_id: If provided, this continues an existing chain (escalation).
                      If ``None``, a fresh chain is started.

        Returns:
            CascadeDecision with model, effort, and chain metadata.
        """
        if chain_id is None:
            chain_id = uuid.uuid4().hex[:16]
            self._chains[chain_id] = []

        attempts = self._chains.get(chain_id, [])
        attempt_number = len(attempts)

        if attempt_number == 0:
            model, reason = self._select_initial_model(task)
        else:
            last = attempts[-1]
            model, reason = self._escalate_from(last.model, task, last.escalation_reason or "escalation")

        effort = _effort_for_model(model, task)
        estimated_cost = (_AVG_TASK_TOKENS / 1_000) * _model_cost(model)

        return CascadeDecision(
            model=model,
            effort=effort,
            attempt_number=attempt_number,
            is_escalated=attempt_number > 0,
            reason=reason,
            estimated_cost_usd=round(estimated_cost, 6),
            chain_id=chain_id,
        )

    def detect_low_confidence(self, output: str) -> tuple[bool, str]:
        """Scan agent output for low-confidence signals.

        Args:
            output: Raw text output from the agent (stdout + stderr).

        Returns:
            Tuple of (is_low_confidence, matched_phrase).
        """
        tail = output[-_CONFIDENCE_SCAN_TAIL:] if len(output) > _CONFIDENCE_SCAN_TAIL else output
        m = _LOW_CONFIDENCE_PATTERN.search(tail)
        if m:
            return True, m.group().strip()
        return False, ""

    def record_and_escalate(
        self,
        chain_id: str,
        task: Task,
        attempt: CascadeAttempt,
        janitor_passed: bool | None = None,
        output: str | None = None,
    ) -> CascadeDecision | None:
        """Record an attempt outcome and decide whether to escalate.

        Call this after the agent finishes.  If escalation is warranted, the
        router records the current attempt as escalated and returns a new
        ``CascadeDecision`` for the next tier.  If the attempt succeeded (or
        no higher tier exists), returns ``None``.

        Also updates the bandit with this observation.

        Args:
            chain_id: Chain identifier from the preceding ``select()`` call.
            task: Task this chain belongs to.
            attempt: Completed attempt (fill in cost_usd, latency_s, success).
            janitor_passed: Whether the janitor verification passed (``None``
                means unknown / not run yet).
            output: Raw agent output text for confidence detection (optional).

        Returns:
            Next ``CascadeDecision`` if escalation is warranted, else ``None``.
        """
        should_escalate, escalation_reason = self._should_escalate(
            attempt=attempt,
            janitor_passed=janitor_passed,
            output=output,
        )

        attempt.success = not should_escalate
        attempt.escalated = should_escalate
        attempt.escalation_reason = escalation_reason if should_escalate else None

        if chain_id not in self._chains:
            self._chains[chain_id] = []
        self._chains[chain_id].append(attempt)

        # Update bandit with this observation
        self._record_bandit(task.role, attempt.model, attempt.success, attempt.cost_usd, attempt.latency_s)

        if not should_escalate:
            logger.debug(
                "Cascade chain %s: %s accepted (attempt %d, cost=%.5f)",
                chain_id,
                attempt.model,
                attempt.attempt_number,
                attempt.cost_usd,
            )
            return None

        # Check if there's a higher tier to escalate to
        cascade_list = _cascade_for_task(task)
        try:
            current_idx = cascade_list.index(attempt.model)
        except ValueError:
            current_idx = -1

        if current_idx >= len(cascade_list) - 1:
            # Already at the top of the cascade; give up
            logger.warning(
                "Cascade chain %s: at top of cascade (%s), cannot escalate further",
                chain_id,
                attempt.model,
            )
            return None

        next_model = cascade_list[current_idx + 1]
        next_effort = _effort_for_model(next_model, task)
        estimated_cost = (_AVG_TASK_TOKENS / 1_000) * _model_cost(next_model)
        next_attempt_number = attempt.attempt_number + 1

        logger.info(
            "Cascade chain %s: escalating %s → %s (reason: %s)",
            chain_id,
            attempt.model,
            next_model,
            escalation_reason,
        )

        return CascadeDecision(
            model=next_model,
            effort=next_effort,
            attempt_number=next_attempt_number,
            is_escalated=True,
            reason=f"escalated from {attempt.model}: {escalation_reason}",
            estimated_cost_usd=round(estimated_cost, 6),
            chain_id=chain_id,
        )

    def get_chain_report(self, chain_id: str, task: Task) -> CascadeChainReport:
        """Build a cost report for a completed chain.

        Args:
            chain_id: Chain to report on.
            task: Task this chain belongs to.

        Returns:
            CascadeChainReport with cost breakdown and savings estimate.
        """
        attempts = self._chains.get(chain_id, [])
        total_cost = sum(a.cost_usd for a in attempts)
        first_cost = attempts[0].cost_usd if attempts else 0.0
        escalation_cost = sum(a.cost_usd for a in attempts[1:])

        final_attempt = attempts[-1] if attempts else None
        final_model = final_attempt.model if final_attempt else "unknown"
        succeeded = (final_attempt.success if final_attempt else False) or (
            len(attempts) > 0 and not attempts[-1].escalated
        )

        # Savings vs. routing directly to Opus for the same tokens
        opus_direct_cost = (_AVG_TASK_TOKENS / 1_000) * _OPUS_COST_PER_1K
        saved = max(0.0, opus_direct_cost - total_cost)

        return CascadeChainReport(
            chain_id=chain_id,
            task_id=task.id,
            role=task.role,
            attempts=list(attempts),
            final_model=final_model,
            succeeded=succeeded,
            total_cost_usd=round(total_cost, 6),
            first_attempt_cost_usd=round(first_cost, 6),
            escalation_overhead_usd=round(escalation_cost, 6),
            saved_vs_direct_opus_usd=round(saved, 6),
        )

    def save_chain(self, chain_id: str, task: Task, metrics_dir: Path) -> None:
        """Append the chain report to ``cascade_chains.jsonl`` in *metrics_dir*.

        Args:
            chain_id: Chain to persist.
            task: Task this chain belongs to.
            metrics_dir: ``.sdd/metrics`` directory.
        """
        import json

        report = self.get_chain_report(chain_id, task)
        record: dict[str, Any] = {
            "timestamp": time.time(),
            **report.to_dict(),
        }
        chains_file = metrics_dir / self.CHAIN_FILE
        try:
            metrics_dir.mkdir(parents=True, exist_ok=True)
            with chains_file.open("a") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            logger.warning("Could not persist cascade chain %s: %s", chain_id, exc)

    def save_bandit(self) -> None:
        """Persist bandit state to disk (no-op if no metrics dir)."""
        if self._bandit_instance is not None and self._bandit_metrics_dir is not None:
            self._bandit_instance.save(self._bandit_metrics_dir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_bandit(self) -> EpsilonGreedyBandit:
        """Lazy-load and return the bandit instance."""
        if self._bandit_instance is None:
            if self._bandit_metrics_dir is not None:
                self._bandit_instance = EpsilonGreedyBandit.load(self._bandit_metrics_dir)
            else:
                self._bandit_instance = EpsilonGreedyBandit()
        return self._bandit_instance

    def _record_bandit(
        self,
        role: str,
        model: str,
        success: bool,
        cost_usd: float,
        latency_s: float,
    ) -> None:
        bandit = self._get_bandit()
        bandit.record(role=role, model=model, success=success, cost_usd=cost_usd, latency_s=latency_s)

    def _select_initial_model(self, task: Task) -> tuple[str, str]:
        """Pick the cheapest viable model for the first attempt.

        Returns:
            Tuple of (model_name, reason).
        """
        cascade = _cascade_for_task(task)

        # High-stakes skip: never start below sonnet
        if task.role in ("manager", "architect", "security"):
            return cascade[0], f"role={task.role!r} starts at {cascade[0]}"

        if task.complexity == Complexity.HIGH or task.scope == Scope.LARGE or task.priority == 1:
            return cascade[0], f"complexity/scope/priority starts at {cascade[0]}"

        # Manager-specified override
        if task.model:
            model_lower = task.model.lower()
            if model_lower in cascade:
                return model_lower, f"manager override: {model_lower}"

        # Bandit proactive skip: if the cheapest model in cascade has been
        # observed enough times and its success rate is below the threshold,
        # skip it.
        bandit = self._get_bandit()
        for i, model in enumerate(cascade):
            # Access internal arm state (read-only)
            arm = bandit._arms.get((task.role, model))  # type: ignore[attr-defined]
            if (
                arm is not None
                and arm.observations >= self._min_observations
                and arm.success_rate < self._quality_threshold
            ):
                logger.debug(
                    "Cascade: proactive skip of %s for role=%s (success_rate=%.2f < threshold=%.2f)",
                    model,
                    task.role,
                    arm.success_rate,
                    self._quality_threshold,
                )
                continue  # skip to next tier
            # This model is viable (unobserved or meets threshold)
            if i > 0:
                return model, "proactive skip of cheaper tiers (bandit data)"
            return model, f"cheapest viable model for role={task.role!r}"

        # All tiers below threshold — start at top anyway
        top = cascade[-1]
        return top, "all tiers below quality threshold; using top"

    def _escalate_from(self, current_model: str, task: Task, reason: str) -> tuple[str, str]:
        """Return the next model in the cascade above *current_model*.

        Returns:
            Tuple of (next_model, reason).  If already at the top, returns
            the same model.
        """
        cascade = _cascade_for_task(task)
        try:
            idx = cascade.index(current_model)
        except ValueError:
            idx = -1

        if idx >= len(cascade) - 1:
            return cascade[-1], f"already at top of cascade ({cascade[-1]})"

        next_model = cascade[idx + 1]
        return next_model, f"escalated from {current_model}: {reason}"

    def _should_escalate(
        self,
        attempt: CascadeAttempt,
        janitor_passed: bool | None,
        output: str | None,
    ) -> tuple[bool, str]:
        """Determine whether this attempt should be escalated.

        Checks in order:
        1. Explicit failure (success=False before calling this)
        2. Janitor verification failure
        3. Low-confidence signal in output
        Returns:
            Tuple of (should_escalate, reason_string).
        """
        # Hard failure — explicit task failure
        if not attempt.success and janitor_passed is None and output is None:
            # Caller explicitly set success=False without further info
            return True, "task failed"

        # Janitor failure
        if janitor_passed is False:
            return True, "janitor verification failed"

        # Low-confidence output
        if output:
            low_conf, phrase = self.detect_low_confidence(output)
            if low_conf:
                return True, f"low-confidence signal in output: {phrase!r}"

        # Explicit failure (even if janitor didn't run yet)
        if not attempt.success:
            return True, "task reported as failed"

        return False, ""


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _cascade_for_task(task: Task) -> list[str]:
    """Return the cascade list appropriate for the given task.

    High-stakes tasks (manager, architect, security, high complexity, large
    scope, critical priority) skip haiku and start at sonnet.

    Args:
        task: Task to get cascade for.

    Returns:
        Ordered list of model names (cheapest first).
    """
    if (
        task.role in ("manager", "architect", "security")
        or task.complexity == Complexity.HIGH
        or task.scope == Scope.LARGE
        or task.priority == 1
    ):
        return ["sonnet", "opus"]
    return list(CASCADE)


def _effort_for_model(model: str, task: Task) -> str:
    """Select an effort level appropriate for the model and task.

    Args:
        model: Model name (e.g. "haiku", "sonnet", "opus").
        task: Task to assess.

    Returns:
        Effort string (e.g. "low", "high", "max").
    """
    if task.effort:
        return task.effort

    model_lower = model.lower()
    if "opus" in model_lower:
        return "max"
    if "haiku" in model_lower:
        return "low"
    # sonnet and unknown models
    return "high"


def load_cascade_savings_summary(metrics_dir: Path) -> dict[str, Any]:
    """Compute aggregate savings from persisted cascade chain records.

    Reads ``cascade_chains.jsonl`` in *metrics_dir* and returns a summary dict
    with total cost, escalation overhead, and savings vs all-Opus baseline.

    Args:
        metrics_dir: ``.sdd/metrics`` directory.

    Returns:
        Dict with keys: ``total_chains``, ``total_cost_usd``,
        ``escalation_overhead_usd``, ``saved_vs_opus_usd``,
        ``escalation_rate`` (fraction of chains that escalated).
    """
    import json

    chains_file = metrics_dir / CascadeRouter.CHAIN_FILE
    if not chains_file.exists():
        return {
            "total_chains": 0,
            "total_cost_usd": 0.0,
            "escalation_overhead_usd": 0.0,
            "saved_vs_opus_usd": 0.0,
            "escalation_rate": 0.0,
        }

    total_chains = 0
    total_cost = 0.0
    escalation_overhead = 0.0
    saved = 0.0
    escalated_count = 0

    try:
        for line in chains_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            total_chains += 1
            total_cost += float(record.get("total_cost_usd", 0.0))
            escalation_overhead += float(record.get("escalation_overhead_usd", 0.0))
            saved += float(record.get("saved_vs_direct_opus_usd", 0.0))
            if record.get("escalation_overhead_usd", 0.0) > 0:
                escalated_count += 1
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read cascade chains from %s: %s", chains_file, exc)

    escalation_rate = escalated_count / total_chains if total_chains > 0 else 0.0

    return {
        "total_chains": total_chains,
        "total_cost_usd": round(total_cost, 6),
        "escalation_overhead_usd": round(escalation_overhead, 6),
        "saved_vs_opus_usd": round(saved, 6),
        "escalation_rate": round(escalation_rate, 3),
    }
