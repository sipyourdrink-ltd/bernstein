"""Unit tests for run-summary and failure-block rendering."""

from __future__ import annotations

from bernstein.core.planning.run_summary import (
    FailureSummary,
    GateResult,
    ModelCost,
    RunSummary,
    TaskCounts,
    render_failure_block,
    render_summary_block,
)


def test_render_summary_includes_all_subsections() -> None:
    summary = RunSummary(
        pr_url="https://github.com/example/repo/pull/7",
        gate_results=[
            GateResult("tests", True, "1024 passed"),
            GateResult("lint", False, "ruff E501"),
        ],
        model_costs=[
            ModelCost("gpt-4o", 1.23),
            ModelCost("gpt-4o-mini", 0.07),
        ],
        wall_clock_seconds=3725.0,
        agent_time_seconds=1800.0,
        tasks=TaskCounts(completed=8, failed=1, skipped=2),
    )
    rendered = render_summary_block(summary)

    assert "## Run summary" in rendered
    assert "### Gate results" in rendered
    assert "### Cost breakdown" in rendered
    assert "### Duration" in rendered
    assert "### Tasks" in rendered
    assert "https://github.com/example/repo/pull/7" in rendered
    # Total cost row.
    assert "$1.3000" in rendered
    # Duration formatting.
    assert "1h" in rendered
    # Task counts.
    assert "Completed: 8" in rendered
    assert "Failed: 1" in rendered
    assert "Skipped: 2" in rendered
    # Markdown is wrapped in HTML comment so YAML loaders ignore it.
    assert rendered.startswith("<!--\n## Run summary")
    assert rendered.rstrip().endswith("-->")


def test_render_summary_with_empty_inputs_uses_placeholders() -> None:
    rendered = render_summary_block(RunSummary())
    assert "PR: n/a" in rendered
    assert "_none_" in rendered
    assert "Wall-clock: 0s" in rendered
    assert "Agent-time: 0s" in rendered


def test_render_failure_truncates_long_errors() -> None:
    big = "x" * 5000
    rendered = render_failure_block(FailureSummary(failing_stage="lint", last_error=big))
    assert "## Failure reason" in rendered
    assert "lint" in rendered
    assert "(error log truncated" in rendered
    # Truncation guarantees the captured text is shorter than the input.
    assert rendered.count("x") < 5000


def test_render_failure_handles_missing_fields() -> None:
    rendered = render_failure_block(FailureSummary())
    assert "Failing stage: n/a" in rendered
    assert "Failed task ids: n/a" in rendered
    assert "(no error captured)" in rendered
