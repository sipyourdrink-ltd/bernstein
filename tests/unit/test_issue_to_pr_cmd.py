"""CLI regressions for the issue-to-PR trace command (issue #3595)."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.orchestration.issue_to_pr import IssueToPRPipeline, PipelineTrace

DOCUMENTED_COMMAND = "bernstein issue-to-pr trace --repo acme/web 42"


def test_documented_trace_command_renders_pipeline_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command advertised in the orchestration guide must stay executable."""
    docs = Path(__file__).parents[2] / "docs" / "orchestration" / "issue-to-pr.md"
    assert DOCUMENTED_COMMAND in docs.read_text(encoding="utf-8")

    snapshot = PipelineTrace(
        repo="acme/web",
        issue_number=42,
        plan_posted=True,
        approved=True,
        pr_number=4242,
        last_revise_at="2026-05-19T12:00:00Z",
    )

    def fake_trace(_self: IssueToPRPipeline, repo: str, issue_number: int) -> PipelineTrace:
        assert repo == "acme/web"
        assert issue_number == 42
        return snapshot

    monkeypatch.setattr(IssueToPRPipeline, "trace", fake_trace)

    result = CliRunner().invoke(cli, shlex.split(DOCUMENTED_COMMAND)[1:])

    assert result.exit_code == 0, result.output
    assert result.output == f"{snapshot.render()}\n"


def test_trace_rejects_non_numeric_issue_without_reading_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid input is a Click usage error and never reaches the GitHub client."""

    def fail_trace(_self: IssueToPRPipeline, _repo: str, _issue_number: int) -> PipelineTrace:
        pytest.fail("trace must not be called for an invalid issue number")

    monkeypatch.setattr(IssueToPRPipeline, "trace", fail_trace)

    result = CliRunner().invoke(
        cli,
        ["issue-to-pr", "trace", "--repo", "acme/web", "not-a-number"],
    )

    assert result.exit_code == 2
    assert "issue_id must be numeric" in result.output
