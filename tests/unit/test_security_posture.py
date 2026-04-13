"""Tests for SEC-019: security posture scoring per run."""

from __future__ import annotations

import pytest
from bernstein.core.security_posture import (
    METRIC_WEIGHTS,
    SecurityMetric,
    compute_posture,
    format_posture_report,
    score_audit_integrity,
    score_permissions,
    score_policy_compliance,
    score_sandbox,
    score_secrets,
)

# ---------------------------------------------------------------------------
# score_permissions
# ---------------------------------------------------------------------------


class TestScorePermissions:
    """Tests for score_permissions."""

    def test_no_requests(self) -> None:
        m = score_permissions(denied=0, escalated=0, total=0)
        assert m.score == pytest.approx(100.0)
        assert m.name == "permissions"
        assert m.weight == METRIC_WEIGHTS["permissions"]

    def test_no_escalations(self) -> None:
        m = score_permissions(denied=5, escalated=0, total=10)
        assert m.score == pytest.approx(100.0)

    def test_all_escalated(self) -> None:
        m = score_permissions(denied=0, escalated=10, total=10)
        assert m.score == pytest.approx(0.0)

    def test_partial_escalation(self) -> None:
        m = score_permissions(denied=2, escalated=3, total=10)
        assert m.score == pytest.approx(70.0)

    def test_details_mention_rate(self) -> None:
        m = score_permissions(denied=1, escalated=5, total=10)
        assert "50%" in m.details


# ---------------------------------------------------------------------------
# score_secrets
# ---------------------------------------------------------------------------


class TestScoreSecrets:
    """Tests for score_secrets."""

    def test_none_detected(self) -> None:
        m = score_secrets(detected=0, blocked=0)
        assert m.score == pytest.approx(100.0)
        assert m.name == "secrets"

    def test_all_blocked(self) -> None:
        m = score_secrets(detected=4, blocked=4)
        assert m.score == pytest.approx(100.0)

    def test_none_blocked(self) -> None:
        m = score_secrets(detected=5, blocked=0)
        assert m.score == pytest.approx(0.0)

    def test_partial_block(self) -> None:
        m = score_secrets(detected=4, blocked=3)
        assert m.score == pytest.approx(75.0)

    def test_details_mention_leaked(self) -> None:
        m = score_secrets(detected=4, blocked=3)
        assert "1 leaked" in m.details


# ---------------------------------------------------------------------------
# score_sandbox
# ---------------------------------------------------------------------------


class TestScoreSandbox:
    """Tests for score_sandbox."""

    def test_no_violations(self) -> None:
        m = score_sandbox(violations=0)
        assert m.score == pytest.approx(100.0)
        assert m.name == "sandbox"

    def test_one_violation(self) -> None:
        m = score_sandbox(violations=1)
        assert m.score == pytest.approx(80.0)

    def test_five_violations_floors_at_zero(self) -> None:
        m = score_sandbox(violations=5)
        assert m.score == pytest.approx(0.0)

    def test_many_violations_floors_at_zero(self) -> None:
        m = score_sandbox(violations=10)
        assert m.score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# score_audit_integrity
# ---------------------------------------------------------------------------


class TestScoreAuditIntegrity:
    """Tests for score_audit_integrity."""

    def test_verified_no_gaps(self) -> None:
        m = score_audit_integrity(verified=True, gaps=0)
        assert m.score == pytest.approx(100.0)
        assert m.name == "audit_integrity"

    def test_unverified_no_gaps(self) -> None:
        m = score_audit_integrity(verified=False, gaps=0)
        assert m.score == pytest.approx(50.0)

    def test_verified_with_gaps(self) -> None:
        m = score_audit_integrity(verified=True, gaps=3)
        assert m.score == pytest.approx(70.0)

    def test_unverified_with_gaps(self) -> None:
        m = score_audit_integrity(verified=False, gaps=3)
        assert m.score == pytest.approx(20.0)

    def test_floors_at_zero(self) -> None:
        m = score_audit_integrity(verified=False, gaps=10)
        assert m.score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# score_policy_compliance
# ---------------------------------------------------------------------------


class TestScorePolicyCompliance:
    """Tests for score_policy_compliance."""

    def test_no_checks(self) -> None:
        m = score_policy_compliance(passed=0, total=0)
        assert m.score == pytest.approx(100.0)
        assert m.name == "policy_compliance"

    def test_all_passed(self) -> None:
        m = score_policy_compliance(passed=8, total=8)
        assert m.score == pytest.approx(100.0)

    def test_none_passed(self) -> None:
        m = score_policy_compliance(passed=0, total=5)
        assert m.score == pytest.approx(0.0)

    def test_partial(self) -> None:
        m = score_policy_compliance(passed=3, total=4)
        assert m.score == pytest.approx(75.0)


# ---------------------------------------------------------------------------
# compute_posture — perfect, mixed, poor
# ---------------------------------------------------------------------------


def _perfect_metrics() -> dict[str, SecurityMetric]:
    """Build a set of metrics that all score 100."""
    return {
        "permissions": score_permissions(denied=0, escalated=0, total=0),
        "secrets": score_secrets(detected=0, blocked=0),
        "sandbox": score_sandbox(violations=0),
        "audit": score_audit_integrity(verified=True, gaps=0),
        "policy": score_policy_compliance(passed=10, total=10),
    }


