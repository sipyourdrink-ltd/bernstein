"""Catalog manifest schema and strict validation.

The manifest schema lives at ``docs/reference/mcp-catalog-schema.json``.
This module defines the in-memory dataclasses, the validator, and the
``CatalogValidationError`` raised on any unknown field or missing
required field. Validation is strict: a single bad entry rejects the
whole fetch so callers can preserve their last-known-good cached copy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Required top-level catalog keys. Any additional keys reject the fetch.
_CATALOG_REQUIRED: frozenset[str] = frozenset({"version", "generated_at", "entries"})
_CATALOG_ALLOWED: frozenset[str] = _CATALOG_REQUIRED

# Required entry keys. Any additional keys reject the fetch.
_ENTRY_REQUIRED: frozenset[str] = frozenset(
    {
        "id",
        "name",
        "description",
        "homepage",
        "repository",
        "install_command",
        "version_pin",
        "transports",
        "verified_by_bernstein",
    }
)
_ENTRY_OPTIONAL: frozenset[str] = frozenset({"auto_upgrade", "signature", "command", "args", "env"})
_ENTRY_ALLOWED: frozenset[str] = _ENTRY_REQUIRED | _ENTRY_OPTIONAL

_ID_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_VALID_TRANSPORTS: frozenset[str] = frozenset({"stdio", "http", "sse"})

_SUPPORTED_SCHEMA_VERSION = 1


class CatalogValidationError(ValueError):
    """Raised when a fetched catalog payload fails strict validation.

    Callers should treat this as a hard failure and preserve any
    previously cached catalog instead of overwriting it.
    """


@dataclass(frozen=True)
class CatalogEntry:
    """A single installable MCP server manifest entry.

    Attributes:
        id: Stable slug used as the ``install <id>`` argument.
        name: Human-readable display name.
        description: One-paragraph summary.
        homepage: Project homepage URL.
        repository: Source repository URL.
        install_command: Argv-style command executed inside the sandbox
            for the install preview. Must NOT contain shell metacharacters.
        version_pin: Exact version string used for upgrade detection.
        transports: MCP transports the server speaks.
        verified_by_bernstein: Whether the Bernstein team reviewed this
            entry. Unverified entries surface a warning before install.
        auto_upgrade: Whether the upgrade subcommand auto-applies new
            version pins.
        signature: Optional detached signature (best-effort in v1.9).
        command: Executable Bernstein writes into the bernstein-managed
            block of the user's MCP config.
        args: Argv list passed to ``command``.
        env: Environment variables to set when launching the server.
    """

    id: str
    name: str
    description: str
    homepage: str
    repository: str
    install_command: tuple[str, ...]
    version_pin: str
    transports: tuple[str, ...]
    verified_by_bernstein: bool
    auto_upgrade: bool = False
    signature: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise back to JSON-friendly dict."""
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "homepage": self.homepage,
            "repository": self.repository,
            "install_command": list(self.install_command),
            "version_pin": self.version_pin,
            "transports": list(self.transports),
            "verified_by_bernstein": self.verified_by_bernstein,
            "auto_upgrade": self.auto_upgrade,
        }
        if self.signature is not None:
            out["signature"] = self.signature
        if self.command is not None:
            out["command"] = self.command
        if self.args:
            out["args"] = list(self.args)
        if self.env:
            out["env"] = dict(self.env)
        return out


