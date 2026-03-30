# Agent Voting Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade cross-model verification from binary pass/fail to configurable multi-model consensus voting (MAJORITY, QUORUM, WEIGHTED, UNANIMOUS).

**Architecture:** Add `src/bernstein/core/voting.py` with `VotingProtocol` + supporting dataclasses; refactor `cross_model_verifier.py` to use `VotingProtocol` internally (backward-compatible: existing `QUORUM(1,1)` behaviour preserved); add `VoteEvent` to `models.py` for lifecycle stream emission.

**Tech Stack:** Python 3.12+, dataclasses, asyncio, existing `call_llm`, existing `lifecycle._emit`.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/bernstein/core/voting.py` | Create | VotingStrategy, TieBreak, VotingConfig, Vote, VotingResult, VoteEvent, VotingProtocol |
| `src/bernstein/core/models.py` | Modify (append) | Add VoteEvent dataclass |
| `src/bernstein/core/cross_model_verifier.py` | Modify | Use VotingProtocol internally; add voting_config param |
| `tests/unit/test_voting.py` | Create | Unit tests for voting.py |
| `tests/unit/test_cross_model_verifier.py` | Modify | Add tests for multi-voter path |

---

### Task 1: Add VoteEvent to models.py

**Files:**
- Modify: `src/bernstein/core/models.py` (append near end)

- [ ] **Step 1: Append VoteEvent dataclass to models.py**

```python
@dataclass(frozen=True)
class VoteEvent:
    """Event emitted for each vote cast and for the final voting result.

    Attributes:
        timestamp: Unix epoch when this event was created.
        task_id: ID of the task being reviewed.
        voter_model: Model that cast this vote (empty string for final-result events).
        verdict: Individual vote verdict or final consensus verdict.
        confidence: Confidence score 0.0–1.0.
        reasoning: One-sentence rationale.
        is_final: True when this event records the aggregated voting result.
        strategy: VotingStrategy value used (e.g. "quorum").
    """

    timestamp: float
    task_id: str
    voter_model: str
    verdict: Literal["approve", "request_changes", "abstain"]
    confidence: float
    reasoning: str
    is_final: bool = False
    strategy: str = ""
