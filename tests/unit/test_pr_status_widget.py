"""Unit tests for the embeddable PR status widget."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bernstein.core.quality.pr_status_widget import (
    WIDGET_SENTINEL,
    RunSummary,
    StatusWidget,
    build_run_summary,
    generate_badge_svg,
    inject_widget_into_pr,
    render_widget_markdown,
    replace_widget_block,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PASSING_METRICS: dict[str, object] = {
    "agents_used": ["claude", "codex"],
    "total_cost_usd": 1.23,
    "quality_gate_passed": True,
    "quality_score": 92,
    "duration_seconds": 180.0,
    "tasks_completed": 5,
    "tasks_failed": 0,
}

_FAILING_METRICS: dict[str, object] = {
    "agents_used": ["gemini"],
    "total_cost_usd": 0.50,
    "quality_gate_passed": False,
    "quality_score": 45,
    "duration_seconds": 60.0,
    "tasks_completed": 2,
    "tasks_failed": 3,
}


def _make_summary(*, passed: bool = True) -> RunSummary:
    metrics = _PASSING_METRICS if passed else _FAILING_METRICS
    return build_run_summary("run-001", metrics)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# build_run_summary
# ---------------------------------------------------------------------------


class TestBuildRunSummary:
    def test_basic_passing(self) -> None:
        s = build_run_summary("run-abc", _PASSING_METRICS)  # type: ignore[arg-type]
        assert s.run_id == "run-abc"
        assert s.agents_used == ["claude", "codex"]
        assert s.total_cost_usd == 1.23
        assert s.quality_gate_passed is True
        assert s.quality_score == 92
        assert s.duration_seconds == 180.0
        assert s.tasks_completed == 5
        assert s.tasks_failed == 0

    def test_basic_failing(self) -> None:
        s = build_run_summary("run-xyz", _FAILING_METRICS)  # type: ignore[arg-type]
        assert s.quality_gate_passed is False
        assert s.quality_score == 45
        assert s.tasks_failed == 3

    def test_missing_keys_use_defaults(self) -> None:
        s = build_run_summary("run-empty", {})
        assert s.agents_used == []
        assert s.total_cost_usd == 0.0
        assert s.quality_gate_passed is False
        assert s.quality_score == 0
        assert s.tasks_completed == 0

    def test_invalid_types_use_defaults(self) -> None:
        bad: dict[str, object] = {
            "total_cost_usd": "not-a-number",
            "quality_score": None,
            "agents_used": "not-a-list",
        }
        s = build_run_summary("run-bad", bad)
        assert s.total_cost_usd == 0.0
        assert s.quality_score == 0
        assert s.agents_used == []

    def test_tuple_agents_coerced_to_list(self) -> None:
        m: dict[str, object] = {"agents_used": ("a", "b")}
        s = build_run_summary("run-tuple", m)
        assert s.agents_used == ["a", "b"]

    def test_frozen(self) -> None:
        s = _make_summary()
        with pytest.raises(AttributeError):
            s.run_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# render_widget_markdown
# ---------------------------------------------------------------------------


class TestRenderWidgetMarkdown:
    def test_contains_sentinel(self) -> None:
        md = render_widget_markdown(_make_summary())
        assert md.startswith(WIDGET_SENTINEL)
        assert md.endswith(WIDGET_SENTINEL)

    def test_passing_content(self) -> None:
        md = render_widget_markdown(_make_summary(passed=True))
        assert "passed" in md
        assert "white_check_mark" in md
        assert "92/100" in md
        assert "`run-001`" in md

    def test_failing_content(self) -> None:
        md = render_widget_markdown(_make_summary(passed=False))
        assert "failed" in md
        assert ":x:" in md
        assert "45/100" in md

    def test_duration_in_minutes(self) -> None:
        md = render_widget_markdown(_make_summary())
        # 180 seconds = 3.0 min
        assert "3.0 min" in md

    def test_agents_listed(self) -> None:
        md = render_widget_markdown(_make_summary())
        assert "claude, codex" in md

    def test_no_agents_shows_none(self) -> None:
        s = build_run_summary("r", {})
        md = render_widget_markdown(s)
        assert "none" in md

    def test_cost_formatted(self) -> None:
        md = render_widget_markdown(_make_summary())
        assert "$1.23" in md


# ---------------------------------------------------------------------------
# generate_badge_svg
# ---------------------------------------------------------------------------


class TestGenerateBadgeSvg:
    def test_passing_high_score_is_green(self) -> None:
        svg = generate_badge_svg(_make_summary(passed=True))
        assert "<svg" in svg
        assert "</svg>" in svg
        assert "#4c1" in svg  # green
        assert "score 92" in svg

    def test_failing_is_red(self) -> None:
        svg = generate_badge_svg(_make_summary(passed=False))
        assert "#e05d44" in svg  # red
        assert "failed" in svg

    def test_passing_low_score_is_yellow(self) -> None:
        metrics: dict[str, object] = {
            **_PASSING_METRICS,
            "quality_score": 65,
        }
        s = build_run_summary("r", metrics)  # type: ignore[arg-type]
        svg = generate_badge_svg(s)
        assert "#dfb317" in svg  # yellow

    def test_label_present(self) -> None:
        svg = generate_badge_svg(_make_summary())
        assert "bernstein" in svg

    def test_valid_xml(self) -> None:
        svg = generate_badge_svg(_make_summary())
        assert svg.startswith("<svg")
        assert svg.endswith("</svg>")


# ---------------------------------------------------------------------------
# replace_widget_block (internal helper)
# ---------------------------------------------------------------------------


class TestReplaceWidgetBlock:
    def test_append_to_empty_body(self) -> None:
        result = replace_widget_block("", "NEW")
        assert result == "NEW"

    def test_append_to_existing_body(self) -> None:
        result = replace_widget_block("Hello PR", "WIDGET")
        assert result == "Hello PR\n\nWIDGET"

    def test_replace_existing_block(self) -> None:
        old_block = f"{WIDGET_SENTINEL}\nold stuff\n{WIDGET_SENTINEL}"
        body = f"PR description\n\n{old_block}"
        result = replace_widget_block(body, "NEW_WIDGET")
        assert "old stuff" not in result
        assert result == "PR description\n\nNEW_WIDGET"

    def test_preserves_trailing_content(self) -> None:
        old_block = f"{WIDGET_SENTINEL}\nold\n{WIDGET_SENTINEL}"
        body = f"Header\n\n{old_block}\n\nFooter"
        result = replace_widget_block(body, "NEW")
        assert "Footer" in result
        assert "NEW" in result


# ---------------------------------------------------------------------------
# inject_widget_into_pr
# ---------------------------------------------------------------------------


class TestInjectWidgetIntoPr:
    @patch("bernstein.core.quality.pr_status_widget.subprocess.run")
    def test_success(self, mock_run: object) -> None:
        from unittest.mock import MagicMock

        mock = MagicMock()
        mock.stdout = "Existing body"
        mock.returncode = 0

        assert isinstance(mock_run, MagicMock)
        mock_run.return_value = mock

        widget = StatusWidget(markdown="WIDGET_MD", badge_url="", details_url="")
        result = inject_widget_into_pr(42, widget)

        assert result is True
        assert mock_run.call_count == 2

        # Verify the edit call includes the widget
        edit_call = mock_run.call_args_list[1]
        assert "gh" in edit_call.args[0]
        assert "edit" in edit_call.args[0]
        assert "WIDGET_MD" in edit_call.args[0][-1]

    @patch("bernstein.core.quality.pr_status_widget.subprocess.run")
    def test_view_failure_returns_false(self, mock_run: object) -> None:
        import subprocess as sp
        from unittest.mock import MagicMock

        assert isinstance(mock_run, MagicMock)
        mock_run.side_effect = sp.CalledProcessError(1, "gh")

        widget = StatusWidget(markdown="W", badge_url="", details_url="")
        assert inject_widget_into_pr(1, widget) is False

    @patch("bernstein.core.quality.pr_status_widget.subprocess.run")
    def test_edit_failure_returns_false(self, mock_run: object) -> None:
        import subprocess as sp
        from unittest.mock import MagicMock

        view_result = MagicMock()
        view_result.stdout = "body"
        view_result.returncode = 0

        assert isinstance(mock_run, MagicMock)
        mock_run.side_effect = [view_result, sp.CalledProcessError(1, "gh")]

        widget = StatusWidget(markdown="W", badge_url="", details_url="")
        assert inject_widget_into_pr(1, widget) is False

    @patch("bernstein.core.quality.pr_status_widget.subprocess.run")
    def test_gh_not_found_returns_false(self, mock_run: object) -> None:
        from unittest.mock import MagicMock

        assert isinstance(mock_run, MagicMock)
        mock_run.side_effect = FileNotFoundError("gh not found")

        widget = StatusWidget(markdown="W", badge_url="", details_url="")
        assert inject_widget_into_pr(1, widget) is False

    @patch("bernstein.core.quality.pr_status_widget.subprocess.run")
    def test_timeout_returns_false(self, mock_run: object) -> None:
        import subprocess as sp
        from unittest.mock import MagicMock

        assert isinstance(mock_run, MagicMock)
        mock_run.side_effect = sp.TimeoutExpired("gh", 30)

        widget = StatusWidget(markdown="W", badge_url="", details_url="")
        assert inject_widget_into_pr(1, widget) is False
