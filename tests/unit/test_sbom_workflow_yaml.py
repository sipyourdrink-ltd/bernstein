"""Structural assertions on ``.github/workflows/sbom.yml`` (issue #3323).

The SBOM run has two entry paths: the ``release: published`` event, and an
explicit dispatch from ``publish.yml`` for the releases that event never fires
for. Both must attach the generated SBOMs to the release, so the attachment
decision is keyed on "a release exists for this ref", never on which event
started the run.

That key is an HTTP status, not an exit code. ``gh`` exits non-zero for a
missing release and for a bad token, a rate limit, or a 5xx alike; reading
every non-zero exit as "no release" skips the attach step on a release that
does exist, and the run still ends green with the SBOMs stranded in a
30-day workflow artifact. Only a 404 may mean absence.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "sbom.yml"

SBOM_FILES = ("bernstein.spdx.json", "bernstein.cyclonedx.json")


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    job = workflow["jobs"]["sbom"]
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def _step(workflow: dict[str, Any], name: str) -> dict[str, Any]:
    match = next((step for step in _steps(workflow) if step.get("name") == name), None)
    assert match is not None, f"{WORKFLOW.name} has no step named {name!r}"
    return match


def test_asset_attachment_is_not_gated_on_the_triggering_event(workflow: dict[str, Any]) -> None:
    """`event_name == 'release'` makes a dispatched run generate SBOMs it never attaches."""
    for step in _steps(workflow):
        conditions = [str(step.get("if", ""))]
        with_block = step.get("with", {})
        if isinstance(with_block, dict):
            conditions.extend(str(value) for value in cast("dict[str, Any]", with_block).values())
        for condition in conditions:
            assert "github.event_name == 'release'" not in condition, (
                f"step {step.get('name')!r} still keys asset attachment on the triggering event"
            )


def test_release_is_resolved_from_the_ref_not_the_event(workflow: dict[str, Any]) -> None:
    """The tag comes from the dispatch input as readily as from the release payload."""
    step = _step(workflow, "Resolve release for ref")
    run = str(step.get("run", ""))
    env = step.get("env", {})
    assert isinstance(env, dict)

    assert step.get("id") == "rel"
    assert "inputs.ref" in str(env)
    assert "github.event.release.tag_name" in str(env)
    assert "releases/tags/" in run, "the release is looked up by the resolved tag"
    assert "release_exists=" in run
    assert "tag=" in run


def test_sboms_are_attached_whenever_a_release_exists(workflow: dict[str, Any]) -> None:
    """Both SBOMs land on the release on the dispatch path as well as the event path."""
    step = _step(workflow, "Attach SBOMs to the release")
    condition = str(step.get("if", ""))
    run = str(step.get("run", ""))

    assert "steps.rel.outputs.release_exists == 'true'" in condition
    assert "gh release upload" in run
    assert "--clobber" in run
    for sbom in SBOM_FILES:
        assert sbom in run, f"{sbom} is never attached to the release"


def test_sbom_job_can_write_release_assets(workflow: dict[str, Any]) -> None:
    """Attaching assets needs `contents: write` on the job."""
    permissions = workflow["jobs"]["sbom"].get("permissions", {})
    assert isinstance(permissions, dict)
    assert permissions.get("contents") == "write"


def test_release_lookup_classifies_the_http_status_not_the_exit_code(
    workflow: dict[str, Any],
) -> None:
    """A non-zero ``gh`` exit is not by itself evidence that the release is absent."""
    run = str(_step(workflow, "Resolve release for ref").get("run", ""))

    assert "HTTP 404" in run, "the lookup must recognise the one status that means absence"
    assert "::error::" in run, "an unclassifiable lookup failure must be reported as an error"
    assert "exit 1" in run, "an unclassifiable lookup failure must fail the job"
    assert "gh release view" not in run, (
        "`gh release view` collapses 404 with auth/rate-limit/transient failures; "
        "the releases-by-tag API surfaces the status"
    )


# --- behavioural: run the step's shell against a stubbed `gh` ------------------

_GH_STUB = """#!/bin/sh
printf '%s' "$GH_STUB_STDERR" >&2
exit "$GH_STUB_EXIT"
"""


def _resolve_release(
    tmp_path: Path, workflow: dict[str, Any], *, gh_exit: int, gh_stderr: str
) -> tuple[int, dict[str, str], str]:
    """Execute the real ``Resolve release for ref`` shell with a stubbed ``gh``.

    Returns (exit code, parsed ``$GITHUB_OUTPUT`` map, combined output).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(_GH_STUB, encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    output_file = tmp_path / "github_output"
    output_file.touch()

    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GITHUB_OUTPUT": str(output_file),
        "GITHUB_REF_NAME": "main",
        "INPUT_REF": "v9.9.9",
        "EVENT_TAG": "",
        "REPO": "sipyourdrink-ltd/bernstein",
        "GH_TOKEN": "stub",
        "GH_STUB_EXIT": str(gh_exit),
        "GH_STUB_STDERR": gh_stderr,
    }
    script = str(_step(workflow, "Resolve release for ref").get("run", ""))
    completed = subprocess.run(
        ["bash", "-e", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )
    outputs: dict[str, str] = {}
    for line in output_file.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            outputs[key] = value
    return completed.returncode, outputs, completed.stdout + completed.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash to run the step body")
def test_existing_release_is_resolved_as_present(tmp_path: Path, workflow: dict[str, Any]) -> None:
    code, outputs, _ = _resolve_release(tmp_path, workflow, gh_exit=0, gh_stderr="")

    assert code == 0
    assert outputs["release_exists"] == "true"
    assert outputs["tag"] == "v9.9.9"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash to run the step body")
def test_a_404_is_resolved_as_absent(tmp_path: Path, workflow: dict[str, Any]) -> None:
    code, outputs, _ = _resolve_release(tmp_path, workflow, gh_exit=1, gh_stderr="gh: Not Found (HTTP 404)\n")

    assert code == 0
    assert outputs["release_exists"] == "false"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash to run the step body")
@pytest.mark.parametrize(
    "gh_stderr",
    [
        "gh: Bad credentials (HTTP 401)\n",
        "gh: Resource not accessible by integration (HTTP 403)\n",
        "gh: API rate limit exceeded (HTTP 403)\n",
        "gh: Server Error (HTTP 502)\n",
        "error connecting to api.github.com\n",
    ],
)
def test_a_non_404_failure_fails_the_job(tmp_path: Path, workflow: dict[str, Any], gh_stderr: str) -> None:
    """The regression: these used to read as "no release" and skip the attach step."""
    code, outputs, combined = _resolve_release(tmp_path, workflow, gh_exit=1, gh_stderr=gh_stderr)

    assert code != 0, f"{gh_stderr.strip()!r} must fail the job, not skip the attachment"
    assert outputs.get("release_exists") != "false", "an unreachable API must not be recorded as a missing release"
    assert "::error::" in combined
