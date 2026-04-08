"""Tests for multi-agent consensus verification."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bernstein.core.consensus_verifier import (
    ConsensusVerifierConfig,
    _writer_family,
    build_consensus_voting_config,
    select_diverse_verifier_models,
    verify_with_consensus,
)
from bernstein.core.models import Task
from bernstein.core.voting import VotingStrategy

if TYPE_CHECKING:
    from pathlib import Path


def _make_task(id: str = "T-001") -> Task:
    return Task(id=id, title="critical security fix", description="patch SQL injection", role="security")


# ---------------------------------------------------------------------------
# _writer_family
# ---------------------------------------------------------------------------


class TestWriterFamily:
    def test_claude_is_anthropic(self) -> None:
        assert _writer_family("anthropic/claude-sonnet-4") == "anthropic"

    def test_gemini_is_google(self) -> None:
        assert _writer_family("google/gemini-flash-2") == "google"

    def test_gpt_is_openai(self) -> None:
        assert _writer_family("openai/gpt-4o-mini") == "openai"

    def test_unknown_returns_empty(self) -> None:
        assert _writer_family("some/obscure-model-xyz") == ""

    def test_case_insensitive(self) -> None:
        assert _writer_family("ANTHROPIC/Claude") == "anthropic"


# ---------------------------------------------------------------------------
# select_diverse_verifier_models
# ---------------------------------------------------------------------------


class TestSelectDiverseVerifierModels:
    def test_returns_n_models(self) -> None:
        models = select_diverse_verifier_models("anthropic/claude-sonnet", 3)
        assert len(models) == 3

    def test_excludes_writer_family(self) -> None:
        models = select_diverse_verifier_models("anthropic/claude-sonnet", 2)
        for m in models:
            assert "anthropic" not in m.lower() or "claude" not in m.lower()

    def test_excludes_gemini_writer(self) -> None:
        models = select_diverse_verifier_models("google/gemini-flash-1.5", 2)
        for m in models:
            assert "gemini" not in m.lower() and "google" not in m.lower()

    def test_no_duplicates(self) -> None:
        models = select_diverse_verifier_models("anthropic/claude", 4)
        assert len(set(models)) == len(models)

    def test_falls_back_when_pool_exhausted(self) -> None:
        # Requesting more than pool size — returns as many as the pool has, no error
        from bernstein.core.consensus_verifier import _REVIEWER_POOL

        models = select_diverse_verifier_models("anthropic/claude", 10)
        assert len(models) == len(_REVIEWER_POOL)  # capped at pool size

    def test_n_1_returns_single_model(self) -> None:
        models = select_diverse_verifier_models("openai/gpt-4o", 1)
        assert len(models) == 1

    def test_unknown_writer_uses_full_pool(self) -> None:
        models = select_diverse_verifier_models("some/unknown-model", 2)
        assert len(models) == 2


# ---------------------------------------------------------------------------
# build_consensus_voting_config
# ---------------------------------------------------------------------------


class TestBuildConsensusVotingConfig:
    def test_uses_majority_strategy(self) -> None:
        config = build_consensus_voting_config(2)
        assert config.strategy == VotingStrategy.MAJORITY

    def test_tie_break_is_reject(self) -> None:
        from bernstein.core.voting import TieBreak

        config = build_consensus_voting_config(3)
        assert config.tie_break == TieBreak.REJECT

    def test_various_n(self) -> None:
        for n in (1, 2, 3, 5):
            cfg = build_consensus_voting_config(n)
            assert cfg.quorum_n == n


# ---------------------------------------------------------------------------
# ConsensusVerifierConfig
# ---------------------------------------------------------------------------


class TestConsensusVerifierConfig:
    def test_defaults(self) -> None:
        cfg = ConsensusVerifierConfig()
        assert cfg.n_verifiers == 2
        assert cfg.block_on_reject is True

    def test_voter_models_for_auto_selects(self) -> None:
        cfg = ConsensusVerifierConfig(n_verifiers=3)
        models = cfg.voter_models_for("anthropic/claude-sonnet")
        assert len(models) == 3

    def test_voter_models_for_uses_override(self) -> None:
        cfg = ConsensusVerifierConfig(_voter_models=["m1", "m2"])
        assert cfg.voter_models_for("any-model") == ["m1", "m2"]


# ---------------------------------------------------------------------------
# verify_with_consensus (async integration)
# ---------------------------------------------------------------------------


class TestVerifyWithConsensus:
    @pytest.mark.asyncio
    async def test_both_approve_returns_approve(self, tmp_path: Path) -> None:
        task = _make_task()
        approve_json = json.dumps({"verdict": "approve", "feedback": "OK", "confidence": 0.9})
        diff_result = MagicMock(stdout="+patch\n")

        with (
            patch("subprocess.run", return_value=diff_result),
            patch("bernstein.core.voting.call_llm", new=AsyncMock(return_value=approve_json)),
        ):
            verdict = await verify_with_consensus(
                task=task,
                worktree_path=tmp_path,
                writer_model="anthropic/claude-sonnet",
            )

        assert verdict.verdict == "approve"

    @pytest.mark.asyncio
    async def test_one_reject_returns_request_changes(self, tmp_path: Path) -> None:
        task = _make_task()
        approve_json = json.dumps({"verdict": "approve", "feedback": "OK", "confidence": 0.9})
        reject_json = json.dumps({"verdict": "request_changes", "feedback": "SQL injection risk", "confidence": 0.85})
        diff_result = MagicMock(stdout="+bad code\n")

        call_count = 0

        async def alternating(*args: object, **kwargs: object) -> str:
            nonlocal call_count
            call_count += 1
            return approve_json if call_count == 1 else reject_json

        with (
            patch("subprocess.run", return_value=diff_result),
            patch("bernstein.core.voting.call_llm", new=alternating),
        ):
            verdict = await verify_with_consensus(
                task=task,
                worktree_path=tmp_path,
                writer_model="anthropic/claude-sonnet",
            )

        # With n=2 and MAJORITY + REJECT tie-break:
        # 1 approve vs 1 reject → tie → reject
        assert verdict.verdict == "request_changes"

    @pytest.mark.asyncio
    async def test_all_approve_with_n3(self, tmp_path: Path) -> None:
        task = _make_task()
        approve_json = json.dumps({"verdict": "approve", "feedback": "OK", "confidence": 0.9})
        diff_result = MagicMock(stdout="+x\n")
        cfg = ConsensusVerifierConfig(n_verifiers=3)

        with (
            patch("subprocess.run", return_value=diff_result),
            patch("bernstein.core.voting.call_llm", new=AsyncMock(return_value=approve_json)),
        ):
            verdict = await verify_with_consensus(
                task=task,
                worktree_path=tmp_path,
                writer_model="anthropic/claude-sonnet",
                config=cfg,
            )

        assert verdict.verdict == "approve"

    @pytest.mark.asyncio
    async def test_two_reject_one_approve_with_n3(self, tmp_path: Path) -> None:
        task = _make_task()
        approve_json = json.dumps({"verdict": "approve", "feedback": "OK", "confidence": 0.9})
        reject_json = json.dumps({"verdict": "request_changes", "feedback": "Bug found", "confidence": 0.9})
        diff_result = MagicMock(stdout="+x\n")
        cfg = ConsensusVerifierConfig(n_verifiers=3)

        call_count = 0

        async def mostly_reject(*args: object, **kwargs: object) -> str:
            nonlocal call_count
            call_count += 1
            return approve_json if call_count == 1 else reject_json

        with (
            patch("subprocess.run", return_value=diff_result),
            patch("bernstein.core.voting.call_llm", new=mostly_reject),
        ):
            verdict = await verify_with_consensus(
                task=task,
                worktree_path=tmp_path,
                writer_model="anthropic/claude-sonnet",
                config=cfg,
            )

        assert verdict.verdict == "request_changes"

    @pytest.mark.asyncio
    async def test_default_config_used_when_none(self, tmp_path: Path) -> None:
        task = _make_task()
        approve_json = json.dumps({"verdict": "approve", "feedback": "Fine", "confidence": 0.9})
        diff_result = MagicMock(stdout="+x\n")

        with (
            patch("subprocess.run", return_value=diff_result),
            patch("bernstein.core.voting.call_llm", new=AsyncMock(return_value=approve_json)),
        ):
            verdict = await verify_with_consensus(
                task=task,
                worktree_path=tmp_path,
                writer_model="anthropic/claude-sonnet",
                config=None,
            )

        assert verdict.verdict in ("approve", "request_changes")
