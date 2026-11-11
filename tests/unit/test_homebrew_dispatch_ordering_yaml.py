"""Automated Homebrew publication must follow the actual PyPI upload."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")
ROOT = Path(__file__).resolve().parents[2]


def _workflow() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load((ROOT / ".github/workflows/publish.yml").read_text()))


def _dispatcher() -> tuple[dict[str, Any], dict[str, Any]]:
    matches = [
        (job, step)
        for job in _workflow()["jobs"].values()
        for step in job.get("steps", [])
        if "gh workflow run publish-homebrew.yml" in step.get("run", "")
    ]
    assert len(matches) == 1, "the automated publisher must dispatch Homebrew exactly once"
    return matches[0]


def test_homebrew_dispatch_waits_for_successful_pypi_upload_and_release() -> None:
    job, step = _dispatcher()
    needs = job.get("needs", [])
    assert set([needs] if isinstance(needs, str) else needs) == {"publish", "github-release"}
    # Normal GitHub dependency success semantics exclude failed/cancelled/skipped
    # prerequisites; an unconditional condition or tolerated error would bypass it.
    for node in (job, step):
        assert "if" not in node
        assert not node.get("continue-on-error", False)
    uploader = _workflow()["jobs"]["publish"]
    uploads = [s for s in uploader["steps"] if s.get("uses", "").startswith("pypa/gh-action-pypi-publish@")]
    assert len(uploads) == 1
    assert "if" not in uploads[0]
    assert not uploads[0].get("continue-on-error", False)
    assert "if" not in uploader
    assert not uploader.get("continue-on-error", False)
    assert uploader["environment"]["name"] == "pypi"


def test_homebrew_dispatch_preserves_its_existing_credential_boundary() -> None:
    job, step = _dispatcher()
    assert job["environment"] == "release-channels"
    assert job["permissions"] == {"actions": "write"}
    assert step["env"] == {
        "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "GH_REPO": "${{ github.repository }}",
        "TAG": "${{ env.RELEASE_TAG }}",
    }
    assert job["steps"][0]["uses"].startswith("step-security/harden-runner@")
    assert not any("checkout@" in s.get("uses", "") for s in job["steps"])


def test_checkout_free_homebrew_dispatch_has_explicit_repository_context() -> None:
    job, step = _dispatcher()
    assert not any("checkout@" in s.get("uses", "") for s in job["steps"])
    assert step["env"].get("GH_REPO") == "${{ github.repository }}"


def test_pypi_approval_does_not_delay_independent_release_consumers() -> None:
    release = _workflow()["jobs"]["github-release"]
    assert release["needs"] == "build"
    runs = "\n".join(s.get("run", "") for s in release["steps"])
    assert "gh workflow run publish-docker.yml" in runs
    assert "gh workflow run sbom.yml" in runs


@pytest.mark.parametrize("tag", ["v3.23.0", "v3.24.0rc1"])
@pytest.mark.parametrize("exit_code", [0, 17])
def test_homebrew_dispatch_preserves_version_and_propagates_failure(tmp_path: Path, tag: str, exit_code: int) -> None:
    _, step = _dispatcher()
    bash = shutil.which("bash")
    assert bash is not None, "bash is required to execute the native dispatch command"
    executable = tmp_path / "gh"
    record = tmp_path / "argv"
    executable.write_text(
        '#!/bin/sh\n[ "$GH_REPO" = "$EXPECTED_REPO" ] || exit 97\n'
        'printf "%s\\n" "$@" > "$RECORD"\nexit "$RECORD_EXIT"\n'
    )
    executable.chmod(0o700)
    result = subprocess.run(
        [bash, "-e", "-c", step["run"]],
        cwd=tmp_path,
        env={
            "PATH": str(tmp_path),
            "TAG": tag,
            "GH_REPO": step["env"].get("GH_REPO", "").replace("${{ github.repository }}", "example-owner/release-repo"),
            "EXPECTED_REPO": "example-owner/release-repo",
            "RECORD": record.as_posix(),
            "RECORD_EXIT": str(exit_code),
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == exit_code
    assert record.read_text().splitlines() == [
        "workflow",
        "run",
        "publish-homebrew.yml",
        "--ref",
        "main",
        "-f",
        f"version={tag.removeprefix('v')}",
    ]
