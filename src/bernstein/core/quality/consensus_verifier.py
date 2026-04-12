"""Multi-agent consensus verification — N independent reviewers for critical tasks.

For security fixes, data migrations, and API changes, spawn N verifier agents
(default 2) that independently review the producing agent's output.  Merge only
if >50% of verifiers approve.  Uses different model providers for each verifier
to avoid correlated errors.

This is the nuclear option for correctness.  It wraps the existing
:class:`~bernstein.core.voting.VotingProtocol` with an opinionated configuration
that selects diverse reviewer models and applies a strict majority threshold.

Typical usage::

    config = ConsensusVerifierConfig(n_verifiers=2)
    verdict = await verify_with_consensus(
        task=task,
        worktree_path=path,
        writer_model="anthropic/claude-sonnet-4",
        config=config,
    )
    if verdict.verdict == "request_changes":
        # Block merge, open fix task
        ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.cross_model_verifier import (
    CrossModelVerdict,
    CrossModelVerifierConfig,
    verify_with_cross_model,
)
from bernstein.core.voting import TieBreak, VotingConfig, VotingStrategy

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Diverse reviewer pool (one entry per provider family)
# ---------------------------------------------------------------------------

#: Candidate reviewer models from different providers.  Ordered cheapest-first.
#: The pool is intentionally broad so that when the writer is from one family
#: we can still assemble N reviewers from unrelated families.
_REVIEWER_POOL: list[tuple[str, str]] = [
    # (provider_family, openrouter_model_id)
    ("google", "google/gemini-flash-1.5"),
    ("anthropic", "anthropic/claude-haiku-4-5-20251001"),
    ("openai", "openai/gpt-5.4-mini"),
    ("meta", "meta-llama/llama-3.3-70b-instruct"),
    ("qwen", "qwen/qwen-2.5-coder-7b-instruct"),
    ("mistral", "mistralai/mistral-7b-instruct"),
]

#: Writer model prefix → provider family string.
_WRITER_FAMILY: dict[str, str] = {
    "anthropic": "anthropic",
    "claude": "anthropic",
    "google": "google",
    "gemini": "google",
    "openai": "openai",
    "gpt": "openai",
    "codex": "openai",
    "meta": "meta",
    "llama": "meta",
    "qwen": "qwen",
    "mistral": "mistral",
}


def _writer_family(writer_model: str) -> str:
    """Return the provider family string for *writer_model*.

    Args:
        writer_model: Model identifier string (e.g. ``"anthropic/claude-sonnet-4"``).

    Returns:
        Provider family string, or ``""`` if unknown.
    """
    lower = writer_model.lower()
    for prefix, family in _WRITER_FAMILY.items():
        if prefix in lower:
            return family
    return ""


def select_diverse_verifier_models(writer_model: str, n: int) -> list[str]:
    """Select *n* reviewer models from providers different from the writer.

    Prioritises diversity — no two selected reviewers share a provider family.
    If the pool is too small to fill *n* distinct families after excluding the
    writer's family, reviewers from already-used families are added as fallback.

    Args:
        writer_model: Model identifier used by the writing agent.
        n: Number of verifier models to select.

    Returns:
        List of *n* OpenRouter model identifiers.

    Example::

        models = select_diverse_verifier_models("anthropic/claude-sonnet-4", 2)
        # → ["google/gemini-flash-1.5", "openai/gpt-5.4-mini"]
    """
    excluded_family = _writer_family(writer_model)

    # First pass: prefer models from different families, exclude writer family
    selected: list[str] = []
    used_families: set[str] = set()
    for family, model in _REVIEWER_POOL:
        if len(selected) >= n:
            break
        if family == excluded_family:
            continue
        if family in used_families:
            continue
        selected.append(model)
        used_families.add(family)

    # Second pass: fill remaining slots from any family (including excluded)
    if len(selected) < n:
        for _family, model in _REVIEWER_POOL:
            if len(selected) >= n:
                break
            if model not in selected:
                selected.append(model)

    return selected[:n]


def build_consensus_voting_config(n: int) -> VotingConfig:
    """Build a :class:`~bernstein.core.voting.VotingConfig` for strict majority.

    ">50% approve" translates to:
    - MAJORITY strategy (more approvals than rejections wins)
    - REJECT tie-break (equal approve/reject counts → reject, preserving safety)

    For n=2 this means unanimous approval is required (any single rejection is
    a tie, resolved as reject).  For n=3, 2-of-3 suffices.

    Args:
        n: Total number of verifier votes expected.

    Returns:
        :class:`~bernstein.core.voting.VotingConfig` suitable for consensus.
    """
    return VotingConfig(
        strategy=VotingStrategy.MAJORITY,
        quorum_k=n,
        quorum_n=n,
        tie_break=TieBreak.REJECT,
        abstention_threshold=0.3,
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsensusVerifierConfig:
    """Configuration for multi-agent consensus verification.

    Attributes:
        n_verifiers: Number of independent verifier models to use (default 2).
        max_diff_chars: Truncate diffs at this length for cost control.
        max_tokens: Token cap per reviewer response.
        provider: LLM provider key (passed through to call_llm).
        block_on_reject: When True a ``request_changes`` consensus blocks merge.
    """

    n_verifiers: int = 2
    max_diff_chars: int = 12_000
    max_tokens: int = 512
    provider: str = "openrouter"
    block_on_reject: bool = True
    _voter_models: list[str] = field(default_factory=list[str])

    def voter_models_for(self, writer_model: str) -> list[str]:
        """Return the voter model list, auto-selecting if not pre-configured.

        Args:
            writer_model: The model that wrote the code under review.

        Returns:
            List of *n_verifiers* model identifiers.
        """
        if self._voter_models:
            return list(self._voter_models)
        return select_diverse_verifier_models(writer_model, self.n_verifiers)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def verify_with_consensus(
    task: Task,
    worktree_path: Path,
    writer_model: str,
    config: ConsensusVerifierConfig | None = None,
) -> CrossModelVerdict:
    """Run N-model consensus verification on a completed task's diff.

    Selects *n_verifiers* diverse reviewer models (different providers from the
    writer), casts independent votes via
    :class:`~bernstein.core.voting.VotingProtocol`, and returns an ``approve``
    verdict only when the strict majority threshold is met.

    On LLM failure, individual failed voters abstain; the threshold still
    applies to the remaining valid votes.

    Args:
        task: Completed task under review.
        worktree_path: Git worktree path for diff extraction.
        writer_model: Model identifier that wrote the code under review.
        config: Consensus configuration; defaults to
            :class:`ConsensusVerifierConfig` with ``n_verifiers=2``.

    Returns:
        :class:`~bernstein.core.cross_model_verifier.CrossModelVerdict` with
        ``approve`` or ``request_changes`` decision.
    """
    if config is None:
        config = ConsensusVerifierConfig()

    voter_models = config.voter_models_for(writer_model)
    voting_cfg = build_consensus_voting_config(len(voter_models))

    verifier_cfg = CrossModelVerifierConfig(
        enabled=True,
        provider=config.provider,
        max_diff_chars=config.max_diff_chars,
        max_tokens=config.max_tokens,
        block_on_issues=config.block_on_reject,
        voting_config=voting_cfg,
    )

    logger.info(
        "consensus_verifier: task=%s writer=%s voters=%s",
        task.id,
        writer_model,
        voter_models,
    )

    verdict = await verify_with_cross_model(
        task=task,
        worktree_path=worktree_path,
        writer_model=writer_model,
        config=verifier_cfg,
        voter_models=voter_models,
    )

    logger.info(
        "consensus_verifier: task=%s verdict=%s voters=%d",
        task.id,
        verdict.verdict,
        len(voter_models),
    )
    return verdict
