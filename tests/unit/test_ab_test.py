"""Tests for A/B test runner: comparison logic, report generation, timeout."""

from __future__ import annotations

from bernstein.core.ab_test import (
    ABTestConfig,
    ABTestReport,
    ABTestResult,
    determine_winner,
)


# ---------------------------------------------------------------------------
# Fixtures — reusable configs/results
# ---------------------------------------------------------------------------

_CONFIG = ABTestConfig(
    task_description="Fix login bug",
    model_a="opus",
    model_b="sonnet",
    role="backend",
    scope="medium",
)


def _result(
    model: str = "opus",
    variant: str = "a",
    task_id: str = "t-1",
    duration: float = 60.0,
    cost: float = 0.10,
    inp: int = 1000,
    out: int = 500,
    passed: bool = True,
    quality: bool = True,
    status: str = "done",
) -> ABTestResult:
    return ABTestResult(
        model=model,
        variant=variant,  # type: ignore[arg-type]
        task_id=task_id,
        duration_seconds=duration,
        cost_usd=cost,
        input_tokens=inp,
        output_tokens=out,
        passed=passed,
        quality_passed=quality,
        status=status,
    )


# ---------------------------------------------------------------------------
# determine_winner tests
# ---------------------------------------------------------------------------


class TestDetermineWinner:
    """Tests for the winner-determination logic."""

    def test_a_passes_b_fails(self) -> None:
        """Model A wins when B fails."""
        ra = _result(model="opus", passed=True)
        rb = _result(model="sonnet", variant="b", passed=False)

        winner, reason = determine_winner(ra, rb)

        assert winner == "a"
        assert "opus passed" in reason
        assert "sonnet failed" in reason

    def test_b_passes_a_fails(self) -> None:
        """Model B wins when A fails."""
        ra = _result(model="opus", passed=False)
        rb = _result(model="sonnet", variant="b", passed=True)

        winner, reason = determine_winner(ra, rb)

        assert winner == "b"
        assert "sonnet passed" in reason

    def test_both_fail_is_tie(self) -> None:
        """Both failing is a tie."""
        ra = _result(passed=False)
        rb = _result(variant="b", passed=False)

        winner, reason = determine_winner(ra, rb)

        assert winner == "tie"
        assert "both" in reason

    def test_quality_gate_decides(self) -> None:
        """Quality gates break ties when both passed."""
        ra = _result(quality=True)
        rb = _result(variant="b", quality=False)

        winner, reason = determine_winner(ra, rb)

        assert winner == "a"
        assert "quality" in reason

    def test_a_wins_on_cost(self) -> None:
        """Model A wins when significantly cheaper."""
        ra = _result(cost=0.05)
        rb = _result(variant="b", cost=0.15)

        winner, reason = determine_winner(ra, rb)

        assert winner == "a"
        assert "cheaper" in reason

    def test_b_wins_on_cost(self) -> None:
        """Model B wins when significantly cheaper."""
        ra = _result(cost=0.20)
        rb = _result(variant="b", cost=0.08)

        winner, reason = determine_winner(ra, rb)

        assert winner == "b"
        assert "cheaper" in reason

    def test_a_wins_on_speed(self) -> None:
        """Model A wins on speed when cost is similar."""
        ra = _result(cost=0.10, duration=30.0)
        rb = _result(variant="b", cost=0.10, duration=100.0)

        winner, reason = determine_winner(ra, rb)

        assert winner == "a"
        assert "faster" in reason

    def test_b_wins_on_speed(self) -> None:
        """Model B wins on speed when cost is similar."""
        ra = _result(cost=0.10, duration=120.0)
        rb = _result(variant="b", cost=0.10, duration=30.0)

        winner, reason = determine_winner(ra, rb)

        assert winner == "b"
        assert "faster" in reason

    def test_tie_when_close(self) -> None:
        """Tie when cost and duration are within tolerance."""
        ra = _result(cost=0.10, duration=60.0)
        rb = _result(variant="b", cost=0.10, duration=60.0)

        winner, reason = determine_winner(ra, rb)

        assert winner == "tie"
        assert "tolerance" in reason


# ---------------------------------------------------------------------------
# ABTestReport.to_markdown tests
# ---------------------------------------------------------------------------


