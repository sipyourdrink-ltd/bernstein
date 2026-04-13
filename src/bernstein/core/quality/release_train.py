"""Cross-repo release train orchestration.

A release train gates a coordinated release across multiple repositories behind
a single CI-enforced quality signal.  Each repo must pass its own CI checks
before the train can depart.

State is persisted to ``.sdd/metrics/release_train.jsonl`` for inspection via
the CLI or API.  The orchestrator is deterministic: it checks CI status for
each configured repo, records the result, and either gives a green-light or
blocks the release with a structured error report.

Typical usage::

    from pathlib import Path
    from bernstein.core.quality.release_train import ReleaseTrain, ReleaseTrainOrchestrator

    train = ReleaseTrain(
        name="v2.0.0",
        repos=["owner/api", "owner/frontend", "owner/infra"],
        required_checks=["test", "lint", "typecheck"],
    )
    orch = ReleaseTrainOrchestrator(workdir=Path("."))
    result = orch.evaluate(train)
    if result.can_depart:
        # trigger deployment
        ...

The ``gh`` CLI is used for GitHub checks lookup; the orchestrator gracefully
degrades to ``unknown`` status when ``gh`` is unavailable or the repo is not
on GitHub.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class RepoStatus(StrEnum):
    """CI gate result for a single repository."""

    GREEN = "green"
    RED = "red"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReleaseTrain:
    """Configuration for a cross-repo release train.

    Attributes:
        name: Release train name or version (e.g. ``"v2.0.0"``).
        repos: List of ``owner/repo`` GitHub repository slugs that must pass.
        required_checks: CI check names that must be green in every repo.
            When empty, any passing check counts.
        branch: Branch to evaluate CI against (default ``"main"``).
        fail_fast: When True, stop evaluating after the first red repo.
        allow_unknown: When True, ``unknown`` CI status does not block the train.
    """

    name: str
    repos: list[str]
    required_checks: list[str] = field(default_factory=list[str])
    branch: str = "main"
    fail_fast: bool = False
    allow_unknown: bool = False


@dataclass
class RepoCheckResult:
    """CI gate outcome for a single repository.

    Attributes:
        repo: ``owner/repo`` slug.
        status: Aggregate CI status for this repo.
        failing_checks: Names of checks that failed or are missing.
        passing_checks: Names of checks that passed.
        detail: Human-readable status summary.
    """

    repo: str
    status: RepoStatus
    failing_checks: list[str] = field(default_factory=list[str])
    passing_checks: list[str] = field(default_factory=list[str])
    detail: str = ""


@dataclass
class ReleaseTrainResult:
    """Overall result of a release train evaluation.

    Attributes:
        train_name: Name of the evaluated release train.
        can_depart: True if all repos are green (or unknown is allowed).
        repo_results: Per-repo outcomes.
        evaluated_at: ISO-8601 timestamp of the evaluation.
        blocking_repos: Repos that prevented departure.
    """

    train_name: str
    can_depart: bool
    repo_results: list[RepoCheckResult] = field(default_factory=list[RepoCheckResult])
    evaluated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    blocking_repos: list[str] = field(default_factory=list[str])

    def summary(self) -> str:
        """Return a concise human-readable summary line.

        Returns:
            Short summary string suitable for logs or CLI output.
        """
        green = sum(1 for r in self.repo_results if r.status == RepoStatus.GREEN)
        total = len(self.repo_results)
        if self.can_depart:
            return f"Release train '{self.train_name}' is GO — {green}/{total} repos green"
        blocked = ", ".join(self.blocking_repos)
        return f"Release train '{self.train_name}' is BLOCKED — {blocked}"


# ---------------------------------------------------------------------------
# GitHub CI status lookup
# ---------------------------------------------------------------------------


def _gh_check_runs(repo: str, branch: str, timeout: int = 30) -> list[dict[str, Any]]:
    """Fetch the latest check runs for a branch via the ``gh`` CLI.

    Args:
        repo: ``owner/repo`` slug.
        branch: Branch name.
        timeout: Subprocess timeout in seconds.

    Returns:
        List of check-run dicts from the GitHub API.  Empty on error.
    """
    cmd = [
        "gh",
        "api",
        f"/repos/{repo}/commits/{branch}/check-runs",
        "--jq",
        ".check_runs[] | {name: .name, conclusion: .conclusion, status: .status}",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.debug(
                "gh check-runs failed for %s@%s: %s",
                repo,
                branch,
                result.stderr.strip()[:200],
            )
            return []
        # Each line is a JSON object (jq newline-delimited output)
        runs: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return runs
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("gh CLI unavailable for %s: %s", repo, exc)
        return []


def _index_runs_by_name(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index check runs by name, keeping the latest (first-seen) per name."""
    by_name: dict[str, dict[str, Any]] = {}
    for run in runs:
        name = str(run.get("name", ""))
        if name and name not in by_name:
            by_name[name] = run
    return by_name


