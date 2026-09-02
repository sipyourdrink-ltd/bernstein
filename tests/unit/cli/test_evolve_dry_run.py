"""Tests for ``evolve run --dry-run`` and failure-pattern draft GitHub sync.

The drafts an operator sees under ``--dry-run`` are the run-ledger failure
patterns from :mod:`bernstein.core.persistence.runs_report` -- the same rows
``bernstein runs report`` classifies -- not a second signal derived from live
task metrics. A pattern's identity on GitHub is its fingerprint, carried as a
label, so a recurring failure whose occurrence count grows updates one issue
instead of filing a new one every cycle.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.evolve_cmd import (
    _generate_failure_drafts,
    _show_failure_drafts,
    _sync_failure_drafts_to_github,
    evolve_run,
)
from bernstein.cli.helpers import console as cli_console
from bernstein.core.persistence.runs_report import (
    FailurePatternDraft,
    RunWrapUp,
)
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_CLOSED,
    KIND_RUN_OPEN,
    KIND_TASK_COMPLETED,
    KIND_TASK_SCHEDULED,
    KIND_TASK_STARTED,
    WorkLedger,
    run_ledger_dir,
)
from bernstein.evolution.aggregator import FileMetricsCollector, TaskMetrics

# ---------------------------------------------------------------------------
# Fixtures: real ledger directories on disk (same shape as tests/unit/test_runs_report.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pinned_render_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the console width so table assertions do not depend on the runner.

    Drafts render through a Rich table, which wraps every cell to the console's
    width. That width is resolved once, when the shared console is built at
    import time, so setting ``COLUMNS`` here would be too late -- the width has
    to be set on the console itself. Without the pin, an assertion on a
    rendered title passes on a wide runner and fails on a narrow one.
    """
    monkeypatch.setattr(cli_console, "_width", 200)


def _seed_closed_run(root: Path, run_id: str, *, wrapup: RunWrapUp) -> None:
    """Write a real closed-run ledger under ``root/.sdd``."""
    ledger = WorkLedger.open(run_ledger_dir(root / ".sdd", run_id))
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": run_id})
    ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t1")
    ledger.append(kind=KIND_TASK_STARTED, task_id="t1")
    ledger.append(kind=KIND_TASK_COMPLETED, task_id="t1")
    payload: dict[str, object] = {"run_id": run_id}
    payload.update(wrapup.to_payload())
    ledger.append(kind=KIND_RUN_CLOSED, payload=payload)
    ledger.close()


def _seed_failing_task_metrics(state_dir: Path, *, role: str, count: int) -> None:
    """Record enough failing task metrics to trip the live-metric detector."""
    collector = FileMetricsCollector(state_dir)
    for i in range(count):
        collector.record_task_metrics(
            TaskMetrics(
                timestamp=1_000.0 + i,
                task_id=f"{role}-{i}",
                role=role,
                model="sonnet",
                cost_usd=0.10,
                janitor_passed=False,
            )
        )


# ---------------------------------------------------------------------------
# 1-3, 8: the draft source is the run ledger
# ---------------------------------------------------------------------------


