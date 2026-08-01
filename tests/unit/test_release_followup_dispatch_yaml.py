"""Every entrypoint that creates a GitHub Release must dispatch the follow-ups.

``publish-docker.yml``, ``publish-homebrew.yml`` and ``sbom.yml`` start on
``release: published``. A release created with ``GITHUB_TOKEN`` never emits
that event -- GitHub suppresses it to stop workflows triggering themselves --
so an entrypoint that creates the release with the workflow token and stops
there ships to PyPI and to the GitHub Release and to nothing else: no GHCR
image, no tap formula bump, no SBOM assets, in a run that ends green.

``publish.yml`` gained the explicit dispatches; ``release-major-minor.yml``,
which creates its release the same way, did not. This guard is written against
the property rather than against the two files, so a third entrypoint cannot
reintroduce the gap: any job that runs ``gh release create`` owes the full
dispatch set, with input names the target workflow actually declares.
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

# Workflows whose only native trigger is the release event.
RELEASE_EVENT_CONSUMERS = ("publish-docker.yml", "publish-homebrew.yml", "sbom.yml")

_DISPATCH_RE = re.compile(r"gh workflow run\s+(?P<workflow>[\w.-]+\.ya?ml)")
_DISPATCH_INPUT_RE = re.compile(r"-f\s+(?P<name>[A-Za-z_][\w-]*)=")


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    return cast("dict[str, Any]", jobs)


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def _runs(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in _steps(job) if isinstance(step.get("run"), str)]


def _release_creating_jobs() -> list[tuple[str, str, dict[str, Any]]]:
    """Every (workflow file, job id, job) that creates a GitHub Release."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        workflow = _load(path)
        if not isinstance(workflow, dict):
            continue
        for job_id, job in _jobs(workflow).items():
            if not isinstance(job, dict):
                continue
            job_map = cast("dict[str, Any]", job)
            if any("gh release create" in run for run in _runs(job_map)):
                found.append((path.name, str(job_id), job_map))
    return found


def _dispatches(job: dict[str, Any]) -> dict[str, set[str]]:
    """Map dispatched workflow file -> the input names passed with ``-f``."""
    dispatched: dict[str, set[str]] = {}
    for run in _runs(job):
        match = _DISPATCH_RE.search(run)
        if match is None:
            continue
        dispatched[match.group("workflow")] = set(_DISPATCH_INPUT_RE.findall(run))
    return dispatched


def _declared_dispatch_inputs(workflow_file: str) -> set[str]:
    doc = _load(WORKFLOWS_DIR / workflow_file)
    triggers = doc.get("on", doc.get(True))
    assert isinstance(triggers, dict), f"{workflow_file} must declare mapping-style triggers"
    dispatch = triggers.get("workflow_dispatch")
    assert isinstance(dispatch, dict), f"{workflow_file} must accept workflow_dispatch"
    inputs = dispatch.get("inputs", {})
    assert isinstance(inputs, dict)
    return {str(name) for name in inputs}


def test_the_release_creating_entrypoints_are_the_expected_ones() -> None:
    """Keeps the contract honest if a release entrypoint is added or renamed."""
    creators = {(workflow, job_id) for workflow, job_id, _ in _release_creating_jobs()}

    assert creators == {
        ("publish.yml", "github-release"),
        ("release-major-minor.yml", "release"),
    }, f"unexpected set of release-creating jobs: {sorted(creators)}"


@pytest.mark.parametrize("consumer", RELEASE_EVENT_CONSUMERS)
def test_every_release_creating_job_dispatches_the_consumer(consumer: str) -> None:
    """A release created with GITHUB_TOKEN emits no event, so each is dispatched."""
    for workflow, job_id, job in _release_creating_jobs():
        dispatched = _dispatches(job)
        assert consumer in dispatched, (
            f"{workflow}::{job_id} creates a GitHub Release but never dispatches "
            f"{consumer}, which only starts on `release: published` -- an event a "
            f"GITHUB_TOKEN release does not emit"
        )


def test_every_release_creating_job_may_dispatch_workflows() -> None:
    """Dispatching needs `actions: write` on the job."""
    for workflow, job_id, job in _release_creating_jobs():
        permissions = job.get("permissions", {})
        assert isinstance(permissions, dict), f"{workflow}::{job_id} must declare permissions"
        assert permissions.get("actions") == "write", (
            f"{workflow}::{job_id} dispatches follow-up workflows and needs actions: write"
        )


def test_dispatched_inputs_match_the_target_workflow_inputs() -> None:
    """Every `-f name=` passed on dispatch must be an input the target declares."""
    for workflow, job_id, job in _release_creating_jobs():
        for target, passed_inputs in _dispatches(job).items():
            declared = _declared_dispatch_inputs(target)
            unknown = passed_inputs - declared
            assert not unknown, (
                f"{workflow}::{job_id} passes {sorted(unknown)} to {target}, which declares {sorted(declared)}"
            )


def test_homebrew_dispatch_passes_a_bare_version_not_a_tag() -> None:
    """publish-homebrew.yml takes `3.12.0`; a `v` prefix silently builds the wrong formula."""
    for workflow, job_id, job in _release_creating_jobs():
        for run in _runs(job):
            if "publish-homebrew.yml" not in run:
                continue
            match = re.search(r"-f\s+version=\"?([^\"\s]+)", run)
            assert match is not None, f"{workflow}::{job_id} dispatches homebrew without a version"
            value = match.group(1)
            assert not value.startswith("v"), (
                f"{workflow}::{job_id} passes a tag ({value}) where a bare version is expected"
            )