def _classify_check(check_name: str, run: dict[str, Any] | None) -> tuple[bool, str]:
    """Classify a single check run as passing or failing.

    Returns:
        Tuple of (is_passing, label). When failing, *label* includes the
        reason in parentheses.
    """
    if run is None:
        return False, f"{check_name} (missing)"

    conclusion = str(run.get("conclusion") or "")
    status = str(run.get("status") or "")

    if conclusion == "success":
        return True, check_name
    if conclusion in ("failure", "timed_out", "cancelled", "action_required"):
        return False, f"{check_name} ({conclusion})"
    if status in ("in_progress", "queued", "waiting"):
        return False, f"{check_name} (pending: {status})"
    # neutral / skipped / stale — treat as passing to avoid false blocks
    return True, check_name


def _evaluate_repo(repo: str, train: ReleaseTrain) -> RepoCheckResult:
    """Determine the CI status for a single repo.

    Args:
        repo: ``owner/repo`` slug.
        train: Release train configuration (for branch, required_checks).

    Returns:
        :class:`RepoCheckResult` describing the outcome.
    """
    runs = _gh_check_runs(repo, train.branch)
    if not runs:
        return RepoCheckResult(
            repo=repo,
            status=RepoStatus.UNKNOWN,
            detail="Unable to retrieve CI check runs (gh CLI unavailable or repo inaccessible)",
        )

    by_name = _index_runs_by_name(runs)
    required = set(train.required_checks) if train.required_checks else set(by_name)

    passing: list[str] = []
    failing: list[str] = []

    for check_name in required:
        is_ok, label = _classify_check(check_name, by_name.get(check_name))
        (passing if is_ok else failing).append(label)

    if failing:
        detail = "Failing checks: " + ", ".join(failing)
        return RepoCheckResult(
            repo=repo,
            status=RepoStatus.RED,
            failing_checks=failing,
            passing_checks=passing,
            detail=detail,
        )

    detail = f"All {len(passing)} required checks passed"
    return RepoCheckResult(
        repo=repo,
        status=RepoStatus.GREEN,
        passing_checks=passing,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class ReleaseTrainOrchestrator:
    """Evaluate a :class:`ReleaseTrain` and record the result.

    Args:
        workdir: Project root (used for persisting state to ``.sdd/metrics/``).
    """

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir

    def evaluate(self, train: ReleaseTrain) -> ReleaseTrainResult:
        """Check all repos in the release train and return a go/no-go decision.

        Args:
            train: Release train configuration.

        Returns:
            :class:`ReleaseTrainResult` describing whether the train can depart.
        """
        repo_results: list[RepoCheckResult] = []
        blocking: list[str] = []

        for repo in train.repos:
            result = _evaluate_repo(repo, train)
            repo_results.append(result)

            logger.info(
                "release_train[%s] repo=%s status=%s",
                train.name,
                repo,
                result.status,
            )

            is_blocking = result.status == RepoStatus.RED or (
                result.status == RepoStatus.UNKNOWN and not train.allow_unknown
            )
            if is_blocking:
                blocking.append(repo)
                if train.fail_fast:
                    break

        can_depart = len(blocking) == 0
        train_result = ReleaseTrainResult(
            train_name=train.name,
            can_depart=can_depart,
            repo_results=repo_results,
            blocking_repos=blocking,
        )

        self._persist(train_result)
        logger.info(train_result.summary())
        return train_result

    def get_history(self, train_name: str | None = None) -> list[dict[str, Any]]:
        """Read persisted release train evaluations.

        Args:
            train_name: When provided, filter to events for this train.

        Returns:
            List of raw event dicts, newest first.
        """
        metrics_file = self._workdir / ".sdd" / "metrics" / "release_train.jsonl"
        if not metrics_file.exists():
            return []

        events: list[dict[str, Any]] = []
        for line in metrics_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if train_name is None or event.get("train_name") == train_name:
                events.append(event)

        return list(reversed(events))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _persist(self, result: ReleaseTrainResult) -> None:
        """Append the evaluation result to ``.sdd/metrics/release_train.jsonl``.

        Args:
            result: The release train result to persist.
        """
        metrics_dir = self._workdir / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)

        event: dict[str, Any] = {
            "train_name": result.train_name,
            "can_depart": result.can_depart,
            "evaluated_at": result.evaluated_at,
            "blocking_repos": result.blocking_repos,
            "repos": [
                {
                    "repo": r.repo,
                    "status": r.status,
                    "failing_checks": r.failing_checks,
                    "passing_checks": r.passing_checks,
                    "detail": r.detail,
                }
                for r in result.repo_results
            ],
        }
        try:
            with open(metrics_dir / "release_train.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except OSError as exc:
            logger.debug("Could not persist release train event: %s", exc)