```

- [ ] **Step 2: Commit**

```bash
git add src/bernstein/core/models.py
git commit -m "feat: add VoteEvent to models for voting lifecycle stream"
```

---

### Task 2: Create voting.py core types + VotingProtocol

**Files:**
- Create: `src/bernstein/core/voting.py`
- Create: `tests/unit/test_voting.py`

- [ ] **Step 1: Write failing tests first**

```python
# tests/unit/test_voting.py
"""Tests for the agent voting protocol."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import pytest

from bernstein.core.voting import (
    TieBreak,
    Vote,
    VoteEvent,
    VotingConfig,
    VotingProtocol,
    VotingResult,
    VotingStrategy,
)
from bernstein.core.models import Task


def _vote(verdict: str, confidence: float = 0.9, model: str = "m") -> Vote:
    return Vote(
        voter_model=model,
        verdict=verdict,  # type: ignore[arg-type]
        confidence=confidence,
        reasoning="test",
        timestamp=time.time(),
    )


def _make_task(id: str = "T-001") -> Task:
    return Task(id=id, title="test", description="test", role="backend")


class TestVotingStrategyMajority:
    def test_two_approve_one_reject(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.MAJORITY)
        protocol = VotingProtocol(config)
        votes = [_vote("approve"), _vote("approve"), _vote("request_changes")]
        result = protocol.tally(votes)
        assert result.final_verdict == "approve"

    def test_two_reject_one_approve(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.MAJORITY)
        protocol = VotingProtocol(config)
        votes = [_vote("request_changes"), _vote("request_changes"), _vote("approve")]
        result = protocol.tally(votes)
        assert result.final_verdict == "request_changes"

    def test_tie_defaults_to_reject(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.MAJORITY, tie_break=TieBreak.REJECT)
        protocol = VotingProtocol(config)
        votes = [_vote("approve"), _vote("request_changes")]
        result = protocol.tally(votes)
        assert result.final_verdict == "request_changes"

    def test_tie_accept(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.MAJORITY, tie_break=TieBreak.ACCEPT)
        protocol = VotingProtocol(config)
        votes = [_vote("approve"), _vote("request_changes")]
        result = protocol.tally(votes)
        assert result.final_verdict == "approve"

    def test_all_abstain_falls_back_to_tie_break(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.MAJORITY, tie_break=TieBreak.REJECT)
        protocol = VotingProtocol(config)
        votes = [_vote("abstain"), _vote("abstain")]
        result = protocol.tally(votes)
        assert result.final_verdict == "request_changes"


class TestVotingStrategyQuorum:
    def test_quorum_2_of_3_met(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=2, quorum_n=3)
        protocol = VotingProtocol(config)
        votes = [_vote("approve"), _vote("approve"), _vote("request_changes")]
        result = protocol.tally(votes)
        assert result.final_verdict == "approve"

    def test_quorum_2_of_3_not_met(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=2, quorum_n=3)
        protocol = VotingProtocol(config)
        votes = [_vote("approve"), _vote("request_changes"), _vote("request_changes")]
        result = protocol.tally(votes)
        assert result.final_verdict == "request_changes"

    def test_quorum_1_of_1_approve(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=1, quorum_n=1)
        protocol = VotingProtocol(config)
        votes = [_vote("approve")]
        result = protocol.tally(votes)
        assert result.final_verdict == "approve"

    def test_quorum_1_of_1_reject(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=1, quorum_n=1)
        protocol = VotingProtocol(config)
        votes = [_vote("request_changes")]
        result = protocol.tally(votes)
        assert result.final_verdict == "request_changes"

    def test_abstain_counts_against_quorum(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=2, quorum_n=3)
        protocol = VotingProtocol(config)
        votes = [_vote("approve"), _vote("abstain"), _vote("abstain")]
        result = protocol.tally(votes)
        assert result.final_verdict == "request_changes"


class TestVotingStrategyWeighted:
    def test_higher_confidence_approve_wins(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.WEIGHTED)
        protocol = VotingProtocol(config)
        votes = [_vote("approve", 0.9), _vote("request_changes", 0.3)]
        result = protocol.tally(votes)
        assert result.final_verdict == "approve"

    def test_higher_confidence_reject_wins(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.WEIGHTED)
        protocol = VotingProtocol(config)
        votes = [_vote("approve", 0.3), _vote("request_changes", 0.9)]
        result = protocol.tally(votes)
        assert result.final_verdict == "request_changes"

    def test_equal_weight_tie_break_reject(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.WEIGHTED, tie_break=TieBreak.REJECT)
        protocol = VotingProtocol(config)
        votes = [_vote("approve", 0.5), _vote("request_changes", 0.5)]
        result = protocol.tally(votes)
        assert result.final_verdict == "request_changes"


class TestVotingStrategyUnanimous:
    def test_all_approve(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.UNANIMOUS)
        protocol = VotingProtocol(config)
        votes = [_vote("approve"), _vote("approve"), _vote("approve")]
        result = protocol.tally(votes)
        assert result.final_verdict == "approve"

    def test_one_reject_fails(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.UNANIMOUS)
        protocol = VotingProtocol(config)
        votes = [_vote("approve"), _vote("approve"), _vote("request_changes")]
        result = protocol.tally(votes)
        assert result.final_verdict == "request_changes"

    def test_abstain_does_not_block_unanimous(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.UNANIMOUS)
        protocol = VotingProtocol(config)
        votes = [_vote("approve"), _vote("abstain")]
        result = protocol.tally(votes)
        assert result.final_verdict == "approve"

    def test_all_abstain_unanimous_is_reject(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.UNANIMOUS, tie_break=TieBreak.REJECT)
        protocol = VotingProtocol(config)
        votes = [_vote("abstain"), _vote("abstain")]
        result = protocol.tally(votes)
        assert result.final_verdict == "request_changes"


class TestAbstention:
    def test_low_confidence_vote_becomes_abstain(self) -> None:
        # When confidence < abstention_threshold the vote should be abstain
        config = VotingConfig(abstention_threshold=0.5)
        protocol = VotingProtocol(config)
        vote = protocol.maybe_abstain(_vote("approve", confidence=0.3))
        assert vote.verdict == "abstain"

    def test_high_confidence_vote_is_unchanged(self) -> None:
        config = VotingConfig(abstention_threshold=0.5)
        protocol = VotingProtocol(config)
        vote = protocol.maybe_abstain(_vote("approve", confidence=0.8))
        assert vote.verdict == "approve"


class TestVotingResult:
    def test_result_contains_all_votes(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=1, quorum_n=2)
        protocol = VotingProtocol(config)
        votes = [_vote("approve", model="m1"), _vote("request_changes", model="m2")]
        result = protocol.tally(votes)
        assert len(result.votes) == 2
        assert result.strategy == VotingStrategy.QUORUM

    def test_escalate_tie_break_sets_needs_escalation(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.MAJORITY, tie_break=TieBreak.ESCALATE)
        protocol = VotingProtocol(config)
        votes = [_vote("approve"), _vote("request_changes")]
        result = protocol.tally(votes)
        assert result.needs_escalation is True
        # Safe default during escalation
        assert result.final_verdict == "request_changes"


class TestCollectVotes:
    """Integration tests for VotingProtocol.collect_votes."""

    @pytest.mark.asyncio
    async def test_collect_votes_two_models(self, tmp_path: Path) -> None:
        from bernstein.core.cross_model_verifier import CrossModelVerifierConfig
        import json

        task = _make_task()
        config = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=2, quorum_n=2)
        protocol = VotingProtocol(config)
        verifier_cfg = CrossModelVerifierConfig()

        diff_result = MagicMock(stdout="+x\n")
        approve_json = json.dumps({"verdict": "approve", "feedback": "OK", "confidence": 0.9, "issues": []})

        with (
            patch("subprocess.run", return_value=diff_result),
            patch(
                "bernstein.core.voting.call_llm",
                new=AsyncMock(return_value=approve_json),
            ),
        ):
            result = await protocol.collect_votes(
                task=task,
                worktree_path=tmp_path,
                voter_models=["google/gemini-flash-1.5", "anthropic/claude-haiku-3-5"],
                verifier_cfg=verifier_cfg,
            )

        assert result.final_verdict == "approve"
        assert len(result.votes) == 2
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/sasha/IdeaProjects/personal_projects/bernstein/.sdd/worktrees/backend-25156e91
uv run pytest tests/unit/test_voting.py -x -q 2>&1 | head -30
```

Expected: ImportError — voting module does not exist yet.

- [ ] **Step 3: Create src/bernstein/core/voting.py**

```python
"""Agent voting protocol — configurable multi-model consensus for task verification.

