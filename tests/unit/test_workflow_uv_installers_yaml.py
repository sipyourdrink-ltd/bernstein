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
        "pr-observability-summary.yml",
        "nightly-canary.yml",
    ),
)
def test_project_install_workflows_use_local_bootstrap_action(workflow_name: str) -> None:
    """Project install workflows must use the repo bootstrap action for uv setup."""
    workflow = WORKFLOWS_DIR / workflow_name
    steps = _iter_steps(_load_workflow(workflow))
    uses_values = [step.get("uses") for step in steps]
    assert "./.github/actions/bootstrap" in uses_values


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
