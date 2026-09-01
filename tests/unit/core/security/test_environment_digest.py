"""Tests for environment digest computation.

Every test here builds the repository it measures inside ``tmp_path``.

The first version of this file pointed ``_get_git_head`` at an absolute
path that existed only on the machine that wrote the tests, and asserted
the answer equalled one specific branch name. It passed there and nowhere
else: CI has no such directory, so the call returned the ``HEAD_UNKNOWN``
fallback and ``shard 1`` went red. A test anchored on the machine it runs
on measures the machine, not the code - so each test below constructs its
own repository and asserts against what it just built.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.security.environment_digest import (
    _get_git_head,
    compare_digests,
    compute_environment_digest,
)


class _Plan:
    """Minimal stand-in for the plan object the digest reads attributes off."""

    def __init__(self, touched: list[str] | None = None, config: list[str] | None = None) -> None:
        self.touched_files = touched or []
        self.config_files = config or []


def _init_repo(root: Path) -> str:
    """Create a git repo with one commit in *root*; return its HEAD sha."""
    run = lambda *a: subprocess.run(a, cwd=root, check=True, capture_output=True, text=True)  # noqa: E731
    run("git", "init", "-q")
    run("git", "config", "user.email", "test@example.invalid")
    run("git", "config", "user.name", "Test")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "init")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_compare_digests_match() -> None:
    """Equal digests compare equal."""
    assert compare_digests("abc123", "abc123") is True


def test_compare_digests_mismatch() -> None:
    """Different digests compare unequal."""
    assert compare_digests("abc123", "def456") is False


def test_git_head_is_the_head_of_the_repo_it_is_pointed_at(tmp_path: Path) -> None:
    """The sha comes from *repo_root*, not from the process's own checkout."""
    expected = _init_repo(tmp_path)
    assert _get_git_head(str(tmp_path)) == expected
    assert len(expected) == 40


def test_git_head_falls_back_to_the_head_file_when_git_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no ``git`` binary the ref named by ``.git/HEAD`` is read directly."""
    sha = "0" * 40
    git_dir = tmp_path / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "refs" / "heads" / "main").write_text(f"{sha}\n")

    def _no_git(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _no_git)
    assert _get_git_head(str(tmp_path)) == sha


def test_git_head_is_unknown_when_there_is_no_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented sentinel, not an exception and not a stale sha."""

    def _no_git(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _no_git)
    assert _get_git_head(str(tmp_path)) == "HEAD_UNKNOWN"


def test_digest_is_a_sha256_over_head_and_the_named_files(tmp_path: Path) -> None:
    """Shape check: 64 hex characters, computed without error."""
    _init_repo(tmp_path)
    (tmp_path / "bernstein.yaml").write_text("k: v\n")
    digest = compute_environment_digest(str(tmp_path), _Plan(["pyproject.toml"], ["bernstein.yaml"]))
    assert len(digest) == 64
    int(digest, 16)


def test_digest_is_deterministic_for_identical_inputs(tmp_path: Path) -> None:
    """Two plans naming the same files over the same tree agree byte for byte."""
    _init_repo(tmp_path)
    first = compute_environment_digest(str(tmp_path), _Plan(["pyproject.toml"]))
    second = compute_environment_digest(str(tmp_path), _Plan(["pyproject.toml"]))
    assert first == second


def test_digest_changes_when_a_touched_file_changes(tmp_path: Path) -> None:
    """The property the digest exists for: content, not just the path, is covered."""
    _init_repo(tmp_path)
    before = compute_environment_digest(str(tmp_path), _Plan(["pyproject.toml"]))
    (tmp_path / "pyproject.toml").write_text("[project]\nname='y'\n")
    after = compute_environment_digest(str(tmp_path), _Plan(["pyproject.toml"]))
    assert before != after


def test_missing_touched_file_raises_instead_of_hashing_nothing(tmp_path: Path) -> None:
    """A plan naming a file that is not there is an error, never a silent digest."""
    _init_repo(tmp_path)
    with pytest.raises(FileNotFoundError):
        compute_environment_digest(str(tmp_path), _Plan(["does-not-exist.toml"]))
