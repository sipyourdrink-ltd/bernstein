"""The tap credential is checked before the formula is built, and the error names where to fix it.

v3.19.2 failed at the LAST step of `publish-homebrew.yml` -- the cross-repo push -- with
``HOMEBREW_TAP_TOKEN secret not set``, after the version had been resolved, the sdist hashed and the
formula written. The tap stayed on 3.19.1 (#5826).

The wasted work is the smaller half. The error named the secret and not the one thing an operator
needed: **which environment to define it on**. #5819 moved this job from `pypi` to
`release-channels`, and an environment secret does not follow a job across environments -- the value
still exists on `pypi`, so from every angle except the one that matters the secret "is set". An
error that does not name the environment sends the reader to look at a secret that is right there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-homebrew.yml"

PREFLIGHT = "Check the tap credential is defined on this environment"


def _job() -> dict[str, Any]:
    doc = cast("dict[str, Any]", yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))
    job = doc["jobs"]["update-formula"]
    assert isinstance(job, dict)
    return cast("dict[str, Any]", job)


def _steps() -> list[dict[str, Any]]:
    return [step for step in _job().get("steps", []) if isinstance(step, dict)]


def _step_names() -> list[str]:
    return [str(step.get("name", "")) for step in _steps()]


def _step(name: str) -> dict[str, Any]:
    for step in _steps():
        if step.get("name") == name:
            return step
    pytest.fail(f"publish-homebrew.yml has no step named {name!r}")


def test_workflow_exists() -> None:
    assert WORKFLOW.is_file()


def test_the_credential_is_checked_before_the_formula_is_built() -> None:
    """Failing last wasted every step before it, and shipped nothing."""
    names = _step_names()

    assert PREFLIGHT in names
    assert names.index(PREFLIGHT) < names.index("Generate formula")
    assert names.index(PREFLIGHT) < names.index("Push to homebrew-tap repo")


def test_the_preflight_runs_after_the_version_is_resolved() -> None:
    """A pre-release takes a deliberate skip path; it must not become a credential failure."""
    names = _step_names()

    assert names.index("Get version and SHA") < names.index(PREFLIGHT)
    assert _step(PREFLIGHT).get("if") == "steps.meta.outputs.skip != 'true'"


def test_the_error_names_the_environment_the_job_reads() -> None:
    """The secret exists on `pypi`. Naming only the secret sends the reader to look at it."""
    run = str(_step(PREFLIGHT).get("run", ""))

    assert "${RELEASE_ENVIRONMENT}" in run
    assert "gh secret set HOMEBREW_TAP_TOKEN --env" in run
    assert "is not visible here" in run


def test_the_named_environment_is_the_one_the_job_actually_uses() -> None:
    """A preflight naming the wrong environment is worse than one naming none.

    It would send an operator to define the secret somewhere this job will never read it, and the
    next run would fail identically with the same confident advice.
    """
    job = _job()

    assert job.get("env", {}).get("RELEASE_ENVIRONMENT") == job.get("environment")


def test_the_push_step_still_refuses_without_the_credential() -> None:
    """The preflight is what should catch this; the push must not push anonymously if reached."""
    run = str(_step("Push to homebrew-tap repo").get("run", ""))

    assert 'if [ -z "$GH_TOKEN" ]' in run
    assert "exit 1" in run
    assert "${RELEASE_ENVIRONMENT}" in run


def test_the_credential_is_never_printed() -> None:
    """A preflight that leaks a PAT into a public log is worse than no preflight."""
    run = str(_step(PREFLIGHT).get("run", ""))

    for leak in ('echo "$GH_TOKEN', 'echo "${GH_TOKEN', "printf '%s' \"${GH_TOKEN"):
        assert leak not in run
