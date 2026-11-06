"""Validate emitted documents against the vendored official CycloneDX schema.

One loader for the vendored fixtures, so the AI-BOM test and the SBOM test
validate against the same bytes instead of each keeping its own copy of the
schema path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft7Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

FIXTURE_DIR = Path(__file__).parent

#: The BOM schema the encoders pin themselves to.
BOM_SCHEMA_NAME = "bom-1.7.schema.json"

#: Schemas the BOM schema references and that the emitted documents reach.
_SUPPORT_SCHEMA_NAMES = ("spdx.schema.json", "jsf-0.82.schema.json")

#: Referenced by the BOM schema, but only from the signature definitions.
#: Absent on purpose (see README.md); kept in the optional list so a checkout
#: that vendors it later is used rather than silently skipped.
_OPTIONAL_SCHEMA_NAMES = ("cryptography-defs.schema.json",)


def _load(name: str) -> dict[str, Any]:
    raw = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AssertionError(f"vendored schema {name} is not a JSON object")
    return cast("dict[str, Any]", raw)


def validate_cyclonedx(document: dict[str, Any]) -> list[str]:
    """Return the schema-validation errors for ``document``.

    Args:
        document: A decoded CycloneDX JSON document.

    Returns:
        One human-readable string per violation, sorted by location. An empty
        list means the document validates against the vendored official
        schema.
    """
    resources: list[tuple[str, Resource[Any]]] = []
    for name in (*_SUPPORT_SCHEMA_NAMES, *_OPTIONAL_SCHEMA_NAMES):
        path = FIXTURE_DIR / name
        if not path.exists():
            continue
        contents = _load(name)
        uri = str(contents.get("$id", name))
        resource = cast("Resource[Any]", Resource.from_contents(contents, default_specification=DRAFT7))
        resources.append((uri, resource))

    schema = _load(BOM_SCHEMA_NAME)
    registry = cast("Registry[Any]", Registry().with_resources(resources))
    validator = Draft7Validator(schema, registry=registry)
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    return [f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}" for error in errors]
