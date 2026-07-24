# Task-payload schema registry

**When a task payload's shape changes between versions, who checks that old and new code can still talk to each other?**

`SchemaRegistry` (`core/protocols/schema_registry.py`) is a small
in-memory registry for versioned task-payload schemas. Each
`SchemaVersion` declares its field names, field types, and which
fields are required; the registry can then compare two versions for
backward/forward compatibility, validate a payload dict against a
specific version, and best-effort migrate a payload from one version
to another.

If you only have time for one sentence: **register a `SchemaVersion`
per payload revision, then call `check_compatibility(old, new)` before
you ship a breaking change, or `validate_payload(payload, version)` to
check one payload against the schema it claims to be**. The rest of
this page covers the compatibility rules and the API.

Do not confuse this with `bernstein.mcp.input_validation.SchemaRegistry`
- an unrelated class with the same name that validates *MCP tool-call*
inputs against JSON Schema files on disk. This page is about the
`core.protocols.schema_registry` module, which is a plain Python
registry for task-payload field schemas and carries no JSON Schema
dependency.

---

## Data model

Source: `schema_registry.py`.

- `SchemaVersion(version, fields, required_fields, deprecated_fields)`
  - `fields`: `dict[str, str]` mapping field name to a type tag
    (`"str"`, `"int"`, `"float"`, `"bool"`, `"list"`, `"dict"`,
    `"None"`).
  - `required_fields`: frozenset of field names that must be present.
  - `deprecated_fields`: frozenset of field names still accepted but
    flagged as deprecated in `validate_payload` warnings.
- `SchemaRegistry` holds a `dict[int, SchemaVersion]` keyed by version
  number. `register()` raises `ValueError` on a duplicate version
  number; there is no update-in-place.

## Compatibility semantics

`check_compatibility(old_version, new_version)` returns a
`CompatibilityResult(compatible, breaking_changes, warnings)`.
`compatible` is `True` only when `breaking_changes` is empty. Rules,
in the order the code checks them:

| Change | Classified as |
|---|---|
| A field required in `old` is missing from `new.fields` entirely | Breaking |
| A field required in `new` did not exist in `old.fields` at all | Breaking |
| A field present in both versions changed its type tag | Breaking |
| An optional field present in `old` was dropped in `new` | Warning |
| A new optional field was added in `new` | Warning |
| A field is listed in `new.deprecated_fields` | Warning |

This gives you both directions in one call: "can new code read data an
old producer wrote" (backward) and "can old code read data a new
producer wrote" (forward), per the docstring's compatibility
definitions - the function does not label individual findings as
backward vs. forward, it reports the union as `breaking_changes`.

## Payload validation

`validate_payload(payload, version)` returns a
`ValidationResult(valid, errors, warnings)`:

- **Errors** (any of these makes `valid=False`): a required field is
  missing; the payload contains a field name not declared in
  `schema.fields`; a declared field's value does not match its
  declared Python type via `isinstance`.
- **Warnings**: use of a field listed in `schema.deprecated_fields`.

Type checking uses a fixed lookup table (`_TYPE_MAP`) from the type
tag string to the actual Python type - unrecognised type tags are
skipped rather than rejected, so a typo in a schema's `fields` dict
silently disables checking for that one field rather than raising.

## Migration

`migrate_payload(payload, from_version, to_version)` is best-effort,
not compatibility-checked:

- Fields absent from the target schema are dropped.
- Fields whose type tag changed between source and target are dropped
  (logged at debug level), since no type coercion is attempted.
- Everything else is copied through unchanged.
- New required fields on the target schema are **not** filled in - the
  caller must populate them after migration; `migrate_payload` will
  happily return a payload that then fails `validate_payload` against
  the target version.

## Usage

There is no CLI command for this module - it is a plain Python API.

```python
from bernstein.core.protocols.schema_registry import SchemaRegistry, SchemaVersion

registry = SchemaRegistry()
registry.register(SchemaVersion(
    version=1,
    fields={"name": "str", "priority": "int"},
    required_fields=frozenset({"name"}),
))
registry.register(SchemaVersion(
    version=2,
    fields={"name": "str", "priority": "int", "owner": "str"},
    required_fields=frozenset({"name", "owner"}),
))

compat = registry.check_compatibility(1, 2)
assert not compat.compatible          # "owner" is newly required in v2
print(compat.breaking_changes)

result = registry.validate_payload({"name": "fix bug"}, version=1)
assert result.valid

migrated = registry.migrate_payload({"name": "fix bug", "priority": 2}, from_version=1, to_version=2)
# migrated == {"name": "fix bug", "priority": 2}; "owner" is still missing
```

## Limitations

- **In-memory only.** `SchemaRegistry` instances hold no persistence
  layer; nothing is written to disk or the audit chain. A process
  restart loses every `register()` call unless the caller re-registers
  schemas at startup.
- **No wire-level enforcement.** Registering and checking schemas does
  not, by itself, gate what a running task server accepts on any
  route. It is a library for callers who want compatibility checks
  before shipping a payload-shape change, not an interceptor.
- Type checking is limited to the fixed `_TYPE_MAP` set of Python
  builtins; nested structure (e.g. the shape of a `dict` field's
  values) is not validated.

## Source

`src/bernstein/core/protocols/schema_registry.py`
