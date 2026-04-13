# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Comprehensive tests for team adoption dashboard observability module.

Tests cover:
- UserMetrics / TeamMetrics dataclass construction
- aggregate_user_metrics with various archive scenarios
- aggregate_team_metrics across multiple users
- compute_adoption_score edge cases
- render_team_dashboard_data output shape
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.observability.team_dashboard import (
    _ACTIVE_THRESHOLD,
    _FREQ_SATURATION,
    TeamMetrics,
    UserMetrics,
    aggregate_team_metrics,
    aggregate_user_metrics,
    compute_adoption_score,
    render_team_dashboard_data,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_archive(path: Path, records: list[dict[str, object]]) -> None:
    """Write a list of dicts as JSONL to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _make_record(
    task_id: str = "t1",
    status: str = "done",
    assigned_agent: str | None = "alice",
    cost_usd: float | None = 0.5,
    owned_files: list[str] | None = None,
) -> dict[str, object]:
    """Build a minimal archive record dict."""
    return {
        "task_id": task_id,
        "title": f"Task {task_id}",
        "role": "backend",
        "status": status,
        "created_at": 1000.0,
        "completed_at": 1060.0,
        "duration_seconds": 60.0,
        "result_summary": None,
        "cost_usd": cost_usd,
        "assigned_agent": assigned_agent,
        "owned_files": owned_files or [],
        "tenant_id": "default",
        "claimed_by_session": None,
    }


# ---------------------------------------------------------------------------
# Frozen dataclass tests
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_user_metrics_frozen(self) -> None:
        um = UserMetrics(
            user_id="alice",
            total_runs=5,
            tasks_completed=4,
            tasks_failed=1,
            total_cost_usd=2.5,
            code_lines_merged=10,
            quality_gate_pass_rate=0.8,
        )
        with pytest.raises(AttributeError):
            um.total_runs = 99  # type: ignore[misc]

    def test_team_metrics_frozen(self) -> None:
        tm = TeamMetrics(
            team_name="squad",
            users=(),
            total_runs=0,
            total_cost_usd=0.0,
            avg_quality_score=0.0,
            adoption_score=0.0,
        )
        with pytest.raises(AttributeError):
            tm.total_runs = 99  # type: ignore[misc]

    def test_user_metrics_fields(self) -> None:
        um = UserMetrics(
            user_id="bob",
            total_runs=3,
            tasks_completed=2,
            tasks_failed=1,
            total_cost_usd=1.0,
            code_lines_merged=5,
            quality_gate_pass_rate=0.6667,
        )
        assert um.user_id == "bob"
        assert um.total_runs == 3
        assert um.tasks_completed == 2
        assert um.tasks_failed == 1
        assert um.total_cost_usd == pytest.approx(1.0)
        assert um.code_lines_merged == 5
        assert um.quality_gate_pass_rate == pytest.approx(0.6667)

    def test_team_metrics_users_is_tuple(self) -> None:
        um = UserMetrics("x", 1, 1, 0, 0.0, 0, 1.0)
        tm = TeamMetrics("team", (um,), 1, 0.0, 1.0, 0.5)
        assert isinstance(tm.users, tuple)
        assert len(tm.users) == 1


# ---------------------------------------------------------------------------
# aggregate_user_metrics
# ---------------------------------------------------------------------------


class TestAggregateUserMetrics:
    def test_empty_archive(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        result = aggregate_user_metrics(archive, "alice")
        assert result.total_runs == 0
        assert result.tasks_completed == 0
        assert result.tasks_failed == 0
        assert result.total_cost_usd == pytest.approx(0.0)
        assert result.code_lines_merged == 0
        assert result.quality_gate_pass_rate == pytest.approx(0.0)

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        archive = tmp_path / "does_not_exist.jsonl"
        result = aggregate_user_metrics(archive, "alice")
        assert result.total_runs == 0

    def test_single_completed_task(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [
                _make_record("t1", "done", "alice", 1.5, ["a.py", "b.py"]),
            ],
        )
        result = aggregate_user_metrics(archive, "alice")
        assert result.total_runs == 1
        assert result.tasks_completed == 1
        assert result.tasks_failed == 0
        assert result.total_cost_usd == pytest.approx(1.5)
        assert result.code_lines_merged == 2
        assert result.quality_gate_pass_rate == pytest.approx(1.0)

    def test_mixed_statuses(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [
                _make_record("t1", "done", "alice", 1.0, ["f1.py"]),
                _make_record("t2", "failed", "alice", 0.5),
                _make_record("t3", "done", "alice", 0.75, ["f2.py", "f3.py"]),
            ],
        )
        result = aggregate_user_metrics(archive, "alice")
        assert result.total_runs == 3
        assert result.tasks_completed == 2
        assert result.tasks_failed == 1
        assert result.total_cost_usd == pytest.approx(2.25)
        assert result.code_lines_merged == 3
        assert result.quality_gate_pass_rate == pytest.approx(2 / 3, rel=1e-3)

    def test_case_insensitive_match(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [
                _make_record("t1", "done", "Alice-Agent-1", 0.1),
            ],
        )
        result = aggregate_user_metrics(archive, "alice")
        assert result.total_runs == 1

    def test_substring_match(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [
                _make_record("t1", "done", "team-alice-01", 0.1),
                _make_record("t2", "done", "bob-agent", 0.2),
            ],
        )
        result = aggregate_user_metrics(archive, "alice")
        assert result.total_runs == 1

    def test_null_cost_treated_as_zero(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [
                _make_record("t1", "done", "alice", None),
            ],
        )
        result = aggregate_user_metrics(archive, "alice")
        assert result.total_cost_usd == pytest.approx(0.0)

    def test_malformed_line_skipped(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with archive.open("w", encoding="utf-8") as f:
            f.write(json.dumps(_make_record("t1", "done", "alice", 1.0)) + "\n")
            f.write("THIS IS NOT JSON\n")
            f.write(json.dumps(_make_record("t2", "done", "alice", 2.0)) + "\n")
        result = aggregate_user_metrics(archive, "alice")
        assert result.total_runs == 2
        assert result.total_cost_usd == pytest.approx(3.0)

    def test_no_matching_agent(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [
                _make_record("t1", "done", "bob"),
            ],
        )
        result = aggregate_user_metrics(archive, "alice")
        assert result.total_runs == 0

    def test_none_assigned_agent_skipped(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [
                _make_record("t1", "done", None),
            ],
        )
        result = aggregate_user_metrics(archive, "alice")
        assert result.total_runs == 0


# ---------------------------------------------------------------------------
# aggregate_team_metrics
# ---------------------------------------------------------------------------


class TestAggregateTeamMetrics:
    def test_empty_team(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(archive, [])
        result = aggregate_team_metrics(archive, "squad", [])
        assert result.team_name == "squad"
        assert result.users == ()
        assert result.total_runs == 0
        assert result.total_cost_usd == pytest.approx(0.0)
        assert result.avg_quality_score == pytest.approx(0.0)
        assert result.adoption_score == pytest.approx(0.0)

    def test_two_users(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [
                _make_record("t1", "done", "alice", 1.0, ["a.py"]),
                _make_record("t2", "done", "alice", 0.5),
                _make_record("t3", "done", "bob", 2.0, ["b.py", "c.py"]),
                _make_record("t4", "failed", "bob", 0.3),
            ],
        )
        result = aggregate_team_metrics(archive, "backend", ["alice", "bob"])
        assert result.team_name == "backend"
        assert len(result.users) == 2
        assert result.total_runs == 4
        assert result.total_cost_usd == pytest.approx(3.8)
        # alice: 1.0 pass rate, bob: 1/2=0.5 => avg 0.75
        assert result.avg_quality_score == pytest.approx(0.75)
        assert 0.0 <= result.adoption_score <= 1.0

    def test_inactive_user_excluded_from_quality_avg(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(
            archive,
            [
                _make_record("t1", "done", "alice", 1.0),
            ],
        )
        # charlie has no tasks at all
        result = aggregate_team_metrics(archive, "team", ["alice", "charlie"])
        # Only alice is active (total_runs > 0), so avg_quality = alice's rate = 1.0
        assert result.avg_quality_score == pytest.approx(1.0)

    def test_users_tuple_is_immutable(self, tmp_path: Path) -> None:
        archive = tmp_path / "tasks.jsonl"
        _write_archive(archive, [])
        result = aggregate_team_metrics(archive, "team", ["alice"])
        assert isinstance(result.users, tuple)


# ---------------------------------------------------------------------------
# compute_adoption_score
# ---------------------------------------------------------------------------


class TestComputeAdoptionScore:
    def test_empty_team_returns_zero(self) -> None:
        tm = TeamMetrics("empty", (), 0, 0.0, 0.0, 0.0)
        assert compute_adoption_score(tm) == pytest.approx(0.0)

    def test_fully_active_high_quality_team(self) -> None:
        users = tuple(UserMetrics(f"u{i}", 50, 50, 0, 0.0, 0, 1.0) for i in range(5))
        tm = TeamMetrics("star", users, 250, 0.0, 1.0, 0.0)
        score = compute_adoption_score(tm)
        # All active (0.4), high freq (close to 0.35), quality 1.0 (0.25)
        assert score >= 0.9

    def test_no_active_users(self) -> None:
        users = tuple(UserMetrics(f"u{i}", 0, 0, 0, 0.0, 0, 0.0) for i in range(3))
        tm = TeamMetrics("idle", users, 0, 0.0, 0.0, 0.0)
        score = compute_adoption_score(tm)
        assert score == pytest.approx(0.0)

    def test_partial_adoption(self) -> None:
        users = (
            UserMetrics("active", 10, 8, 2, 1.0, 5, 0.8),
            UserMetrics("idle1", 0, 0, 0, 0.0, 0, 0.0),
            UserMetrics("idle2", 0, 0, 0, 0.0, 0, 0.0),
        )
        tm = TeamMetrics("mixed", users, 10, 1.0, 0.8, 0.0)
        score = compute_adoption_score(tm)
        # 1/3 active ratio, moderate frequency, quality from avg_quality_score
        assert 0.0 < score < 1.0

    def test_score_monotonic_with_runs(self) -> None:
        scores = []
        for run_count in [1, 5, 10, 50, 100]:
            users = (UserMetrics("u", run_count, run_count, 0, 0.0, 0, 1.0),)
            tm = TeamMetrics("t", users, run_count, 0.0, 1.0, 0.0)
            scores.append(compute_adoption_score(tm))
        # Should be non-decreasing
        for i in range(len(scores) - 1):
            assert scores[i] <= scores[i + 1]

    def test_score_bounded_0_1(self) -> None:
        users = (UserMetrics("u", 10000, 10000, 0, 0.0, 0, 1.0),)
        tm = TeamMetrics("t", users, 10000, 0.0, 1.0, 0.0)
        score = compute_adoption_score(tm)
        assert 0.0 <= score <= 1.0

    def test_frequency_saturation_point(self) -> None:
        """At _FREQ_SATURATION runs/user, frequency signal should be near 1.0."""
        users = (UserMetrics("u", _FREQ_SATURATION, _FREQ_SATURATION, 0, 0.0, 0, 1.0),)
        tm = TeamMetrics("t", users, _FREQ_SATURATION, 0.0, 1.0, 0.0)
        score = compute_adoption_score(tm)
        # frequency component = log1p(50)/log1p(50) = 1.0
        # active ratio = 1.0, quality = 1.0 => max score
        expected_max = 0.4 * 1.0 + 0.35 * 1.0 + 0.25 * 1.0
        assert score == pytest.approx(min(1.0, expected_max))

    def test_active_threshold_boundary(self) -> None:
        """User with exactly _ACTIVE_THRESHOLD runs counts as active."""
        users = (UserMetrics("u", _ACTIVE_THRESHOLD, _ACTIVE_THRESHOLD, 0, 0.0, 0, 1.0),)
        tm = TeamMetrics("t", users, _ACTIVE_THRESHOLD, 0.0, 1.0, 0.0)
        score = compute_adoption_score(tm)
        assert score > 0.0

        # User just below threshold is not active
        below = _ACTIVE_THRESHOLD - 1
        users_below = (UserMetrics("u", below, below, 0, 0.0, 0, 0.0),)
        tm_below = TeamMetrics("t", users_below, below, 0.0, 0.0, 0.0)
        score_below = compute_adoption_score(tm_below)
        # With 0 runs, 0 quality => score should be strictly less than the active case
        assert score_below < score


# ---------------------------------------------------------------------------
# render_team_dashboard_data
# ---------------------------------------------------------------------------


class TestRenderTeamDashboardData:
    def test_empty_team(self) -> None:
        tm = TeamMetrics("team", (), 0, 0.0, 0.0, 0.0)
        data = render_team_dashboard_data(tm)
        assert data["team_name"] == "team"
        assert data["total_runs"] == 0
        assert data["total_cost_usd"] == pytest.approx(0.0)
        assert data["avg_quality_score"] == pytest.approx(0.0)
        assert data["adoption_score"] == pytest.approx(0.0)
        assert data["users"] == []
        assert "timestamp" in data

    def test_with_users(self) -> None:
        users = (
            UserMetrics("alice", 5, 4, 1, 2.0, 10, 0.8),
            UserMetrics("bob", 3, 3, 0, 1.0, 6, 1.0),
        )
        tm = TeamMetrics("squad", users, 8, 3.0, 0.9, 0.85)
        data = render_team_dashboard_data(tm)
        assert data["team_name"] == "squad"
        assert data["total_runs"] == 8
        assert data["total_cost_usd"] == pytest.approx(3.0)
        assert len(data["users"]) == 2  # type: ignore[arg-type]

        alice_data = data["users"][0]  # type: ignore[index]
        assert alice_data["user_id"] == "alice"
        assert alice_data["total_runs"] == 5
        assert alice_data["tasks_completed"] == 4
        assert alice_data["tasks_failed"] == 1
        assert alice_data["total_cost_usd"] == pytest.approx(2.0)
        assert alice_data["code_lines_merged"] == 10
        assert alice_data["quality_gate_pass_rate"] == pytest.approx(0.8)

    def test_json_serialisable(self) -> None:
        users = (UserMetrics("u", 1, 1, 0, 0.1, 1, 1.0),)
        tm = TeamMetrics("t", users, 1, 0.1, 1.0, 0.5)
        data = render_team_dashboard_data(tm)
        # Should not raise
        serialised = json.dumps(data)
        assert isinstance(serialised, str)

    def test_timestamp_is_recent(self) -> None:
        import time

        tm = TeamMetrics("t", (), 0, 0.0, 0.0, 0.0)
        before = time.time()
        data = render_team_dashboard_data(tm)
        after = time.time()
        ts = data["timestamp"]
        assert isinstance(ts, float)
        assert before <= ts <= after


# ---------------------------------------------------------------------------
# Integration: end-to-end from archive to dashboard
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_full_pipeline(self, tmp_path: Path) -> None:
        archive = tmp_path / "archive" / "tasks.jsonl"
        _write_archive(
            archive,
            [
                _make_record("t1", "done", "alice", 1.0, ["a.py"]),
                _make_record("t2", "done", "alice", 0.5, ["b.py"]),
                _make_record("t3", "failed", "alice", 0.2),
                _make_record("t4", "done", "bob", 2.0, ["c.py", "d.py"]),
                _make_record("t5", "done", "bob", 1.5, ["e.py"]),
            ],
        )
        team = aggregate_team_metrics(archive, "eng-team", ["alice", "bob"])
        assert team.total_runs == 5
        assert team.total_cost_usd == pytest.approx(5.2)
        assert len(team.users) == 2

        data = render_team_dashboard_data(team)
        assert data["team_name"] == "eng-team"
        assert data["total_runs"] == 5
        assert 0.0 < data["adoption_score"] <= 1.0  # type: ignore[operator]

        # Verify JSON round-trip
        restored = json.loads(json.dumps(data))
        assert restored["team_name"] == "eng-team"
