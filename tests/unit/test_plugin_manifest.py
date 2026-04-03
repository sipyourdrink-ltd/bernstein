"""Tests for T776 — plugin manifest validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.plugin_manifest import (
    ManifestValidationError,
    load_plugin_manifest,
    validate_manifest,
)

# ---------------------------------------------------------------------------
# validate_manifest — happy path
# ---------------------------------------------------------------------------


class TestValidateManifestValid:
    def test_minimal_manifest(self) -> None:
        m = validate_manifest(
            {
                "name": "my-plugin",
                "version": "1.0.0",
                "description": "A test plugin",
                "entry_point": "myplugin.main",
                "hooks": ["on_task_created"],
                "required_permissions": ["read_file"],
            }
        )
        assert m.name == "my-plugin"
        assert m.version == "1.0.0"
        assert m.config_schema is None

    def test_full_manifest_with_config_schema(self) -> None:
        m = validate_manifest(
            {
                "name": "full-plugin",
                "version": "2.1.0",
                "description": "Full featured",
                "entry_point": "full_plugin.core",
                "hooks": ["on_task_created", "on_task_completed"],
                "required_permissions": ["read_file", "write_file"],
                "config_schema": {"type": "object", "properties": {"api_key": {"type": "string"}}},
            }
        )
        assert m.config_schema is not None
        assert m.config_schema["type"] == "object"


# ---------------------------------------------------------------------------
# validate_manifest — required fields
# ---------------------------------------------------------------------------


class TestValidateManifestRequiredFields:
    def test_missing_name(self) -> None:
        with pytest.raises(ManifestValidationError, match="name is required"):
            validate_manifest({"version": "1.0.0", "description": "x", "entry_point": "a.b"})

    def test_missing_version(self) -> None:
        with pytest.raises(ManifestValidationError, match="version is required"):
            validate_manifest({"name": "x", "description": "x", "entry_point": "a.b"})

    def test_missing_entry_point(self) -> None:
        with pytest.raises(ManifestValidationError, match="entry_point is required"):
            validate_manifest({"name": "x", "version": "1.0.0", "description": "x"})

    def test_missing_description(self) -> None:
        with pytest.raises(ManifestValidationError, match="description is required"):
            validate_manifest({"name": "x", "version": "1.0.0", "entry_point": "a.b"})


# ---------------------------------------------------------------------------
# Plugin name validation — anti-impersonation
# ---------------------------------------------------------------------------


class TestPluginNameAntiImpersonation:
    """Block names that look like official plugins."""

    def test_official_bernstein_prefix_blocked(self) -> None:
        with pytest.raises(ManifestValidationError, match="reserved"):
            validate_manifest(
                {
                    "name": "bernstein-security",
                    "version": "1.0.0",
                    "description": "Fake official",
                    "entry_point": "fake.core",
                }
            )

    def test_official_claude_blocked(self) -> None:
        with pytest.raises(ManifestValidationError, match="impersonate"):
            validate_manifest(
                {
                    "name": "claude-code-tools",
                    "version": "1.0.0",
                    "description": "Impersonation attempt",
                    "entry_point": "fake.core",
                }
            )

    def test_official_openai_blocked(self) -> None:
        with pytest.raises(ManifestValidationError, match="reserved"):
            validate_manifest(
                {
                    "name": "openai-helper",
                    "version": "1.0.0",
                    "description": "Fake official",
                    "entry_point": "fake.core",
                }
            )

    def test_official_anthropic_blocked(self) -> None:
        with pytest.raises(ManifestValidationError, match="reserved"):
            validate_manifest(
                {
                    "name": "anthropic-gateway",
                    "version": "1.0.0",
                    "description": "Impersonation",
                    "entry_point": "fake.core",
                }
            )

    def test_case_insensitive_anti_impersonation(self) -> None:
        with pytest.raises(ManifestValidationError):
            validate_manifest(
                {
                    "name": "BERNSTEIN-core",
                    "version": "1.0.0",
                    "description": "Fake",
                    "entry_point": "fake.core",
                }
            )

    def test_valid_community_name(self) -> None:
        m = validate_manifest(
            {
                "name": "community-linter",
                "version": "0.1.0",
                "description": "Community plugin",
                "entry_point": "com_lint.main",
            }
        )
        assert m.name == "community-linter"


# ---------------------------------------------------------------------------
# Path traversal prevention
# ---------------------------------------------------------------------------


class TestPathTraversalPrevention:
    """Reject .. and absolute paths in entry_point."""

    def test_dotdot_in_entry_point(self) -> None:
        with pytest.raises(ManifestValidationError, match="path traversal"):
            validate_manifest(
                {
                    "name": "malicious",
                    "version": "1.0.0",
                    "description": "Traversal attempt",
                    "entry_point": "../../etc/passwd",
                }
            )

    def test_absolute_path_in_entry_point(self) -> None:
        with pytest.raises(ManifestValidationError, match="must be a relative"):
            validate_manifest(
                {
                    "name": "abs-path",
                    "version": "1.0.0",
                    "description": "Absolute path attempt",
                    "entry_point": "/usr/local/lib/plugin.py",
                }
            )

    def test_file_extension_in_entry_point(self) -> None:
        with pytest.raises(ManifestValidationError, match="module path"):
            validate_manifest(
                {
                    "name": "file-ext",
                    "version": "1.0.0",
                    "description": "File path attempt",
                    "entry_point": "myplugin/main.py",
                }
            )


# ---------------------------------------------------------------------------
# Semver validation
# ---------------------------------------------------------------------------


class TestSemverValidation:
    def test_valid_semver(self) -> None:
        m = validate_manifest(
            {
                "name": "valid",
                "version": "3.14.2",
                "description": "ok",
                "entry_point": "ok.core",
            }
        )
        assert m.version == "3.14.2"

    def test_invalid_semver(self) -> None:
        with pytest.raises(ManifestValidationError, match="semver"):
            validate_manifest(
                {
                    "name": "bad-semver",
                    "version": "1.0",
                    "description": "ok",
                    "entry_point": "ok.core",
                }
            )

    def test_prerelease_semver_reJECTED(self) -> None:
        """1.0.0-beta should fail (strict semver only)."""
        with pytest.raises(ManifestValidationError, match="semver"):
            validate_manifest(
                {
                    "name": "prerelease",
                    "version": "1.0.0-beta",
                    "description": "ok",
                    "entry_point": "ok.core",
                }
            )


# ---------------------------------------------------------------------------
# Field length limits
# ---------------------------------------------------------------------------


class TestFieldLengthLimits:
    def test_name_too_long(self) -> None:
        with pytest.raises(ManifestValidationError, match="too long"):
            validate_manifest(
                {
                    "name": "a" * 2000,
                    "version": "1.0.0",
                    "description": "ok",
                    "entry_point": "ok.core",
                }
            )

    def test_description_too_long(self) -> None:
        with pytest.raises(ManifestValidationError, match="too long"):
            validate_manifest(
                {
                    "name": "my-plugin",
                    "version": "1.0.0",
                    "description": "x" * 2000,
                    "entry_point": "ok.core",
                }
            )

    def test_hooks_too_many(self) -> None:
        with pytest.raises(ManifestValidationError, match="exceeds maximum"):
            validate_manifest(
                {
                    "name": "many-hooks",
                    "version": "1.0.0",
                    "description": "ok",
                    "entry_point": "ok.core",
                    "hooks": [f"hook-{i}" for i in range(101)],
                }
            )


# ---------------------------------------------------------------------------
# Name character validation
# ---------------------------------------------------------------------------


class TestNameCharacters:
    def test_special_chars_blocked(self) -> None:
        with pytest.raises(ManifestValidationError, match="invalid"):
            validate_manifest(
                {
                    "name": "plugin@home",
                    "version": "1.0.0",
                    "description": "ok",
                    "entry_point": "ok.core",
                }
            )

    def test_slash_in_name_blocked(self) -> None:
        with pytest.raises(ManifestValidationError, match="invalid"):
            validate_manifest(
                {
                    "name": "org/plugin",
                    "version": "1.0.0",
                    "description": "ok",
                    "entry_point": "ok.core",
                }
            )

    def test_starts_with_hyphen_blocked(self) -> None:
        with pytest.raises(ManifestValidationError, match="invalid"):
            validate_manifest(
                {
                    "name": "-plugin",
                    "version": "1.0.0",
                    "description": "ok",
                    "entry_point": "ok.core",
                }
            )

    def test_valid_name_with_hyphens_underscores(self) -> None:
        m = validate_manifest(
            {
                "name": "my_cool-plugin_v2",
                "version": "1.0.0",
                "description": "ok",
                "entry_point": "ok.core",
            }
        )
        assert m.name == "my_cool-plugin_v2"


# ---------------------------------------------------------------------------
# load_plugin_manifest
# ---------------------------------------------------------------------------


class TestLoadPluginManifest:
    def test_load_from_yaml(self, tmp_path: Path) -> None:
        manifest_file = tmp_path / "plugin.yaml"
        manifest_file.write_text(
            """
