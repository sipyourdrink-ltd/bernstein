"""A refused merge keeps the agent's ``.sdd/`` output where an operator can find it (#8078).

A review task wrote its result to ``.sdd/verdicts/pr-N-<sha>.md``, as the plan asked. The merge guard
(defect 28) refused the merge, the worktree was cleaned up, and the files were gone. The guard still
refuses the merge; the refused agent output is now copied to a quarantine folder and logged.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from bernstein.core.git import git_pr

VERDICT = ".sdd/verdicts/pr-12-abc123.md"
TOKEN = ".sdd/runtime/agent_tokens/backend-1.token"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo_with_branch(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "agent/review-1")
    for path, text in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        _git(repo, "add", "-f", path)
    _git(repo, "commit", "-q", "-m", "agent output")
    _git(repo, "checkout", "-q", "main")
    return repo


def test_refused_sdd_output_is_kept_and_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    repo = _repo_with_branch(tmp_path, {VERDICT: "verdict: approve\n", "src/app.py": "x = 1\n"})

    with caplog.at_level(logging.WARNING, logger=git_pr.__name__):
        result = git_pr.merge_with_conflict_detection(repo, "agent/review-1")

    # The guard still refuses: nothing reaches main.
    assert result.success is False
    assert VERDICT in result.refused_forbidden_files
    assert not (repo / VERDICT).exists()
    # The refused output is kept, and the result and the log say where.
    assert result.quarantined_to == f"{git_pr.MERGE_QUARANTINE_DIR}/agent_review-1"
    assert result.quarantined_files == [VERDICT]
    assert (repo / result.quarantined_to / VERDICT).read_text() == "verdict: approve\n"
    assert result.quarantined_to in result.error
    assert any(result.quarantined_to in rec.getMessage() for rec in caplog.records)


def test_credentials_are_refused_and_never_copied(tmp_path: Path) -> None:
    repo = _repo_with_branch(tmp_path, {VERDICT: "verdict: approve\n", TOKEN: "secret-token\n"})

    result = git_pr.merge_with_conflict_detection(repo, "agent/review-1")

    assert result.success is False
    assert TOKEN in result.refused_forbidden_files
    assert result.quarantined_files == [VERDICT]
    assert not list((repo / git_pr.MERGE_QUARANTINE_DIR).rglob("*.token"))


@pytest.mark.parametrize(
    ("path", "kept"),
    [
        (".sdd/verdicts/a.md", True),
        ("./.sdd/notes/b.txt", True),
        (".sdd/auth/jwt_secret", False),
        (".sdd/attestations/key.pem", False),
        (".sdd/runtime/agent_tokens/t.token", False),
        (f"{git_pr.MERGE_QUARANTINE_DIR}/x/.sdd/a.md", False),
        ("auth/jwt_secret", False),
        ("bernstein.yaml", False),
        (".env", False),
    ],
)
def test_only_sdd_agent_output_is_quarantinable(path: str, kept: bool) -> None:
    assert git_pr._quarantinable(path) is kept