VotingProtocol wraps one or more LLM reviewers into a single verdict using
configurable strategies: MAJORITY, QUORUM, WEIGHTED, or UNANIMOUS.

The existing cross-model verifier becomes a thin adapter over VotingProtocol
with QUORUM(1, 1), preserving full backward compatibility.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from bernstein.core.llm import call_llm

if TYPE_CHECKING:
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
        confidence: Self-reported confidence 0.0–1.0.
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
        votes: All votes cast (including abstentions).
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
                reasoning=f"Abstained: confidence {vote.confidence:.2f} < threshold {self._config.abstention_threshold:.2f}",
                timestamp=vote.timestamp,
            )
        return vote

    def tally(self, votes: list[Vote]) -> VotingResult:
        """Aggregate votes into a final verdict.

        Args:
            votes: All votes cast (abstentions included).

        Returns:
            VotingResult with final_verdict and metadata.
        """
        # Apply abstention threshold to each vote
        processed = [self.maybe_abstain(v) for v in votes]

        non_abstained = [v for v in processed if v.verdict != "abstain"]
        approvals = [v for v in non_abstained if v.verdict == "approve"]
        rejections = [v for v in non_abstained if v.verdict == "request_changes"]

        avg_confidence = (
            sum(v.confidence for v in non_abstained) / len(non_abstained)
            if non_abstained
            else 0.0
        )

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
        reasoning = (
            f"{strategy}: {a_count} approve, {r_count} reject, {abs_count} abstain → {verdict}"
        )

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
            return "request_changes", True  # safe default + flag
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
    # Async collection
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

        diff = _get_diff(worktree_path, task.owned_files)
        if len(diff) > verifier_cfg.max_diff_chars:
            diff = diff[: verifier_cfg.max_diff_chars] + "\n... (truncated)"
        prompt = _build_prompt(task, diff)

        votes: list[Vote] = []
        for model in voter_models:
            vote = await self._call_voter(model, prompt, verifier_cfg)
            votes.append(vote)

        result = self.tally(votes)
        logger.info(
            "voting: task=%s strategy=%s verdict=%s votes=%d",
            task.id,
            self._config.strategy,
            result.final_verdict,
            len(votes),
        )
        return result

    async def _call_voter(
        self,
        model: str,
        prompt: str,
        verifier_cfg: CrossModelVerifierConfig,
    ) -> Vote:
        """Invoke a single voter model and parse its Vote."""
        import json

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
        """Parse LLM response into a Vote, defaulting to abstain on failure."""
        import contextlib
        import json

        text = raw.strip()
        if text.startswith("```"):
            text = "\n".join(
                line for line in text.splitlines() if not line.strip().startswith("```")
            ).strip()

        data: dict[str, object] = {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}") + 1
            if start >= 0 and end > start:
                with contextlib.suppress(json.JSONDecodeError):
                    data = json.loads(text[start:end])

        if not data:
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
        confidence = max(0.0, min(1.0, confidence))  # clamp

        return Vote(
            voter_model=model,
            verdict=verdict,
            confidence=confidence,
            reasoning=str(data.get("feedback", data.get("reasoning", ""))),
        )
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/unit/test_voting.py -x -q 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/bernstein/core/voting.py tests/unit/test_voting.py
git commit -m "feat: add voting protocol — MAJORITY/QUORUM/WEIGHTED/UNANIMOUS with abstention and tie-breaking"
```

---

### Task 3: Refactor cross_model_verifier.py to use VotingProtocol

**Files:**
- Modify: `src/bernstein/core/cross_model_verifier.py`
- Modify: `tests/unit/test_cross_model_verifier.py` (add multi-voter tests)

- [ ] **Step 1: Add voting_config field to CrossModelVerifierConfig and update verify_with_cross_model**

Add `voting_config: VotingConfig | None = None` to `CrossModelVerifierConfig`.

Update `verify_with_cross_model` to:
1. If `config.voting_config` is provided, use `VotingProtocol.collect_votes` with the voter list from `voting_config`.
2. Otherwise, fall back to single-reviewer path (QUORUM 1-of-1) — backward compatible.

Convert `VotingResult` → `CrossModelVerdict` at the boundary.

- [ ] **Step 2: Add new tests to test_cross_model_verifier.py**

```python
class TestMultiVoterVerification:
    @pytest.mark.asyncio
    async def test_quorum_2_of_3_approve(self, tmp_path: Path) -> None:
        from bernstein.core.voting import VotingConfig, VotingStrategy

        task = _make_task()
        voting_cfg = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=2, quorum_n=3)
        config = CrossModelVerifierConfig(voting_config=voting_cfg)

        diff_response = MagicMock(stdout="+code\n")
        approve_json = json.dumps({"verdict": "approve", "feedback": "OK", "confidence": 0.9, "issues": []})

        with (
            patch("subprocess.run", return_value=diff_response),
            patch("bernstein.core.voting.call_llm", new=AsyncMock(return_value=approve_json)),
        ):
            verdict = await verify_with_cross_model(
                task, tmp_path, "claude-sonnet", config,
                voter_models=["google/gemini-flash-1.5", "anthropic/claude-haiku-3-5", "openai/gpt-4o-mini"],
            )

        assert verdict.verdict == "approve"

    @pytest.mark.asyncio
    async def test_single_voter_backward_compat(self, tmp_path: Path) -> None:
        """No voting_config → old QUORUM(1,1) behaviour."""
        task = _make_task()
        config = CrossModelVerifierConfig(enabled=True)
        diff_response = MagicMock(stdout="+x\n")
        approve_json = json.dumps({"verdict": "approve", "feedback": "Fine", "issues": []})

        with (
            patch("subprocess.run", return_value=diff_response),
            patch("bernstein.core.cross_model_verifier.call_llm", new=AsyncMock(return_value=approve_json)),
        ):
            verdict = await verify_with_cross_model(task, tmp_path, "claude-sonnet", config)

        assert verdict.verdict == "approve"
