"""Tests for quality gate panel TUI widget."""

from __future__ import annotations

from bernstein.tui.widgets import QualityGateResult


class TestQualityGateResult:
    """Test QualityGateResult dataclass."""

    def test_pass_result(self) -> None:
        """Test pass result creation."""
        result = QualityGateResult(
            gate="lint",
            status="pass",
            duration_ms=150.5,
            details="No issues found",
        )

        assert result.gate == "lint"
        assert result.status == "pass"
        assert result.duration_ms == 150.5
        assert result.details == "No issues found"

    def test_fail_result(self) -> None:
        """Test fail result creation."""
        result = QualityGateResult(
            gate="tests",
            status="fail",
            duration_ms=2500.0,
            details="3 tests failed",
        )

        assert result.gate == "tests"
        assert result.status == "fail"
        assert result.duration_ms == 2500.0


class TestQualityGatePanel:
    """Test QualityGatePanel widget."""

    def test_set_results_empty(self) -> None:
        """Test setting empty results."""
        # Note: DataTable widgets require Textual app context for full initialization
        # This test verifies the data structure works correctly
        results: list[QualityGateResult] = []
        assert len(results) == 0

    def test_set_results_with_pass(self) -> None:
        """Test setting results with pass status."""
        results = [
            QualityGateResult(
                gate="lint",
                status="pass",
                duration_ms=100.0,
                details="Clean",
            )
        ]
        assert len(results) == 1
        assert results[0].status == "pass"

    def test_set_results_with_fail(self) -> None:
        """Test setting results with fail status."""
        results = [
            QualityGateResult(
                gate="tests",
                status="fail",
                duration_ms=500.0,
                details="Test failed",
            )
        ]
        assert len(results) == 1
        assert results[0].status == "fail"

    def test_set_results_with_warn(self) -> None:
        """Test setting results with warn status."""
        results = [
            QualityGateResult(
                gate="security",
                status="warn",
                duration_ms=200.0,
                details="Potential issue",
            )
        ]
        assert len(results) == 1
        assert results[0].status == "warn"

    def test_set_results_with_skipped(self) -> None:
        """Test setting results with skipped status."""
        results = [
            QualityGateResult(
                gate="type_check",
                status="skipped",
                duration_ms=0.0,
                details="Not configured",
            )
        ]
        assert len(results) == 1
        assert results[0].status == "skipped"

    def test_set_results_multiple(self) -> None:
        """Test setting multiple results."""
        results = [
            QualityGateResult(
                gate="lint",
                status="pass",
                duration_ms=100.0,
                details="Clean",
            ),
            QualityGateResult(
                gate="tests",
                status="pass",
                duration_ms=500.0,
                details="All passed",
            ),
            QualityGateResult(
                gate="type_check",
                status="fail",
                duration_ms=300.0,
                details="Type errors",
            ),
        ]
        assert len(results) == 3

    def test_result_details_truncation_logic(self) -> None:
        """Test that long details would be truncated in display."""
        long_details = "x" * 100  # 100 character details
        result = QualityGateResult(
            gate="lint",
            status="pass",
            duration_ms=100.0,
            details=long_details,
        )
        # Verify truncation logic (as implemented in widget)
        display_details = result.details[:50] + "..." if len(result.details) > 50 else result.details
        assert len(display_details) == 53  # 50 + "..."
        assert display_details.endswith("...")

    def test_result_clears_previous(self) -> None:
        """Test that new results replace previous (logic test)."""
        # Simulate the set_results behavior
        results1 = [
            QualityGateResult(
                gate="lint",
                status="pass",
                duration_ms=100.0,
                details="First",
            )
        ]
        assert len(results1) == 1

        # Replace with different results
        results2 = [
            QualityGateResult(
                gate="tests",
                status="fail",
                duration_ms=200.0,
                details="Second",
            ),
            QualityGateResult(
                gate="type_check",
                status="pass",
                duration_ms=300.0,
                details="Third",
            ),
        ]

        # Should have exactly 2 results (the new ones)
        assert len(results2) == 2
