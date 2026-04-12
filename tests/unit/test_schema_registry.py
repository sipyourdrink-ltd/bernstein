"""Tests for the schema registry with backward/forward compatibility checks."""

from __future__ import annotations

import pytest

from bernstein.core.protocols.schema_registry import (
    CompatibilityResult,
    SchemaRegistry,
    SchemaVersion,
    ValidationResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry() -> SchemaRegistry:
    """Return an empty schema registry."""
    return SchemaRegistry()


@pytest.fixture()
def v1() -> SchemaVersion:
    """Version 1 schema: name (required), priority (optional)."""
    return SchemaVersion(
        version=1,
        fields={"name": "str", "priority": "int"},
        required_fields=frozenset({"name"}),
    )


@pytest.fixture()
def v2_compatible() -> SchemaVersion:
    """Version 2 schema: backward/forward compatible with v1.

    Adds an optional 'tags' field and deprecates 'priority'.
    """
    return SchemaVersion(
        version=2,
        fields={"name": "str", "priority": "int", "tags": "list"},
        required_fields=frozenset({"name"}),
        deprecated_fields=frozenset({"priority"}),
    )


@pytest.fixture()
def v3_breaking() -> SchemaVersion:
    """Version 3 schema: breaks backward compat by removing 'priority'
    and adding a new required 'owner' field.
    """
    return SchemaVersion(
        version=3,
        fields={"name": "str", "tags": "list", "owner": "str"},
        required_fields=frozenset({"name", "owner"}),
    )


# ---------------------------------------------------------------------------
# SchemaVersion dataclass
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_frozen(self, v1: SchemaVersion) -> None:
        with pytest.raises(AttributeError):
            v1.version = 99  # type: ignore[misc]

    def test_defaults(self) -> None:
        schema = SchemaVersion(version=1, fields={"x": "str"})
        assert schema.required_fields == frozenset()
        assert schema.deprecated_fields == frozenset()

    def test_fields_accessible(self, v1: SchemaVersion) -> None:
        assert v1.version == 1
        assert v1.fields == {"name": "str", "priority": "int"}
        assert v1.required_fields == frozenset({"name"})
        assert v1.deprecated_fields == frozenset()


# ---------------------------------------------------------------------------
# CompatibilityResult dataclass
# ---------------------------------------------------------------------------


class TestCompatibilityResult:
    def test_frozen(self) -> None:
        result = CompatibilityResult(compatible=True)
        with pytest.raises(AttributeError):
            result.compatible = False  # type: ignore[misc]

    def test_defaults(self) -> None:
        result = CompatibilityResult(compatible=True)
        assert result.breaking_changes == ()
        assert result.warnings == ()

    def test_with_details(self) -> None:
        result = CompatibilityResult(
            compatible=False,
            breaking_changes=("removed field",),
            warnings=("deprecated field",),
        )
        assert not result.compatible
        assert len(result.breaking_changes) == 1
        assert len(result.warnings) == 1


# ---------------------------------------------------------------------------
# ValidationResult dataclass
# ---------------------------------------------------------------------------


class TestValidationResult:
    def test_frozen(self) -> None:
        result = ValidationResult(valid=True)
        with pytest.raises(AttributeError):
            result.valid = False  # type: ignore[misc]

    def test_defaults(self) -> None:
        result = ValidationResult(valid=True)
        assert result.errors == ()
        assert result.warnings == ()


# ---------------------------------------------------------------------------
# SchemaRegistry.register / get / latest
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_and_get(self, registry: SchemaRegistry, v1: SchemaVersion) -> None:
        registry.register(v1)
        assert registry.get(1) is v1

    def test_duplicate_version_raises(self, registry: SchemaRegistry, v1: SchemaVersion) -> None:
        registry.register(v1)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(v1)

    def test_get_missing_raises(self, registry: SchemaRegistry) -> None:
        with pytest.raises(KeyError, match="not found"):
            registry.get(999)

    def test_latest_empty_raises(self, registry: SchemaRegistry) -> None:
        with pytest.raises(ValueError, match="No schemas registered"):
            registry.latest()

    def test_latest_returns_highest(
        self,
        registry: SchemaRegistry,
        v1: SchemaVersion,
        v2_compatible: SchemaVersion,
    ) -> None:
        registry.register(v1)
        registry.register(v2_compatible)
        assert registry.latest() is v2_compatible

    def test_latest_unordered_registration(self, registry: SchemaRegistry) -> None:
        """Registration order doesn't affect latest()."""
        s5 = SchemaVersion(version=5, fields={"x": "str"})
        s3 = SchemaVersion(version=3, fields={"x": "str"})
        registry.register(s5)
        registry.register(s3)
        assert registry.latest() is s5


# ---------------------------------------------------------------------------
# SchemaRegistry.check_compatibility
# ---------------------------------------------------------------------------


class TestCheckCompatibility:
    def test_compatible_versions(
        self,
        registry: SchemaRegistry,
        v1: SchemaVersion,
        v2_compatible: SchemaVersion,
    ) -> None:
        registry.register(v1)
        registry.register(v2_compatible)
        result = registry.check_compatibility(1, 2)
        assert result.compatible
        assert result.breaking_changes == ()

    def test_warnings_for_optional_and_deprecated(
        self,
        registry: SchemaRegistry,
        v1: SchemaVersion,
        v2_compatible: SchemaVersion,
    ) -> None:
        registry.register(v1)
        registry.register(v2_compatible)
        result = registry.check_compatibility(1, 2)
        assert any("tags" in w and "added" in w for w in result.warnings)
        assert any("deprecated" in w for w in result.warnings)

    def test_breaking_removed_required_field(self, registry: SchemaRegistry) -> None:
        old = SchemaVersion(
            version=1,
            fields={"name": "str", "priority": "int"},
            required_fields=frozenset({"name", "priority"}),
        )
        new = SchemaVersion(
            version=2,
            fields={"name": "str"},
            required_fields=frozenset({"name"}),
        )
        registry.register(old)
        registry.register(new)
        result = registry.check_compatibility(1, 2)
        assert not result.compatible
        assert any("priority" in b and "removed" in b.lower() for b in result.breaking_changes)

    def test_breaking_new_required_field(
        self,
        registry: SchemaRegistry,
        v1: SchemaVersion,
        v3_breaking: SchemaVersion,
    ) -> None:
        registry.register(v1)
        registry.register(v3_breaking)
        result = registry.check_compatibility(1, 3)
        assert not result.compatible
        assert any("owner" in b for b in result.breaking_changes)

    def test_breaking_type_change(self, registry: SchemaRegistry) -> None:
        old = SchemaVersion(
            version=1,
            fields={"count": "int"},
            required_fields=frozenset({"count"}),
        )
        new = SchemaVersion(
            version=2,
            fields={"count": "str"},
            required_fields=frozenset({"count"}),
        )
        registry.register(old)
        registry.register(new)
        result = registry.check_compatibility(1, 2)
        assert not result.compatible
        assert any("type changed" in b for b in result.breaking_changes)

    def test_identical_schemas_compatible(self, registry: SchemaRegistry) -> None:
        s1 = SchemaVersion(
            version=1,
            fields={"a": "str"},
            required_fields=frozenset({"a"}),
        )
        s2 = SchemaVersion(
            version=2,
            fields={"a": "str"},
            required_fields=frozenset({"a"}),
        )
        registry.register(s1)
        registry.register(s2)
        result = registry.check_compatibility(1, 2)
        assert result.compatible
        assert result.breaking_changes == ()
        assert result.warnings == ()

    def test_missing_version_raises(self, registry: SchemaRegistry, v1: SchemaVersion) -> None:
        registry.register(v1)
        with pytest.raises(KeyError):
            registry.check_compatibility(1, 99)

    def test_removed_optional_field_warns_but_compatible(self, registry: SchemaRegistry) -> None:
        old = SchemaVersion(
            version=1,
            fields={"a": "str", "b": "int"},
            required_fields=frozenset({"a"}),
        )
        new = SchemaVersion(
            version=2,
            fields={"a": "str"},
            required_fields=frozenset({"a"}),
        )
        registry.register(old)
        registry.register(new)
        result = registry.check_compatibility(1, 2)
        assert result.compatible
        assert any("'b'" in w and "removed" in w.lower() for w in result.warnings)


# ---------------------------------------------------------------------------
# SchemaRegistry.validate_payload
# ---------------------------------------------------------------------------


class TestValidatePayload:
    def test_valid_payload(self, registry: SchemaRegistry, v1: SchemaVersion) -> None:
        registry.register(v1)
        result = registry.validate_payload({"name": "task1", "priority": 3}, version=1)
        assert result.valid
        assert result.errors == ()

    def test_missing_required_field(self, registry: SchemaRegistry, v1: SchemaVersion) -> None:
        registry.register(v1)
        result = registry.validate_payload({"priority": 3}, version=1)
        assert not result.valid
        assert any("name" in e for e in result.errors)

    def test_unknown_field(self, registry: SchemaRegistry, v1: SchemaVersion) -> None:
        registry.register(v1)
        result = registry.validate_payload({"name": "task1", "bogus": True}, version=1)
        assert not result.valid
        assert any("bogus" in e for e in result.errors)

    def test_wrong_type(self, registry: SchemaRegistry, v1: SchemaVersion) -> None:
        registry.register(v1)
        result = registry.validate_payload({"name": "task1", "priority": "high"}, version=1)
        assert not result.valid
        assert any("priority" in e and "type" in e for e in result.errors)

    def test_deprecated_field_warning(
        self,
        registry: SchemaRegistry,
        v2_compatible: SchemaVersion,
    ) -> None:
        registry.register(v2_compatible)
        result = registry.validate_payload({"name": "task1", "priority": 1}, version=2)
        assert result.valid
        assert any("deprecated" in w for w in result.warnings)

    def test_empty_payload_missing_required(self, registry: SchemaRegistry, v1: SchemaVersion) -> None:
        registry.register(v1)
        result = registry.validate_payload({}, version=1)
        assert not result.valid

    def test_only_required_fields_valid(self, registry: SchemaRegistry, v1: SchemaVersion) -> None:
        registry.register(v1)
        result = registry.validate_payload({"name": "task1"}, version=1)
        assert result.valid

    def test_multiple_errors_reported(self, registry: SchemaRegistry, v1: SchemaVersion) -> None:
        registry.register(v1)
        result = registry.validate_payload({"priority": "wrong", "extra": 1}, version=1)
        assert not result.valid
        # Missing required 'name', wrong type 'priority', unknown 'extra'
        assert len(result.errors) >= 3


# ---------------------------------------------------------------------------
# SchemaRegistry.migrate_payload
# ---------------------------------------------------------------------------


class TestMigratePayload:
    def test_forward_migration_keeps_common_fields(
        self,
        registry: SchemaRegistry,
        v1: SchemaVersion,
        v2_compatible: SchemaVersion,
    ) -> None:
        registry.register(v1)
        registry.register(v2_compatible)
        migrated = registry.migrate_payload({"name": "task1", "priority": 5}, from_version=1, to_version=2)
        assert migrated["name"] == "task1"
        assert migrated["priority"] == 5

    def test_forward_migration_drops_removed_fields(
        self,
        registry: SchemaRegistry,
        v1: SchemaVersion,
        v3_breaking: SchemaVersion,
    ) -> None:
        registry.register(v1)
        registry.register(v3_breaking)
        migrated = registry.migrate_payload({"name": "task1", "priority": 5}, from_version=1, to_version=3)
        assert "priority" not in migrated
        assert migrated["name"] == "task1"

    def test_migration_skips_type_changed_fields(self, registry: SchemaRegistry) -> None:
        old = SchemaVersion(
            version=1,
            fields={"count": "int", "label": "str"},
            required_fields=frozenset({"count"}),
        )
        new = SchemaVersion(
            version=2,
            fields={"count": "str", "label": "str"},
            required_fields=frozenset({"count"}),
        )
        registry.register(old)
        registry.register(new)
        migrated = registry.migrate_payload({"count": 42, "label": "x"}, from_version=1, to_version=2)
        assert "count" not in migrated
        assert migrated["label"] == "x"

    def test_migration_missing_version_raises(self, registry: SchemaRegistry, v1: SchemaVersion) -> None:
        registry.register(v1)
        with pytest.raises(KeyError):
            registry.migrate_payload({"name": "x"}, from_version=1, to_version=99)

    def test_backward_migration_drops_unknown(
        self,
        registry: SchemaRegistry,
        v1: SchemaVersion,
        v2_compatible: SchemaVersion,
    ) -> None:
        registry.register(v1)
        registry.register(v2_compatible)
        migrated = registry.migrate_payload(
            {"name": "task1", "tags": ["a", "b"]},
            from_version=2,
            to_version=1,
        )
        assert "tags" not in migrated
        assert migrated["name"] == "task1"

    def test_migration_empty_payload(
        self,
        registry: SchemaRegistry,
        v1: SchemaVersion,
        v2_compatible: SchemaVersion,
    ) -> None:
        registry.register(v1)
        registry.register(v2_compatible)
        migrated = registry.migrate_payload({}, from_version=1, to_version=2)
        assert migrated == {}
