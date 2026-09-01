"""Tests for the DecompositionEmitter policy.

These tests verify:
- Emission fires when threshold is met
- Duplicate prevention via seen_digests
- No emission below threshold
- No emission without issue_number
"""

from __future__ import annotations

from bernstein.core.orchestration.decomposition_emitter import DecompositionEmitter
from bernstein.core.orchestration.decomposition_proposal import DecompositionProposal
from bernstein.core.tasks.models import Task, TaskStatus


class TestDecompositionEmitter:
    """Test the DecompositionEmitter policy."""

    def test_emit_fires_at_threshold(self) -> None:
        """Emission should fire when propose_count terminal tasks exist."""
        emitter = DecompositionEmitter(propose_count=2)

        task1 = Task(
            id="task-1",
            title="Test task 1",
            role="backend",
            description="Test task 1 description",
            status=TaskStatus.FAILED,
            retry_count=3,
            max_retries=3,
            result_summary="test timeout",
            terminal_reason="retry_exhausted",
            metadata={"issue_number": 42, "repo": "owner/repo"},
        )

        task2 = Task(
            id="task-2",
            title="Test task 2",
            role="backend",
            description="Test task 2 description",
            status=TaskStatus.FAILED,
            retry_count=3,
            max_retries=3,
            result_summary="compilation error",
            terminal_reason="retry_exhausted",
            metadata={"issue_number": 42, "repo": "owner/repo"},
        )

        # Initially no emission
        assert emitter.check_exhaustion(task1) is None
        assert emitter.check_exhaustion(task2) is not None

    def test_dedup_prevents_re_emission(self) -> None:
        """Same evidence set should not cause duplicate proposals."""
        emitter = DecompositionEmitter(propose_count=2)

        task1 = Task(
            id="task-1",
            title="Test task 1",
            role="backend",
            description="Test task 1 description",
            status=TaskStatus.FAILED,
            retry_count=3,
            max_retries=3,
            result_summary="test timeout",
            terminal_reason="retry_exhausted",
            metadata={"issue_number": 42, "repo": "owner/repo"},
        )

        task2 = Task(
            id="task-2",
            title="Test task 2",
            role="backend",
            description="Test task 2 description",
            status=TaskStatus.FAILED,
            retry_count=3,
            max_retries=3,
            result_summary="compilation error",
            terminal_reason="retry_exhausted",
            metadata={"issue_number": 42, "repo": "owner/repo"},
        )

        # First task alone - no emission (below threshold)
        assert emitter.check_exhaustion(task1) is None

        # Second task reaches threshold - first emission
        proposal1 = emitter.check_exhaustion(task2)
        assert proposal1 is not None

        # Re-checking the same tasks should not emit again (dedup)
        proposal2 = emitter.check_exhaustion(task1)
        assert proposal2 is None

    def test_no_emission_below_threshold(self) -> None:
        """Below threshold should not trigger emission."""
        emitter = DecompositionEmitter(propose_count=3)

        task1 = Task(
            id="task-1",
            title="Test task 1",
            role="backend",
            description="Test task 1 description",
            status=TaskStatus.FAILED,
            retry_count=3,
            max_retries=3,
            result_summary="test timeout",
            terminal_reason="retry_exhausted",
            metadata={"issue_number": 42, "repo": "owner/repo"},
        )

        # Only one task, below threshold
        assert emitter.check_exhaustion(task1) is None

    def test_no_emission_without_issue_number(self) -> None:
        """Tasks without issue_number should not trigger emission."""
        emitter = DecompositionEmitter(propose_count=2)

        task = Task(
            id="task-1",
            title="Test task 1",
            role="backend",
            description="Test task 1 description",
            status=TaskStatus.FAILED,
            retry_count=3,
            max_retries=3,
            result_summary="test timeout",
            terminal_reason="retry_exhausted",
            metadata={},  # No issue_number
        )

        assert emitter.check_exhaustion(task) is None

    def test_emit_fires_at_threshold_with_more_than_two_tasks(self) -> None:
        """Emission should fire when more than two terminal tasks exist."""
        emitter = DecompositionEmitter(propose_count=2)

        task1 = Task(
            id="task-1",
            title="Test task 1",
            role="backend",
            description="Test task 1 description",
            status=TaskStatus.FAILED,
            retry_count=3,
            max_retries=3,
            result_summary="test timeout",
            terminal_reason="retry_exhausted",
            metadata={"issue_number": 42, "repo": "owner/repo"},
        )

        task2 = Task(
            id="task-2",
            title="Test task 2",
            role="backend",
            description="Test task 2 description",
            status=TaskStatus.FAILED,
            retry_count=3,
            max_retries=3,
            result_summary="compilation error",
            terminal_reason="retry_exhausted",
            metadata={"issue_number": 42, "repo": "owner/repo"},
        )

        task3 = Task(
            id="task-3",
            title="Test task 3",
            role="backend",
            description="Test task 3 description",
            status=TaskStatus.FAILED,
            retry_count=3,
            max_retries=3,
            result_summary="lint error",
            terminal_reason="retry_exhausted",
            metadata={"issue_number": 42, "repo": "owner/repo"},
        )

        # All three tasks should trigger emission (any 2+ should fire)
        assert emitter.check_exhaustion(task1) is None  # First task - below threshold
        assert emitter.check_exhaustion(task2) is not None  # Second task - reaches threshold
        assert emitter.check_exhaustion(task3) is not None  # Third task - also triggers

    def test_emit_proposal_posts_comment(self) -> None:
        """emit_proposal should call GitHub client._post_comment."""
        from unittest.mock import Mock

        emitter = DecompositionEmitter()
        proposal = DecompositionProposal(
            issue_number=42,
            repo="owner/repo",
            evidence_digests=("abc123",),
            attempts=[
                {
                    "attempt_number": 1,
                    "model": "claude-3-opus",
                    "effort": "medium",
                    "failure_reason": "test timeout",
                    "terminal_reason": "retry_exhausted",
                }
            ],
        )

        mock_gh_client = Mock()
        mock_gh_client._post_comment = Mock()

        result = emitter.emit_proposal(proposal, mock_gh_client, 42)

        assert result is True
        mock_gh_client._post_comment.assert_called_once_with(42, proposal.to_comment_body())

    def test_emit_proposal_handles_client_error(self) -> None:
        """emit_proposal should return False if client raises exception."""
        from unittest.mock import Mock

        emitter = DecompositionEmitter()
        proposal = DecompositionProposal(
            issue_number=42,
            repo="owner/repo",
            evidence_digests=("abc123",),
            attempts=[
                {
                    "attempt_number": 1,
                    "model": "claude-3-opus",
                    "effort": "medium",
                    "failure_reason": "test timeout",
                    "terminal_reason": "retry_exhausted",
                }
            ],
        )

        mock_gh_client = Mock()
        mock_gh_client._post_comment = Mock(side_effect=Exception("GitHub API error"))

        result = emitter.emit_proposal(proposal, mock_gh_client, 42)

        assert result is False
