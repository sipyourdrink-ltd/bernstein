"""Plugin-manifest schema identity and the validator the package validates with.

``bernstein.core.skills.lifecycle`` validates plugin manifests at import time,
so whatever it validates with has to ship inside the wheel. Both names lived in
``scripts/gen_distribution_manifests.py`` until 2026-08-31, which made the
installed console script fail on startup with ``No module named 'scripts'``:
the repo's tooling directory is not part of the distribution. The dependency
runs the other way now — the repo script imports this module, the way the other
repo scripts already import the package.

Deliberately stdlib-only. The publish workflow runs the generator under a bare
``python3`` with no installed environment, so nothing here may need a
third-party package.
"""

from __future__ import annotations

import re

#: Validation runs against the copies vendored under
#: ``schemas/agent-plugins/1.0.0/`` - never fetched at load time.
PLUGIN_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"


_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def schema_errors(instance: object, schema: dict, root: dict, path: str = "$") -> list[str]:
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
        return schema_errors(instance, target, root, path)  # type: ignore[arg-type]

    if "oneOf" in schema:
        branch_errors = [schema_errors(instance, branch, root, path) for branch in schema["oneOf"]]
        passing = [errs for errs in branch_errors if not errs]
        if len(passing) != 1:
            best = min(branch_errors, key=len)
            detail = "; ".join(best) if best else "matches more than one alternative"
            errors.append(f"{path}: does not match exactly one allowed shape ({detail})")
        return errors

    if "not" in schema and not schema_errors(instance, schema["not"], root, path):
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
                errors.extend(schema_errors(key, property_names, root, key_path))
            if key in properties:
                errors.extend(schema_errors(value, properties[key], root, key_path))
            elif additional is False:
                errors.append(f"{path}: unexpected property {key!r}")
            elif isinstance(additional, dict):
                errors.extend(schema_errors(value, additional, root, key_path))

    if isinstance(instance, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(instance):
            errors.extend(schema_errors(item, schema["items"], root, f"{path}[{i}]"))

    return errors