```

- [ ] **Step 3: Run full test suite for affected files**

```bash
uv run pytest tests/unit/test_voting.py tests/unit/test_cross_model_verifier.py -x -q
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/bernstein/core/cross_model_verifier.py tests/unit/test_cross_model_verifier.py
git commit -m "feat: refactor cross_model_verifier to use VotingProtocol; backward-compatible QUORUM(1,1) default"
```

---

### Task 4: Emit VoteEvents into lifecycle stream

**Files:**
- Modify: `src/bernstein/core/voting.py` (emit events in `collect_votes`)

- [ ] **Step 1: Import and emit VoteEvent in collect_votes**

After collecting all votes and calling `tally()`, emit:
1. One `VoteEvent(is_final=False)` per cast vote
2. One `VoteEvent(is_final=True)` for the final result

Use `bernstein.core.lifecycle._emit` indirectly by importing `LifecycleEvent` — or better, define a separate listener pattern. Since `VoteEvent` is not a `LifecycleEvent`, emit it via a module-level `_vote_listeners` list analogous to lifecycle's `_listeners`.

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/unit/test_voting.py -x -q
```

- [ ] **Step 3: Commit**

```bash
git add src/bernstein/core/voting.py
git commit -m "feat: emit VoteEvents into lifecycle stream from VotingProtocol"
```
