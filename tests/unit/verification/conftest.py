"""Isolated local Git bases for real candidate verification tests."""

import subprocess
from pathlib import Path

import pytest


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def candidate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "value.py").write_text("VALUE = 1\n")
    (repo / "README.md").write_text("Hello\n")
    (repo / "test_value.py").write_text("from value import VALUE\n\ndef test_value():\n    assert VALUE == 1\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")

    def make(content, filename="value.py"):
        (repo / filename).write_text(content)
        diff = git(repo, "diff") + "\n"
        git(repo, "checkout", "--", filename)
        return {"diff": diff, "base_commit": base, "workdir": str(repo), "backend": "test"}

    return repo, make