class TestABTestReportMarkdown:
    """Tests for the markdown report renderer."""

    def test_markdown_contains_models(self) -> None:
        """Markdown output includes both model names."""
        ra = _result(model="opus")
        rb = _result(model="sonnet", variant="b")
        report = ABTestReport(
            test_id="abc123",
            config=_CONFIG,
            result_a=ra,
            result_b=rb,
            winner="a",
            reason="opus was cheaper",
        )

        md = report.to_markdown()

        assert "opus" in md
        assert "sonnet" in md

    def test_markdown_contains_winner(self) -> None:
        """Markdown output declares the winner."""
        ra = _result(model="opus", cost=0.05)
        rb = _result(model="sonnet", variant="b", cost=0.20)
        report = ABTestReport(
            test_id="def456",
            config=_CONFIG,
            result_a=ra,
            result_b=rb,
            winner="a",
            reason="opus was cheaper",
        )

        md = report.to_markdown()

        assert "Winner" in md
        assert "opus was cheaper" in md

    def test_markdown_contains_metrics(self) -> None:
        """Markdown output includes cost and duration."""
        ra = _result(model="opus", cost=0.1234, duration=45.6)
        rb = _result(model="sonnet", variant="b", cost=0.5678, duration=90.1)
        report = ABTestReport(
            test_id="ghi789",
            config=_CONFIG,
            result_a=ra,
            result_b=rb,
            winner="a",
            reason="cheaper",
        )

        md = report.to_markdown()

        assert "$0.1234" in md
        assert "$0.5678" in md
        assert "45.6s" in md
        assert "90.1s" in md

    def test_markdown_timeout_note(self) -> None:
        """Markdown includes timeout warning when test timed out."""
        ra = _result(status="timeout", passed=False)
        rb = _result(variant="b", status="timeout", passed=False)
        report = ABTestReport(
            test_id="timeout1",
            config=_CONFIG,
            result_a=ra,
            result_b=rb,
            winner="tie",
            reason="both timed out",
            timed_out=True,
        )

        md = report.to_markdown()

        assert "timed out" in md.lower()

    def test_markdown_no_timeout_note_when_clean(self) -> None:
        """Markdown does not mention timeout for normal runs."""
        ra = _result()
        rb = _result(variant="b")
        report = ABTestReport(
            test_id="clean1",
            config=_CONFIG,
            result_a=ra,
            result_b=rb,
            winner="tie",
            reason="within tolerance",
            timed_out=False,
        )

        md = report.to_markdown()

        assert "timed out" not in md.lower()

    def test_markdown_task_description(self) -> None:
        """Markdown output includes the task description."""
        ra = _result()
        rb = _result(variant="b")
        report = ABTestReport(
            test_id="desc1",
            config=_CONFIG,
            result_a=ra,
            result_b=rb,
            winner="tie",
            reason="tie",
        )

        md = report.to_markdown()

        assert "Fix login bug" in md


# ---------------------------------------------------------------------------
# ABTestConfig frozen dataclass
# ---------------------------------------------------------------------------


class TestABTestConfig:
    """Tests for ABTestConfig defaults and immutability."""

    def test_defaults(self) -> None:
        """Config has sensible defaults."""
        cfg = ABTestConfig(task_description="test", model_a="a", model_b="b")

        assert cfg.role == "backend"
        assert cfg.scope == "medium"
        assert cfg.timeout_seconds == 1800

    def test_custom_timeout(self) -> None:
        """Timeout can be overridden."""
        cfg = ABTestConfig(
            task_description="test",
            model_a="a",
            model_b="b",
            timeout_seconds=60,
        )

        assert cfg.timeout_seconds == 60


# ---------------------------------------------------------------------------
# ABTestResult frozen dataclass
# ---------------------------------------------------------------------------


class TestABTestResult:
    """Tests for ABTestResult creation."""

    def test_defaults(self) -> None:
        """Result defaults are conservative."""
        r = ABTestResult(
            model="opus",
            variant="a",
            task_id="t-1",
            duration_seconds=10.0,
        )

        assert r.cost_usd == 0.0
        assert r.input_tokens == 0
        assert r.output_tokens == 0
        assert r.passed is False
        assert r.quality_passed is False
        assert r.status == "unknown"
