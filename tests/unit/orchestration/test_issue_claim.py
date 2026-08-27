"""Unit tests for :mod:`bernstein.core.orchestration.issue_claim`."""

from __future__ import annotations

from datetime import UTC, datetime

from bernstein.core.orchestration.issue_claim import (
    CLAIM_MARKER,
    RESOLVED_MARKER,
    build_claim_body,
    build_completion_body,
    build_release_body,
    find_claim_comment,
    has_marker,
)


class TestConstants:
    """Tests for module constants."""

    def test_claim_marker_format(self) -> None:
        """CLAIM_MARKER is the expected HTML comment format."""
        assert CLAIM_MARKER == "<!-- bernstein:issue:intake -->"

    def test_resolved_marker_format(self) -> None:
        """RESOLVED_MARKER is the expected HTML comment format."""
        assert RESOLVED_MARKER == "<!-- bernstein:issue:resolved -->"


class TestBuildClaimBody:
    """Tests for build_claim_body function."""

    def test_basic_claim_body(self) -> None:
        """build_claim_body produces expected output format."""
        fp = "abc123"
        run_id = "run-001"
        start_time = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)

        result = build_claim_body(fingerprint=fp, run_id=run_id, start_time=start_time)

        assert CLAIM_MARKER in result
        assert fp in result
        assert run_id in result
        assert "2024-01-15T10:30:00+00:00" in result

    def test_claim_body_contains_all_elements(self) -> None:
        """build_claim_body includes fingerprint, run_id, and start_time."""
        fp = "worker-fp"
        run_id = "test-run"
        start_time = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)

        result = build_claim_body(fingerprint=fp, run_id=run_id, start_time=start_time)

        assert "Intake run via bernstein" in result
        assert f"fingerprint `{fp}`" in result
        assert f"run_id `{run_id}`" in result


class TestBuildCompletionBody:
    """Tests for build_completion_body function."""

    def test_completion_body_without_pr(self) -> None:
        """build_completion_body works without a PR URL."""
        fp = "fp-xyz"
        run_id = "run-123"

        result = build_completion_body(fingerprint=fp, run_id=run_id, pr_url=None)

        assert CLAIM_MARKER in result
        assert RESOLVED_MARKER in result
        assert fp in result
        assert run_id in result
        assert "Completed via bernstein" in result
        assert result.endswith(".")

    def test_completion_body_with_pr(self) -> None:
        """build_completion_body includes PR URL when provided."""
        fp = "fp-abc"
        run_id = "run-456"
        pr_url = "https://github.com/owner/repo/pull/789"

        result = build_completion_body(fingerprint=fp, run_id=run_id, pr_url=pr_url)

        assert pr_url in result
        assert ": " + pr_url in result

    def test_completion_body_contains_all_elements(self) -> None:
        """build_completion_body includes all required elements."""
        fp = "test-fp"
        run_id = "test-run-id"
        pr_url = "https://example.com/pr/1"

        result = build_completion_body(fingerprint=fp, run_id=run_id, pr_url=pr_url)

        assert f"fingerprint `{fp}`" in result
        assert f"run_id `{run_id}`" in result


class TestBuildReleaseBody:
    """Tests for build_release_body function."""

    def test_release_body_basic(self) -> None:
        """build_release_body produces expected output."""
        fp = "fp-release"
        run_id = "run-release"
        reason = "task cancelled"

        result = build_release_body(fingerprint=fp, run_id=run_id, reason=reason)

        assert CLAIM_MARKER in result
        assert RESOLVED_MARKER in result
        assert fp in result
        assert run_id in result
        assert reason in result
        assert "Released via bernstein" in result

    def test_release_body_includes_reason(self) -> None:
        """build_release_body includes the release reason."""
        fp = "fp"
        run_id = "run"
        reason = "resource constraints"

        result = build_release_body(fingerprint=fp, run_id=run_id, reason=reason)

        assert reason in result


class TestFindClaimComment:
    """Tests for find_claim_comment function."""

    def test_returns_first_claim_comment(self) -> None:
        """find_claim_comment returns the first matching comment."""
        comments = [
            {"body": "Just a regular comment"},
            {"body": f"Some text {CLAIM_MARKER} more text"},
            {"body": f"Another {CLAIM_MARKER} claim"},
        ]

        result = find_claim_comment(comments)

        assert result is not None
        assert "Some text" in result["body"]

    def test_returns_none_when_no_claim(self) -> None:
        """find_claim_comment returns None when no claim marker present."""
        comments = [
            {"body": "No claim here"},
            {"body": "Just normal text"},
        ]

        result = find_claim_comment(comments)

        assert result is None

    def test_returns_none_for_empty_list(self) -> None:
        """find_claim_comment returns None for empty comments list."""
        result = find_claim_comment([])

        assert result is None

    def test_handles_missing_body_key(self) -> None:
        """find_claim_comment handles comments without body key."""
        comments = [
            {"id": 1},
            {"body": "Regular comment"},
        ]

        result = find_claim_comment(comments)

        assert result is None

    def test_handles_non_string_body(self) -> None:
        """find_claim_comment handles non-string body values."""
        comments = [
            {"body": 123},
            {"body": None},
            {"body": f"Has {CLAIM_MARKER}"},
        ]

        result = find_claim_comment(comments)

        assert result is not None
        assert "Has" in result["body"]


class TestHasMarker:
    """Tests for has_marker function."""

    def test_returns_true_when_marker_present(self) -> None:
        """has_marker returns True when marker is in body."""
        body = f"Some text {CLAIM_MARKER} more text"

        result = has_marker(body, CLAIM_MARKER)

        assert result is True

    def test_returns_false_when_marker_absent(self) -> None:
        """has_marker returns False when marker is not in body."""
        body = "No markers here"

        result = has_marker(body, CLAIM_MARKER)

        assert result is False

    def test_resolved_marker_detection(self) -> None:
        """has_marker works for RESOLVED_MARKER."""
        body = f"{CLAIM_MARKER}\n{RESOLVED_MARKER}\nDone"

        assert has_marker(body, RESOLVED_MARKER) is True
        assert has_marker(body, "<!-- wrong -->") is False

    def test_empty_body(self) -> None:
        """has_marker returns False for empty body."""
        assert has_marker("", CLAIM_MARKER) is False

    def test_marker_exact_match(self) -> None:
        """has_marker requires exact marker match."""
        body = "<!-- bernstein:issue:intake-extra -->"

        assert has_marker(body, CLAIM_MARKER) is False
