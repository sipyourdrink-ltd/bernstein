"""Structural assertions on ``.github/workflows/release-major-minor.yml`` (#3948).

The "Commit and tag" step in this workflow used to land the version-bump
commit with a bare ``git push`` straight to ``main``. The branch ruleset has
no bypass actors and rejects direct pushes outright, so every major/minor
release attempted through this workflow died at that step. The workflow also
reimplemented build, PyPI publish, and GitHub Release creation inline instead
of delegating to ``publish.yml``, so even a successful run would have skipped
the npm/docker/homebrew fan-out that ``publish.yml`` performs for every other
release.

These tests guard the fix: the bump now lands through a pull request with
auto-merge armed for the merge queue, and building/publishing/releasing stays
owned by the same ``auto-release.yml`` -> ``publish.yml`` chain patch releases
already go through.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS_DIR / "release-major-minor.yml"

_GIT_PUSH_RE = re.compile(r"^\s*git push\b(?P<rest>.*)$", re.MULTILINE)
# A bare `git push` (nothing after it) or one that names `main`/`HEAD:main`
# explicitly. A push naming any other ref (a tag, a feature branch) is not
# what the branch ruleset blocks and must not be flagged.
_BARE_OR_MAIN_RE = re.compile(r"^(origin)?(\s+(main|HEAD:(refs/heads/)?main))?$")


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def _runs(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in _steps(job) if isinstance(step.get("run"), str)]


def _uses(job: dict[str, Any]) -> list[str]:
    return [step["uses"] for step in _steps(job) if isinstance(step.get("uses"), str)]


def _pushes_to_main(run_text: str) -> list[str]:
    """``git push`` invocations in *run_text* that would land on main.

    A bare ``git push`` (no explicit refspec) pushes the current branch to
    its tracked upstream -- main, on a checkout of main -- which is exactly
    the shape the branch ruleset rejects. An explicit push naming ``main`` or
    ``HEAD:main`` is equally direct.
    """
    offenders = []
    for match in _GIT_PUSH_RE.finditer(run_text):
        rest = match.group("rest").strip()
        if _BARE_OR_MAIN_RE.match(rest):
            offenders.append(match.group(0).strip())
    return offenders


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return _load(WORKFLOW)


@pytest.fixture(scope="module")
def release_job(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    job = jobs.get("release")
    assert isinstance(job, dict), "release-major-minor.yml must keep a `release` job"
    return cast("dict[str, Any]", job)


def test_no_direct_push_to_main(release_job: dict[str, Any]) -> None:
    """The bump commit must never land on main via a bare ``git push`` (#3948)."""
    offenders = [offender for run in _runs(release_job) for offender in _pushes_to_main(run)]
    assert not offenders, (
        f"release-major-minor.yml pushes directly to main: {offenders}. The branch "
        "ruleset has no bypass actors and rejects this outright -- land the bump "
        "through a pull request instead."
    )


def test_bump_lands_through_a_pull_request(release_job: dict[str, Any]) -> None:
    """The version bump must open a PR rather than commit straight to main."""
    uses = _uses(release_job)
    assert any(step.startswith("peter-evans/create-pull-request@") for step in uses), (
        "release-major-minor.yml must open the version bump through a pull request"
    )


def test_bump_pr_enables_auto_merge(release_job: dict[str, Any]) -> None:
    """The bump PR must arm auto-merge so the merge queue lands it unattended."""
    assert any("gh pr merge --auto" in run for run in _runs(release_job)), (
        "release-major-minor.yml opens a bump PR but never arms auto-merge for it"
    )


def test_the_enqueue_passes_no_merge_strategy(release_job: dict[str, Any]) -> None:
    """Enqueueing must not name a merge method; the queue applies its own.

    A strategy flag on the enqueue call conflicts with the method the
    merge queue is configured with, and the release bump is the worst
    place to discover that -- the operator is waiting on a tag.
    """
    enqueue = [run for run in _runs(release_job) if "gh pr merge" in run]
    assert enqueue, "expected an enqueue call for the bump PR"
    for run in enqueue:
        for flag in ("--squash", "--merge", "--rebase"):
            assert flag not in run, f"the enqueue call passes {flag}; the merge queue applies its own configured method"


def test_a_token_downgrade_is_announced(release_job: dict[str, Any]) -> None:
    """Falling back to GITHUB_TOKEN must not look like a normal release.

    A PR opened with GITHUB_TOKEN starts no ``pull_request`` workflows, so
    its required contexts never report and the armed auto-merge can never
    fire. The run still concludes green, which is exactly how a release
    silently stalls; the fallback therefore has to say so.
    """
    runs = _runs(release_job)
    assert any("::warning" in run and "GITHUB_TOKEN" in run for run in runs), (
        "the GITHUB_TOKEN fallback must annotate the run: its bump PR cannot merge on its own"
    )


def test_no_inline_publish_reimplementation(release_job: dict[str, Any]) -> None:
    """Build/PyPI-publish/GitHub-Release steps belong to publish.yml, not here."""
    runs = _runs(release_job)
    uses = _uses(release_job)

    assert not any("gh release create" in run for run in runs), (
        "release-major-minor.yml must not create the GitHub Release inline; "
        "publish.yml owns that for every tag, including major/minor ones"
    )
    assert not any(step.startswith("pypa/gh-action-pypi-publish@") for step in uses), (
        "release-major-minor.yml must not publish to PyPI inline; publish.yml does"
    )
    assert not any(run.strip() == "uv build" for run in runs), (
        "release-major-minor.yml must not build dist artifacts inline; publish.yml does"
    )
    for target in ("publish-docker.yml", "publish-homebrew.yml", "sbom.yml"):
        assert not any(f"gh workflow run {target}" in run for run in runs), (
            f"release-major-minor.yml must not dispatch {target} itself; publish.yml "
            "already dispatches it once it creates the release"
        )


def test_release_job_has_no_pypi_environment(release_job: dict[str, Any]) -> None:
    """The job no longer publishes, so it does not need the pypi OIDC environment."""
    assert "environment" not in release_job, (
        "release-major-minor.yml no longer publishes to PyPI directly; drop the pypi environment binding"
    )


def test_release_job_permissions_match_its_narrowed_scope(release_job: dict[str, Any]) -> None:
    """Publish-only permissions (id-token, attestations) are no longer needed here."""
    permissions = release_job.get("permissions", {})
    assert isinstance(permissions, dict)
    assert permissions.get("contents") == "write"
    assert permissions.get("pull-requests") == "write"
    for stale in ("id-token", "attestations", "actions"):
        assert stale not in permissions, (
            f"release-major-minor.yml no longer needs `{stale}`; it stopped publishing directly"
        )


def test_bump_uses_the_supported_bump_script(release_job: dict[str, Any]) -> None:
    """pyproject.toml, uv.lock, and the distribution manifests must move together."""
    assert any("scripts/bump_version.py" in run for run in _runs(release_job)), (
        "release-major-minor.yml must bump the version via scripts/bump_version.py, "
        "the only path that keeps uv.lock and the distribution manifests in sync"
    )
