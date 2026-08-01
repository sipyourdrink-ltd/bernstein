"""Structural assertions on ``.github/workflows/publish.yml`` (issues #2642,
#3322, #3323).

The MCP-registry publish job runs with ``id-token: write``; the tool it
downloads and executes there must be pinned to an immutable release and
integrity-checked before it runs, so an upstream ``releases/latest`` move
cannot change what executes in the privileged job.

Two further contracts are pinned here:

* the npm wrapper publish must fail the job on a publish error instead of
  demoting it to a warning inside a green run, and
* every workflow that consumes the ``release: published`` event must also be
  dispatched explicitly, because a release created with ``GITHUB_TOKEN`` never
  emits that event.
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
WORKFLOW = WORKFLOWS_DIR / "publish.yml"

# Workflows that only ever ran off `release: published` and therefore never ran
# for a release the publish chain created itself.
RELEASE_EVENT_CONSUMERS = ("publish-docker.yml", "publish-homebrew.yml", "sbom.yml")

_DISPATCH_RE = re.compile(r"gh workflow run\s+(?P<workflow>[\w.-]+\.ya?ml)")
_DISPATCH_INPUT_RE = re.compile(r"-f\s+(?P<name>[A-Za-z_][\w-]*)=")


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _steps(workflow: dict[str, Any], job_name: str) -> list[dict[str, Any]]:
    job = workflow["jobs"][job_name]
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def _step_run(workflow: dict[str, Any], job_name: str, step_name: str) -> str:
    for step_value in _steps(workflow, job_name):
        if step_value.get("name") == step_name:
            run = step_value.get("run")
            assert isinstance(run, str)
            return run
    pytest.fail(f"{WORKFLOW.name}::{job_name} has no step named {step_name!r}")


def _dispatches(workflow: dict[str, Any], job_name: str) -> dict[str, set[str]]:
    """Map dispatched workflow file -> the input names passed with ``-f``."""
    dispatched: dict[str, set[str]] = {}
    for step in _steps(workflow, job_name):
        run = step.get("run")
        if not isinstance(run, str):
            continue
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


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return _load(WORKFLOW)


def test_mcp_publisher_download_is_pinned_not_latest(workflow: dict[str, Any]) -> None:
    run = _step_run(workflow, "publish-mcp-registry", "Install mcp-publisher")
    assert "releases/latest" not in run
    # Immutable release-tag asset URL.
    assert "releases/download/" in run


def test_mcp_publisher_download_is_checksum_verified(workflow: dict[str, Any]) -> None:
    """The pinned asset is verified against a recorded sha256 before it is
    extracted and executed."""
    job = workflow["jobs"]["publish-mcp-registry"]
    install_step = next(
        step for step in job["steps"] if isinstance(step, dict) and step.get("name") == "Install mcp-publisher"
    )
    run = install_step["run"]
    env = install_step.get("env", {})
    assert "MCP_PUBLISHER_SHA256" in env
    # A pinned, non-empty 64-hex checksum.
    checksum = str(env["MCP_PUBLISHER_SHA256"])
    assert len(checksum) == 64
    assert all(c in "0123456789abcdef" for c in checksum)
    # The checksum is enforced (sha256sum -c) before the archive is unpacked.
    verify_pos = run.find("sha256sum -c")
    extract_pos = run.find("tar ")
    assert verify_pos != -1, "checksum is never verified"
    assert extract_pos != -1, "archive is never extracted"
    assert verify_pos < extract_pos, "checksum must be verified before extraction"


def test_npm_publish_failure_fails_the_job(workflow: dict[str, Any]) -> None:
    """A failed wrapper publish must fail the job, not warn inside a green run."""
    run = _step_run(workflow, "publish-npm", "Publish to npm")

    assert "::warning::npm wrapper publish failed" not in run, (
        "a publish failure demoted to ::warning: leaves the job green while the registry stays behind"
    )
    assert "::error::" in run, "a publish failure must be reported as an error annotation"
    assert 'exit "$code"' in run, "the npm exit code must propagate to the job result"


def test_npm_publish_tolerates_only_an_already_published_version(workflow: dict[str, Any]) -> None:
    """The single non-fatal failure is a version already on the registry."""
    run = _step_run(workflow, "publish-npm", "Publish to npm")

    # npm reports a re-publish of an existing version as EPUBLISHCONFLICT
    # (older clients: E403 "cannot publish over the previously published versions").
    assert "EPUBLISHCONFLICT" in run
    assert "cannot publish over the previously published version" in run
    # The registry answers an unauthorised token with a 404 on the PUT. That
    # must stay outside the tolerated set, or the original defect returns in a
    # new shape.
    assert "404" not in run, "a 404 (token without publish rights) must not be treated as success"


def test_npm_missing_token_fails_the_job(workflow: dict[str, Any]) -> None:
    """A release channel without its credential is a failure, not a silent skip."""
    run = _step_run(workflow, "publish-npm", "Publish to npm")

    assert "::warning::NPM_TOKEN is not configured" not in run
    assert "::error::NPM_TOKEN is not configured" in run
    assert "exit 1" in run


def test_github_release_dispatches_every_release_event_consumer(workflow: dict[str, Any]) -> None:
    """A GITHUB_TOKEN release emits no `release: published`, so each consumer is dispatched."""
    dispatched = _dispatches(workflow, "github-release")

    for consumer in RELEASE_EVENT_CONSUMERS:
        assert consumer in dispatched, f"{consumer} consumes `release: published` but is never dispatched"


def test_dispatched_inputs_match_the_target_workflow_inputs(workflow: dict[str, Any]) -> None:
    """Every `-f name=` passed on dispatch must be an input the target declares."""
    dispatched = _dispatches(workflow, "github-release")

    for workflow_file, passed_inputs in dispatched.items():
        declared = _declared_dispatch_inputs(workflow_file)
        unknown = passed_inputs - declared
        assert not unknown, f"{workflow_file} does not declare dispatch input(s) {sorted(unknown)}"
