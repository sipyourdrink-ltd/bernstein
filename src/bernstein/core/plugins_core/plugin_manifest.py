"""Plugin manifest validation — strict schema with anti-impersonation and path traversal prevention (T776).

Mirrors Claude Code's Zod-style plugin manifest validation from
``utils/plugins/schemas.ts``.

Usage:
    >>> manifest = load_plugin_manifest(Path("plugin.yaml"))
    >>> validate_manifest(manifest)  # raises on invalid
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Plugin manifest schema
# ---------------------------------------------------------------------------

#: Regex for valid plugin names — alphanumeric, hyphens, underscores.
#: Blocks special characters, slashes, and lookalike impersonation (e.g. "anthropic-").
_PLUGIN_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

#: Official prefixes that cannot be used in plugin names to prevent impersonation.
_OFFICIAL_PREFIXES = (
    "bernstein-",
    "anthropic-",
    "openai-",
    "claude-code-",
)

#: Max length for any single string field in the manifest.
_MAX_STRING_LENGTH = 1024

#: Max number of hooks/tools/skills a plugin can declare.
_MAX_DECLARATIONS = 100


@dataclass(frozen=True)
class PluginManifest:
    """Validated plugin manifest.

    Attributes:
        name: Plugin name (unique, validated).
        version: Semantic version string (``1.0.0``).
        description: Human-readable description.
        entry_point: Python module or path to the plugin's main module.
        hooks: List of hook names the plugin implements.
        required_permissions: Permissions the plugin requires.
        config_schema: Optional JSON schema for plugin configuration.
    """

    name: str
    version: str
    description: str
    entry_point: str
    hooks: list[str]
    required_permissions: list[str]
    config_schema: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# ValidationError
# ---------------------------------------------------------------------------


class ManifestValidationError(Exception):
    """Raised when a plugin manifest fails validation.

    Attributes:
        errors: List of individual validation error messages.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(
            f"Plugin manifest validation failed ({len(errors)} error(s)):\n" + "\n".join(f"  - {e}" for e in errors),
        )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_plugin_name(name: str) -> list[str]:
    """Validate plugin name format and block impersonation."""
    errors: list[str] = []
    if not name:
        errors.append("Plugin name is required")
        return errors

    if len(name) > _MAX_STRING_LENGTH:
        errors.append(f"Plugin name too long ({len(name)} > {_MAX_STRING_LENGTH} chars)")

    if not _PLUGIN_NAME_RE.match(name):
        errors.append(
            f"Plugin name '{name}' contains invalid characters. "
            "Only alphanumeric, hyphens, and underscores are allowed."
        )

    # Anti-impersonation: block names that look like official plugins
    name_lower = name.lower()
    for prefix in _OFFICIAL_PREFIXES:
        if name_lower.startswith(prefix):
            errors.append(f"Plugin name '{name}' is reserved — cannot impersonate official '{prefix}' plugins.")

    return errors


def _validate_semver(version: str) -> list[str]:
    """Validate semantic versioning format (``MAJOR.MINOR.PATCH``)."""
    errors: list[str] = []
    if not version:
        errors.append("Plugin version is required")
        return errors

    semver_re = re.compile(r"^\d+\.\d+\.\d+$")
    if not semver_re.match(version):
        errors.append(f"Plugin version '{version}' must follow semver (e.g. '1.0.0').")

    return errors


def _validate_no_path_traversal(value: str, field_name: str) -> list[str]:
    """Reject ``..`` path traversal patterns."""
    errors: list[str] = []
    if ".." in value:
        errors.append(f"{field_name} contains path traversal ('..'): '{value}'")
    # Block absolute paths in entry points
    if value.startswith("/"):
        errors.append(f"{field_name} must be a relative Python module path, not absolute: '{value}'")
    return errors


