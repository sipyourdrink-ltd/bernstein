"""Structural assertions on ``.github/workflows/publish.yml`` (issues #2642, #3325).

The MCP-registry publish job runs with ``id-token: write``; the tool it
downloads and executes there must be pinned to an immutable release and
integrity-checked before it runs, so an upstream ``releases/latest`` move
cannot change what executes in the privileged job.

The RPM publish job is pinned for a different reason: it is the only
distribution channel whose failure mode is invisible from the repository,
so it must be reachable on the tag trigger and must fail the job instead of
warning.
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
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish.yml"


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


COPR_JOB = "publish-copr"


def _step(workflow: dict[str, Any], job_name: str, step_name: str) -> dict[str, Any]:
    job = workflow["jobs"][job_name]
    for step_value in job.get("steps", []):
        if isinstance(step_value, dict) and step_value.get("name") == step_name:
            return cast("dict[str, Any]", step_value)
    pytest.fail(f"{WORKFLOW.name}::{job_name} has no step named {step_name!r}")


def _step_run(workflow: dict[str, Any], job_name: str, step_name: str) -> str:
    run = _step(workflow, job_name, step_name).get("run")
    assert isinstance(run, str)
    return run


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


def test_copr_job_publishes_from_the_tag_trigger_not_the_release_event(workflow: dict[str, Any]) -> None:
    """A release created with ``GITHUB_TOKEN`` raises no ``release`` event.

    Hanging the RPM publish off ``release: published`` would make it
    unreachable for every automated release, exactly as it already is for the
    workflows tracked in #3323. It must sit on the same trigger the PyPI job
    uses.
    """
    triggers = workflow.get("on") or workflow.get(True)
    assert isinstance(triggers, dict)
    assert "push" in triggers
    assert "release" not in triggers, "the RPM publish must not depend on the release event"

    job = workflow["jobs"][COPR_JOB]
    assert job.get("needs") == "publish", "the RPM wrapper resolves the package from PyPI at run time"


def test_copr_job_fails_the_workflow_when_the_submission_fails(workflow: dict[str, Any]) -> None:
    """No warn-and-continue: a dropped RPM publish has to be visible (#3322)."""
    job = workflow["jobs"][COPR_JOB]
    assert job.get("continue-on-error") is not True

    submit = _step(workflow, COPR_JOB, "Submit build to Copr")
    assert submit.get("continue-on-error") is not True
    run = submit["run"]
    assert "set -euo pipefail" in run
    assert "::warning::" not in run
    assert "|| true" not in run
    assert "|| echo" not in run

    # `copr-cli build` exits non-zero when the build fails only if it waits for
    # it, so the job's verdict tracks the published RPM, not just the upload.
    commands = [line.strip() for line in run.splitlines() if not line.strip().startswith("#")]
    submit_commands = [line for line in commands if line.startswith("copr-cli build")]
    assert submit_commands, "the step must invoke `copr-cli build`"
    for command in submit_commands:
        assert "--nowait" not in command


def test_copr_config_secret_is_written_from_the_environment_with_owner_only_mode(
    workflow: dict[str, Any],
) -> None:
    """The credential must not be interpolated into the shell text, and the
    file copr-cli reads must not be world-readable on a shared runner."""
    step = _step(workflow, COPR_JOB, "Write copr-cli configuration")
    env = step.get("env", {})
    assert env.get("COPR_CONFIG") == "${{ secrets.COPR_CONFIG }}"

    run = step["run"]
    assert "${{" not in run, "secrets must reach the shell through env, not expression interpolation"
    assert "chmod 600" in run
    assert "~/.config/copr" in run


def test_copr_job_fails_loudly_when_the_secret_is_absent(workflow: dict[str, Any]) -> None:
    """A missing credential is a broken release channel, not a skip."""
    run = _step(workflow, COPR_JOB, "Write copr-cli configuration")["run"]
    assert "::error::" in run
    assert "exit 1" in run
    assert "exit 0" not in run


def test_copr_cli_install_is_version_pinned(workflow: dict[str, Any]) -> None:
    """An unpinned installer lets an upstream release change what runs here."""
    run = _step(workflow, COPR_JOB, "Install rpmbuild and copr-cli")["run"]
    assert re.search(r"copr-cli==\d+\.\d+", run), "copr-cli must be installed from a pinned version"


def test_copr_job_can_be_replayed_for_an_already_published_tag(workflow: dict[str, Any]) -> None:
    """Republishing one channel must not require cutting a new release."""
    triggers = workflow.get("on") or workflow.get(True)
    assert isinstance(triggers, dict)
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert "copr_only" in inputs
    assert inputs["copr_only"]["type"] == "boolean"

    # The gate lives on the root jobs; everything else skips through `needs`.
    for job_name in ("protocol-gate", "test", "version-check"):
        condition = workflow["jobs"][job_name].get("if")
        assert isinstance(condition, str), f"{job_name} must be skippable for a channel-only replay"
        assert "copr_only" in condition

    copr_condition = workflow["jobs"][COPR_JOB].get("if")
    assert isinstance(copr_condition, str)
    assert "always()" in copr_condition, "a skipped upstream job must not skip the replay"
    assert "inputs.copr_only" in copr_condition
    assert "needs.publish.result == 'success'" in copr_condition


def test_copr_replay_checks_out_packaging_sources_that_carry_the_builder(workflow: dict[str, Any]) -> None:
    """A replayed tag can predate the build script; the sources must come from
    the dispatch ref in that mode or the job fails on a missing file."""
    job = workflow["jobs"][COPR_JOB]
    checkout = next(
        step
        for step in job["steps"]
        if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/checkout@")
    )
    ref = checkout["with"]["ref"]
    assert "inputs.copr_only" in ref
