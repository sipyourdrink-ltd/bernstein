"""Tests for cross-repo release train orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.release_train import (
    ReleaseTrain,
    ReleaseTrainOrchestrator,
    RepoCheckResult,
    RepoStatus,
    _evaluate_repo,
    _gh_check_runs,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def orchestrator(workdir: Path) -> ReleaseTrainOrchestrator:
    return ReleaseTrainOrchestrator(workdir=workdir)


SAMPLE_TRAIN = ReleaseTrain(
    name="v1.0.0",
    repos=["owner/api", "owner/frontend"],
    required_checks=["test", "lint"],
    branch="main",
)

# ---------------------------------------------------------------------------
# _gh_check_runs — unit tests with subprocess mocked
# ---------------------------------------------------------------------------


def _make_run(name: str, conclusion: str = "success", status: str = "completed") -> str:
    return json.dumps({"name": name, "conclusion": conclusion, "status": status})


@patch("bernstein.core.quality.release_train.subprocess.run")
def test_gh_check_runs_parses_jq_output(mock_run) -> None:
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "\n".join([_make_run("test", "success"), _make_run("lint", "success")])
    mock_run.return_value.stderr = ""

    runs = _gh_check_runs("owner/repo", "main")
    assert len(runs) == 2
    assert runs[0]["name"] == "test"


@patch("bernstein.core.quality.release_train.subprocess.run")
def test_gh_check_runs_returns_empty_on_nonzero_exit(mock_run) -> None:
    mock_run.return_value.returncode = 1
    mock_run.return_value.stdout = ""
    mock_run.return_value.stderr = "not found"

    runs = _gh_check_runs("owner/missing", "main")
    assert runs == []


@patch("bernstein.core.quality.release_train.subprocess.run", side_effect=FileNotFoundError)
def test_gh_check_runs_returns_empty_when_gh_missing(mock_run) -> None:
    runs = _gh_check_runs("owner/repo", "main")
    assert runs == []


# ---------------------------------------------------------------------------
# _evaluate_repo
# ---------------------------------------------------------------------------


@patch("bernstein.core.quality.release_train._gh_check_runs")
def test_evaluate_repo_green_when_all_checks_pass(mock_runs) -> None:
    mock_runs.return_value = [
        {"name": "test", "conclusion": "success", "status": "completed"},
        {"name": "lint", "conclusion": "success", "status": "completed"},
    ]
    train = ReleaseTrain(name="v1", repos=["owner/api"], required_checks=["test", "lint"])
    result = _evaluate_repo("owner/api", train)

    assert result.status == RepoStatus.GREEN
    assert result.failing_checks == []
    assert set(result.passing_checks) == {"test", "lint"}


@patch("bernstein.core.quality.release_train._gh_check_runs")
def test_evaluate_repo_red_when_check_fails(mock_runs) -> None:
    mock_runs.return_value = [
        {"name": "test", "conclusion": "failure", "status": "completed"},
        {"name": "lint", "conclusion": "success", "status": "completed"},
    ]
    train = ReleaseTrain(name="v1", repos=["owner/api"], required_checks=["test", "lint"])
    result = _evaluate_repo("owner/api", train)

    assert result.status == RepoStatus.RED
    assert any("test" in f for f in result.failing_checks)


@patch("bernstein.core.quality.release_train._gh_check_runs")
def test_evaluate_repo_red_when_required_check_missing(mock_runs) -> None:
    mock_runs.return_value = [
        {"name": "lint", "conclusion": "success", "status": "completed"},
        # "test" is absent
    ]
    train = ReleaseTrain(name="v1", repos=["owner/api"], required_checks=["test", "lint"])
    result = _evaluate_repo("owner/api", train)

    assert result.status == RepoStatus.RED
    assert any("test" in f for f in result.failing_checks)


@patch("bernstein.core.quality.release_train._gh_check_runs")
def test_evaluate_repo_red_when_check_timed_out(mock_runs) -> None:
    mock_runs.return_value = [
        {"name": "test", "conclusion": "timed_out", "status": "completed"},
    ]
    train = ReleaseTrain(name="v1", repos=["owner/api"], required_checks=["test"])
    result = _evaluate_repo("owner/api", train)
    assert result.status == RepoStatus.RED


@patch("bernstein.core.quality.release_train._gh_check_runs")
def test_evaluate_repo_unknown_when_gh_unavailable(mock_runs) -> None:
    mock_runs.return_value = []
    train = ReleaseTrain(name="v1", repos=["owner/api"], required_checks=["test"])
    result = _evaluate_repo("owner/api", train)
    assert result.status == RepoStatus.UNKNOWN


@patch("bernstein.core.quality.release_train._gh_check_runs")
def test_evaluate_repo_in_progress_check_is_red(mock_runs) -> None:
    mock_runs.return_value = [
        {"name": "test", "conclusion": None, "status": "in_progress"},
    ]
    train = ReleaseTrain(name="v1", repos=["owner/api"], required_checks=["test"])
    result = _evaluate_repo("owner/api", train)
    assert result.status == RepoStatus.RED


# ---------------------------------------------------------------------------
# ReleaseTrainOrchestrator
# ---------------------------------------------------------------------------


@patch("bernstein.core.quality.release_train._evaluate_repo")
def test_orchestrator_go_when_all_repos_green(mock_eval, orchestrator) -> None:
    mock_eval.return_value = RepoCheckResult(
        repo="owner/api",
        status=RepoStatus.GREEN,
        passing_checks=["test", "lint"],
        detail="All checks passed",
    )

    result = orchestrator.evaluate(SAMPLE_TRAIN)
    assert result.can_depart is True
    assert result.blocking_repos == []


@patch("bernstein.core.quality.release_train._evaluate_repo")
def test_orchestrator_blocked_when_repo_red(mock_eval, orchestrator) -> None:
    def side_effect(repo, train):
        if repo == "owner/api":
            return RepoCheckResult(repo=repo, status=RepoStatus.RED, failing_checks=["test (failure)"])
        return RepoCheckResult(repo=repo, status=RepoStatus.GREEN)

    mock_eval.side_effect = side_effect

    result = orchestrator.evaluate(SAMPLE_TRAIN)
    assert result.can_depart is False
    assert "owner/api" in result.blocking_repos


@patch("bernstein.core.quality.release_train._evaluate_repo")
def test_orchestrator_unknown_blocks_by_default(mock_eval, orchestrator) -> None:
    mock_eval.return_value = RepoCheckResult(repo="owner/api", status=RepoStatus.UNKNOWN)
    train = ReleaseTrain(name="v1", repos=["owner/api"], allow_unknown=False)

    result = orchestrator.evaluate(train)
    assert result.can_depart is False
    assert "owner/api" in result.blocking_repos


@patch("bernstein.core.quality.release_train._evaluate_repo")
def test_orchestrator_unknown_allowed_does_not_block(mock_eval, orchestrator) -> None:
    mock_eval.return_value = RepoCheckResult(repo="owner/api", status=RepoStatus.UNKNOWN)
    train = ReleaseTrain(name="v1", repos=["owner/api"], allow_unknown=True)

    result = orchestrator.evaluate(train)
    assert result.can_depart is True


@patch("bernstein.core.quality.release_train._evaluate_repo")
def test_orchestrator_fail_fast_stops_early(mock_eval, orchestrator) -> None:
    call_count = 0

    def side_effect(repo, train):
        nonlocal call_count
        call_count += 1
        return RepoCheckResult(repo=repo, status=RepoStatus.RED)

    mock_eval.side_effect = side_effect
    train = ReleaseTrain(name="v1", repos=["owner/a", "owner/b", "owner/c"], fail_fast=True)

    result = orchestrator.evaluate(train)
    assert result.can_depart is False
    assert call_count == 1  # stopped after first failure


@patch("bernstein.core.quality.release_train._evaluate_repo")
def test_orchestrator_persists_result(mock_eval, orchestrator, workdir) -> None:
    mock_eval.return_value = RepoCheckResult(repo="owner/api", status=RepoStatus.GREEN)
    train = ReleaseTrain(name="v1.2.3", repos=["owner/api"])

    orchestrator.evaluate(train)

    metrics_file = workdir / ".sdd" / "metrics" / "release_train.jsonl"
    assert metrics_file.exists()
    event = json.loads(metrics_file.read_text().strip())
    assert event["train_name"] == "v1.2.3"
    assert event["can_depart"] is True


@patch("bernstein.core.quality.release_train._evaluate_repo")
def test_orchestrator_get_history_filters_by_name(mock_eval, orchestrator) -> None:
    mock_eval.return_value = RepoCheckResult(repo="owner/api", status=RepoStatus.GREEN)

    orchestrator.evaluate(ReleaseTrain(name="v1", repos=["owner/api"]))
    orchestrator.evaluate(ReleaseTrain(name="v2", repos=["owner/api"]))

    history_v1 = orchestrator.get_history("v1")
    history_v2 = orchestrator.get_history("v2")
    all_history = orchestrator.get_history()

    assert len(history_v1) == 1
    assert len(history_v2) == 1
    assert len(all_history) == 2


@patch("bernstein.core.quality.release_train._evaluate_repo")
def test_orchestrator_get_history_empty_when_no_file(mock_eval, orchestrator) -> None:
    history = orchestrator.get_history()
    assert history == []


# ---------------------------------------------------------------------------
# ReleaseTrainResult.summary()
# ---------------------------------------------------------------------------


def test_result_summary_go() -> None:
    pytest.importorskip("bernstein.core.release_train")
    from bernstein.core.release_train import ReleaseTrainResult

    r = ReleaseTrainResult(
        train_name="v2",
        can_depart=True,
        repo_results=[RepoCheckResult(repo="a/b", status=RepoStatus.GREEN)],
    )
    assert "GO" in r.summary()
    assert "v2" in r.summary()


def test_result_summary_blocked() -> None:
    from bernstein.core.release_train import ReleaseTrainResult

    r = ReleaseTrainResult(
        train_name="v3",
        can_depart=False,
        blocking_repos=["owner/broken"],
    )
    summary = r.summary()
    assert "BLOCKED" in summary
    assert "owner/broken" in summary