@dataclass(frozen=True)
class Catalog:
    """A validated catalog payload.

    Attributes:
        version: Schema version. Currently ``1``.
        generated_at: ISO-8601 UTC string when the catalog was generated.
        entries: All installable MCP server entries.
    """

    version: int
    generated_at: str
    entries: tuple[CatalogEntry, ...]

    def find(self, entry_id: str) -> CatalogEntry | None:
        """Return the entry with the given ``id`` or ``None``."""
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict."""
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "entries": [entry.to_dict() for entry in self.entries],
        }


def _ensure_str(value: Any, field_name: str) -> str:
    """Validate that ``value`` is a non-empty string."""
    if not isinstance(value, str) or not value:
        raise CatalogValidationError(f"field {field_name!r} must be a non-empty string, got {type(value).__name__}")
    return value


def _ensure_bool(value: Any, field_name: str) -> bool:
    """Validate that ``value`` is a bool."""
    if not isinstance(value, bool):
        raise CatalogValidationError(f"field {field_name!r} must be a bool, got {type(value).__name__}")
    return value


def _ensure_str_list(value: Any, field_name: str, *, min_items: int = 1) -> tuple[str, ...]:
    """Validate a list of strings with a minimum length."""
    if not isinstance(value, list):
        raise CatalogValidationError(f"field {field_name!r} must be a list, got {type(value).__name__}")
    if len(value) < min_items:
        raise CatalogValidationError(f"field {field_name!r} must have at least {min_items} item(s)")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise CatalogValidationError(f"field {field_name!r}[{index}] must be a non-empty string")
        out.append(item)
    return tuple(out)


def _ensure_env(value: Any, field_name: str) -> dict[str, str]:
    """Validate an env mapping of string -> string."""
    if not isinstance(value, dict):
        raise CatalogValidationError(f"field {field_name!r} must be an object, got {type(value).__name__}")
    out: dict[str, str] = {}
    for key, val in value.items():
        if not isinstance(key, str) or not key:
            raise CatalogValidationError(f"field {field_name!r} keys must be non-empty strings")
        if not isinstance(val, str):
            raise CatalogValidationError(f"field {field_name!r}[{key!r}] must be a string")
        out[key] = val
    return out


def _validate_entry(raw: Any, index: int) -> CatalogEntry:
    """Validate a single entry dict."""
    if not isinstance(raw, dict):
        raise CatalogValidationError(f"entries[{index}] must be an object, got {type(raw).__name__}")

    keys = set(raw.keys())
    missing = _ENTRY_REQUIRED - keys
    if missing:
        raise CatalogValidationError(f"entries[{index}] missing required field(s): {sorted(missing)}")
    unknown = keys - _ENTRY_ALLOWED
    if unknown:
        raise CatalogValidationError(f"entries[{index}] has unknown field(s): {sorted(unknown)}")

    entry_id = _ensure_str(raw["id"], f"entries[{index}].id")
    if not _ID_PATTERN.match(entry_id):
        raise CatalogValidationError(f"entries[{index}].id {entry_id!r} does not match pattern {_ID_PATTERN.pattern!r}")

    install_command = _ensure_str_list(raw["install_command"], f"entries[{index}].install_command")

    transports = _ensure_str_list(raw["transports"], f"entries[{index}].transports")
    for transport in transports:
        if transport not in _VALID_TRANSPORTS:
            raise CatalogValidationError(
                f"entries[{index}].transports has unsupported value {transport!r}; valid: {sorted(_VALID_TRANSPORTS)}"
            )

    args_value = raw.get("args")
    args: tuple[str, ...] = (
        () if args_value is None else _ensure_str_list(args_value, f"entries[{index}].args", min_items=0)
    )

    env_value = raw.get("env")
    env: dict[str, str] = {}
    if env_value is not None:
        env = _ensure_env(env_value, f"entries[{index}].env")

    signature_raw = raw.get("signature")
    signature: str | None = None
    if signature_raw is not None:
        signature = _ensure_str(signature_raw, f"entries[{index}].signature")

    command_raw = raw.get("command")
    command: str | None = None
    if command_raw is not None:
        command = _ensure_str(command_raw, f"entries[{index}].command")

    auto_upgrade_raw = raw.get("auto_upgrade", False)
    auto_upgrade = _ensure_bool(auto_upgrade_raw, f"entries[{index}].auto_upgrade")

    return CatalogEntry(
        id=entry_id,
        name=_ensure_str(raw["name"], f"entries[{index}].name"),
        description=_ensure_str(raw["description"], f"entries[{index}].description"),
        homepage=_ensure_str(raw["homepage"], f"entries[{index}].homepage"),
        repository=_ensure_str(raw["repository"], f"entries[{index}].repository"),
        install_command=install_command,
        version_pin=_ensure_str(raw["version_pin"], f"entries[{index}].version_pin"),
        transports=transports,
        verified_by_bernstein=_ensure_bool(raw["verified_by_bernstein"], f"entries[{index}].verified_by_bernstein"),
        auto_upgrade=auto_upgrade,
        signature=signature,
        command=command,
        args=args,
        env=env,
    )


def validate_catalog(payload: Any) -> Catalog:
    """Validate a parsed JSON payload and return a :class:`Catalog`.

    Args:
        payload: Parsed JSON object. Lists, scalars, etc. are rejected.

    Returns:
        A :class:`Catalog` value object.

    Raises:
        CatalogValidationError: If any required field is missing, any
            unknown field is present, or any value is the wrong type.
            Callers should preserve any cached copy on this error.
    """
    if not isinstance(payload, dict):
        raise CatalogValidationError(f"top-level payload must be an object, got {type(payload).__name__}")

    keys = set(payload.keys())
    missing = _CATALOG_REQUIRED - keys
    if missing:
        raise CatalogValidationError(f"catalog missing required field(s): {sorted(missing)}")
    unknown = keys - _CATALOG_ALLOWED
    if unknown:
        raise CatalogValidationError(f"catalog has unknown field(s): {sorted(unknown)}")

    version_raw = payload["version"]
    if not isinstance(version_raw, int) or isinstance(version_raw, bool):
        raise CatalogValidationError(f"field 'version' must be an integer, got {type(version_raw).__name__}")
    if version_raw != _SUPPORTED_SCHEMA_VERSION:
        raise CatalogValidationError(
            f"unsupported catalog schema version {version_raw!r}; this client expects {_SUPPORTED_SCHEMA_VERSION}"
        )

    generated_at = _ensure_str(payload["generated_at"], "generated_at")

    entries_raw = payload["entries"]
    if not isinstance(entries_raw, list):
        raise CatalogValidationError(f"field 'entries' must be a list, got {type(entries_raw).__name__}")

    entries: list[CatalogEntry] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(entries_raw):
        entry = _validate_entry(raw, index)
        if entry.id in seen_ids:
            raise CatalogValidationError(f"entries[{index}] duplicates id {entry.id!r}")
        seen_ids.add(entry.id)
        entries.append(entry)

    return Catalog(
        version=version_raw,
        generated_at=generated_at,
        entries=tuple(entries),
    )


__all__ = [
    "Catalog",
    "CatalogEntry",
    "CatalogValidationError",
    "validate_catalog",
]
