from __future__ import annotations

import subprocess
from pathlib import Path

from bernstein.core.tasks.context_extractors import extract_test_to_source_map


def _git(repo: Path, *args: str) -> None:
    """Run git in *repo* with an identity, whatever the host is configured with.

    Every command that writes a commit goes through here. A runner with no
    global ``user.email`` fails ``git revert`` with "unable to auto-detect
    email address" and exit 128, which is a fixture problem wearing the
    costume of a product failure -- and it only shows up off the developer's
    own machine, where the global identity happens to exist.
    """
    subprocess.run(
        (
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            *args,
        ),
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _commit(repo: Path, message: str, *paths: str) -> None:
    for path in paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(message, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)


def test_a_test_that_never_co_changed_is_not_offered(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _commit(tmp_path, "source change", "src/a.py", "tests/test_a.py")
    _commit(tmp_path, "unrelated test", "tests/test_never.py")
    assert extract_test_to_source_map(tmp_path, ["src/a.py"]) == {"src/a.py": ["tests/test_a.py"]}


def test_only_commits_that_landed_green_contribute(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _commit(tmp_path, "source change", "src/a.py", "tests/test_reverted.py")
    reverted = subprocess.check_output(("git", "-C", str(tmp_path), "rev-parse", "HEAD"), text=True).strip()
    _git(tmp_path, "revert", "--no-edit", reverted)
    _commit(tmp_path, "another source change", "src/a.py", "tests/test_kept.py")
    assert extract_test_to_source_map(tmp_path, ["src/a.py"]) == {"src/a.py": ["tests/test_kept.py"]}


def test_a_commit_whose_subject_merely_starts_with_revert_still_counts(tmp_path: Path) -> None:
    """ "Revert" in a subject line is prose, not evidence that anything was undone.

    "Revert to the previous retry policy" is a perfectly ordinary change, and
    dropping it costs the map the tests that landed with it. The trailer that
    ``git revert`` writes is the thing that actually records an undo.
    """
    _git(tmp_path, "init")
    _commit(tmp_path, "Revert to the previous retry policy", "src/a.py", "tests/test_policy.py")
    assert extract_test_to_source_map(tmp_path, ["src/a.py"]) == {"src/a.py": ["tests/test_policy.py"]}


def test_the_map_is_stable_under_input_reordering(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _commit(tmp_path, "green source change", "src/a.py", "tests/test_a.py")
    _commit(tmp_path, "green source change", "src/b.py", "tests/test_b.py")
    assert extract_test_to_source_map(tmp_path, ["src/a.py", "src/b.py"]) == extract_test_to_source_map(
        tmp_path, ["src/b.py", "src/a.py"]
    )


def test_a_target_with_no_history_yields_an_empty_map_and_no_error(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    assert extract_test_to_source_map(tmp_path, ["src/missing.py"]) == {"src/missing.py": []}


def test_a_test_whose_path_git_would_quote_is_still_found(tmp_path: Path) -> None:
    """Plain ``--name-only`` hands back C-quoted paths for anything non-ASCII.

    ``tests/test_ünïcode.py`` comes out as ``"tests/test_\\303\\274n..."`` --
    a string that starts with a double quote, so a ``tests/`` prefix check
    silently drops it and the map claims the file has no covering test. The
    map is read as "these are the tests that cover this", which makes a
    missing row a wrong answer rather than a thin one.
    """
    _git(tmp_path, "init")
    _commit(tmp_path, "source change", "src/a.py", "tests/test_ünïcode.py")
    assert extract_test_to_source_map(tmp_path, ["src/a.py"]) == {"src/a.py": ["tests/test_ünïcode.py"]}
