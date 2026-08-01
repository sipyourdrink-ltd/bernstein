"""Structural assertions for the release reconciliation workflow."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict, cast

import yaml

WorkflowStep = TypedDict(
    "WorkflowStep",
    {
        "name": object,
        "id": object,
        "if": object,
        "env": object,
        "run": object,
    },
    total=False,
)


class WorkflowJob(TypedDict, total=False):
    steps: list[object]
    permissions: dict[str, object]


class WorkflowFile(TypedDict, total=False):
    jobs: dict[str, WorkflowJob]


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "reconcile-release.yml"


def _load() -> WorkflowFile:
    return cast("WorkflowFile", yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))


def _steps() -> list[WorkflowStep]:
    jobs = _load().get("jobs", {})
    assert isinstance(jobs, dict)
    job = jobs.get("reconcile")
    assert isinstance(job, dict), "expected reconcile job"
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    return [cast("WorkflowStep", step) for step in steps if isinstance(step, dict)]


def _step(name: str) -> WorkflowStep:
    match = next((step for step in _steps() if step.get("name") == name), None)
    assert match is not None, f"expected step {name!r}"
    return match


def _run(step: WorkflowStep) -> str:
    run = step.get("run", "")
    assert isinstance(run, str)
    return run


def test_compare_step_audits_github_release_assets() -> None:
    """Release reconciliation must detect tag releases that exist without dist assets."""
    run = _run(_step("Compare versions and release assets"))

    assert '"gh",' in run
    assert '"release",' in run
    assert '"view",' in run
    assert '"--json",' in run
    assert '"assets",' in run
    assert "asset_count" in run
    assert "missing_assets = release_exists and asset_count == 0" in run
    assert "missing_assets=" in run
    assert "drift = version_drift or missing_assets" in run


def test_compare_step_covers_every_published_channel() -> None:
    """Drift detection must cover each channel the publish chain writes to (#3322, #3323).

    A channel absent from the comparison set cannot be seen to fall behind: the
    npm wrapper sat at a stale version for months precisely because nothing
    compared the registry to `pyproject.toml`.
    """
    run = _run(_step("Compare versions and release assets"))

    # npm wrapper.
    assert "registry.npmjs.org/bernstein-orchestrator" in run
    assert "npm_version=" in run
    assert "npm_drift" in run

    # Homebrew tap formula.
    assert "homebrew-tap" in run
    assert "brew_version=" in run
    assert "brew_drift" in run

    # SBOM assets on the GitHub Release.
    assert "bernstein.spdx.json" in run
    assert "bernstein.cyclonedx.json" in run
    assert "missing_sboms" in run

    # Every channel feeds the single drift verdict.
    for flag in ("version_drift", "missing_assets", "npm_drift", "brew_drift", "missing_sboms"):
        assert flag in run.split("drift = ")[-1].splitlines()[0], f"{flag} is computed but not part of the verdict"


def test_drift_issue_includes_missing_asset_context() -> None:
    """The tracking issue should open for missing assets and include asset evidence."""
    step = _step("Open or update drift issue (idempotent)")
    condition = step.get("if", "")
    assert isinstance(condition, str)
    assert "steps.cmp.outputs.drift == 'true'" in condition

    env = step.get("env", {})
    assert isinstance(env, dict)
    assert "MISSING_ASSETS" in env
    assert "ASSET_COUNT" in env

    run = _run(step)
    assert "GitHub Release missing dist assets" in run
    assert "GitHub Release asset count" in run


def test_drift_issue_reports_every_channel() -> None:
    """The tracking issue must name the channel that fell behind."""
    step = _step("Open or update drift issue (idempotent)")
    env = step.get("env", {})
    assert isinstance(env, dict)
    for name in ("NPM", "BREW", "MISSING_SBOMS"):
        assert name in env, f"drift issue body cannot report the {name} channel"

    run = _run(step)
    assert "npm" in run
    assert "Homebrew" in run
    assert "SBOM" in run


def test_no_drift_notice_reports_asset_count() -> None:
    """A clean reconciliation should make the checked asset count visible in logs."""
    step = _step("No drift")
    env = step.get("env", {})
    assert isinstance(env, dict)
    assert "ASSET_COUNT" in env
    assert "MISSING_ASSETS" in env

    run = _run(step)
    assert "assets=${ASSET_COUNT}" in run


def test_compare_step_covers_the_rpm_channel() -> None:
    """Copr is in the comparison set (#3325).

    The RPM channel has no repository-visible artefact, so a dropped publish
    is invisible until someone installs the package by hand. The reconciler
    is the only surface that can see it.
    """
    run = _run(_step("Compare versions and release assets"))

    assert "copr.fedorainfracloud.org/api_3/package" in run
    assert "copr_version=" in run
    assert "copr_drift=" in run
    assert "drift = version_drift or missing_assets or copr_drift" in run


def test_copr_lookup_failure_does_not_break_the_pypi_comparison() -> None:
    """A Copr API outage must not take the PyPI drift detector down with it."""
    run = _run(_step("Compare versions and release assets"))

    except_clause = next(line for line in run.splitlines() if line.strip().startswith("except ("))
    for exception in ("urllib.error.URLError", "TimeoutError", "ValueError", "KeyError", "OSError"):
        assert exception in except_clause

    assert 'copr_version = "unknown"' in run
    assert "::warning::" in run


def test_drift_issue_reports_the_rpm_channel_version() -> None:
    step = _step("Open or update drift issue (idempotent)")
    env = step.get("env", {})
    assert isinstance(env, dict)
    assert "COPR" in env

    run = _run(step)
    assert "latest on Copr" in run


def test_no_drift_notice_reports_the_rpm_channel_version() -> None:
    step = _step("No drift")
    env = step.get("env", {})
    assert isinstance(env, dict)
    assert "COPR_VERSION" in env

    run = _run(step)
    assert "copr=${COPR_VERSION}" in run
