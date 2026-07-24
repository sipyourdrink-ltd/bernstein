"""Structural assertions on ``.github/workflows/publish.yml`` (issue #2642).

The MCP-registry publish job runs with ``id-token: write``; the tool it
downloads and executes there must be pinned to an immutable release and
integrity-checked before it runs, so an upstream ``releases/latest`` move
cannot change what executes in the privileged job.
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
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish.yml"


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _step_run(workflow: dict[str, Any], job_name: str, step_name: str) -> str:
    job = workflow["jobs"][job_name]
    for step_value in job.get("steps", []):
        if isinstance(step_value, dict) and step_value.get("name") == step_name:
            run = step_value.get("run")
            assert isinstance(run, str)
            return run
    pytest.fail(f"{WORKFLOW.name}::{job_name} has no step named {step_name!r}")


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
