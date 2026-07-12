#!/usr/bin/env python3
"""Regenerate the distribution manifests from ``pyproject.toml`` (#2369).

The MCP registry manifest (``server.json``) and the plugin manifest
(``.plugin/plugin.json``) are release artifacts: their version fields must
track the package version, and the release workflow - not hand edits - is
their single source. This script is a deterministic projection: given the
same ``pyproject.toml`` and manifest inputs it produces byte-identical
outputs, so CI can diff instead of trusting a human.

Usage::

    python scripts/gen_distribution_manifests.py            # rewrite in place
    python scripts/gen_distribution_manifests.py --check    # exit 1 on drift

``--check`` is wired into the unit suite and the publish workflow, so a
stale ``server.json`` blocks the registry publish step instead of shipping
a listing that resolves to the wrong package version.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVER_JSON = REPO / "server.json"
PLUGIN_JSON = REPO / ".plugin" / "plugin.json"


def pyproject_version() -> str:
    """Return ``project.version`` from ``pyproject.toml``."""
    with (REPO / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def render_server_json(version: str) -> str:
    """Return the canonical ``server.json`` payload for *version*."""
    data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
    data["version"] = version
    for package in data.get("packages", []):
        if package.get("registryType") == "oci":
            # The registry schema forbids a top-level version on OCI
            # packages; the version rides in the identifier tag instead
            # (e.g. ghcr.io/owner/image:1.0.0).
            package["identifier"] = f"{_oci_image_ref(package['identifier'])}:{version}"
            package.pop("version", None)
        else:
            package["version"] = version
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _oci_image_ref(identifier: str) -> str:
    """Return *identifier* without its tag, keeping any registry port."""
    ref, sep, tag = identifier.rpartition(":")
    if sep and "/" not in tag:
        return ref
    return identifier


def render_plugin_json(version: str) -> str:
    """Return the canonical ``.plugin/plugin.json`` payload for *version*."""
    data = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    data["version"] = version
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 when a manifest differs from its regenerated form.",
    )
    args = parser.parse_args(argv)

    version = pyproject_version()
    targets = {
        SERVER_JSON: render_server_json(version),
        PLUGIN_JSON: render_plugin_json(version),
    }

    drift: list[str] = []
    for path, rendered in targets.items():
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        if current == rendered:
            continue
        if args.check:
            drift.append(str(path.relative_to(REPO)))
        else:
            path.write_text(rendered, encoding="utf-8")
            print(f"regenerated {path.relative_to(REPO)} (version {version})")

    if drift:
        print(
            "distribution manifest drift detected in: "
            + ", ".join(drift)
            + "; run scripts/gen_distribution_manifests.py",
            file=sys.stderr,
        )
        return 1

    # Signed-image provenance: the MCP registry listing and the Docker MCP
    # catalog must resolve to the same canonical signed GHCR image, and the
    # listing must pin this release version. A divergence here would publish a
    # listing that pulls a different (or unsigned) image than the catalog, so
    # it fails the release like manifest drift does. Uses the current on-disk
    # server.json (already regenerated above in write mode).
    provenance = _load_image_provenance().verify_signed_image_provenance(repo_root=REPO, version=version)
    if not provenance.ok:
        print(f"signed-image provenance mismatch: {provenance.reason}", file=sys.stderr)
        return 1

    if args.check:
        print(f"distribution manifests in sync (version {version})")
    print(f"signed-image provenance OK: {provenance.image_ref}")
    return 0


def _load_image_provenance() -> object:
    """Load the stdlib-only image-provenance module by file path.

    Loading it directly (rather than importing ``bernstein.core.skills``) keeps
    this release gate runnable with a bare ``python3`` even when the bernstein
    package and its dependencies are not pip-installed in the publish job.
    """
    import importlib.util

    module_path = REPO / "src" / "bernstein" / "core" / "skills" / "image_provenance.py"
    spec = importlib.util.spec_from_file_location("_bernstein_image_provenance", module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        msg = f"cannot load image provenance module at {module_path}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    # Register before exec so the module's ``@dataclass`` definitions can resolve
    # their own annotations (dataclasses looks the class module up in sys.modules
    # under ``from __future__ import annotations``).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    raise SystemExit(main())
