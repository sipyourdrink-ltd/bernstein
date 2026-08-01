"""Structural assertions on ``.github/workflows/sbom.yml`` (issue #3323).

The SBOM run has two entry paths: the ``release: published`` event, and an
explicit dispatch from ``publish.yml`` for the releases that event never fires
for. Both must attach the generated SBOMs to the release, so the attachment
decision is keyed on "a release exists for this ref", never on which event
started the run.
"""

from __future__ import annotations

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
    assert "gh release view" in run
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
