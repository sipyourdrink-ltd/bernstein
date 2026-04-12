"""Agent voting protocol — configurable multi-model consensus for task verification.

VotingProtocol wraps one or more LLM reviewers into a single verdict using
configurable strategies: MAJORITY, QUORUM, WEIGHTED, or UNANIMOUS.

The existing cross-model verifier becomes a thin adapter over VotingProtocol
with QUORUM(1, 1), preserving full backward compatibility.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from bernstein.core.llm import call_llm

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bernstein.core.cross_model_verifier import CrossModelVerifierConfig
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class VotingStrategy(StrEnum):
    """Aggregation strategy for multi-model votes."""

    MAJORITY = "majority"
    QUORUM = "quorum"
    WEIGHTED = "weighted"
    UNANIMOUS = "unanimous"


class TieBreak(StrEnum):
    """How to resolve a tie (equal approve vs reject weight/count)."""

    REJECT = "reject"
    ACCEPT = "accept"
    ESCALATE = "escalate"  # Route to stronger model; safe-defaults to reject


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VotingConfig:
    """Configuration for the voting protocol.

    Attributes:
        strategy: Aggregation algorithm.
        quorum_k: Required approvals for QUORUM strategy.
        quorum_n: Total voters for QUORUM strategy (informational).
        abstention_threshold: Confidence below this → vote becomes abstain.
        tie_break: How to break ties.
    """

    strategy: VotingStrategy = VotingStrategy.QUORUM
    quorum_k: int = 1
    quorum_n: int = 1
    abstention_threshold: float = 0.3
    tie_break: TieBreak = TieBreak.REJECT


@dataclass(frozen=True)
class Vote:
    """A single model's verdict on a task diff.

    Attributes:
        voter_model: OpenRouter model identifier.
        verdict: "approve", "request_changes", or "abstain".
        confidence: Self-reported confidence 0.0-1.0.
        reasoning: One-sentence rationale.
        timestamp: Unix epoch when the vote was cast.
    """

    voter_model: str
    verdict: Literal["approve", "request_changes", "abstain"]
    confidence: float
    reasoning: str
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class VotingResult:
    """Aggregated result of all votes.

    Attributes:
        votes: All votes cast (after abstention processing).
        final_verdict: Consensus decision.
        confidence: Mean confidence of non-abstaining voters.
        reasoning: Short summary of how the verdict was reached.
        strategy: Strategy used.
        needs_escalation: True when tie_break=ESCALATE was triggered.
    """

    votes: list[Vote]
    final_verdict: Literal["approve", "request_changes"]
    confidence: float
    reasoning: str
    strategy: VotingStrategy
    needs_escalation: bool = False


# ---------------------------------------------------------------------------
# Vote event listener registry
# ---------------------------------------------------------------------------

_vote_listeners: list[Callable[[object], None]] = []


def add_vote_listener(callback: Callable[[object], None]) -> None:
    """Register a callback invoked on every VoteEvent."""
    _vote_listeners.append(callback)


def remove_vote_listener(callback: Callable[[object], None]) -> None:
    """Unregister a previously registered vote callback."""
    with contextlib.suppress(ValueError):
        _vote_listeners.remove(callback)


def _emit_vote_event(event: object) -> None:
    """Dispatch a VoteEvent to all registered listeners."""
    for cb in _vote_listeners:
        try:
            cb(event)
        except Exception:
            logger.exception("Vote listener raised")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class VotingProtocol:
    """Multi-model voting coordinator.

    Args:
        config: Voting configuration.
    """

    def __init__(self, config: VotingConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def maybe_abstain(self, vote: Vote) -> Vote:
        """Return a new Vote with verdict=abstain if confidence is below threshold.

        Args:
            vote: Original vote.

        Returns:
            The original vote unchanged, or a new abstain vote.
        """
        if vote.confidence < self._config.abstention_threshold:
            return Vote(
                voter_model=vote.voter_model,
                verdict="abstain",
                confidence=vote.confidence,
                reasoning=(
                    f"Abstained: confidence {vote.confidence:.2f} < threshold {self._config.abstention_threshold:.2f}"
                ),
                timestamp=vote.timestamp,
            )
        return vote

    def tally(self, votes: list[Vote]) -> VotingResult:
        """Aggregate votes into a final verdict.

        Applies abstention threshold to each vote before tallying.

        Args:
            votes: All votes cast (abstentions included).

        Returns:
            VotingResult with final_verdict and metadata.
        """
        processed = [self.maybe_abstain(v) for v in votes]

        non_abstained = [v for v in processed if v.verdict != "abstain"]
        approvals = [v for v in non_abstained if v.verdict == "approve"]
        rejections = [v for v in non_abstained if v.verdict == "request_changes"]

        avg_confidence = sum(v.confidence for v in non_abstained) / len(non_abstained) if non_abstained else 0.0

        strategy = self._config.strategy
        if strategy == VotingStrategy.MAJORITY:
            verdict, needs_esc = self._majority(approvals, rejections)
        elif strategy == VotingStrategy.QUORUM:
            verdict, needs_esc = self._quorum(approvals)
        elif strategy == VotingStrategy.WEIGHTED:
            verdict, needs_esc = self._weighted(approvals, rejections)
        elif strategy == VotingStrategy.UNANIMOUS:
            verdict, needs_esc = self._unanimous(approvals, non_abstained)
        else:
            verdict, needs_esc = "request_changes", False

        a_count = len(approvals)
        r_count = len(rejections)
        abs_count = len(processed) - len(non_abstained)
        reasoning = f"{strategy}: {a_count} approve, {r_count} reject, {abs_count} abstain → {verdict}"

        return VotingResult(
            votes=processed,
            final_verdict=verdict,
            confidence=avg_confidence,
            reasoning=reasoning,
            strategy=strategy,
            needs_escalation=needs_esc,
        )

    # ------------------------------------------------------------------
    # Strategy implementations
    # ------------------------------------------------------------------

    def _tie_break(self) -> tuple[Literal["approve", "request_changes"], bool]:
        tb = self._config.tie_break
        if tb == TieBreak.ACCEPT:
            return "approve", False
        if tb == TieBreak.ESCALATE:
            return "request_changes", True  # safe default + escalation flag
        return "request_changes", False  # REJECT

    def _majority(
        self,
        approvals: list[Vote],
        rejections: list[Vote],
    ) -> tuple[Literal["approve", "request_changes"], bool]:
        if len(approvals) > len(rejections):
            return "approve", False
        if len(rejections) > len(approvals):
            return "request_changes", False
        return self._tie_break()

    def _quorum(
        self,
        approvals: list[Vote],
    ) -> tuple[Literal["approve", "request_changes"], bool]:
        if len(approvals) >= self._config.quorum_k:
            return "approve", False
        return "request_changes", False

    def _weighted(
        self,
        approvals: list[Vote],
        rejections: list[Vote],
    ) -> tuple[Literal["approve", "request_changes"], bool]:
        approve_w = sum(v.confidence for v in approvals)
        reject_w = sum(v.confidence for v in rejections)
        if approve_w > reject_w:
            return "approve", False
        if reject_w > approve_w:
            return "request_changes", False
        return self._tie_break()

    def _unanimous(
        self,
        approvals: list[Vote],
        non_abstained: list[Vote],
    ) -> tuple[Literal["approve", "request_changes"], bool]:
        if not non_abstained:
            return self._tie_break()
        if len(approvals) == len(non_abstained):
            return "approve", False
        return "request_changes", False

    # ------------------------------------------------------------------
    # Async vote collection
    # ------------------------------------------------------------------

    async def collect_votes(
        self,
        task: Task,
        worktree_path: Path,
        voter_models: list[str],
        verifier_cfg: CrossModelVerifierConfig,
    ) -> VotingResult:
        """Call each voter model and tally the results.

        Reuses the diff-fetching and prompt-building logic from
        cross_model_verifier. Each voter gets the same diff and prompt;
        only the model differs.

        Args:
            task: Completed task under review.
            worktree_path: Git worktree for diff extraction.
            voter_models: List of OpenRouter model identifiers.
            verifier_cfg: Shared verifier config (diff limits, provider, etc.).

        Returns:
            VotingResult with all individual votes and final verdict.
        """
        from bernstein.core.cross_model_verifier import _build_prompt, _get_diff
        from bernstein.core.models import VoteEvent

        diff = _get_diff(worktree_path, task.owned_files)
        if len(diff) > verifier_cfg.max_diff_chars:
            diff = diff[: verifier_cfg.max_diff_chars] + "\n... (truncated)"
        prompt = _build_prompt(task, diff)

        votes: list[Vote] = []
        for model in voter_models:
            vote = await self._call_voter(model, prompt, verifier_cfg)
            votes.append(vote)
            # Emit per-vote event
            _emit_vote_event(
                VoteEvent(
                    timestamp=vote.timestamp,
                    task_id=task.id,
                    voter_model=vote.voter_model,
                    verdict=vote.verdict,
                    confidence=vote.confidence,
                    reasoning=vote.reasoning,
                    is_final=False,
                    strategy=str(self._config.strategy),
                )
            )

        result = self.tally(votes)

        # Emit final aggregated event
        _emit_vote_event(
            VoteEvent(
                timestamp=time.time(),
                task_id=task.id,
                voter_model="",
                verdict=result.final_verdict,
                confidence=result.confidence,
                reasoning=result.reasoning,
                is_final=True,
                strategy=str(self._config.strategy),
            )
        )

        logger.info(
            "voting: task=%s strategy=%s verdict=%s votes=%d",
            task.id,
            self._config.strategy,
            result.final_verdict,
            len(votes),
        )
        return result

    # ------------------------------------------------------------------
    # Voter call + response parsing
    # ------------------------------------------------------------------

    async def _call_voter(
        self,
        model: str,
        prompt: str,
        verifier_cfg: CrossModelVerifierConfig,
    ) -> Vote:
        """Invoke a single voter model and parse its Vote."""
        try:
            raw = await call_llm(
                prompt=prompt,
                model=model,
                provider=verifier_cfg.provider,
                max_tokens=verifier_cfg.max_tokens,
                temperature=0.0,
            )
        except RuntimeError as exc:
            logger.warning("voting: voter %s failed: %s — abstaining", model, exc)
            return Vote(
                voter_model=model,
                verdict="abstain",
                confidence=0.0,
                reasoning=f"Voter call failed: {exc}",
            )

        return self._parse_vote(raw, model)

    def _parse_vote(self, raw: str, model: str) -> Vote:
        """Parse LLM response into a Vote, defaulting to abstain on parse failure."""
        text = raw.strip()
        if text.startswith("```"):
            text = "\n".join(line for line in text.splitlines() if not line.strip().startswith("```")).strip()

        data: dict[str, object] = {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}") + 1
            if start >= 0 and end > start:
                with contextlib.suppress(json.JSONDecodeError):
                    data = json.loads(text[start:end])

        if not data:
            logger.warning("voting: unparseable response from %s — abstaining: %.100s", model, text)
            return Vote(
                voter_model=model,
                verdict="abstain",
                confidence=0.0,
                reasoning="Unparseable response — abstaining",
            )

        raw_verdict = str(data.get("verdict", "abstain")).lower()
        verdict: Literal["approve", "request_changes", "abstain"]
        if raw_verdict == "approve":
            verdict = "approve"
        elif raw_verdict == "request_changes":
            verdict = "request_changes"
        else:
            verdict = "abstain"

        confidence = float(data.get("confidence", 0.8))
        confidence = max(0.0, min(1.0, confidence))  # clamp to [0, 1]

        reasoning = str(data.get("feedback", data.get("reasoning", "")))

        return Vote(
            voter_model=model,
            verdict=verdict,
            confidence=confidence,
            reasoning=reasoning,
        )
