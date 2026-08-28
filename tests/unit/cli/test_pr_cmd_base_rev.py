"""The base revision a pull-request description is composed against.

``_enrich_summary_with_git`` diffs the run branch against the *name* of the
pull request's base -- ``main`` -- as a local ref. Nothing in a working
clone maintains that ref: a single-branch clone does not have it at all,
and a clone that has fetched since it was created has a stale one. Both
shipped: a description composed where ``main`` did not resolve was
published under "the diff could not be read" with no commit list and no
provenance, and a stale ``main`` composes the description against commits
the pull request does not touch.

These tests build the two clone shapes with real git, because the defect
was in which revision the real git commands were pointed at.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bernstein.cli.commands.pr_cmd import _base_rev, _diff_bytes, _enrich_summary_with_git
from bernstein.core.integrations.pr_gen import SessionSummary, load_session_summary


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _commit_file(cwd: Path, name: str, body: str, message: str) -> None:
    (cwd / name).write_text(body, encoding="utf-8")
    _git(cwd, "add", name)
    _git(cwd, "commit", "-qm", message)


@pytest.fixture
def clones(tmp_path: Path) -> tuple[Path, Path]:
    """An upstream repository and a working clone of it, like a lane's."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "t@example.invalid")
    _git(upstream, "config", "user.name", "t")
    _commit_file(upstream, "base.py", "BASE = 1\n", "base")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(upstream), str(clone))
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "t")
    return upstream, clone


def _work_branch(clone: Path) -> None:
    _git(clone, "checkout", "-qb", "work", "origin/main")
    _commit_file(clone, "feature.py", "def f() -> int:\n    return 2\n", "feature")


def test_a_clone_without_a_local_base_still_composes_the_description(
    clones: tuple[Path, Path],
) -> None:
    """The #4687 shape: only ``origin/main`` exists, and the description
    must still get its diff-stat, its commit list, and its provenance."""
    _upstream, clone = clones
    _work_branch(clone)
    _git(clone, "branch", "-D", "main")

    enriched = _enrich_summary_with_git(
        SessionSummary(session_id="T-1", goal="g", branch="work", base_branch="main"),
        clone,
    )

    assert enriched.git_error == "", "every git query failed over a resolvable base"
    assert "feature.py" in enriched.diff_stat
    assert [c.subject for c in enriched.commits] == ["feature"]
    assert enriched.provenance is not None


def test_a_stale_local_base_does_not_shape_the_description(
    clones: tuple[Path, Path],
) -> None:
    """A local ``main`` pinned behind ``origin/main`` must not put the
    upstream commits it is missing into the branch's description."""
    upstream, clone = clones
    _commit_file(upstream, "upstream_only.py", "U = 3\n", "upstream moved on")
    _git(clone, "fetch", "-q", "origin")
    _work_branch(clone)  # branched from the NEW origin/main; local main is stale

    enriched = _enrich_summary_with_git(
        SessionSummary(session_id="T-2", goal="g", branch="work", base_branch="main"),
        clone,
    )

    assert enriched.git_error == ""
    assert "feature.py" in enriched.diff_stat
    assert "upstream_only.py" not in enriched.diff_stat, "the description was composed against the stale local main"
    diff, error = _diff_bytes(clone, "origin/main", "work")
    assert not error
    assert enriched.provenance is not None
    from bernstein.core.integrations.pr_gen import build_provenance

    assert enriched.provenance.diff_hash == build_provenance(diff=diff, journal_head="").diff_hash


def test_the_base_name_is_kept_for_a_workspace_with_no_remote(tmp_path: Path) -> None:
    """An operator running ``bernstein pr`` in a plain local repository has
    no ``origin/main``; the local name is then the only answer."""
    _git(tmp_path, "init", "-q", "-b", "main")
    assert _base_rev(tmp_path, "main") == "main"


def test_the_wrap_up_placeholder_is_not_an_answer(tmp_path: Path) -> None:
    """Wrap-up files already on disk say ``(no uncommitted changes)`` where
    the branch diff-stat belongs -- an answer to a different question. Ten
    pull requests published it as their whole diff-stat block; loading one
    must leave the field empty so the git enrichment recomputes it."""
    sessions = tmp_path / ".sdd" / "runtime" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "1-wrapup.json").write_text(
        json.dumps(
            {
                "session_id": "S-1",
                "branch": "work",
                "git_diff_stat": "(no uncommitted changes)",
            }
        ),
        encoding="utf-8",
    )

    summary = load_session_summary("S-1", workdir=tmp_path)

    assert summary.diff_stat == ""
