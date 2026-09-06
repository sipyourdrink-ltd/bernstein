"""Shared orphan-ratchet diagnostics."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.unit._orphan_scan import PythonSourceTree, assert_orphans_match, pull_request_head_sha


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_a_stale_merge_baseline_exonerates_the_branch() -> None:
    known = frozenset({"still_orphaned", "fixed_on_base"})
    branch = set(known)
    merged = {"still_orphaned", "new_on_base"}

    with pytest.raises(AssertionError) as caught:
        assert_orphans_match(
            known=known,
            current=merged,
            branch_current=branch,
            subject="test modules",
            wire_or_delete="wire or delete each module",
        )

    message = str(caught.value)
    assert "baseline is stale" in message
    assert "branch is not at fault" in message
    assert "became caller-less on the base branch: ['new_on_base']" in message
    assert "gained a caller or was deleted on the base branch: ['fixed_on_base']" in message


def test_a_branch_regression_keeps_its_own_distinct_message() -> None:
    with pytest.raises(AssertionError) as caught:
        assert_orphans_match(
            known=frozenset({"existing"}),
            current={"existing", "introduced"},
            branch_current={"existing", "introduced"},
            subject="test modules",
            wire_or_delete="wire or delete each module",
        )

    message = str(caught.value)
    assert "branch introduced new caller-less test modules: ['introduced']" in message
    assert "baseline is stale" not in message


def test_a_branch_that_shrinks_the_allowlist_still_updates_the_ratchet() -> None:
    with pytest.raises(AssertionError, match=r"\['fixed'\].*strike.*KNOWN_ORPHANS"):
        assert_orphans_match(
            known=frozenset({"existing", "fixed"}),
            current={"existing"},
            branch_current={"existing"},
            subject="test modules",
            wire_or_delete="wire or delete each module",
        )


def test_an_unchanged_ratchet_passes() -> None:
    assert_orphans_match(
        known=frozenset({"existing"}),
        current={"existing"},
        branch_current={"existing"},
        subject="test modules",
        wire_or_delete="wire or delete each module",
    )


def test_the_pr_head_tree_is_read_without_changing_the_merge_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Tests")
    source = repo / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("import branch_dependency\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "branch head")
    head_sha = _git(repo, "rev-parse", "HEAD")

    source.write_text("import merged_dependency\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "synthetic merge result")
    merge_sha = _git(repo, "rev-parse", "HEAD")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"head": {"sha": head_sha}}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))

    assert pull_request_head_sha(repo) == head_sha
    branch_tree = PythonSourceTree(repo, repo / "src", head_sha)
    assert branch_tree.sources[source] == "import branch_dependency\n"
    dotted, _ = branch_tree.import_index(set())
    assert "branch_dependency" in dotted
    assert "merged_dependency" not in dotted
    assert source.read_text(encoding="utf-8") == "import merged_dependency\n"
    assert _git(repo, "rev-parse", "HEAD") == merge_sha
