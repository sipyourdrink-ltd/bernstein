"""Structural checks for uv installer usage in selected workflows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
BOOTSTRAP_ACTION = REPO_ROOT / ".github" / "actions" / "bootstrap" / "action.yml"
PINNED_UV_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")

OWNED_WORKFLOWS = (
    "docs-observability-snapshot.yml",
    "pr-observability-summary.yml",
    "nightly-canary.yml",
)

CURL_PIPE_SH_RE = re.compile(r"curl\b[^\n|]*\|\s*(?:ba)?sh\b")
UNPINNED_PIP_UV_RE = re.compile(r"\bpip\s+install\b[^\n]*\buv\b")


def _load_workflow(path: Path) -> dict[str, object]:
    parsed = cast(object, yaml.safe_load(path.read_text(encoding="utf-8")))
    assert isinstance(parsed, dict), f"{path} must parse as a YAML mapping"
    return cast(dict[str, object], parsed)


def _iter_steps(value: object) -> list[dict[str, object]]:
    steps: list[dict[str, object]] = []
    if isinstance(value, dict):
        node = cast(dict[str, object], value)
        maybe_steps = node.get("steps")
        if isinstance(maybe_steps, list):
            for raw_step in cast(list[object], maybe_steps):
                if isinstance(raw_step, dict):
                    steps.append(cast(dict[str, object], raw_step))
        for child in node.values():
            steps.extend(_iter_steps(child))
    elif isinstance(value, list):
        for child in cast(list[object], value):
            steps.extend(_iter_steps(child))
    return steps


@pytest.mark.parametrize("workflow_name", OWNED_WORKFLOWS)
def test_owned_workflows_do_not_install_uv_with_curl_pipe_shell(workflow_name: str) -> None:
    """Owned workflows must not pipe a live installer script into a shell."""
    workflow = WORKFLOWS_DIR / workflow_name
    text = workflow.read_text(encoding="utf-8")
    assert not CURL_PIPE_SH_RE.search(text), f"{workflow_name} must not install uv with `curl ... | sh`"


@pytest.mark.parametrize("workflow_name", OWNED_WORKFLOWS)
def test_owned_workflows_do_not_install_uv_with_unpinned_pip(workflow_name: str) -> None:
    """Owned workflows must not install uv from an unpinned pip requirement."""
    workflow = WORKFLOWS_DIR / workflow_name
    text = workflow.read_text(encoding="utf-8")
    assert not UNPINNED_PIP_UV_RE.search(text), f"{workflow_name} must not install uv with unpinned pip"


@pytest.mark.parametrize(
    "workflow_name",
    (
        "docs-observability-snapshot.yml",
        "nightly-canary.yml",
    ),
)
def test_project_install_workflows_use_local_bootstrap_action(workflow_name: str) -> None:
    """Project install workflows must use the repo bootstrap action for uv setup."""
    workflow = WORKFLOWS_DIR / workflow_name
    steps = _iter_steps(_load_workflow(workflow))
    uses_values = [step.get("uses") for step in steps]
    assert "./.github/actions/bootstrap" in uses_values


@pytest.mark.parametrize("workflow_name", ("pr-policy.yml", "pr-observability-summary.yml"))
def test_pr_setups_inline_the_bootstrap_pins_and_retry(workflow_name: str) -> None:
    bootstrap = _iter_steps(_load_workflow(BOOTSTRAP_ACTION))
    expected = next(step for step in bootstrap if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"))
    setup, retry = [
        step
        for step in _iter_steps(_load_workflow(WORKFLOWS_DIR / workflow_name))
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
    ]
    assert setup["uses"] == retry["uses"] == expected["uses"]
    assert setup["with"] == retry["with"]
    assert setup["with"]["version"] == expected["with"]["version"]
    assert setup["with"]["enable-cache"] is False
    assert setup["continue-on-error"] is True
    assert retry["if"] == f"steps.{setup['id']}.outcome == 'failure'"


def test_local_bootstrap_action_pins_setup_uv_version() -> None:
    """The shared bootstrap action must pin both setup-uv and the uv binary."""
    steps = _iter_steps(_load_workflow(BOOTSTRAP_ACTION))
    setup_uv_steps = [step for step in steps if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")]

    assert setup_uv_steps, "bootstrap action must use astral-sh/setup-uv"
    for step in setup_uv_steps:
        uses = step.get("uses")
        assert isinstance(uses, str)
        action_ref = uses.rsplit("@", 1)[1].split("#", 1)[0].strip()
        assert re.fullmatch(r"[0-9a-f]{40}", action_ref), f"setup-uv action must use a SHA pin: {uses}"

        with_block = step.get("with")
        assert isinstance(with_block, dict)
        with_values = cast(dict[str, object], with_block)
        uv_version = with_values.get("version")
        assert isinstance(uv_version, str)
        assert PINNED_UV_VERSION_RE.fullmatch(uv_version), f"uv must use an explicit semver pin: {uv_version}"
        assert uv_version != "latest"

    pinned_versions = {cast(str, cast(dict[str, object], step["with"])["version"]) for step in setup_uv_steps}
    assert len(pinned_versions) == 1, "bootstrap setup-uv steps must install the same uv version"


def test_local_bootstrap_action_retries_transient_setup_uv_failure() -> None:
    """A single transient astral-sh/setup-uv fetch failure must not redden the
    job outright (issue #3360): the primary attempt tolerates one failure via
    `continue-on-error`, and a retry step re-runs setup-uv once, gated on the
    primary step's recorded outcome. The retry step itself must not be
    continue-on-error, so a second consecutive failure genuinely fails the job.
    """
    parsed = _load_workflow(BOOTSTRAP_ACTION)
    runs = parsed.get("runs")
    assert isinstance(runs, dict)
    raw_steps = cast(dict[str, object], runs).get("steps")
    assert isinstance(raw_steps, list)
    step_list = [cast(dict[str, object], step) for step in cast(list[object], raw_steps) if isinstance(step, dict)]

    primary_steps = [
        step for step in step_list if str(step.get("uses", "")).startswith("astral-sh/setup-uv@") and step.get("id")
    ]
    assert primary_steps, "bootstrap action must have an identifiable (id-tagged) primary setup-uv step"

    for primary in primary_steps:
        step_id = primary["id"]
        assert primary.get("continue-on-error") is True, (
            f"primary setup-uv step {step_id!r} must be continue-on-error so a transient "
            "fetch failure does not redden the job before a retry gets a chance"
        )

        failure_guard = f"steps.{step_id}.outcome == 'failure'"
        retry_steps = [
            step
            for step in step_list
            if step is not primary
            and str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
            and failure_guard in str(step.get("if", ""))
        ]
        assert retry_steps, f"no retry step gated on `{failure_guard}` found for primary step {step_id!r}"

        for retry in retry_steps:
            assert retry.get("continue-on-error") is not True, (
                f"retry step for {step_id!r} must not itself be continue-on-error, "
                "so a second consecutive setup-uv failure genuinely fails the job"
            )
