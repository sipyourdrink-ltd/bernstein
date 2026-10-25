"""Every entrypoint that creates a GitHub Release must dispatch the follow-ups.

``publish-docker.yml``, ``publish-homebrew.yml`` and ``sbom.yml`` start on
``release: published``. A release created with ``GITHUB_TOKEN`` never emits
that event -- GitHub suppresses it to stop workflows triggering themselves --
so an entrypoint that creates the release with the workflow token and stops
there ships to PyPI and to the GitHub Release and to nothing else: no GHCR
image, no tap formula bump, no SBOM assets, in a run that ends green.

``publish.yml`` is the only release-creating entrypoint left: it used to share
this gap with ``release-major-minor.yml``, which created its own release
inline the same way, until #3948 moved that workflow onto the same
PR-through-the-merge-queue path every other release takes, deleting its
inline release creation (and its own copy of these dispatches) entirely. This
guard is written against the property rather than against a fixed file list,
so a future entrypoint cannot reintroduce the gap: any job that runs
``gh release create`` owes the full dispatch set in its own job or a dependent
job, with input names the target workflow actually declares.
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


def _followup_jobs(workflow_file: str, creator: str) -> dict[str, Any]:
    """The creator and jobs reachable downstream in the same native graph."""
    jobs = _jobs(_load(WORKFLOWS_DIR / workflow_file))
    reachable = {creator}
    while True:
        expanded = reachable | {
            name
            for name, job in jobs.items()
            if reachable.intersection([job["needs"]] if isinstance(job.get("needs"), str) else job.get("needs", []))
        }
        if expanded == reachable:
            return {name: jobs[name] for name in reachable}
        reachable = expanded


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
    """Keeps the contract honest if a release entrypoint is added or renamed.

    ``release-major-minor.yml`` used to be a second creator here; #3948 moved
    it onto the PR-through-the-merge-queue path, so ``publish.yml`` is now the
    only job in the repo that runs ``gh release create``.
    """
    creators = {(workflow, job_id) for workflow, job_id, _ in _release_creating_jobs()}

    assert creators == {
        ("publish.yml", "github-release"),
    }, f"unexpected set of release-creating jobs: {sorted(creators)}"


@pytest.mark.parametrize("consumer", RELEASE_EVENT_CONSUMERS)
def test_every_release_creator_reaches_each_consumer_once(consumer: str) -> None:
    """Token-created releases emit no event; dispatch each consumer exactly once."""
    for workflow, creator, _ in _release_creating_jobs():
        matches = [
            run
            for job in _followup_jobs(workflow, creator).values()
            for run in _runs(job)
            if any(m.group("workflow") == consumer for m in _DISPATCH_RE.finditer(run))
        ]
        assert len(matches) == 1, f"{workflow}::{creator} must reach exactly one {consumer} dispatch"


def test_every_followup_dispatcher_may_dispatch_workflows() -> None:
    """Each actual dispatcher needs actions:write, including dependent jobs."""
    for workflow, creator, _ in _release_creating_jobs():
        for name, job in _followup_jobs(workflow, creator).items():
            if _dispatches(job):
                assert job.get("permissions", {}).get("actions") == "write", f"{workflow}::{name}"


def test_dispatched_inputs_match_the_target_workflow_inputs() -> None:
    """Every dispatched input is declared, including those in dependent jobs."""
    for workflow, creator, _ in _release_creating_jobs():
        for job in _followup_jobs(workflow, creator).values():
            for target, passed_inputs in _dispatches(job).items():
                assert passed_inputs <= _declared_dispatch_inputs(target), f"{workflow}::{target}"


def test_homebrew_dispatch_passes_a_bare_version_not_a_tag() -> None:
    """Homebrew needs the bare version; discovery must not silently become vacuous."""
    runs = [
        run
        for workflow, creator, _ in _release_creating_jobs()
        for job in _followup_jobs(workflow, creator).values()
        for run in _runs(job)
        if "gh workflow run publish-homebrew.yml" in run
    ]
    assert len(runs) == 1
    match = re.search(r'-f\s+version="?([^"\s]+)', runs[0])
    assert match is not None
    assert match.group(1) == "${TAG#v}"