name: yaml-test
version: 1.2.3
description: Loaded from YAML
entry_point: yaml_plugin.core
hooks:
  - on_task_created
  - on_task_completed
""",
            encoding="utf-8",
        )
        m = load_plugin_manifest(manifest_file)
        assert m.name == "yaml-test"
        assert len(m.hooks) == 2

    def test_load_from_json(self, tmp_path: Path) -> None:
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(
            json.dumps(
                {
                    "name": "json-test",
                    "version": "0.1.0",
                    "description": "Loaded from JSON",
                    "entry_point": "json_plugin.core",
                    "required_permissions": ["read"],
                }
            ),
            encoding="utf-8",
        )
        m = load_plugin_manifest(manifest_file)
        assert m.name == "json-test"

    def test_load_validates(self, tmp_path: Path) -> None:
        """Loading also validates (path traversal in entry_point)."""
        manifest_file = tmp_path / "manifest.json"
        manifest_file.write_text(
            json.dumps(
                {
                    "name": "bad",
                    "version": "1.0.0",
                    "description": "bad",
                    "entry_point": "../../evil",
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ManifestValidationError, match="path traversal"):
            load_plugin_manifest(manifest_file)

    def test_load_nonexistent_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_plugin_manifest(tmp_path / "nonexistent.yaml")

    def test_load_non_mapping_raises(self, tmp_path: Path) -> None:
        manifest_file = tmp_path / "manifest.yaml"
        manifest_file.write_text("- not a mapping\n", encoding="utf-8")
        with pytest.raises(ManifestValidationError, match="mapping"):
            load_plugin_manifest(manifest_file)

    def test_multiple_errors_reported(self) -> None:
        """ManifestValidationError should report all errors, not stop at first."""
        with pytest.raises(ManifestValidationError) as excinfo:
            validate_manifest({"name": "a/b", "version": "bad"})
        err = excinfo.value
        assert len(err.errors) >= 2  # at least entry_point missing + bad version
