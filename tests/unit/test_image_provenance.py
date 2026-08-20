"""Signed-image provenance verification tests (#2369).

Covers the deterministic, offline consistency check that the MCP registry
listing (``server.json``) and the Docker MCP catalog entry agree on the same
canonical signed GHCR image pinned to the release version, plus the reference
parser and the graceful offline path of the attestation check.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.skills.image_provenance import (
    canonical_signed_image,
    image_from_docker_catalog,
    oci_reference_from_server_json,
    owner_from_server_json,
    parse_image_reference,
    source_from_docker_catalog,
    verify_attestation,
    verify_signed_image_provenance,
)


def _write_repo(
    root: Path,
    *,
    owner: str = "sipyourdrink-ltd",
    oci_identifier: str = "ghcr.io/sipyourdrink-ltd/bernstein:3.4.1",
    catalog_image: str | None = "ghcr.io/sipyourdrink-ltd/bernstein",
    catalog_source_project: str | None = "https://github.com/sipyourdrink-ltd/bernstein",
    catalog_source_commit: str | None = "ec2c1306eba4f51ace107382dab495156e7f20e6",
) -> None:
    server = {
        "name": f"io.github.{owner}/bernstein",
        "repository": {"url": f"https://github.com/{owner}/bernstein", "source": "github"},
        "version": "3.4.1",
        "packages": [
            {"registryType": "pypi", "identifier": "bernstein", "version": "3.4.1"},
            {"registryType": "oci", "identifier": oci_identifier},
        ],
    }
    (root / "server.json").write_text(json.dumps(server), encoding="utf-8")
    catalog_dir = root / "packaging" / "docker-mcp"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    if catalog_image is not None:
        lines = ["name: bernstein", f"image: {catalog_image}", "type: server"]
        if catalog_source_project or catalog_source_commit:
            lines.append("source:")
            if catalog_source_project:
                lines.append(f"  project: {catalog_source_project}")
            if catalog_source_commit:
                lines.append(f"  commit: {catalog_source_commit}")
        (catalog_dir / "server.yaml").write_text("\n".join(lines) + "\n", "utf-8")


# ---------------------------------------------------------------------------
# Reference parsing
# ---------------------------------------------------------------------------


def test_parse_image_reference_registry_repo_tag() -> None:
    ref = parse_image_reference("ghcr.io/sipyourdrink-ltd/bernstein:3.4.1")
    assert ref.registry == "ghcr.io"
    assert ref.repository == "sipyourdrink-ltd/bernstein"
    assert ref.tag == "3.4.1"
    assert ref.repo_ref == "ghcr.io/sipyourdrink-ltd/bernstein"
    assert ref.full_ref == "ghcr.io/sipyourdrink-ltd/bernstein:3.4.1"


def test_parse_image_reference_untagged() -> None:
    ref = parse_image_reference("ghcr.io/o/bernstein")
    assert ref.tag == ""
    assert ref.full_ref == "ghcr.io/o/bernstein"


def test_parse_image_reference_registry_port_is_not_a_tag() -> None:
    ref = parse_image_reference("localhost:5000/team/bernstein:1.0.0")
    assert ref.registry == "localhost:5000"
    assert ref.repository == "team/bernstein"
    assert ref.tag == "1.0.0"


def test_canonical_signed_image_shape() -> None:
    ref = canonical_signed_image("acme", "9.9.9")
    assert ref.full_ref == "ghcr.io/acme/bernstein:9.9.9"


# ---------------------------------------------------------------------------
# Manifest extraction
# ---------------------------------------------------------------------------


def test_oci_and_catalog_and_owner_extraction(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    assert (
        oci_reference_from_server_json(tmp_path / "server.json").full_ref == "ghcr.io/sipyourdrink-ltd/bernstein:3.4.1"
    )
    assert image_from_docker_catalog(tmp_path / "packaging" / "docker-mcp" / "server.yaml").repo_ref == (
        "ghcr.io/sipyourdrink-ltd/bernstein"
    )
    assert source_from_docker_catalog(tmp_path / "packaging" / "docker-mcp" / "server.yaml") == {
        "project": "https://github.com/sipyourdrink-ltd/bernstein",
        "commit": "ec2c1306eba4f51ace107382dab495156e7f20e6",
    }
    assert owner_from_server_json(tmp_path / "server.json") == "sipyourdrink-ltd"


# ---------------------------------------------------------------------------
# Consistency verdict
# ---------------------------------------------------------------------------


def test_verify_ok_when_listing_and_catalog_agree(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    result = verify_signed_image_provenance(repo_root=tmp_path, version="3.4.1")
    assert result.ok, result.reason
    assert result.image_ref == "ghcr.io/sipyourdrink-ltd/bernstein:3.4.1"


def test_verify_fails_when_oci_tag_does_not_pin_version(tmp_path: Path) -> None:
    _write_repo(tmp_path, oci_identifier="ghcr.io/sipyourdrink-ltd/bernstein:3.3.0")
    result = verify_signed_image_provenance(repo_root=tmp_path, version="3.4.1")
    assert not result.ok
    assert "does not pin the release version" in result.reason


def test_verify_fails_when_repo_is_not_canonical(tmp_path: Path) -> None:
    _write_repo(tmp_path, oci_identifier="ghcr.io/someone-else/bernstein:3.4.1")
    result = verify_signed_image_provenance(repo_root=tmp_path, version="3.4.1")
    assert not result.ok
    assert "not the canonical signed image" in result.reason


def test_verify_fails_when_catalog_disagrees_with_listing(tmp_path: Path) -> None:
    _write_repo(tmp_path, catalog_image="ghcr.io/other/bernstein")
    result = verify_signed_image_provenance(repo_root=tmp_path, version="3.4.1")
    assert not result.ok
    assert "docker catalog image" in result.reason


def test_verify_fails_when_catalog_source_project_disagrees(tmp_path: Path) -> None:
    _write_repo(tmp_path, catalog_source_project="https://github.com/someone-else/bernstein")
    result = verify_signed_image_provenance(repo_root=tmp_path, version="3.4.1")
    assert not result.ok
    assert "source.project" in result.reason


def test_verify_fails_when_catalog_source_commit_missing(tmp_path: Path) -> None:
    _write_repo(tmp_path, catalog_source_commit=None)
    result = verify_signed_image_provenance(repo_root=tmp_path, version="3.4.1")
    assert not result.ok
    assert "source.commit pinned" in result.reason


def test_verify_fails_when_catalog_source_commit_invalid(tmp_path: Path) -> None:
    _write_repo(tmp_path, catalog_source_commit="not-a-valid-sha")
    result = verify_signed_image_provenance(repo_root=tmp_path, version="3.4.1")
    assert not result.ok
    assert "valid commit SHA" in result.reason


def test_verify_fails_when_catalog_missing(tmp_path: Path) -> None:
    _write_repo(tmp_path, catalog_image=None)
    result = verify_signed_image_provenance(repo_root=tmp_path, version="3.4.1")
    assert not result.ok
    assert "no image reference" in result.reason


# ---------------------------------------------------------------------------
# Attestation (offline path)
# ---------------------------------------------------------------------------


def test_verify_attestation_reports_unavailable_without_gh(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    result = verify_attestation("ghcr.io/sipyourdrink-ltd/bernstein:3.4.1", owner="sipyourdrink-ltd")
    assert result.available is False
    assert result.verified is False
    assert "gh CLI not on PATH" in result.detail


def test_repo_manifests_are_provenance_consistent() -> None:
    """The real in-tree manifests agree on the canonical signed image."""
    repo_root = Path(__file__).resolve().parents[2]
    server_json = repo_root / "server.json"
    version = json.loads(server_json.read_text(encoding="utf-8"))["version"]
    result = verify_signed_image_provenance(repo_root=repo_root, version=version)
    assert result.ok, result.reason
