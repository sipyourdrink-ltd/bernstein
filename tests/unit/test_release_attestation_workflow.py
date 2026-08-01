"""Smoke test: the release-attestation workflow steps are present and well-formed.

Closes the FINOS AIGF CTRL-MODEL-SUPPLY-CHAIN release-artefact gap by
asserting that the publish workflow actually calls
``actions/attest-build-provenance`` against ``dist/*`` with the right
permissions. Without this guard the gap could silently re-open during
a workflow refactor.

The test parses the YAML and walks the job tree; it does not execute
the workflows. The end-to-end ``gh attestation verify`` against the
public attestations endpoint is the responsibility of the dedicated
release-attestation smoke job (see ``release-attestation`` workflow).

Scope note: the auto-release workflow only pushes release tags.
The actual artefact build + Sigstore attestation + PyPI, npm, and
GitHub Release publish lives in publish.yml.
The attestation assertion therefore only applies to publish.yml.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLISH_WF = REPO_ROOT / ".github" / "workflows" / "publish.yml"
AUTO_RELEASE_WF = REPO_ROOT / ".github" / "workflows" / "auto-release.yml"

ATTEST_ACTION_PREFIX = "actions/attest-build-provenance"

type YamlMap = dict[str, object]


def _as_map(value: object, context: str) -> YamlMap:
    assert isinstance(value, dict), f"{context} must be a mapping"
    return cast("YamlMap", value)


def _load_yaml(path: Path) -> YamlMap:
    parsed: object = yaml.safe_load(path.read_text())
    return _as_map(parsed, path.name)


def _all_steps(jobs: object) -> list[tuple[str, YamlMap]]:
    out: list[tuple[str, YamlMap]] = []
    jobs_map = _as_map(jobs, "jobs")
    for job_name, job_value in jobs_map.items():
        if not isinstance(job_value, dict):
            continue
        job = cast("YamlMap", job_value)
        steps_value = job.get("steps", [])
        if not isinstance(steps_value, list):
            continue
        steps = cast("list[object]", steps_value)
        for step_value in steps:
            if isinstance(step_value, dict):
                out.append((job_name, cast("YamlMap", step_value)))
    return out


def _job(data: YamlMap, job_name: str) -> YamlMap:
    jobs = _as_map(data["jobs"], "jobs")
    return _as_map(jobs[job_name], job_name)


def _step(data: YamlMap, job_name: str, step_name: str) -> YamlMap:
    job = _job(data, job_name)
    steps_value = job.get("steps", [])
    assert isinstance(steps_value, list)
    steps = cast("list[object]", steps_value)
    for step_value in steps:
        if isinstance(step_value, dict):
            step = cast("YamlMap", step_value)
            if step.get("name") == step_name:
                return step
    pytest.fail(f"{PUBLISH_WF.name}::{job_name} has no step named {step_name!r}")


def _step_run(data: YamlMap, job_name: str, step_name: str) -> str:
    run = _step(data, job_name, step_name).get("run")
    assert isinstance(run, str)
    return run


@pytest.mark.parametrize("workflow_path", [PUBLISH_WF])
def test_workflow_yaml_parses(workflow_path: Path) -> None:
    """Workflow YAML is syntactically valid -- prevents typos breaking CI silently."""
    data = _load_yaml(workflow_path)
    assert isinstance(data, dict)
    assert "jobs" in data


@pytest.mark.parametrize("workflow_path", [PUBLISH_WF])
def test_workflow_calls_attest_build_provenance(workflow_path: Path) -> None:
    """At least one job step uses ``actions/attest-build-provenance@<ref>``."""
    data = _load_yaml(workflow_path)
    steps = _all_steps(data["jobs"])
    matching = [
        (job, step)
        for job, step in steps
        if isinstance(uses := step.get("uses"), str) and uses.startswith(ATTEST_ACTION_PREFIX)
    ]
    assert matching, (
        f"{workflow_path.name} no longer calls actions/attest-build-provenance -- "
        "FINOS AIGF CTRL-MODEL-SUPPLY-CHAIN release-artefact gap would re-open"
    )


@pytest.mark.parametrize("workflow_path", [PUBLISH_WF])
def test_attest_step_has_subject_path(workflow_path: Path) -> None:
    """The attest step must declare subject-path so ``dist/*`` is actually attested."""
    data = _load_yaml(workflow_path)
    steps = _all_steps(data["jobs"])
    for _job, step in steps:
        uses = step.get("uses")
        if isinstance(uses, str) and uses.startswith(ATTEST_ACTION_PREFIX):
            with_block = _as_map(step.get("with", {}), "attest with")
            assert "subject-path" in with_block, (
                f"{workflow_path.name}: attest step missing subject-path -- nothing would be signed"
            )
            assert "dist" in str(with_block["subject-path"])


@pytest.mark.parametrize("workflow_path", [PUBLISH_WF])
def test_attest_job_has_required_permissions(workflow_path: Path) -> None:
    """The job hosting the attest step must declare id-token: write + attestations: write."""
    data = _load_yaml(workflow_path)
    jobs = _as_map(data["jobs"], "jobs")
    for job_name, job_value in jobs.items():
        if not isinstance(job_value, dict):
            continue
        job = cast("YamlMap", job_value)
        steps_value = job.get("steps", [])
        assert isinstance(steps_value, list)
        steps = cast("list[object]", steps_value)
        has_attest = False
        for step_value in steps:
            if not isinstance(step_value, dict):
                continue
            step = cast("YamlMap", step_value)
            uses = step.get("uses")
            if isinstance(uses, str) and uses.startswith(ATTEST_ACTION_PREFIX):
                has_attest = True
                break
        if not has_attest:
            continue
        perms = _as_map(job.get("permissions", {}), "permissions")
        # Permissions block can be an empty dict, a single string, or a mapping.
        # We only need the mapping form here -- attest needs writable scopes.
        assert perms.get("id-token") == "write", (
            f"{workflow_path.name}::{job_name} missing id-token: write -- Sigstore keyless OIDC will fail at runtime"
        )
        assert perms.get("attestations") == "write", (
            f"{workflow_path.name}::{job_name} missing attestations: write -- "
            "the GitHub attestations API will reject the upload"
        )


def test_attest_action_pinned_to_commit_sha() -> None:
    """The attest action ref is pinned to a 40-char commit sha (Sonar S7409 / supply-chain)."""
    data = _load_yaml(PUBLISH_WF)
    steps = _all_steps(data["jobs"])
    for _job, step in steps:
        uses = step.get("uses")
        if isinstance(uses, str) and uses.startswith(ATTEST_ACTION_PREFIX):
            ref = uses.split("@", 1)[1] if "@" in uses else ""
            assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), (
                f"actions/attest-build-provenance must be pinned to a 40-char sha, got: {uses}"
            )
            return
    pytest.fail("publish.yml has no attest-build-provenance step")


def test_protocol_gate_does_not_ignore_install_or_pytest_failures() -> None:
    """The protocol gate must fail when dependency install or pytest exits non-zero."""
    data = _load_yaml(PUBLISH_WF)
    run = _step_run(data, "protocol-gate", "Run protocol compatibility check")
    unsafe_lines = [line.strip() for line in run.splitlines() if line.strip().startswith(("uv pip install", "uv run "))]
    assert unsafe_lines
    assert all("|| true" not in line for line in unsafe_lines), unsafe_lines


def test_protocol_gate_installs_extra_dependencies_without_bare_uv_pip() -> None:
    """The protocol gate must not call ``uv pip install`` without creating a venv first."""
    data = _load_yaml(PUBLISH_WF)
    run = _step_run(data, "protocol-gate", "Run protocol compatibility check")
    assert "uv pip install" not in run
    assert "uv run --with mcp --with a2a pytest tests/protocol/ -v" in run


def test_protocol_gate_status_uses_pytest_exit_code() -> None:
    """The compat JSON status must use pytest's exit code rather than output substrings."""
    data = _load_yaml(PUBLISH_WF)
    run = _step_run(data, "protocol-gate", "Run protocol compatibility check")
    assert '"FAILED" not in test_output' not in run
    assert "pytest_exit_code" in run


def test_publish_test_job_runs_release_tests() -> None:
    """The release guard test job must run tests, not only lint."""
    data = _load_yaml(PUBLISH_WF)
    job = _job(data, "test")
    assert job.get("name") == "Verify tests pass"

    steps_value = job.get("steps", [])
    assert isinstance(steps_value, list)
    steps = cast("list[object]", steps_value)
    runs = [
        run
        for step_value in steps
        if isinstance(step_value, dict)
        if isinstance(run := cast("YamlMap", step_value).get("run"), str)
    ]
    assert "uv run python scripts/run_tests.py -k release -x" in "\n".join(runs)


def test_github_release_uploads_and_asserts_dist_assets() -> None:
    """Existing GitHub Releases must receive dist assets and fail if assets are absent."""
    data = _load_yaml(PUBLISH_WF)
    run = _step_run(data, "github-release", "Create release")
    assert "|| echo" not in run
    assert "gh release upload" in run
    assert "--clobber" in run
    assert "gh release view" in run
    assert "asset_count" in run


def test_auto_release_does_not_create_github_releases() -> None:
    """auto-release must only tag; publish.yml owns GitHub Release creation."""
    text = AUTO_RELEASE_WF.read_text(encoding="utf-8")
    assert "gh release create" not in text
    assert "gh release upload" not in text


def test_npm_publish_failures_do_not_block_python_release() -> None:
    """The npm wrapper stays off the Python release critical path.

    The wrapper used to be kept off that path by swallowing its own failures,
    which also hid a broken token for months (#3322). Isolation is a property
    of the job graph, not of the step's error handling: no job may depend on
    ``publish-npm``, so a red wrapper publish reports the problem without
    holding back PyPI or the GitHub Release.
    """
    data = _load_yaml(PUBLISH_WF)
    job = _job(data, "publish-npm")
    assert job.get("name") == "Publish npm wrapper"

    jobs = cast("YamlMap", data["jobs"])
    for job_id, raw_job in jobs.items():
        needs = cast("YamlMap", raw_job).get("needs")
        dependencies = [needs] if isinstance(needs, str) else list(needs or [])
        assert "publish-npm" not in dependencies, f"job {job_id!r} would be blocked by the npm wrapper publish"

    publish_step = _step(data, "publish-npm", "Publish to npm")
    run = publish_step.get("run")
    assert isinstance(run, str)
    assert "npm publish --access public" in run
