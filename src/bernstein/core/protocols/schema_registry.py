"""Schema registry for task payloads with compatibility validation.

Provides versioned schema definitions for task payloads and checks
backward/forward compatibility between schema versions. This allows
the orchestrator to safely evolve task payload formats while maintaining
interoperability with agents running older or newer protocol versions.

Compatibility semantics:
- BACKWARD: new schema can read old data (no removed required fields).
- FORWARD: old schema can read new data (no new required fields without defaults).
- Breaking: removed required field or changed field type.

Usage::

    from bernstein.core.protocols.schema_registry import (
        SchemaRegistry,
        SchemaVersion,
    )

    registry = SchemaRegistry()
    v1 = SchemaVersion(
        version=1,
        fields={"name": "str", "priority": "int"},
        required_fields=frozenset({"name"}),
    )
    registry.register(v1)

    result = registry.validate_payload({"name": "fix bug"}, version=1)
    assert result.valid
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchemaVersion:
    """A versioned schema definition for task payloads.

    Attributes:
        version: Monotonically increasing version number.
        fields: Mapping of field name to field type string (e.g. "str", "int").
        required_fields: Fields that must be present in a valid payload.
        deprecated_fields: Fields still accepted but scheduled for removal.
    """

    version: int
    fields: dict[str, str]
    required_fields: frozenset[str] = field(default_factory=lambda: frozenset[str]())
    deprecated_fields: frozenset[str] = field(default_factory=lambda: frozenset[str]())


@dataclass(frozen=True)
class CompatibilityResult:
    """Result of a schema compatibility check.

    Attributes:
        compatible: Whether the two schema versions are compatible.
        breaking_changes: Descriptions of breaking incompatibilities.
        warnings: Non-breaking but noteworthy differences.
    """

    compatible: bool
    breaking_changes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationResult:
    """Result of validating a payload against a schema version.

    Attributes:
        valid: Whether the payload conforms to the schema.
        errors: Descriptions of validation failures.
        warnings: Non-fatal validation notes (e.g. deprecated field usage).
    """

    valid: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


# Maps Python type names to their built-in type objects for validation.
_TYPE_MAP: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "None": type(None),
}


class SchemaRegistry:
    """Registry for versioned task payload schemas.

    Stores schema versions and provides compatibility checking,
    payload validation, and payload migration between versions.
    """

    def __init__(self) -> None:
        self._schemas: dict[int, SchemaVersion] = {}

    def register(self, schema: SchemaVersion) -> None:
        """Register a schema version.

        Args:
            schema: The schema version to register.

        Raises:
            ValueError: If a schema with the same version is already registered.
        """
        if schema.version in self._schemas:
            msg = f"Schema version {schema.version} is already registered"
            raise ValueError(msg)
        self._schemas[schema.version] = schema
        logger.debug("Registered schema version %d", schema.version)

    def get(self, version: int) -> SchemaVersion:
        """Get a schema by version number.

        Args:
            version: The version number to look up.

        Returns:
            The schema for the requested version.

        Raises:
            KeyError: If the version is not registered.
        """
        if version not in self._schemas:
            msg = f"Schema version {version} not found"
            raise KeyError(msg)
        return self._schemas[version]

    def latest(self) -> SchemaVersion:
        """Get the latest (highest-numbered) registered schema.

        Returns:
            The schema with the highest version number.

        Raises:
            ValueError: If no schemas are registered.
        """
        if not self._schemas:
            msg = "No schemas registered"
            raise ValueError(msg)
        max_version = max(self._schemas)
        return self._schemas[max_version]

    def check_compatibility(
        self,
        old_version: int,
        new_version: int,
    ) -> CompatibilityResult:
        """Check backward and forward compatibility between schema versions.

        Backward compatibility: the new schema can read data written by
        the old schema (no required fields were removed).

        Forward compatibility: the old schema can read data written by
        the new schema (no new required fields without defaults).

        Breaking changes:
        - A required field in the old schema was removed in the new schema.
        - A field's type was changed between versions.

        Args:
            old_version: The older schema version number.
            new_version: The newer schema version number.

        Returns:
            Compatibility result with any breaking changes and warnings.

        Raises:
            KeyError: If either version is not registered.
        """
        old = self.get(old_version)
        new = self.get(new_version)

        breaking: list[str] = []
        warnings: list[str] = []

        # --- Backward compatibility: new schema must handle old data ---
        # If a required field in the old schema was removed entirely from
        # the new schema, old data would contain a field the new schema
        # doesn't know about. More critically, if a required field in the
        # old schema is removed from new, old payloads are still valid but
        # new code may not process them correctly.
        for req_field in old.required_fields:
            if req_field not in new.fields:
                breaking.append(f"Required field '{req_field}' removed in v{new_version}")

        # --- Forward compatibility: old schema must handle new data ---
        # New required fields that didn't exist in old schema mean old
        # consumers cannot produce valid payloads for the new schema.
        for req_field in new.required_fields:
            if req_field not in old.fields:
                breaking.append(f"New required field '{req_field}' added in v{new_version} (not in v{old_version})")

        # --- Type changes are always breaking ---
        common_fields = set(old.fields) & set(new.fields)
        for f_name in sorted(common_fields):
            if old.fields[f_name] != new.fields[f_name]:
                breaking.append(f"Field '{f_name}' type changed from '{old.fields[f_name]}' to '{new.fields[f_name]}'")

        # --- Warnings for non-breaking changes ---
        removed_optional = (set(old.fields) - set(new.fields)) - old.required_fields
        for f_name in sorted(removed_optional):
            warnings.append(f"Optional field '{f_name}' removed in v{new_version}")

        new_optional = (set(new.fields) - set(old.fields)) - new.required_fields
        for f_name in sorted(new_optional):
            warnings.append(f"Optional field '{f_name}' added in v{new_version}")

        for f_name in sorted(new.deprecated_fields):
            if f_name in new.fields:
                warnings.append(f"Field '{f_name}' is deprecated in v{new_version}")

        compatible = len(breaking) == 0
        return CompatibilityResult(
            compatible=compatible,
            breaking_changes=tuple(breaking),
            warnings=tuple(warnings),
        )

    def validate_payload(
        self,
        payload: dict[str, object],
        version: int,
    ) -> ValidationResult:
        """Validate a payload against a schema version.

        Checks that all required fields are present, that no unknown fields
        are included, and that field values match the declared types.

        Args:
            payload: The payload dict to validate.
            version: The schema version to validate against.

        Returns:
            Validation result with errors and warnings.

        Raises:
            KeyError: If the version is not registered.
        """
        schema = self.get(version)
        errors: list[str] = []
        warnings: list[str] = []

        self._check_required_fields(schema, payload, errors)
        self._check_unknown_and_types(schema, payload, errors)
        self._check_deprecated(schema, payload, warnings)

        valid = len(errors) == 0
        return ValidationResult(
            valid=valid,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _check_required_fields(schema: SchemaVersion, payload: dict[str, object], errors: list[str]) -> None:
        """Check that all required fields are present."""
        for req in sorted(schema.required_fields):
            if req not in payload:
                errors.append(f"Missing required field '{req}'")

    @staticmethod
    def _check_unknown_and_types(schema: SchemaVersion, payload: dict[str, object], errors: list[str]) -> None:
        """Check for unknown fields and validate types of known fields."""
        for key in sorted(payload):
            if key not in schema.fields:
                errors.append(f"Unknown field '{key}'")
                continue
            expected_type_str = schema.fields[key]
            expected_type = _TYPE_MAP.get(expected_type_str)
            if expected_type is None:
                continue
            value = payload[key]
            if not isinstance(value, expected_type):
                actual_type = type(value).__name__
                errors.append(f"Field '{key}' expected type '{expected_type_str}', got '{actual_type}'")

    @staticmethod
    def _check_deprecated(schema: SchemaVersion, payload: dict[str, object], warnings: list[str]) -> None:
        """Warn on deprecated field usage."""
        for key in sorted(payload):
            if key in schema.deprecated_fields:
                warnings.append(f"Field '{key}' is deprecated")

    def migrate_payload(
        self,
        payload: dict[str, object],
        from_version: int,
        to_version: int,
    ) -> dict[str, object]:
        """Migrate a payload from one schema version to another.

        Applies a best-effort migration:
        - Keeps fields that exist in both versions (with compatible types).
        - Drops fields removed in the target version.
        - Leaves new required fields absent (caller must fill them).

        Args:
            payload: The source payload.
            from_version: The source schema version.
            to_version: The target schema version.

        Returns:
            A new dict containing the migrated payload.

        Raises:
            KeyError: If either version is not registered.
        """
        source = self.get(from_version)
        target = self.get(to_version)

        result: dict[str, object] = {}

        for key, value in payload.items():
            # Skip fields not in the target schema.
            if key not in target.fields:
                logger.debug(
                    "Dropping field '%s' during migration v%d -> v%d",
                    key,
                    from_version,
                    to_version,
                )
                continue

            # Skip fields whose types changed (can't safely migrate).
            if key in source.fields and key in target.fields and source.fields[key] != target.fields[key]:
                logger.debug(
                    "Skipping field '%s' due to type change (%s -> %s)",
                    key,
                    source.fields[key],
                    target.fields[key],
                )
                continue

            result[key] = value

        return result
