"""Git processes started by the suite must not run background housekeeping."""

from __future__ import annotations

import subprocess
from pathlib import Path


def test_git_background_maintenance_is_disabled_for_the_suite(tmp_path: Path) -> None:
    """`git init` anywhere in the suite inherits housekeeping-off config.

    `git commit` may hand off to `git maintenance`, which writes and then
    unlinks `.git/objects/maintenance.lock` on its own schedule. A helper that
    walks or deletes `.git` right after committing races that unlink and dies
    with `FileNotFoundError: 'maintenance.lock'`.
    """
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    for key, expected in (("gc.auto", "0"), ("maintenance.auto", "false")):
        result = subprocess.run(
            ["git", "config", "--get", key],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == expected, f"{key} is not pinned for the suite"
