"""Tests for scripts/render_docker_mcp_catalog.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def script_path() -> Path:
    """Path to the render script."""
    repo_root = Path(__file__).parent.parent.parent
    return repo_root / "scripts" / "render_docker_mcp_catalog.py"


@pytest.fixture()
def sample_yaml(tmp_path: Path) -> Path:
    """Create a minimal server.yaml template."""
    content = """\
name: bernstein
image: ghcr.io/sipyourdrink-ltd/bernstein
type: server
source:
  project: https://github.com/sipyourdrink-ltd/bernstein
  commit: ec2c1306eba4f51ace107382dab495156e7f20e6
run:
  command:
    - mcp
"""
    template = tmp_path / "server.yaml"
    template.write_text(content, encoding="utf-8")
    return template


def test_render_substitutes_commit(script_path: Path, sample_yaml: Path) -> None:
    """Rendered payload carries the given 40-hex commit."""
    release_commit = "12f877d0a1b2c3d4e5f6789012345678901234ab"

    result = subprocess.run(
        [sys.executable, str(script_path), str(sample_yaml), release_commit],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert release_commit in result.stdout
    assert "ec2c1306eba4f51ace107382dab495156e7f20e6" not in result.stdout


def test_render_rejects_malformed_commit(script_path: Path, sample_yaml: Path) -> None:
    """Malformed commit hash is reported."""
    bad_commits = [
        "12f877d0",  # too short
        "12f877d0a1b2c3d4e5f6789012345678901234AB",  # uppercase
        "not-a-commit-hash-at-all-just-some-text",
        "",
    ]

    for bad in bad_commits:
        result = subprocess.run(
            [sys.executable, str(script_path), str(sample_yaml), bad],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1, f"should reject {bad!r}"
        assert "40 lowercase hex" in result.stderr.lower() or "error" in result.stderr.lower()


def test_render_rejects_template_without_source_commit(script_path: Path, tmp_path: Path) -> None:
    """Template missing source.commit field is reported."""
    template = tmp_path / "no-commit.yaml"
    template.write_text("name: test\nimage: example.com/test\ntype: server\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script_path), str(template), "a" * 40],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "source.commit" in result.stderr.lower()


def test_render_preserves_yaml_structure(script_path: Path, sample_yaml: Path) -> None:
    """Rendering changes only the commit hash, not YAML structure."""
    original = sample_yaml.read_text(encoding="utf-8")
    release_commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    result = subprocess.run(
        [sys.executable, str(script_path), str(sample_yaml), release_commit],
        capture_output=True,
        text=True,
        check=True,
    )

    rendered = result.stdout

    # Key fields unchanged
    assert "name: bernstein" in rendered
    assert "image: ghcr.io/sipyourdrink-ltd/bernstein" in rendered
    assert "type: server" in rendered

    # Only commit line changed
    assert f"  commit: {release_commit}" in rendered
    assert "ec2c1306eba4f51ace107382dab495156e7f20e6" not in rendered

    # Line count should be identical
    assert len(rendered.splitlines()) == len(original.splitlines())