class TestDraftsComeFromRunLedgers:
    """``_generate_failure_drafts`` reads classified runs, not live metrics."""

    def test_drafts_come_from_run_ledgers_not_live_task_metrics(self, tmp_path: Path) -> None:
        """A failing ledger produces a draft; failing task metrics alone do not."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir(parents=True, exist_ok=True)
        # Live task metrics that the old detector would have turned into a draft.
        _seed_failing_task_metrics(state_dir, role="backend", count=5)
        # One real gate failure in the ledger.
        _seed_closed_run(tmp_path, "run-gate", wrapup=RunWrapUp(gate_name="lint", failing_check="ruff check ."))

        drafts = _generate_failure_drafts(state_dir)

        assert [type(d) for d in drafts] == [FailurePatternDraft]
        draft = drafts[0]
        assert draft.fingerprint
        assert draft.contributing_run_ids == ["run-gate"]
        assert "ruff check ." in draft.title
        # The role name from the live-metric source never reaches a draft.
        assert "backend" not in draft.title
        assert "backend" not in draft.body

    def test_repeated_scan_of_unchanged_ledgers_yields_identical_fingerprints(self, tmp_path: Path) -> None:
        """A second pass over the same ledgers produces zero new fingerprints."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir(parents=True, exist_ok=True)
        _seed_closed_run(tmp_path, "run-a", wrapup=RunWrapUp(gate_name="lint", failing_check="ruff check ."))
        _seed_closed_run(tmp_path, "run-b", wrapup=RunWrapUp(error_kind="adapter", error_message="exited 137"))

        first = {d.fingerprint for d in _generate_failure_drafts(state_dir)}
        second = {d.fingerprint for d in _generate_failure_drafts(state_dir)}

        assert first == second
        assert second - first == set()

    def test_successful_runs_never_contribute_to_a_draft(self, tmp_path: Path) -> None:
        """PR_OPENED and NO_CHANGES runs are absent from every draft."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir(parents=True, exist_ok=True)
        _seed_closed_run(tmp_path, "run-pr", wrapup=RunWrapUp(branch="fix/thing", pr_number=7))
        _seed_closed_run(tmp_path, "run-nochange", wrapup=RunWrapUp(commits_over_base=0))
        _seed_closed_run(tmp_path, "run-gate", wrapup=RunWrapUp(gate_name="lint", failing_check="ruff check ."))

        drafts = _generate_failure_drafts(state_dir)

        contributing = {rid for d in drafts for rid in d.contributing_run_ids}
        assert contributing == {"run-gate"}

    def test_draft_body_states_occurrence_count_and_most_recent_run_id(self, tmp_path: Path) -> None:
        """The body an operator reads names how often and which run last hit it."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir(parents=True, exist_ok=True)
        for run_id in ("run-1", "run-2", "run-3"):
            _seed_closed_run(tmp_path, run_id, wrapup=RunWrapUp(gate_name="lint", failing_check="ruff check ."))

        drafts = _generate_failure_drafts(state_dir)

        assert len(drafts) == 1
        draft = drafts[0]
        assert draft.occurrence_count == 3
        assert "3" in draft.body
        assert draft.most_recent_run_id in draft.body

    def test_initialised_workspace_with_no_finished_runs_produces_no_drafts(self, tmp_path: Path) -> None:
        """An initialised workspace nothing has run in yields an empty list, not an error."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir(parents=True, exist_ok=True)

        assert _generate_failure_drafts(state_dir) == []


# ---------------------------------------------------------------------------
# 4-5: GitHub identity is the fingerprint
# ---------------------------------------------------------------------------


def _draft(
    *,
    fingerprint: str,
    title: str,
    occurrence_count: int = 1,
    run_id: str = "run-1",
) -> FailurePatternDraft:
    return FailurePatternDraft(
        fingerprint=fingerprint,
        title=title,
        body="body",
        occurrence_count=occurrence_count,
        most_recent_run_id=run_id,
        contributing_run_ids=[run_id],
        sample_evidence="lint: ruff check .",
    )


class TestSyncFailureDraftsToGithub:
    """Fingerprint -> issue identity, with the ``gh`` CLI mocked out."""

    def test_recurring_pattern_comments_on_one_issue_as_occurrence_count_grows(self) -> None:
        """Same fingerprint, a title that changed: comment, never a second issue.

        This is the load-bearing property. Title-derived identity files a new
        issue on every cycle a recurring failure recurs, because the title
        carries the occurrence count.
        """
        fingerprint = "a" * 64
        first_pass = [_draft(fingerprint=fingerprint, title="GATE_FAILED: lint (3 runs)", occurrence_count=3)]
        second_pass = [_draft(fingerprint=fingerprint, title="GATE_FAILED: lint (4 runs)", occurrence_count=4)]

        mock_gh = MagicMock()
        mock_gh.available = True
        existing = MagicMock()
        existing.number = 501
        mock_gh.find_by_fingerprint.side_effect = [None, existing]
        mock_gh.create_issue.return_value = MagicMock(number=501)
        mock_gh.comment_on_issue.return_value = True

        with patch("bernstein.core.git.github.GitHubClient", return_value=mock_gh):
            _sync_failure_drafts_to_github(first_pass, "owner/repo")
            _sync_failure_drafts_to_github(second_pass, "owner/repo")

        assert mock_gh.create_issue.call_count == 1
        assert mock_gh.create_issue.call_args.kwargs["fingerprint"] == fingerprint
        assert mock_gh.comment_on_issue.call_count == 1
        assert mock_gh.comment_on_issue.call_args[0][0] == 501

    def test_each_new_fingerprint_creates_exactly_one_issue(self) -> None:
        """One create per new fingerprint, no comments."""
        drafts = [
            _draft(fingerprint="a" * 64, title="GATE_FAILED: lint"),
            _draft(fingerprint="b" * 64, title="INFRA_ERROR: adapter"),
        ]

        mock_gh = MagicMock()
        mock_gh.available = True
        mock_gh.find_by_fingerprint.return_value = None
        mock_gh.create_issue.return_value = MagicMock(number=1)

        with patch("bernstein.core.git.github.GitHubClient", return_value=mock_gh):
            _sync_failure_drafts_to_github(drafts, "owner/repo")

        assert mock_gh.create_issue.call_count == 2
        created = {call.kwargs["fingerprint"] for call in mock_gh.create_issue.call_args_list}
        assert created == {"a" * 64, "b" * 64}
        mock_gh.comment_on_issue.assert_not_called()

    def test_skips_when_gh_unavailable(self) -> None:
        """No ``gh`` CLI means no lookups and no writes."""
        mock_gh = MagicMock()
        mock_gh.available = False

        with patch("bernstein.core.git.github.GitHubClient", return_value=mock_gh):
            _sync_failure_drafts_to_github([_draft(fingerprint="a" * 64, title="t")], "owner/repo")

        mock_gh.find_by_fingerprint.assert_not_called()
        mock_gh.create_issue.assert_not_called()


# ---------------------------------------------------------------------------
# 6: --dry-run does not reach the network
# ---------------------------------------------------------------------------


def test_dry_run_without_github_makes_no_subprocess_calls(tmp_path: Path) -> None:
    """``evolve run --dry-run`` prints drafts and shells out to nothing."""
    state_dir = tmp_path / ".sdd"
    state_dir.mkdir(parents=True, exist_ok=True)
    _seed_closed_run(tmp_path, "run-gate", wrapup=RunWrapUp(gate_name="lint", failing_check="ruff check ."))

    runner = CliRunner()
    with patch("bernstein.core.git.github.subprocess.run") as mock_run:
        result = runner.invoke(evolve_run, ["--dry-run", "--dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    mock_run.assert_not_called()
    assert "ruff check ." in result.output


# ---------------------------------------------------------------------------
# _show_failure_drafts routing
# ---------------------------------------------------------------------------


class TestShowFailureDrafts:
    """Drafts render, and only reach GitHub when the operator asked."""

    def test_no_drafts_prints_message(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An empty draft list says so instead of printing an empty table."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir(parents=True, exist_ok=True)
        with patch("bernstein.cli.commands.evolve_cmd._generate_failure_drafts", return_value=[]):
            _show_failure_drafts(tmp_path, state_dir, github_sync=False, github_repo=None)

        out = capsys.readouterr().out
        assert "No failure-pattern drafts found." in out
        assert "Failure-Pattern Drafts" not in out

    def test_draft_rows_reach_the_operator(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Every rendered row carries the identity, the count and the last run."""
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir(parents=True, exist_ok=True)
        fingerprint = "abcdef12" + "0" * 56
        drafts = [_draft(fingerprint=fingerprint, title="GATE_FAILED: lint", occurrence_count=4, run_id="run-7")]

        with patch("bernstein.cli.commands.evolve_cmd._generate_failure_drafts", return_value=drafts):
            _show_failure_drafts(tmp_path, state_dir, github_sync=False, github_repo=None)

        out = capsys.readouterr().out
        assert "abcdef12" in out
        assert "GATE_FAILED: lint" in out
        assert "4" in out
        assert "run-7" in out

    def test_github_sync_called_when_enabled(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir(parents=True, exist_ok=True)
        drafts = [_draft(fingerprint="a" * 64, title="GATE_FAILED: lint")]

        with (
            patch("bernstein.cli.commands.evolve_cmd._generate_failure_drafts", return_value=drafts),
            patch("bernstein.cli.commands.evolve_cmd._sync_failure_drafts_to_github") as mock_sync,
        ):
            _show_failure_drafts(tmp_path, state_dir, github_sync=True, github_repo="owner/repo")

        mock_sync.assert_called_once_with(drafts, "owner/repo")

    def test_github_sync_not_called_when_disabled(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir(parents=True, exist_ok=True)
        drafts = [_draft(fingerprint="a" * 64, title="GATE_FAILED: lint")]

        with (
            patch("bernstein.cli.commands.evolve_cmd._generate_failure_drafts", return_value=drafts),
            patch("bernstein.cli.commands.evolve_cmd._sync_failure_drafts_to_github") as mock_sync,
        ):
            _show_failure_drafts(tmp_path, state_dir, github_sync=False, github_repo=None)

        mock_sync.assert_not_called()
