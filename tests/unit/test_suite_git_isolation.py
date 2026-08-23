"""Git processes started by the suite must not run background housekeeping."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.integration import test_agents_md_dogfood as dogfood


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


def test_helper_that_replaces_the_environment_forwards_the_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_git_init` must hand git the housekeeping pins it scrubbed away.

    The pins travel as ``GIT_CONFIG_KEY_*`` / ``GIT_CONFIG_VALUE_*`` /
    ``GIT_CONFIG_COUNT``, so they reach only a git that *inherits* the
    environment. `_git_init` replaces the environment wholesale to fix author
    and committer identity; without forwarding, its ``git commit`` hands off
    to background maintenance, which packs and unlinks loose objects while
    the caller reads the repo tree - the flake that failed
    `test_two_consecutive_syncs_are_byte_identical` on main.

    Asserted on the environment actually handed to git rather than on
    ``git config --get``: the pins live only in the environment and are never
    written into the repo, and a ``git config`` call that inherits them reads
    them back from its own environment, which proves nothing about the repo.
    """
    calls: list[dict[str, str]] = []
    real_run = subprocess.run

    def _capture(cmd, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs.get("env") or {}))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(dogfood.subprocess, "run", _capture)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")

    dogfood._git_init(repo)

    expected = {k: v for k, v in os.environ.items() if k.startswith("GIT_CONFIG_")}
    assert expected, "the suite fixture did not pin housekeeping; nothing to forward"
    assert calls, "_git_init ran no subprocess"
    for env in calls:
        missing = [k for k in expected if env.get(k) != expected[k]]
        assert not missing, (
            f"_git_init dropped {missing} when it replaced env=; forward every "
            f"GIT_CONFIG_* key so git keeps background maintenance off"
        )
