"""MCP tool normalization layer (MCP-003).

Normalizes tool names to snake_case, validates parameter schemas against
JSON Schema draft-07 subset, and wraps all tool errors in a consistent
:class:`McpToolError` format.

Usage::

    from bernstein.core.protocols.mcp_tool_normalization import (
        normalize_tool_name,
        validate_tool_params,
        McpToolError,
        ToolNormalizer,
    )

    normalizer = ToolNormalizer()
    normalizer.register_tool("myServer.SearchIssues", schema={...})
    normalized = normalizer.normalize_call("myServer.SearchIssues", {"query": "bug"})
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, cast

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# McpToolError — consistent error wrapper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class McpToolError:
    """Consistent error format for MCP tool failures.

    Attributes:
        tool_name: Normalized tool name that failed.
        original_name: Original tool name before normalization.
        code: Machine-readable error code.
        message: Human-readable error description.
        details: Optional additional context.
    """

    tool_name: str
    original_name: str
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        result: dict[str, Any] = {
            "tool_name": self.tool_name,
            "original_name": self.original_name,
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            result["details"] = self.details
        return result


class McpToolException(Exception):
    """Exception wrapping an :class:`McpToolError` for raise/catch patterns."""

    def __init__(self, error: McpToolError) -> None:
        self.error = error
        super().__init__(error.message)


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

# Pattern that matches boundaries for splitting into snake_case
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_tool_name(name: str) -> str:
    """Normalize a tool name to snake_case.

    Handles camelCase, PascalCase, kebab-case, dot.separated, and
    slash/separated names.

    Examples::

        >>> normalize_tool_name("searchIssues")
        'search_issues'
        >>> normalize_tool_name("myServer.SearchIssues")
        'my_server_search_issues'
        >>> normalize_tool_name("get-user-profile")
        'get_user_profile'
        >>> normalize_tool_name("already_snake_case")
        'already_snake_case'

    Args:
        name: Original tool name in any casing convention.

    Returns:
        snake_case normalized name.
    """
    if not name:
        return name

    # Insert underscores at camelCase boundaries
    result = _CAMEL_BOUNDARY.sub("_", name)
    # Replace non-alphanumeric sequences with underscore
    result = _NON_ALNUM.sub("_", result.lower())
    # Strip leading/trailing underscores
    result = result.strip("_")
    return result


# ---------------------------------------------------------------------------
# Schema validation (JSON Schema draft-07 subset)
# ---------------------------------------------------------------------------

# Supported JSON Schema types
_VALID_TYPES = frozenset({"string", "number", "integer", "boolean", "array", "object", "null"})


@dataclass(frozen=True)
class SchemaValidationError:
    """One validation failure in a parameter schema.

    Attributes:
        path: JSON pointer path to the failing element (e.g. ``"/properties/query"``).
        message: Description of the validation failure.
    """

    path: str
    message: str


def validate_tool_schema(schema: dict[str, Any]) -> list[SchemaValidationError]:
    """Validate a tool's parameter schema against a JSON Schema subset.

    Checks:
    - ``type`` is a recognized JSON Schema type
    - ``properties`` values are dicts with a ``type`` field
    - ``required`` entries reference existing properties
    - ``items`` (for arrays) is a dict with a ``type`` field

    Args:
        schema: The tool's ``inputSchema`` or parameter schema dict.

    Returns:
        List of validation errors (empty if schema is valid).
    """
    errors: list[SchemaValidationError] = []

    # Top-level type check
    schema_type = schema.get("type")
    if schema_type is not None and schema_type not in _VALID_TYPES:
        errors.append(SchemaValidationError(path="/type", message=f"Unknown type: {schema_type!r}"))

    # Properties check
    properties: Any = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            errors.append(SchemaValidationError(path="/properties", message="'properties' must be an object"))
        else:
            props_dict: dict[str, Any] = cast("dict[str, Any]", properties)
            for prop_name_raw, prop_schema_raw in props_dict.items():
                prop_name: str = str(prop_name_raw)
                prop_path = f"/properties/{prop_name}"
                if not isinstance(prop_schema_raw, dict):
                    errors.append(SchemaValidationError(path=prop_path, message="Property schema must be an object"))
                    continue
                prop_dict: dict[str, Any] = cast("dict[str, Any]", prop_schema_raw)
                prop_type: str | None = cast("str | None", prop_dict.get("type"))
                if prop_type is not None and prop_type not in _VALID_TYPES:
                    errors.append(
                        SchemaValidationError(
                            path=f"{prop_path}/type",
                            message=f"Unknown type: {prop_type!r}",
                        )
                    )

    # Required check
    required: Any = schema.get("required")
    if required is not None:
        if not isinstance(required, list):
            errors.append(SchemaValidationError(path="/required", message="'required' must be an array"))
        elif properties is not None and isinstance(properties, dict):
            req_list: list[str] = cast("list[str]", required)
            for req_name in req_list:
                if req_name not in properties:
                    errors.append(
                        SchemaValidationError(
                            path="/required",
                            message=f"Required property {req_name!r} not found in properties",
                        )
                    )

    # Items check (for array types)
    items: Any = schema.get("items")
    if items is not None:
        if not isinstance(items, dict):
            errors.append(SchemaValidationError(path="/items", message="'items' must be an object"))
        else:
            items_dict: dict[str, Any] = cast("dict[str, Any]", items)
            items_type: str | None = cast("str | None", items_dict.get("type"))
            if items_type is not None and items_type not in _VALID_TYPES:
                errors.append(
                    SchemaValidationError(
                        path="/items/type",
                        message=f"Unknown type: {items_type!r}",
                    )
                )

    return errors


def validate_tool_params(params: dict[str, Any], schema: dict[str, Any]) -> list[SchemaValidationError]:
    """Validate tool call parameters against a schema.

    Checks:
    - All required parameters are present.
    - Parameter types match schema types (basic type check).

    Args:
        params: Actual parameters from the tool call.
        schema: The tool's parameter schema.

    Returns:
        List of validation errors (empty if valid).
    """
    errors: list[SchemaValidationError] = []

    # Check required parameters
    required_raw: Any = schema.get("required", [])
    if isinstance(required_raw, list):
        req_list: list[str] = cast("list[str]", required_raw)
        for req_name in req_list:
            if req_name not in params:
                errors.append(
                    SchemaValidationError(
                        path=f"/params/{req_name}",
                        message=f"Missing required parameter: {req_name!r}",
                    )
                )

    # Check types of provided parameters
    properties_raw: Any = schema.get("properties", {})
    if isinstance(properties_raw, dict):
        props_dict: dict[str, Any] = cast("dict[str, Any]", properties_raw)
        for param_name, param_value in params.items():
            if param_name not in props_dict:
                continue
            prop_schema_raw: Any = props_dict[param_name]
            if not isinstance(prop_schema_raw, dict):
                continue
            prop_schema: dict[str, Any] = cast("dict[str, Any]", prop_schema_raw)
            expected_type: str | None = cast("str | None", prop_schema.get("type"))
            if expected_type is not None and not _type_matches(param_value, expected_type):
                errors.append(
                    SchemaValidationError(
                        path=f"/params/{param_name}",
                        message=(f"Type mismatch: expected {expected_type!r}, got {type(param_value).__name__!r}"),
                    )
                )

    return errors


def _type_matches(value: Any, expected: str) -> bool:
    """Check if a Python value matches a JSON Schema type string."""
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, int | float)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return True  # Unknown type -> pass


# ---------------------------------------------------------------------------
# ToolNormalizer — registry-based normalization layer
# ---------------------------------------------------------------------------


@dataclass
class _ToolEntry:
    """Internal record for a registered tool."""

    original_name: str
    normalized_name: str
    server_name: str
    schema: dict[str, Any]


class ToolNormalizer:
    """Registry that normalizes tool names and validates parameters.

    Maintains a mapping from original tool names to their normalized
    snake_case equivalents and optional parameter schemas.  Provides
    :meth:`normalize_call` to validate + normalize in one step.
    """

    def __init__(self) -> None:
        self._by_original: dict[str, _ToolEntry] = {}
        self._by_normalized: dict[str, _ToolEntry] = {}

    def register_tool(
        self,
        name: str,
        *,
        server_name: str = "",
        schema: dict[str, Any] | None = None,
    ) -> str:
        """Register a tool and return its normalized name.

        Args:
            name: Original tool name.
            server_name: Name of the MCP server providing this tool.
            schema: Optional JSON Schema for the tool's parameters.

        Returns:
            The snake_case normalized name.
        """
        normalized = normalize_tool_name(name)
        entry = _ToolEntry(
            original_name=name,
            normalized_name=normalized,
            server_name=server_name,
            schema=schema if schema is not None else {},
        )
        self._by_original[name] = entry
        self._by_normalized[normalized] = entry
        return normalized

    def get_normalized_name(self, original_name: str) -> str | None:
        """Look up the normalized name for an original tool name.

        Args:
            original_name: The original tool name.

        Returns:
            Normalized name, or None if not registered.
        """
        entry = self._by_original.get(original_name)
        return entry.normalized_name if entry is not None else None

    def get_original_name(self, normalized_name: str) -> str | None:
        """Look up the original name for a normalized tool name.

        Args:
            normalized_name: The normalized tool name.

        Returns:
            Original name, or None if not registered.
        """
        entry = self._by_normalized.get(normalized_name)
        return entry.original_name if entry is not None else None

    def normalize_call(
        self,
        tool_name: str,
        params: dict[str, Any],
    ) -> tuple[str, dict[str, Any], list[McpToolError]]:
        """Normalize a tool call: resolve name, validate params.

        Args:
            tool_name: Original or normalized tool name.
            params: Tool call parameters.

        Returns:
            Tuple of (normalized_name, params, errors).  Errors is empty
            on success.
        """
        errors: list[McpToolError] = []

        # Resolve the entry -- try original name first, then normalized
        entry = self._by_original.get(tool_name)
        if entry is None:
            entry = self._by_normalized.get(tool_name)
        if entry is None:
            # Not registered -- normalize the name but skip validation
            normalized = normalize_tool_name(tool_name)
            return normalized, params, errors

        normalized = entry.normalized_name
        original = entry.original_name

        # Validate params against schema if available
        if entry.schema:
            validation_errors = validate_tool_params(params, entry.schema)
            for verr in validation_errors:
                errors.append(
                    McpToolError(
                        tool_name=normalized,
                        original_name=original,
                        code="PARAM_VALIDATION_FAILED",
                        message=verr.message,
                        details={"path": verr.path},
                    )
                )

        return normalized, params, errors

    @property
    def tool_count(self) -> int:
        """Number of registered tools."""
        return len(self._by_original)

    def list_tools(self) -> list[dict[str, str]]:
        """Return a list of all registered tools with both names."""
        return [
            {
                "original": entry.original_name,
                "normalized": entry.normalized_name,
                "server": entry.server_name,
            }
            for entry in self._by_original.values()
        ]
