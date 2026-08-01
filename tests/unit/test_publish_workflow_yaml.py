"""Structural assertions on ``.github/workflows/publish.yml``
(issues #2642, #3322, #3323, #3325).

The MCP-registry publish job runs with ``id-token: write``; the tool it
downloads and executes there must be pinned to an immutable release and
integrity-checked before it runs, so an upstream ``releases/latest`` move
cannot change what executes in the privileged job.

The RPM publish job is pinned for a different reason: it is the only
distribution channel whose failure mode is invisible from the repository,
so it must be reachable on the tag trigger and must fail the job instead of
warning.

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


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


COPR_JOB = "publish-copr"

# Workflows that only ever ran off `release: published` and therefore never ran
# for a release the publish chain created itself.
RELEASE_EVENT_CONSUMERS = ("publish-docker.yml", "publish-homebrew.yml", "sbom.yml")

_DISPATCH_RE = re.compile(r"gh workflow run\s+(?P<workflow>[\w.-]+\.ya?ml)")
_DISPATCH_INPUT_RE = re.compile(r"-f\s+(?P<name>[A-Za-z_][\w-]*)=")


def _steps(workflow: dict[str, Any], job_name: str) -> list[dict[str, Any]]:
    job = workflow["jobs"][job_name]
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def _step(workflow: dict[str, Any], job_name: str, step_name: str) -> dict[str, Any]:
    job = workflow["jobs"][job_name]
    for step_value in job.get("steps", []):
        if isinstance(step_value, dict) and step_value.get("name") == step_name:
            return cast("dict[str, Any]", step_value)
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


# copr-cli 2.5 imports `rich` at module scope but declares only
# `copr, humanize, jinja2, setuptools`, so a clean `pip install copr-cli==2.5`
# produces a CLI that cannot start. Recorded here so a future bump cannot
# walk back into a release with a known-incomplete dependency set.
COPR_CLI_RELEASES_WITH_UNDECLARED_IMPORTS = frozenset({"2.5"})


def test_copr_cli_pin_avoids_releases_with_undeclared_imports(workflow: dict[str, Any]) -> None:
    run = _step(workflow, COPR_JOB, "Install rpmbuild and copr-cli")["run"]
    match = re.search(r"copr-cli==(\S+?)\"", run)
    assert match is not None, "copr-cli must be installed from a pinned version"
    assert match.group(1) not in COPR_CLI_RELEASES_WITH_UNDECLARED_IMPORTS


def test_copr_cli_install_is_proven_to_run_before_the_credential_is_written(
    workflow: dict[str, Any],
) -> None:
    """Running the CLI once at install time turns an unusable install into an
    install-step failure instead of a failure after the token is on disk."""
    run = _step(workflow, COPR_JOB, "Install rpmbuild and copr-cli")["run"]
    install_pos = run.find("copr-cli==")
    smoke_pos = run.find("copr-cli --version")
    assert smoke_pos != -1, "the install step must run copr-cli once to prove it imports"
    assert install_pos < smoke_pos

    # The smoke check has to be in the install step, which runs before the
    # step that writes ~/.config/copr.
    steps = [step.get("name") for step in workflow["jobs"][COPR_JOB]["steps"] if isinstance(step, dict)]
    assert steps.index("Install rpmbuild and copr-cli") < steps.index("Write copr-cli configuration")
