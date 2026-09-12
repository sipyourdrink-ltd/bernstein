"""The release tag lands on the commit CI validated, executed rather than read.

`auto-release.yml` decides *whether* to release from `inputs.head_sha` and used
to create the tag from whatever `main` pointed at when the job ran. The two are
about one CI run apart. Observed on v3.19.2: the version bump landed as
`0345f137e`, the merge queue merged an unrelated pull request 55 seconds later
as `f569d3fde`, and the tag was created on `f569d3fde`. That instance was
harmless -- the extra commit only touched `web/package-lock.json` -- but the
mechanism folds whatever lands in that window into the release, including a
commit the release was never tested with. For a patch release carrying a
security fix that is the wrong default (#5796).

Asserting the YAML says `git tag ... "${HEAD_SHA}"` would prove only that the
string is present. These tests take the step's own `run:` block out of the
workflow and execute it against a real repository whose tip has moved past
`head_sha`, then ask git where the tag actually points.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-release.yml"


def _tag_step_script() -> str:
    """The `run:` body of the `release` job's `Tag` step, verbatim."""
    doc = cast("dict[str, Any]", yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))
    for step in doc["jobs"]["release"]["steps"]:
        if isinstance(step, dict) and step.get("name") == "Tag":
            run = step.get("run")
            assert isinstance(run, str)
            return run
    pytest.fail("auto-release.yml::release has no step named 'Tag'")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(work: Path, message: str) -> str:
    (work / "file.txt").write_text(message, encoding="utf-8")
    _git(work, "add", "file.txt")
    _git(work, "commit", "-m", message)
    return _git(work, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A remote whose `main` has moved one commit past the validated commit.

    Returns the working clone, the validated sha, and the sha of the commit
    that landed after it -- the shape of the v3.19.2 release.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True,
        capture_output=True,
    )
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "ci@example.test")
    _git(work, "config", "user.name", "ci")
    _git(work, "checkout", "-b", "main")

    _commit(work, "base")
    validated = _commit(work, "chore: bump version to 9.9.9")
    later = _commit(work, "chore(deps): unrelated lockfile churn")
    _git(work, "push", "-u", "origin", "main")

    # HEAD is deliberately left on the TIP, not on the validated commit. That
    # is what an unpinned `actions/checkout` gave the job, and it is the state
    # that produced the v3.19.2 tag. The step must reach the validated commit
    # from `HEAD_SHA` regardless of where HEAD happens to be -- the checkout is
    # now pinned as well, and neither guarantee should depend on the other.
    _git(work, "checkout", "--detach", later)
    return work, validated, later


def _run_tag_step(repo_dir: Path, *, version: str, head_sha: str, summary: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", _tag_step_script()],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "FINAL_VERSION": version,
            "HEAD_SHA": head_sha,
            "GITHUB_STEP_SUMMARY": str(summary),
        },
    )


def test_the_tag_resolves_to_the_validated_commit_not_the_tip(repo: tuple[Path, str, str], tmp_path: Path) -> None:
    work, validated, later = repo
    summary = tmp_path / "summary.md"

    result = _run_tag_step(work, version="9.9.9", head_sha=validated, summary=summary)

    assert result.returncode == 0, result.stderr
    pushed = _git(work, "rev-parse", "refs/tags/v9.9.9^{commit}")
    assert pushed == validated
    assert pushed != later


def test_the_tag_reaches_the_remote(repo: tuple[Path, str, str], tmp_path: Path) -> None:
    """A tag that never leaves the runner releases nothing."""
    work, validated, _ = repo
    _run_tag_step(work, version="9.9.9", head_sha=validated, summary=tmp_path / "s.md")

    remote = _git(work, "ls-remote", "--tags", "origin", "v9.9.9")
    assert remote, "the tag was not pushed to origin"


def test_the_summary_names_the_commits_left_out_of_the_release(repo: tuple[Path, str, str], tmp_path: Path) -> None:
    """Release notes are reconciled by hand, so the gap has to be visible."""
    work, validated, later = repo
    summary = tmp_path / "summary.md"

    _run_tag_step(work, version="9.9.9", head_sha=validated, summary=summary)

    text = summary.read_text(encoding="utf-8")
    assert later[:7] in text
    assert "unrelated lockfile churn" in text
    assert validated[:7] not in text.split("NOT in this release:", 1)[1]


def test_no_summary_noise_when_the_tip_has_not_moved(repo: tuple[Path, str, str], tmp_path: Path) -> None:
    """The common case is head_sha == tip; it must stay quiet."""
    work, _, later = repo
    summary = tmp_path / "summary.md"
    _git(work, "checkout", "--detach", later)

    result = _run_tag_step(work, version="9.9.9", head_sha=later, summary=summary)

    assert result.returncode == 0, result.stderr
    assert _git(work, "rev-parse", "refs/tags/v9.9.9^{commit}") == later
    assert not summary.exists() or summary.read_text(encoding="utf-8") == ""


def test_a_tag_already_pointing_elsewhere_never_reaches_the_remote(repo: tuple[Path, str, str], tmp_path: Path) -> None:
    """The version guard is upstream; this is the backstop under it.

    `Check tag` short-circuits the whole job when the version is already
    tagged, so this state should not be reachable. If it is reached anyway --
    a tag created between that check and this step -- the step must refuse
    rather than push a release pointing at code CI never validated. `set -e`
    plus the explicit comparison give two independent ways to stop, and both
    stop before the push.
    """
    work, validated, later = repo
    _git(work, "tag", "v9.9.9", later)

    result = _run_tag_step(work, version="9.9.9", head_sha=validated, summary=tmp_path / "s.md")

    assert result.returncode != 0
    assert not _git(work, "ls-remote", "--tags", "origin", "v9.9.9")
