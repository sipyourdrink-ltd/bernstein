"""Tests for the PR cost annotation module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bernstein.github_app.cost_reporter import (
    aggregate_pr_cost,
    build_cost_summary,
    post_pr_cost_comment,
)

# ---------------------------------------------------------------------------
# aggregate_pr_cost
# ---------------------------------------------------------------------------


class TestAggregatePrCost:
    def test_empty_list_returns_zero(self) -> None:
        assert aggregate_pr_cost([]) == pytest.approx(0.0)

    def test_sums_cost_fields(self) -> None:
        costs = [{"cost_usd": 0.001}, {"cost_usd": 0.002}, {"cost_usd": 0.003}]
        assert aggregate_pr_cost(costs) == pytest.approx(0.006)

    def test_missing_cost_field_treated_as_zero(self) -> None:
        costs = [{"cost_usd": 0.005}, {"some_other_field": "x"}, {"cost_usd": 0.001}]
        assert aggregate_pr_cost(costs) == pytest.approx(0.006)

    def test_string_cost_converted(self) -> None:
        costs = [{"cost_usd": "0.002"}]
        assert aggregate_pr_cost(costs) == pytest.approx(0.002)


# ---------------------------------------------------------------------------
# build_cost_summary
# ---------------------------------------------------------------------------


class TestBuildCostSummary:
    def test_contains_cost(self) -> None:
        summary = build_cost_summary(cost_usd=0.0042, task_count=3, model="claude-sonnet-4-6")
        assert "0.0042" in summary

    def test_contains_task_count(self) -> None:
        summary = build_cost_summary(cost_usd=0.001, task_count=5, model="claude-opus-4-6")
        assert "5" in summary

    def test_contains_model(self) -> None:
        summary = build_cost_summary(cost_usd=0.001, task_count=1, model="claude-haiku-4-5-20251001")
        assert "claude-haiku-4-5-20251001" in summary

    def test_contains_annotation_marker(self) -> None:
        summary = build_cost_summary(cost_usd=0.0, task_count=0, model="x")
        assert "bernstein-cost-annotation" in summary


# ---------------------------------------------------------------------------
# post_pr_cost_comment — mocked gh
# ---------------------------------------------------------------------------


class TestPostPrCostComment:
    def _mock_find_no_existing(self) -> MagicMock:
        """Return a mock subprocess result that reports no existing comment."""
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = "null\n"
        mock.stderr = ""
        return mock

    def _mock_create_success(self) -> MagicMock:
        """Return a mock subprocess result indicating successful comment creation."""
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = b'{"id": 123}'
        mock.stderr = b""
        return mock

    def test_creates_new_comment_when_none_exists(self) -> None:
        find_mock = self._mock_find_no_existing()
        create_mock = self._mock_create_success()

        call_count = 0

        def side_effect(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return find_mock
            return create_mock

        with patch("subprocess.run", side_effect=side_effect):
            result = post_pr_cost_comment(
                pr_number=42,
                repo="acme/widgets",
                cost_usd=0.0035,
                task_count=2,
                model="claude-sonnet-4-6",
            )

        assert result is True

    def test_updates_existing_comment_when_found(self) -> None:
        find_mock = MagicMock()
        find_mock.returncode = 0
        find_mock.stdout = "789\n"  # Existing comment ID
        find_mock.stderr = ""

        update_mock = MagicMock()
        update_mock.returncode = 0
        update_mock.stdout = b'{"id": 789}'
        update_mock.stderr = b""

        call_count = 0

        def side_effect(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return find_mock
            return update_mock

        with patch("subprocess.run", side_effect=side_effect):
            result = post_pr_cost_comment(
                pr_number=10,
                repo="acme/widgets",
                cost_usd=0.001,
            )

        assert result is True

    def test_gh_failure_returns_false(self) -> None:
        find_mock = self._mock_find_no_existing()
        fail_mock = MagicMock()
        fail_mock.returncode = 1
        fail_mock.stdout = b""
        fail_mock.stderr = b"Forbidden"

        call_count = 0

        def side_effect(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return find_mock
            return fail_mock

        with patch("subprocess.run", side_effect=side_effect):
            result = post_pr_cost_comment(
                pr_number=99,
                repo="acme/widgets",
                cost_usd=0.002,
            )

        assert result is False

    def test_file_not_found_returns_false(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
            result = post_pr_cost_comment(
                pr_number=1,
                repo="acme/widgets",
                cost_usd=0.0,
            )
        assert result is False
