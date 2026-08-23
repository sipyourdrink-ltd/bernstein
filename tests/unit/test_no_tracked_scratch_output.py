"""Regression guard: agent working notes must not reach the repository.

An agent preparing a change writes its PR body, review draft, or scratch
notes beside the checkout. Three such files were committed by accident in
a single week and landed on ``main`` (``scratch/pr4360_body.md``,
``pr4376_body.md``, ``pr4377_body.md``) — each one arrived via ``git add
-A`` in a change that had nothing to do with it.

``.gitignore`` now covers ``scratch/``. This test is the part that fails
loudly: an ignore rule only helps for paths nobody has already tracked,
and a file added with ``git add -f`` stays tracked forever regardless.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories that only ever hold working notes, never shipped content.
SCRATCH_DIRS = ("scratch",)


def _tracked_paths() -> list[str]:
    """Return every path git tracks, repo-relative."""
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


def test_no_scratch_directory_is_tracked() -> None:
    """No file under a scratch directory may be tracked by git."""
    tracked = _tracked_paths()
    offenders = [p for p in tracked if p.split("/", 1)[0] in SCRATCH_DIRS]
    assert not offenders, (
        "working notes are tracked and will ship with the tag: "
        f"{offenders}. Remove them with `git rm --cached` — adding the "
        "directory to .gitignore does not untrack a file already in the index."
    )


def test_scratch_directory_is_ignored() -> None:
    """A new file under ``scratch/`` must be ignored, not merely untracked.

    Untracked-but-not-ignored is how the three committed files happened:
    they showed up in ``git status`` and got swept into ``git add -A``.
    """
    for name in SCRATCH_DIRS:
        probe = f"{name}/probe-not-written-to-disk.md"
        result = subprocess.run(
            ["git", "check-ignore", "-q", probe],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        assert result.returncode == 0, (
            f"{probe} is not matched by .gitignore, so an agent's working "
            f"notes under {name}/ can be swept into a commit by `git add -A`."
        )
