"""Tests for the agent voting protocol."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bernstein.core.models import Task, VoteEvent
from bernstein.core.voting import (
    TieBreak,
    Vote,
    VotingConfig,
    VotingProtocol,
    VotingStrategy,
    _vote_listeners,
    add_vote_listener,
    remove_vote_listener,
)

if TYPE_CHECKING:
    from pathlib import Path


def _vote(
    verdict: str,
    confidence: float = 0.9,
    model: str = "m",
) -> Vote:
    return Vote(
        voter_model=model,
        verdict=verdict,  # type: ignore[arg-type]
        confidence=confidence,
        reasoning="test",
        timestamp=time.time(),
    )


def _make_task(id: str = "T-001") -> Task:
    return Task(id=id, title="test task", description="desc", role="backend")


# ---------------------------------------------------------------------------
# VotingStrategy.MAJORITY
# ---------------------------------------------------------------------------


class TestMajority:
    def test_two_approve_one_reject(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.MAJORITY))
        result = protocol.tally([_vote("approve"), _vote("approve"), _vote("request_changes")])
        assert result.final_verdict == "approve"

    def test_two_reject_one_approve(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.MAJORITY))
        result = protocol.tally([_vote("request_changes"), _vote("request_changes"), _vote("approve")])
        assert result.final_verdict == "request_changes"

    def test_tie_defaults_to_reject(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.MAJORITY, tie_break=TieBreak.REJECT))
        result = protocol.tally([_vote("approve"), _vote("request_changes")])
        assert result.final_verdict == "request_changes"
        assert not result.needs_escalation

    def test_tie_accept(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.MAJORITY, tie_break=TieBreak.ACCEPT))
        result = protocol.tally([_vote("approve"), _vote("request_changes")])
        assert result.final_verdict == "approve"

    def test_tie_escalate_sets_flag_and_safe_default(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.MAJORITY, tie_break=TieBreak.ESCALATE))
        result = protocol.tally([_vote("approve"), _vote("request_changes")])
        assert result.final_verdict == "request_changes"
        assert result.needs_escalation is True

    def test_all_abstain_falls_back_to_tie_break_reject(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.MAJORITY, tie_break=TieBreak.REJECT))
        result = protocol.tally([_vote("abstain"), _vote("abstain")])
        assert result.final_verdict == "request_changes"


# ---------------------------------------------------------------------------
# VotingStrategy.QUORUM
# ---------------------------------------------------------------------------


class TestQuorum:
    def test_2_of_3_met(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=2, quorum_n=3))
        result = protocol.tally([_vote("approve"), _vote("approve"), _vote("request_changes")])
        assert result.final_verdict == "approve"

    def test_2_of_3_not_met(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=2, quorum_n=3))
        result = protocol.tally([_vote("approve"), _vote("request_changes"), _vote("request_changes")])
        assert result.final_verdict == "request_changes"

    def test_1_of_1_approve(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=1, quorum_n=1))
        result = protocol.tally([_vote("approve")])
        assert result.final_verdict == "approve"

    def test_1_of_1_reject(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=1, quorum_n=1))
        result = protocol.tally([_vote("request_changes")])
        assert result.final_verdict == "request_changes"

    def test_abstain_does_not_count_toward_quorum(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=2, quorum_n=3))
        # Only 1 approve even though abstains are present
        result = protocol.tally([_vote("approve"), _vote("abstain"), _vote("abstain")])
        assert result.final_verdict == "request_changes"

    def test_exactly_k_approvals_meets_quorum(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=3, quorum_n=3))
        result = protocol.tally([_vote("approve"), _vote("approve"), _vote("approve")])
        assert result.final_verdict == "approve"


# ---------------------------------------------------------------------------
# VotingStrategy.WEIGHTED
# ---------------------------------------------------------------------------


class TestWeighted:
    def test_higher_confidence_approve_wins(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.WEIGHTED))
        result = protocol.tally([_vote("approve", 0.9), _vote("request_changes", 0.3)])
        assert result.final_verdict == "approve"

    def test_higher_confidence_reject_wins(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.WEIGHTED))
        result = protocol.tally([_vote("approve", 0.3), _vote("request_changes", 0.9)])
        assert result.final_verdict == "request_changes"

    def test_equal_weight_tie_break_reject(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.WEIGHTED, tie_break=TieBreak.REJECT))
        result = protocol.tally([_vote("approve", 0.5), _vote("request_changes", 0.5)])
        assert result.final_verdict == "request_changes"

    def test_equal_weight_tie_break_accept(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.WEIGHTED, tie_break=TieBreak.ACCEPT))
        result = protocol.tally([_vote("approve", 0.5), _vote("request_changes", 0.5)])
        assert result.final_verdict == "approve"


# ---------------------------------------------------------------------------
# VotingStrategy.UNANIMOUS
# ---------------------------------------------------------------------------


class TestUnanimous:
    def test_all_approve(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.UNANIMOUS))
        result = protocol.tally([_vote("approve"), _vote("approve"), _vote("approve")])
        assert result.final_verdict == "approve"

    def test_one_reject_fails(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.UNANIMOUS))
        result = protocol.tally([_vote("approve"), _vote("approve"), _vote("request_changes")])
        assert result.final_verdict == "request_changes"

    def test_abstain_does_not_block_unanimous(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.UNANIMOUS))
        result = protocol.tally([_vote("approve"), _vote("abstain")])
        assert result.final_verdict == "approve"

    def test_all_abstain_uses_tie_break_reject(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.UNANIMOUS, tie_break=TieBreak.REJECT))
        result = protocol.tally([_vote("abstain"), _vote("abstain")])
        assert result.final_verdict == "request_changes"

    def test_all_abstain_uses_tie_break_accept(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.UNANIMOUS, tie_break=TieBreak.ACCEPT))
        result = protocol.tally([_vote("abstain"), _vote("abstain")])
        assert result.final_verdict == "approve"


# ---------------------------------------------------------------------------
# Abstention threshold
# ---------------------------------------------------------------------------


class TestAbstention:
    def test_low_confidence_vote_becomes_abstain(self) -> None:
        config = VotingConfig(abstention_threshold=0.5)
        protocol = VotingProtocol(config)
        vote = protocol.maybe_abstain(_vote("approve", confidence=0.3))
        assert vote.verdict == "abstain"

    def test_high_confidence_vote_is_unchanged(self) -> None:
        config = VotingConfig(abstention_threshold=0.5)
        protocol = VotingProtocol(config)
        vote = protocol.maybe_abstain(_vote("approve", confidence=0.8))
        assert vote.verdict == "approve"

    def test_exactly_at_threshold_is_not_abstain(self) -> None:
        # Strict less-than: confidence == threshold means it is NOT abstained
        config = VotingConfig(abstention_threshold=0.5)
        protocol = VotingProtocol(config)
        vote = protocol.maybe_abstain(_vote("approve", confidence=0.5))
        assert vote.verdict == "approve"

    def test_tally_applies_abstention_threshold_automatically(self) -> None:
        # Low-confidence approve should be treated as abstain → quorum not met
        config = VotingConfig(
            strategy=VotingStrategy.QUORUM,
            quorum_k=1,
            quorum_n=1,
            abstention_threshold=0.5,
        )
        protocol = VotingProtocol(config)
        result = protocol.tally([_vote("approve", confidence=0.2)])
        # After abstention: 0 approvals → quorum not met
        assert result.final_verdict == "request_changes"


# ---------------------------------------------------------------------------
# VotingResult structure
# ---------------------------------------------------------------------------


class TestVotingResult:
    def test_result_contains_all_votes(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=1, quorum_n=2)
        protocol = VotingProtocol(config)
        votes = [_vote("approve", model="m1"), _vote("request_changes", model="m2")]
        result = protocol.tally(votes)
        assert len(result.votes) == 2
        assert result.strategy == VotingStrategy.QUORUM

    def test_confidence_is_mean_of_non_abstained(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.MAJORITY)
        protocol = VotingProtocol(config)
        # abstain threshold is 0.3 by default; 0.8 and 0.6 are above it
        votes = [_vote("approve", 0.8), _vote("request_changes", 0.6)]
        result = protocol.tally(votes)
        assert abs(result.confidence - 0.7) < 1e-9

    def test_confidence_zero_when_all_abstain(self) -> None:
        config = VotingConfig(strategy=VotingStrategy.MAJORITY)
        protocol = VotingProtocol(config)
        result = protocol.tally([_vote("abstain")])
        assert result.confidence == pytest.approx(0.0)

    def test_reasoning_contains_strategy_and_verdict(self) -> None:
        protocol = VotingProtocol(VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=1))
        result = protocol.tally([_vote("approve")])
        assert "quorum" in result.reasoning.lower()
        assert "approve" in result.reasoning


# ---------------------------------------------------------------------------
# Vote event listeners
# ---------------------------------------------------------------------------


class TestVoteEventListeners:
    def test_add_and_remove_listener(self) -> None:
        events: list[object] = []
        add_vote_listener(events.append)
        assert events.append in _vote_listeners
        remove_vote_listener(events.append)
        assert events.append not in _vote_listeners

    def test_remove_nonexistent_listener_is_noop(self) -> None:
        remove_vote_listener(lambda e: None)  # should not raise


# ---------------------------------------------------------------------------
# VotingProtocol._parse_vote
# ---------------------------------------------------------------------------


class TestParseVote:
    def _protocol(self) -> VotingProtocol:
        return VotingProtocol(VotingConfig())

    def test_parse_approve(self) -> None:
        protocol = self._protocol()
        raw = json.dumps({"verdict": "approve", "feedback": "Looks good.", "confidence": 0.95})
        vote = protocol._parse_vote(raw, "m")
        assert vote.verdict == "approve"
        assert vote.confidence == pytest.approx(0.95)
        assert vote.reasoning == "Looks good."

    def test_parse_request_changes(self) -> None:
        protocol = self._protocol()
        raw = json.dumps({"verdict": "request_changes", "feedback": "Bug.", "confidence": 0.8})
        vote = protocol._parse_vote(raw, "m")
        assert vote.verdict == "request_changes"

    def test_unknown_verdict_becomes_abstain(self) -> None:
        protocol = self._protocol()
        raw = json.dumps({"verdict": "UNKNOWN", "feedback": "?", "confidence": 0.5})
        vote = protocol._parse_vote(raw, "m")
        assert vote.verdict == "abstain"

    def test_invalid_json_becomes_abstain(self) -> None:
        protocol = self._protocol()
        vote = protocol._parse_vote("not json at all", "m")
        assert vote.verdict == "abstain"
        assert vote.confidence == pytest.approx(0.0)

    def test_confidence_clamped_above_1(self) -> None:
        protocol = self._protocol()
        raw = json.dumps({"verdict": "approve", "feedback": "ok", "confidence": 9.9})
        vote = protocol._parse_vote(raw, "m")
        assert vote.confidence == pytest.approx(1.0)

    def test_confidence_clamped_below_0(self) -> None:
        protocol = self._protocol()
        raw = json.dumps({"verdict": "approve", "feedback": "ok", "confidence": -1.5})
        vote = protocol._parse_vote(raw, "m")
        assert vote.confidence == pytest.approx(0.0)

    def test_strips_markdown_fences(self) -> None:
        protocol = self._protocol()
        inner = json.dumps({"verdict": "approve", "feedback": "ok", "confidence": 0.9})
        raw = f"```json\n{inner}\n```"
        vote = protocol._parse_vote(raw, "m")
        assert vote.verdict == "approve"

    def test_extracts_json_from_surrounding_text(self) -> None:
        protocol = self._protocol()
        inner = json.dumps({"verdict": "approve", "feedback": "fine", "confidence": 0.85})
        raw = f"Here is my review:\n{inner}\nEnd of review."
        vote = protocol._parse_vote(raw, "m")
        assert vote.verdict == "approve"

    def test_uses_reasoning_key_as_fallback_for_feedback(self) -> None:
        protocol = self._protocol()
        raw = json.dumps({"verdict": "approve", "reasoning": "Looks correct.", "confidence": 0.8})
        vote = protocol._parse_vote(raw, "m")
        assert vote.reasoning == "Looks correct."


# ---------------------------------------------------------------------------
# VotingProtocol.collect_votes (async)
# ---------------------------------------------------------------------------


class TestCollectVotes:
    @pytest.mark.asyncio
    async def test_two_voters_both_approve(self, tmp_path: Path) -> None:
        from bernstein.core.cross_model_verifier import CrossModelVerifierConfig

        task = _make_task()
        config = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=2, quorum_n=2)
        protocol = VotingProtocol(config)
        verifier_cfg = CrossModelVerifierConfig()

        diff_result = MagicMock(stdout="+x\n")
        approve_json = json.dumps({"verdict": "approve", "feedback": "OK", "confidence": 0.9})

        with (
            patch("subprocess.run", return_value=diff_result),
            patch("bernstein.core.voting.call_llm", new=AsyncMock(return_value=approve_json)),
        ):
            result = await protocol.collect_votes(
                task=task,
                worktree_path=tmp_path,
                voter_models=["google/gemini-flash-1.5", "anthropic/claude-haiku-4-5-20251001"],
                verifier_cfg=verifier_cfg,
            )

        assert result.final_verdict == "approve"
        assert len(result.votes) == 2

    @pytest.mark.asyncio
    async def test_voter_llm_failure_becomes_abstain(self, tmp_path: Path) -> None:
        from bernstein.core.cross_model_verifier import CrossModelVerifierConfig

        task = _make_task()
        config = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=1, quorum_n=1)
        protocol = VotingProtocol(config)
        verifier_cfg = CrossModelVerifierConfig()

        diff_result = MagicMock(stdout="+x\n")

        with (
            patch("subprocess.run", return_value=diff_result),
            patch(
                "bernstein.core.voting.call_llm",
                new=AsyncMock(side_effect=RuntimeError("API down")),
            ),
        ):
            result = await protocol.collect_votes(
                task=task,
                worktree_path=tmp_path,
                voter_models=["some/model"],
                verifier_cfg=verifier_cfg,
            )

        # LLM failure → abstain → quorum not met → request_changes
        assert result.final_verdict == "request_changes"
        assert result.votes[0].verdict == "abstain"

    @pytest.mark.asyncio
    async def test_vote_events_emitted(self, tmp_path: Path) -> None:
        from bernstein.core.cross_model_verifier import CrossModelVerifierConfig

        task = _make_task()
        config = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=1, quorum_n=1)
        protocol = VotingProtocol(config)
        verifier_cfg = CrossModelVerifierConfig()

        diff_result = MagicMock(stdout="+x\n")
        approve_json = json.dumps({"verdict": "approve", "feedback": "OK", "confidence": 0.9})

        emitted: list[VoteEvent] = []
        add_vote_listener(emitted.append)
        try:
            with (
                patch("subprocess.run", return_value=diff_result),
                patch("bernstein.core.voting.call_llm", new=AsyncMock(return_value=approve_json)),
            ):
                await protocol.collect_votes(
                    task=task,
                    worktree_path=tmp_path,
                    voter_models=["some/model"],
                    verifier_cfg=verifier_cfg,
                )
        finally:
            remove_vote_listener(emitted.append)

        # 1 per-vote event + 1 final event
        assert len(emitted) == 2
        assert emitted[0].is_final is False
        assert emitted[1].is_final is True
        assert emitted[1].verdict == "approve"

    @pytest.mark.asyncio
    async def test_diff_truncated(self, tmp_path: Path) -> None:
        from bernstein.core.cross_model_verifier import CrossModelVerifierConfig

        task = _make_task()
        config = VotingConfig(strategy=VotingStrategy.QUORUM, quorum_k=1, quorum_n=1)
        protocol = VotingProtocol(config)
        verifier_cfg = CrossModelVerifierConfig(max_diff_chars=10)

        long_diff = "+" + "x" * 200
        diff_result = MagicMock(stdout=long_diff)
        approve_json = json.dumps({"verdict": "approve", "feedback": "OK", "confidence": 0.9})

        captured: list[str] = []

        async def fake_llm(prompt: str, **kwargs: object) -> str:
            captured.append(prompt)
            return approve_json

        with (
            patch("subprocess.run", return_value=diff_result),
            patch("bernstein.core.voting.call_llm", new=fake_llm),
        ):
            await protocol.collect_votes(
                task=task,
                worktree_path=tmp_path,
                voter_models=["some/model"],
                verifier_cfg=verifier_cfg,
            )

        assert "(truncated)" in captured[0]
