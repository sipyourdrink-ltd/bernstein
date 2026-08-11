#!/usr/bin/env python3
"""Regenerate the distribution manifests from ``pyproject.toml`` (#2369).

The MCP registry manifest (``server.json``), the plugin manifest
(``.plugin/plugin.json``), and the Agent Plugins 1.0.0 root manifests
(``plugin.json``, ``mcp.json``, #3540) and the citation metadata
(``CITATION.cff``, #3571) are release artifacts: their version
fields must track the package version, and the release workflow - not hand
edits - is their single source. This script is a deterministic projection: given the
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
import os
import re
import sys
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVER_JSON = REPO / "server.json"
PLUGIN_JSON = REPO / ".plugin" / "plugin.json"
MCP_JSON = REPO / ".mcp.json"
ROOT_PLUGIN_JSON = REPO / "plugin.json"
ROOT_MCP_JSON = REPO / "mcp.json"
CITATION_CFF = REPO / "CITATION.cff"
SKILLS_DIR = REPO / "skills"

#: Top-level ``CITATION.cff`` keys this script owns. Anchored to the start of a
#: line so they match only at the document root: ``cff-version`` is a different
#: key, and ``preferred-citation`` has its own indented block.
_CFF_VERSION = re.compile(r"^version:.*$", re.MULTILINE)
_CFF_DATE_RELEASED = re.compile(r"^date-released:.*$", re.MULTILINE)

#: Canonical schema identifiers for the Agent Plugins 1.0.0 manifests.
#: Validation runs against the copies vendored under
#: ``schemas/agent-plugins/1.0.0/`` - never fetched at load time.
PLUGIN_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"

#: plugin.json fields the 1.0.0 schema accepts (it sets
#: ``additionalProperties: false``, so host-specific keys such as
#: ``commands`` or ``skills`` paths must not leak into the root manifest).
_SPEC_PLUGIN_FIELDS = (
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
)


class ManifestValidationError(RuntimeError):
    """A rendered manifest does not conform to its vendored schema."""


def _load_vendored_schema(filename: str) -> dict[str, object]:
    path = REPO / "schemas" / "agent-plugins" / "1.0.0" / filename
    schema = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(schema, dict):  # pragma: no cover - defensive
        msg = f"vendored schema {filename} is not a JSON object"
        raise ManifestValidationError(msg)
    return schema


_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _schema_errors(instance: object, schema: dict, root: dict, path: str = "$") -> list[str]:
    """Validate *instance* against the JSON Schema subset the vendored schemas use.

    The publish workflow runs this script with a bare ``python3`` (no pip
    environment), so the ``jsonschema`` package cannot be imported here. This
    evaluator is driven by the vendored schema documents themselves and covers
    exactly the keywords they use: ``$ref`` (into ``#/$defs``), ``oneOf``,
    ``not``, ``enum``, ``const``, ``type``, ``minLength``, ``maxLength``,
    ``pattern``, ``properties``, ``required``, ``additionalProperties``,
    ``propertyNames``, and ``items``. The unit suite cross-checks the committed
    manifests against the same schema files with the real ``jsonschema``
    implementation, so the two cannot silently diverge.
    """
    errors: list[str] = []

    ref = schema.get("$ref")
    if isinstance(ref, str):
        target: object = root
        for part in ref.removeprefix("#/").split("/"):
            target = target[part]  # type: ignore[index]
        return _schema_errors(instance, target, root, path)  # type: ignore[arg-type]

    if "oneOf" in schema:
        branch_errors = [_schema_errors(instance, branch, root, path) for branch in schema["oneOf"]]
        passing = [errs for errs in branch_errors if not errs]
        if len(passing) != 1:
            best = min(branch_errors, key=len)
            detail = "; ".join(best) if best else "matches more than one alternative"
            errors.append(f"{path}: does not match exactly one allowed shape ({detail})")
        return errors

    if "not" in schema and not _schema_errors(instance, schema["not"], root, path):
        errors.append(f"{path}: value {instance!r} is disallowed here")

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']}")

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected {schema['const']!r}, got {instance!r}")

    expected_type = schema.get("type")
    if isinstance(expected_type, str):
        py_type = _TYPE_MAP[expected_type]
        bool_as_number = expected_type in ("integer", "number") and isinstance(instance, bool)
        if bool_as_number or not isinstance(instance, py_type):
            errors.append(f"{path}: expected type {expected_type}, got {type(instance).__name__}")
            return errors

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            errors.append(f"{path}: {instance!r} does not match pattern {schema['pattern']!r}")

    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}: missing required property {name!r}")
        property_names = schema.get("propertyNames")
        additional = schema.get("additionalProperties")
        for key, value in instance.items():
            key_path = f"{path}.{key}"
            if isinstance(property_names, dict):
                errors.extend(_schema_errors(key, property_names, root, key_path))
            if key in properties:
                errors.extend(_schema_errors(value, properties[key], root, key_path))
            elif additional is False:
                errors.append(f"{path}: unexpected property {key!r}")
            elif isinstance(additional, dict):
                errors.extend(_schema_errors(value, additional, root, key_path))

    if isinstance(instance, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(instance):
            errors.extend(_schema_errors(item, schema["items"], root, f"{path}[{i}]"))

    return errors


def _require_schema_valid(document: object, schema_file: str, manifest_name: str) -> None:
    """Raise :class:`ManifestValidationError` if *document* violates the vendored schema."""
    schema = _load_vendored_schema(schema_file)
    errors = _schema_errors(document, schema, schema)
    if errors:
        listing = "\n  ".join(errors)
        msg = (
            f"{manifest_name} does not conform to the vendored schemas/agent-plugins/1.0.0/{schema_file}:\n  {listing}"
        )
        raise ManifestValidationError(msg)


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


def discover_skills() -> list[str]:
    """Return the skill names at the fixed ``skills/`` discovery location.

    Mirrors the Agent Plugins 1.0.0 discovery rule: each immediate child
    directory of ``skills/`` containing a regular ``SKILL.md`` is one skill;
    deeper descendants are never searched. A child directory *without* a
    ``SKILL.md`` is a packaging error (hosts would silently ignore it), so it
    raises instead of being skipped.
    """
    skills: list[str] = []
    for child in sorted(SKILLS_DIR.iterdir()):
        if not child.is_dir():
            continue
        if not (child / "SKILL.md").is_file():
            msg = (
                f"skills/{child.name}/ has no SKILL.md; conformant hosts would "
                "silently ignore it - remove the directory or add the skill file"
            )
            raise RuntimeError(msg)
        skills.append(child.name)
    if not skills:
        msg = "skills/ contains no discoverable skill (expected skills/*/SKILL.md)"
        raise RuntimeError(msg)
    return skills


def render_root_plugin_json(version: str) -> str:
    """Return the canonical Agent Plugins 1.0.0 root ``plugin.json``.

    Metadata is projected from ``.plugin/plugin.json`` (the single editing
    surface for bundle metadata), restricted to the fields the 1.0.0 schema
    accepts, with the ``$schema`` marker and the ``pyproject.toml`` version
    stamped in. Skill discovery is directory-convention based in the spec, so
    the manifest carries no skill list - but generation still requires the
    ``skills/`` tree to be discoverable, failing fast on a broken bundle.
    """
    discover_skills()
    source = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    data: dict[str, object] = {"$schema": PLUGIN_SCHEMA_ID}
    for field in _SPEC_PLUGIN_FIELDS:
        if field in source:
            data[field] = source[field]
    data["version"] = version
    # Fields are copied from .plugin/plugin.json verbatim, so a malformed
    # source (numeric name, string keywords, ...) must fail here instead of
    # shipping a schema-invalid root manifest.
    _require_schema_valid(data, "plugin.schema.json", "plugin.json")
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_root_mcp_json() -> str:
    """Return the canonical Agent Plugins 1.0.0 root ``mcp.json``.

    Server entries are projected from ``.mcp.json`` (kept as-is for current
    host integrations) with the spec-required ``type`` field added; entries
    without an explicit type are stdio servers.
    """
    source = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    servers: dict[str, object] = {}
    for name, entry in source["mcpServers"].items():
        server = dict(entry)
        server.setdefault("type", "stdio")
        servers[name] = server
    data = {"$schema": MCP_SCHEMA_ID, "mcpServers": servers}
    # Entries are copied from .mcp.json with only the type defaulted, so a
    # malformed transport (missing command/url, unknown type, stray keys)
    # must fail here instead of being written or accepted by --check.
    _require_schema_valid(data, "mcp.schema.json", "mcp.json")
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def release_date() -> str:
    """Return the date to stamp when the recorded version changes (UTC, ISO 8601).

    ``SOURCE_DATE_EPOCH`` wins when it is set, so a reproducible-build
    environment can pin what this returns. Otherwise the current UTC date, which
    is the release date: this value is only ever read on a run that is changing
    the recorded version, and that is the release commit.
    """
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        return datetime.fromtimestamp(int(epoch), tz=UTC).date().isoformat()
    return datetime.now(tz=UTC).date().isoformat()


def render_citation_cff(version: str, current: str, *, today: str) -> str:
    """Return ``CITATION.cff`` with ``version`` and ``date-released`` stamped.

    Citation metadata drifted from the released package once already: the file
    carried 3.10.0 while 3.14.159 was on PyPI (#3571). It is a release artifact
    like the manifests above, so it is stamped by the release job rather than by
    hand.

    ``date-released`` is the one field here that ``pyproject.toml`` cannot
    supply, and a projection that reads the clock unconditionally would report
    drift every day after a release instead of on the release. So the date is
    rewritten only when the recorded version actually changes, and preserved
    verbatim otherwise. That keeps this a deterministic projection of
    ``(pyproject version, current file)``: it is idempotent, and ``--check``
    stays a drift test rather than a clock test.

    The rewrite is textual on purpose. Round-tripping this file through a YAML
    library would reflow the folded ``abstract`` block and reorder keys, turning
    a two-line release stamp into a whole-file diff.
    """
    # Substituting the first match only stamps the first match. A file with two
    # top-level ``version:`` keys would keep the stale one, and the projection
    # would then agree with itself while the artifact stayed wrong -- so a
    # document this rewrite cannot fully own is rejected rather than half-stamped.
    for label, pattern in (("version", _CFF_VERSION), ("date-released", _CFF_DATE_RELEASED)):
        occurrences = len(pattern.findall(current))
        if occurrences != 1:
            raise ManifestValidationError(
                f"CITATION.cff: expected exactly one top-level '{label}:' key, found {occurrences}"
            )
        # A key that opens a block (``version:`` followed by list items) leaves an
        # empty value on its own line. Stamping that line would produce a scalar
        # and orphan the block underneath it -- a file that is no longer valid
        # YAML but that this projection would then consider in sync.
        if not _cff_value(pattern, current):
            raise ManifestValidationError(
                f"CITATION.cff: '{label}' is not a scalar; stamping it would leave its block behind"
            )

    recorded = _cff_value(_CFF_VERSION, current)
    rendered = _CFF_VERSION.sub(f'version: "{version}"', current, count=1)
    if recorded != version:
        rendered = _CFF_DATE_RELEASED.sub(f'date-released: "{today}"', rendered, count=1)

    # Validate what was produced, not what was intended. A non-scalar key
    # (``version:`` followed by an indented block) leaves an empty value behind,
    # and a hand-edited ``date-released`` can be a date only by appearance.
    if _cff_value(_CFF_VERSION, rendered) != version:
        raise ManifestValidationError(
            f"CITATION.cff: 'version' did not stamp to {version!r}; the key is probably not a scalar"
        )
    stamped_date = _cff_value(_CFF_DATE_RELEASED, rendered)
    try:
        date.fromisoformat(stamped_date)
    except ValueError as exc:
        raise ManifestValidationError(
            f"CITATION.cff: 'date-released' is not an ISO 8601 date: {stamped_date!r}"
        ) from exc
    return rendered


def _cff_value(pattern: re.Pattern[str], text: str) -> str:
    """Return the scalar value of the ``CITATION.cff`` key *pattern* matches."""
    match = pattern.search(text)
    if match is None:  # pragma: no cover - callers check first
        return ""
    return match.group(0).split(":", 1)[1].strip().strip("\"'")


def _restore_replaced(replaced: dict[Path, str | None]) -> None:
    """Put back the bytes a failed run replaced.

    ``None`` means the file did not exist before this run, so restoring it means
    removing it again rather than writing an empty file.
    """
    for path, previous in replaced.items():
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(previous, encoding="utf-8")
        # Reporting must not be able to raise here: this runs while recovering
        # from a failure, and an exception mid-restore leaves exactly the
        # half-rewritten tree the restore exists to prevent.
        label = path.relative_to(REPO) if path.is_relative_to(REPO) else path
        print(f"restored {label}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 when a manifest differs from its regenerated form.",
    )
    args = parser.parse_args(argv)

    version = pyproject_version()
    try:
        targets = {
            SERVER_JSON: render_server_json(version),
            PLUGIN_JSON: render_plugin_json(version),
            ROOT_PLUGIN_JSON: render_root_plugin_json(version),
            ROOT_MCP_JSON: render_root_mcp_json(),
            CITATION_CFF: render_citation_cff(
                version,
                CITATION_CFF.read_text(encoding="utf-8") if CITATION_CFF.is_file() else "",
                today=release_date(),
            ),
        }
    except ManifestValidationError as exc:
        # Schema-invalid projections must fail generation and --check alike,
        # before any drift comparison, file write, or provenance step.
        print(str(exc), file=sys.stderr)
        return 1

    drift: list[str] = []
    replaced: dict[Path, str | None] = {}
    for path, rendered in targets.items():
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        if current == rendered:
            continue
        if args.check:
            drift.append(str(path.relative_to(REPO)))
        else:
            replaced[path] = current if path.is_file() else None
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
        # Provenance reads the regenerated server.json, so the write has to
        # happen before the check can run. When the check then fails, a
        # half-rewritten tree is worse than either clean outcome: the manifests
        # would disagree about which version is being released, and the next
        # run would find no drift and report itself in sync. Put back what this
        # run replaced, so the failure leaves the tree where it found it.
        _restore_replaced(replaced)
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
