"""CLI tests for ``bernstein fork --run <id> --from-step N`` (issue #2295)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.fork_cmd import fork_cmd
from bernstein.core.replay.fork import record_snapshot_event
from bernstein.core.replay.journal import EventJournal
from bernstein.core.sandbox.snapshot import commit_worktree_snapshot


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _seed(repo: Path, sdd: Path, run_id: str, step: int, payload: bytes) -> str:
    journal = EventJournal(run_id, sdd)
    wt = repo / ".sdd" / "worktrees" / f"wt-{run_id}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", str(wt), "HEAD")
    for i in range(step + 1):
        if i == step:
            (wt / "state.txt").write_bytes(payload)
            sha = commit_worktree_snapshot(repo, wt, run_id=run_id, step_index=i)
            record_snapshot_event(journal, snapshot_sha=sha, step_index=i)
        else:
            journal.record("tick", step_index=i)
    return sha


def test_fork_cmd_prints_lineage(repo: Path, tmp_path: Path) -> None:
    sdd = repo / ".sdd"
    sha = _seed(repo, sdd, "parent-1", 1, b"payload")

    runner = CliRunner()
    result = runner.invoke(
        fork_cmd,
        ["--run", "parent-1", "--from-step", "1", "--repo-root", str(repo), "--sdd-dir", str(sdd)],
    )
    assert result.exit_code == 0, result.output
    assert "parent-1" in result.output
    assert sha[:12] in result.output
    # A new run id was minted and printed.
    assert "fork-parent-1-s1-" in result.output


def test_fork_cmd_json(repo: Path, tmp_path: Path) -> None:
    sdd = repo / ".sdd"
    sha = _seed(repo, sdd, "parent-2", 0, b"p")

    runner = CliRunner()
    result = runner.invoke(
        fork_cmd,
        [
            "--run",
            "parent-2",
            "--from-step",
            "0",
            "--repo-root",
            str(repo),
            "--sdd-dir",
            str(sdd),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.output)
    assert payload["parent_run_id"] == "parent-2"
    assert payload["from_step"] == 0
    assert payload["snapshot_sha"] == sha
    assert payload["new_run_id"].startswith("fork-parent-2-s0-")


def test_fork_cmd_missing_snapshot_exits_nonzero(repo: Path, tmp_path: Path) -> None:
    sdd = repo / ".sdd"
    journal = EventJournal("parent-3", sdd)
    journal.record("tick", step_index=0)

    runner = CliRunner()
    result = runner.invoke(
        fork_cmd,
        ["--run", "parent-3", "--from-step", "0", "--repo-root", str(repo), "--sdd-dir", str(sdd)],
    )
    assert result.exit_code != 0
    assert "no snapshot" in result.output.lower()
