"""Carry publication verifies trusted workflow definitions and original ancestry."""

import subprocess
import sys
from pathlib import Path

VERIFIER = Path(__file__).resolve().parents[3] / "scripts/verify_carry_head.py"


def test_publication_requires_current_workflows_and_original_commit(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(message):
        git("add", ".")
        git("commit", "-m", message)
        return git("rev-parse", "HEAD")

    def verify(main, original, head):
        return subprocess.run(
            [sys.executable, str(VERIFIER), str(tmp_path), main, original, head],
            capture_output=True,
            text=True,
        )

    git("init", "-b", "main")
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    workflow = workflows / "ci.yml"
    workflow.write_text("name: original\n")
    commit("original base")
    git("switch", "-c", "contributor")
    (tmp_path / "contribution.txt").write_text("preserve this commit\n")
    original = commit("contribution")
    git("switch", "main")
    workflow.write_text("name: guarded\n")
    main = commit("guard workflows")
    assert "trusted main" in verify(main, original, original).stderr
    assert "original contributor head" in verify(main, original, main).stderr

    git("switch", "-c", "fleet/carry-1", original)
    git("merge", "--no-edit", main)
    (tmp_path / "repair.txt").write_text("bot repair\n")
    reviewed = commit("reviewed repair")
    assert verify(main, original, reviewed).returncode == 0
    assert verify(main, original, "fleet/carry-1").returncode == 1

    workflow.write_text("name: stale\n")
    changed = commit("changed workflow tree")
    assert "trusted GitHub workflow tree" in verify(main, original, changed).stderr