def _validate_entry_point(path: str) -> list[str]:
    """Validate entry point is a safe, relative Python module path."""
    errors = _validate_no_path_traversal(path, "entry_point")

    # Must look like a Python module (no slashes at start/end, no file extension)
    if path.endswith((".py", ".pyc")):
        errors.append(
            f"entry_point should be a module path, not a file: '{path}'. "
            "Use dots to separate packages (e.g. 'mypackage.plugin')."
        )

    if "/" in path or "\\" in path:
        errors.append(f"entry_point uses dot-separated module paths, not file paths: '{path}'")

    return errors


def _validate_string_list(
    items: list[Any],
    field_name: str,
    max_items: int = _MAX_DECLARATIONS,
) -> list[str]:
    """Validate a list of strings."""
    errors: list[str] = []

    if len(items) > max_items:
        errors.append(f"{field_name} exceeds maximum ({len(items)} > {max_items})")

    for i, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{field_name}[{i}] must be a non-empty string")
        elif len(item) > _MAX_STRING_LENGTH:
            errors.append(f"{field_name}[{i}] exceeds maximum length ({len(item)} > {_MAX_STRING_LENGTH})")

    return errors


# ---------------------------------------------------------------------------
# Manifest loading and validation
# ---------------------------------------------------------------------------


def validate_manifest(manifest: dict[str, Any]) -> PluginManifest:
    """Validate a raw manifest dict and return a PluginManifest object.

    Args:
        manifest: Raw manifest dictionary (from YAML or JSON).

    Returns:
        Validated PluginManifest instance.

    Raises:
        ManifestValidationError: When one or more validation errors are found.
    """
    errors: list[str] = []

    # Required fields — cast to str for type safety (values come from Any dict)
    name: str = str(manifest.get("name", ""))
    version: str = str(manifest.get("version", ""))
    description: str = str(manifest.get("description", ""))
    entry_point: str = str(manifest.get("entry_point", ""))
    hooks_raw: Any = manifest.get("hooks", [])
    permissions_raw: Any = manifest.get("required_permissions", [])

    if not name:
        errors.append("Plugin name is required")
    if not version:
        errors.append("Plugin version is required")
    if not entry_point:
        errors.append("Plugin entry_point is required")
    if not description:
        errors.append("Plugin description is required")

    # Field-level validation
    errors.extend(_validate_plugin_name(name))
    errors.extend(_validate_semver(version))
    if len(description) > _MAX_STRING_LENGTH:
        errors.append(f"Description too long ({len(description)} > {_MAX_STRING_LENGTH} chars)")
    errors.extend(_validate_entry_point(entry_point))

    # List fields
    hooks_list: list[Any] = cast("list[Any]", hooks_raw) if isinstance(hooks_raw, list) else []
    perms_list: list[Any] = cast("list[Any]", permissions_raw) if isinstance(permissions_raw, list) else []
    errors.extend(_validate_string_list(hooks_list, "hooks"))
    errors.extend(_validate_string_list(perms_list, "required_permissions"))

    # Config schema (optional)
    config_schema: dict[str, Any] | None = None
    if "config_schema" in manifest:
        cs: Any = manifest["config_schema"]
        if not isinstance(cs, dict):
            errors.append("config_schema must be a mapping")
        else:
            config_schema = cast("dict[str, Any]", cs)

    if errors:
        raise ManifestValidationError(errors)

    return PluginManifest(
        name=name,
        version=version,
        description=description,
        entry_point=entry_point,
        hooks=[str(h) for h in hooks_list],
        required_permissions=[str(p) for p in perms_list],
        config_schema=config_schema,
    )


def load_plugin_manifest(path: Path) -> PluginManifest:
    """Load and validate a plugin manifest from a YAML or JSON file.

    Args:
        path: Path to the manifest file (``plugin.yaml``, ``manifest.yaml``,
            etc.).

    Returns:
        Validated PluginManifest.

    Raises:
        ManifestValidationError: On validation failure.
        FileNotFoundError: If the manifest file does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"Plugin manifest not found: {path}")

    try:
        import yaml

        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except ImportError:
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(raw, dict):
        raise ManifestValidationError([f"Expected manifest to be a mapping in {path}"])

    return validate_manifest(cast("dict[str, Any]", raw))