def _poor_metrics() -> dict[str, SecurityMetric]:
    """Build a set of metrics that all score 0."""
    return {
        "permissions": score_permissions(denied=0, escalated=10, total=10),
        "secrets": score_secrets(detected=5, blocked=0),
        "sandbox": score_sandbox(violations=5),
        "audit": score_audit_integrity(verified=False, gaps=10),
        "policy": score_policy_compliance(passed=0, total=10),
    }


class TestComputePosture:
    """Tests for compute_posture."""

    def test_perfect_score(self) -> None:
        report = compute_posture("run-1", **_perfect_metrics())
        assert report.overall_score == pytest.approx(100.0)
        assert report.grade == "A"
        assert report.run_id == "run-1"
        assert len(report.metrics) == 5
        assert report.recommendations == []

    def test_poor_score(self) -> None:
        report = compute_posture("run-bad", **_poor_metrics())
        assert report.overall_score == pytest.approx(0.0)
        assert report.grade == "F"
        assert len(report.recommendations) > 0

    def test_mixed_score(self) -> None:
        metrics = _perfect_metrics()
        # Introduce a sandbox violation and partial secrets leak
        metrics["sandbox"] = score_sandbox(violations=1)  # 80
        metrics["secrets"] = score_secrets(detected=4, blocked=3)  # 75
        report = compute_posture("run-mix", **metrics)
        # Weighted: 100*0.25 + 75*0.20 + 80*0.20 + 100*0.15 + 100*0.20 = 91.0
        assert report.overall_score == pytest.approx(91.0)
        assert report.grade == "A"

    def test_grade_boundaries(self) -> None:
        """Verify exact grade boundary thresholds."""
        # A >= 90
        m = _perfect_metrics()
        r = compute_posture("g", **m)
        assert r.grade == "A"

        # B boundary: 80..89
        m["permissions"] = score_permissions(denied=0, escalated=4, total=10)  # 60
        r = compute_posture("g", **m)
        # 60*0.25 + 100*0.20 + 100*0.20 + 100*0.15 + 100*0.20 = 90.0
        assert r.grade == "A"

        m["secrets"] = score_secrets(detected=10, blocked=7)  # 70
        r = compute_posture("g", **m)
        # 60*0.25 + 70*0.20 + 100*0.20 + 100*0.15 + 100*0.20 = 84.0
        assert r.grade == "B"

    def test_generated_at_is_iso(self) -> None:
        report = compute_posture("t", **_perfect_metrics())
        assert "T" in report.generated_at  # ISO contains T separator

    def test_recommendations_for_low_scores(self) -> None:
        m = _perfect_metrics()
        m["sandbox"] = score_sandbox(violations=3)  # 40
        m["audit"] = score_audit_integrity(verified=False, gaps=0)  # 50
        report = compute_posture("recs", **m)
        assert any("sandbox" in r.lower() for r in report.recommendations)
        assert any("audit" in r.lower() for r in report.recommendations)


# ---------------------------------------------------------------------------
# Grade assignment edge cases
# ---------------------------------------------------------------------------


class TestGradeAssignment:
    """Verify grade boundaries precisely."""

    @pytest.mark.parametrize(
        ("score", "expected_grade"),
        [
            (100.0, "A"),
            (90.0, "A"),
            (89.99, "B"),
            (80.0, "B"),
            (79.99, "C"),
            (70.0, "C"),
            (69.99, "D"),
            (60.0, "D"),
            (59.99, "F"),
            (0.0, "F"),
        ],
    )
    def test_grade_boundary(self, score: float, expected_grade: str) -> None:
        from bernstein.core.security_posture import _assign_grade

        assert _assign_grade(score) == expected_grade


# ---------------------------------------------------------------------------
# format_posture_report
# ---------------------------------------------------------------------------


class TestFormatPostureReport:
    """Tests for format_posture_report."""

    def test_contains_run_id(self) -> None:
        report = compute_posture("fmt-test", **_perfect_metrics())
        output = format_posture_report(report)
        assert "fmt-test" in output

    def test_contains_grade(self) -> None:
        report = compute_posture("fmt-grade", **_perfect_metrics())
        output = format_posture_report(report)
        assert "[A]" in output

    def test_contains_metric_names(self) -> None:
        report = compute_posture("fmt-metrics", **_perfect_metrics())
        output = format_posture_report(report)
        assert "permissions" in output
        assert "secrets" in output
        assert "sandbox" in output
        assert "audit_integrity" in output
        assert "policy_compliance" in output

    def test_recommendations_panel_when_present(self) -> None:
        m = _perfect_metrics()
        m["sandbox"] = score_sandbox(violations=5)  # 0
        report = compute_posture("fmt-recs", **m)
        output = format_posture_report(report)
        assert "Recommendations" in output

    def test_no_recommendations_panel_when_perfect(self) -> None:
        report = compute_posture("fmt-norecs", **_perfect_metrics())
        output = format_posture_report(report)
        assert "Recommendations" not in output

    def test_returns_string(self) -> None:
        report = compute_posture("fmt-type", **_perfect_metrics())
        result = format_posture_report(report)
        assert isinstance(result, str)
