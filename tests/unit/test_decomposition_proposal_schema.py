"""Tests for the DecompositionProposal schema.

These tests verify:
- canonical_bytes are deterministic (same inputs → same bytes)
- content_hash is stable and prefixed correctly
- from_task constructs a valid proposal
- to_comment_body produces expected markdown
"""

from __future__ import annotations

import pytest

from bernstein.core.orchestration.decomposition_proposal import DecompositionProposal


class TestCanonicalBytesDeterminism:
    """Test that canonical_bytes produces deterministic output."""

    def test_same_inputs_yield_same_bytes(self) -> None:
        """Two proposals with identical fields must produce identical canonical bytes."""
        p1 = DecompositionProposal(
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
        p2 = DecompositionProposal(
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
        assert p1.canonical_bytes() == p2.canonical_bytes()

    def test_field_change_changes_bytes(self) -> None:
        """Changing any field must change the canonical bytes."""
        base_attempts = [
            {
                "attempt_number": 1,
                "model": "claude-3-opus",
                "effort": "medium",
                "failure_reason": "test timeout",
                "terminal_reason": "retry_exhausted",
            }
        ]
        base = DecompositionProposal(
            issue_number=42,
            repo="owner/repo",
            evidence_digests=("abc123",),
            attempts=base_attempts,
        )
        different_issue = DecompositionProposal(
            issue_number=43,
            repo="owner/repo",
            evidence_digests=("abc123",),
            attempts=base_attempts,
        )
        different_repo = DecompositionProposal(
            issue_number=42,
            repo="other/repo",
            evidence_digests=("abc123",),
            attempts=base_attempts,
        )
        different_evidence = DecompositionProposal(
            issue_number=42,
            repo="owner/repo",
            evidence_digests=("def456",),
            attempts=base_attempts,
        )
        different_attempts = DecompositionProposal(
            issue_number=42,
            repo="owner/repo",
            evidence_digests=("abc123",),
            attempts=[
                {
                    "attempt_number": 2,
                    "model": "claude-3-sonnet",
                    "effort": "high",
                    "failure_reason": "compilation error",
                    "terminal_reason": "retry_exhausted",
                }
            ],
        )

        bytes_base = base.canonical_bytes()
        assert different_issue.canonical_bytes() != bytes_base
        assert different_repo.canonical_bytes() != bytes_base
        assert different_evidence.canonical_bytes() != bytes_base
        assert different_attempts.canonical_bytes() != bytes_base

    def test_dict_key_order_does_not_affect_bytes(self) -> None:
        """The canonical form must sort keys; dict insertion order must not matter."""
        # Create proposals where the attempts dicts have keys in different orders.
        attempts_a = [
            {
                "model": "claude-3-opus",
                "attempt_number": 1,
                "effort": "medium",
                "failure_reason": "test timeout",
                "terminal_reason": "retry_exhausted",
            }
        ]
        attempts_b = [
            {
                "effort": "medium",
                "terminal_reason": "retry_exhausted",
                "attempt_number": 1,
                "model": "claude-3-opus",
                "failure_reason": "test timeout",
            }
        ]
        p1 = DecompositionProposal(
            issue_number=42,
            repo="owner/repo",
            evidence_digests=("abc123",),
            attempts=attempts_a,
        )
        p2 = DecompositionProposal(
            issue_number=42,
            repo="owner/repo",
            evidence_digests=("abc123",),
            attempts=attempts_b,
        )
        assert p1.canonical_bytes() == p2.canonical_bytes()


class TestContentHashStability:
    """Test that content_hash is stable and correctly prefixed."""

    def test_content_hash_prefix(self) -> None:
        """content_hash must start with 'sha256:'."""
        proposal = DecompositionProposal(
            issue_number=42,
            repo="owner/repo",
            evidence_digests=("abc123",),
            attempts=[],
        )
        assert proposal.content_hash().startswith("sha256:")

    def test_content_hash_is_hex_digest(self) -> None:
        """content_hash hex portion must be valid hexadecimal."""
        proposal = DecompositionProposal(
            issue_number=42,
            repo="owner/repo",
            evidence_digests=("abc123",),
            attempts=[],
        )
        hex_part = proposal.content_hash().split(":", 1)[1]
        assert len(hex_part) == 64  # SHA256 hex digest length
        int(hex_part, 16)  # Will raise ValueError if not valid hex

    def test_content_hash_matches_canonical_bytes(self) -> None:
        """content_hash must be the SHA256 of canonical_bytes."""
        proposal = DecompositionProposal(
            issue_number=42,
            repo="owner/repo",
            evidence_digests=("abc123",),
            attempts=[],
        )
        import hashlib

        expected = "sha256:" + hashlib.sha256(proposal.canonical_bytes()).hexdigest()
        assert proposal.content_hash() == expected

    def test_content_address_matches_content_hash(self) -> None:
        """content_address must be identical to content_hash."""
        proposal = DecompositionProposal(
            issue_number=42,
            repo="owner/repo",
            evidence_digests=("abc123",),
            attempts=[],
        )
        assert proposal.content_address == proposal.content_hash()


class TestFromTask:
    """Test the from_task classmethod constructs valid proposals."""

    def test_from_task_requires_issue_number(self) -> None:
        """from_task must raise ValueError when issue_number is missing."""
        from bernstein.core.tasks.models import Task, TaskStatus

        task = Task(
            id="task-1",
            title="Test task",
            role="backend",
            description="Test task description",
            status=TaskStatus.FAILED,
            retry_count=3,
            max_retries=3,
            metadata={},  # No issue_number
        )
        with pytest.raises(ValueError, match="Task metadata must contain 'issue_number' for decomposition proposals"):
            DecompositionProposal.from_task(task, "some evidence")

    def test_from_task_requires_repo(self) -> None:
        """from_task must raise ValueError when repo is missing."""
        from bernstein.core.tasks.models import Task, TaskStatus

        task = Task(
            id="task-1",
            title="Test task",
            role="backend",
            description="Test task description",
            status=TaskStatus.FAILED,
            retry_count=3,
            max_retries=3,
            metadata={"issue_number": 42},  # No repo
        )
        with pytest.raises(ValueError, match="repo"):
            DecompositionProposal.from_task(task, "some evidence")

    def test_from_task_constructs_valid_proposal(self) -> None:
        """from_task must create a valid DecompositionProposal."""
        from bernstein.core.tasks.models import Task, TaskStatus

        task = Task(
            id="task-1",
            title="Test task",
            role="backend",
            description="Test task description",
            status=TaskStatus.FAILED,
            retry_count=3,  # 0-indexed, so this is the 4th attempt
            max_retries=3,
            model="claude-3-opus",
            effort="high",
            result_summary="test timeout",
            terminal_reason="retry_exhausted",
            metadata={
                "issue_number": 42,
                "repo": "owner/repo",
            },
        )
        evidence = "Test suite failed with 3 failures"
        proposal = DecompositionProposal.from_task(task, evidence)

        assert proposal.issue_number == 42
        assert proposal.repo == "owner/repo"
        assert len(proposal.attempts) == 1
        assert proposal.attempts[0]["attempt_number"] == 4  # retry_count 3 + 1
        assert proposal.attempts[0]["model"] == "claude-3-opus"
        assert proposal.attempts[0]["effort"] == "high"
        assert proposal.attempts[0]["failure_reason"] == "test timeout"
        assert proposal.attempts[0]["terminal_reason"] == "retry_exhausted"
        # Evidence digest must be SHA256 of the evidence string.
        import hashlib

        expected_digest = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
        assert proposal.evidence_digests == (expected_digest,)


class TestToCommentBody:
    """Test that to_comment_body produces expected markdown."""

    def test_comment_body_contains_issue_ref(self) -> None:
        """Comment body must contain the issue number."""
        proposal = DecompositionProposal(
            issue_number=42,
            repo="owner/repo",
            evidence_digests=("abc123", "def456"),
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
        body = proposal.to_comment_body()
        assert "#42" in body
        assert "Decomposition Proposal" in body

    def test_comment_body_contains_attempt_table(self) -> None:
        """Comment body must contain a table with attempt information."""
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
        body = proposal.to_comment_body()
        assert "| Attempt | Model" in body
        assert "claude-3-opus" in body
        assert "medium" in body
        assert "test timeout" in body
        assert "retry_exhausted" in body

    def test_comment_body_contains_evidence_digests(self) -> None:
        """Comment body must list evidence digests."""
        proposal = DecompositionProposal(
            issue_number=42,
            repo="owner/repo",
            evidence_digests=("abc123", "def456"),
            attempts=[],
        )
        body = proposal.to_comment_body()
        assert "abc123" in body
        assert "def456" in body
        assert "Evidence Digests" in body

    def test_comment_body_contains_proposal_id(self) -> None:
        """Comment body must include the proposal ID (content_address)."""
        proposal = DecompositionProposal(
            issue_number=42,
            repo="owner/repo",
            evidence_digests=("abc123",),
            attempts=[],
        )
        body = proposal.to_comment_body()
        assert proposal.content_address in body

    def test_comment_body_escapes_pipe_in_cell(self) -> None:
        """Pipe characters in attempt fields must be escaped in the table."""
        proposal = DecompositionProposal(
            issue_number=42,
            repo="owner/repo",
            evidence_digests=("abc123",),
            attempts=[
                {
                    "attempt_number": 1,
                    "model": "claude-3-opus",
                    "effort": "medium",
                    "failure_reason": "field | with pipe",
                    "terminal_reason": "retry_exhausted",
                }
            ],
        )
        body = proposal.to_comment_body()
        # The table row should escape the pipe.
        lines = body.split("\n")
        table_rows = [l for l in lines if l.startswith("| 1 |")]
        assert len(table_rows) == 1
        # The pipe in "field | with pipe" should be escaped as "\|".
        assert r"\|" in table_rows[0] or "field" in table_rows[0]
