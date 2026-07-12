"""Signed-image provenance for the packaged distribution (#2369).

The packaged agent skill / plugin ships two release manifests that point a host
at a runnable image:

* ``server.json`` -- the MCP registry listing, whose ``oci`` package identifier
  pins ``ghcr.io/<owner>/bernstein:<version>``; and
* ``packaging/docker-mcp/server.yaml`` -- the Docker MCP catalog submission,
  whose ``image`` field names the same GHCR repository.

The image those manifests resolve to is signed at build time with a Sigstore
keyless build-provenance attestation (``actions/attest-build-provenance`` in
``publish-docker.yml``). This module makes that provenance *verifiable* as part
of the aggregate install-verification path, so a host does not have to trust the
manifests -- it can prove that:

1. both manifests name the **same** GHCR repository (no split-brain between the
   registry listing and the catalog entry),
2. the registry listing pins the **release version** (``:<version>``), so a
   pull resolves to the exact image the release built, and
3. that repository is the **canonical** signed image derived from the
   ``server.json`` ``repository`` owner -- not a look-alike.

Steps 1-3 are a deterministic, offline projection of the manifests: two
operators with the same tree recompute the same verdict. When a network and the
``gh`` CLI are available, :func:`verify_attestation` additionally runs
``gh attestation verify oci://<ref>`` to check the live Sigstore attestation --
the same command the docs document for a manual check.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: Registry every signed bernstein image lives under.
CANONICAL_REGISTRY = "ghcr.io"
#: Image name (the repository's final path segment) for the signed image.
CANONICAL_IMAGE_NAME = "bernstein"


@dataclass(frozen=True, slots=True)
class ImageReference:
    """A parsed OCI image reference (``registry/repository[:tag]``)."""

    registry: str
    repository: str
    tag: str

    @property
    def repo_ref(self) -> str:
        """``registry/repository`` without the tag."""
        return f"{self.registry}/{self.repository}"

    @property
    def full_ref(self) -> str:
        """``registry/repository:tag`` (tag omitted when empty)."""
        return f"{self.repo_ref}:{self.tag}" if self.tag else self.repo_ref


@dataclass(frozen=True, slots=True)
class ImageProvenanceResult:
    """Verdict of :func:`verify_signed_image_provenance`."""

    ok: bool
    image_ref: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "image_ref": self.image_ref, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class AttestationResult:
    """Outcome of an online ``gh attestation verify`` check."""

    verified: bool
    available: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"verified": self.verified, "available": self.available, "detail": self.detail}


def parse_image_reference(identifier: str) -> ImageReference:
    """Parse ``registry/repository[:tag]`` into an :class:`ImageReference`.

    A ``:`` is only treated as a tag separator when it appears in the final path
    segment (so a registry port like ``localhost:5000/x`` is not mistaken for a
    tag). A digest reference (``@sha256:...``) keeps the digest as the tag.
    """
    ident = identifier.strip()
    repo_with_reg, _, digest = ident.partition("@")
    if digest:
        registry, _, repository = repo_with_reg.partition("/")
        return ImageReference(registry=registry, repository=repository, tag=f"@{digest}")
    registry, _, rest = ident.partition("/")
    if "/" not in rest and not _looks_like_registry(registry):
        # No registry component (bare ``name[:tag]``); treat the whole thing as
        # the repository under an empty registry.
        registry, rest = "", ident
    repository, sep, tag = rest.rpartition(":")
    if not sep or "/" in tag:
        return ImageReference(registry=registry, repository=rest, tag="")
    return ImageReference(registry=registry, repository=repository, tag=tag)


def _looks_like_registry(candidate: str) -> bool:
    """Return whether *candidate* looks like a registry host (has a dot/port)."""
    return "." in candidate or ":" in candidate


def oci_reference_from_server_json(server_json_path: Path) -> ImageReference | None:
    """Return the OCI image reference pinned in *server_json_path*, or ``None``."""
    try:
        data = json.loads(server_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for package in data.get("packages", []):
        if isinstance(package, dict) and package.get("registryType") == "oci":
            identifier = package.get("identifier")
            if isinstance(identifier, str) and identifier:
                return parse_image_reference(identifier)
    return None


def image_from_docker_catalog(server_yaml_path: Path) -> ImageReference | None:
    """Return the ``image`` reference from a Docker MCP catalog ``server.yaml``.

    The catalog payload is small and shape-stable, so the ``image:`` line is
    read without a YAML dependency (the file may not be importable as a package
    resource in every context).
    """
    try:
        text = server_yaml_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("image:"):
            value = line.split(":", 1)[1].strip().strip("'\"")
            if value:
                return parse_image_reference(value)
    return None


def owner_from_server_json(server_json_path: Path) -> str | None:
    """Return the GitHub owner from the ``server.json`` repository URL."""
    try:
        data = json.loads(server_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    url = (data.get("repository") or {}).get("url", "")
    if not isinstance(url, str) or "github.com/" not in url:
        return None
    owner = url.split("github.com/", 1)[1].strip("/").split("/", 1)[0]
    return owner or None


def canonical_signed_image(owner: str, version: str) -> ImageReference:
    """Return the canonical signed image reference for *owner* at *version*."""
    return ImageReference(
        registry=CANONICAL_REGISTRY,
        repository=f"{owner}/{CANONICAL_IMAGE_NAME}",
        tag=version,
    )


def verify_signed_image_provenance(*, repo_root: Path, version: str) -> ImageProvenanceResult:
    """Verify the distribution manifests agree on the signed image (AC3).

    Deterministic and offline. Confirms that:

    * ``server.json`` carries an OCI package pinned to
      ``ghcr.io/<owner>/bernstein:<version>`` (owner taken from its own
      repository URL), and
    * ``packaging/docker-mcp/server.yaml`` names the same GHCR repository,

    so the registry listing and the catalog entry resolve to the identical
    signed image the release built. The returned ``image_ref`` is the verified
    reference a host would pull.
    """
    server_json = repo_root / "server.json"
    catalog_yaml = repo_root / "packaging" / "docker-mcp" / "server.yaml"

    owner = owner_from_server_json(server_json)
    if owner is None:
        return ImageProvenanceResult(ok=False, image_ref="", reason="server.json repository owner not resolvable")
    expected = canonical_signed_image(owner, version)

    oci = oci_reference_from_server_json(server_json)
    if oci is None:
        return ImageProvenanceResult(ok=False, image_ref="", reason="server.json has no OCI package identifier")
    if oci.repo_ref != expected.repo_ref:
        return ImageProvenanceResult(
            ok=False,
            image_ref=oci.full_ref,
            reason=f"server.json OCI repo {oci.repo_ref!r} is not the canonical signed image {expected.repo_ref!r}",
        )
    if oci.tag != version:
        return ImageProvenanceResult(
            ok=False,
            image_ref=oci.full_ref,
            reason=f"server.json OCI tag {oci.tag!r} does not pin the release version {version!r}",
        )

    catalog = image_from_docker_catalog(catalog_yaml)
    if catalog is None:
        return ImageProvenanceResult(
            ok=False,
            image_ref=oci.full_ref,
            reason="docker-mcp catalog server.yaml has no image reference",
        )
    if catalog.repo_ref != expected.repo_ref:
        return ImageProvenanceResult(
            ok=False,
            image_ref=oci.full_ref,
            reason=(
                f"docker catalog image {catalog.repo_ref!r} differs from the registry listing {expected.repo_ref!r}"
            ),
        )

    return ImageProvenanceResult(
        ok=True,
        image_ref=expected.full_ref,
        reason="registry listing and docker catalog agree on the canonical signed image",
    )


def verify_attestation(image_ref: str, *, owner: str, timeout_s: float = 60.0) -> AttestationResult:
    """Verify the live Sigstore build-provenance attestation for *image_ref*.

    Best-effort and online: shells out to ``gh attestation verify oci://<ref>``
    (the command documented for a manual check). When the ``gh`` CLI is absent
    or the call cannot run, ``available`` is ``False`` and the offline
    consistency verdict stands on its own. A present-but-failed verification
    returns ``verified=False`` with the tool's stderr tail as the detail.
    """
    import shutil

    if shutil.which("gh") is None:
        return AttestationResult(verified=False, available=False, detail="gh CLI not on PATH")
    try:
        completed = subprocess.run(
            [
                "gh",
                "attestation",
                "verify",
                f"oci://{image_ref}",
                "--owner",
                owner,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return AttestationResult(verified=False, available=False, detail=f"attestation check could not run: {exc}")
    if completed.returncode == 0:
        return AttestationResult(verified=True, available=True, detail="build-provenance attestation verified")
    tail = (completed.stderr or completed.stdout or "").strip().splitlines()
    return AttestationResult(
        verified=False,
        available=True,
        detail=tail[-1] if tail else f"gh attestation verify exited {completed.returncode}",
    )


__all__ = [
    "CANONICAL_IMAGE_NAME",
    "CANONICAL_REGISTRY",
    "AttestationResult",
    "ImageProvenanceResult",
    "ImageReference",
    "canonical_signed_image",
    "image_from_docker_catalog",
    "oci_reference_from_server_json",
    "owner_from_server_json",
    "parse_image_reference",
    "verify_attestation",
    "verify_signed_image_provenance",
]
