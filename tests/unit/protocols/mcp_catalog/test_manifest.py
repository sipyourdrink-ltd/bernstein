"""Strict-validation tests for the MCP catalog manifest schema."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from bernstein.core.protocols.mcp_catalog.manifest import (
    Catalog,
    CatalogValidationError,
    validate_catalog,
)


def _good_entry() -> dict[str, Any]:
    return {
        "id": "fs-readonly",
        "name": "Filesystem (read-only)",
        "description": "Expose a directory tree as read-only MCP resources.",
        "homepage": "https://example.com/fs-readonly",
        "repository": "https://example.com/fs-readonly.git",
        "install_command": ["true"],
        "version_pin": "1.0.0",
        "transports": ["stdio"],
        "verified_by_bernstein": True,
    }


def _good_catalog() -> dict[str, Any]:
    return {
        "version": 1,
        "generated_at": "2026-04-25T12:00:00Z",
        "entries": [_good_entry()],
    }


def test_valid_catalog_parses() -> None:
    catalog = validate_catalog(_good_catalog())
    assert isinstance(catalog, Catalog)
    assert len(catalog.entries) == 1
    entry = catalog.entries[0]
    assert entry.id == "fs-readonly"
    assert entry.transports == ("stdio",)
    assert entry.auto_upgrade is False
    assert entry.signature is None


def test_unknown_top_level_field_rejects() -> None:
    payload = _good_catalog()
    payload["lol_extra"] = True
    with pytest.raises(CatalogValidationError, match="unknown field"):
        validate_catalog(payload)


def test_missing_required_field_rejects() -> None:
    payload = _good_catalog()
    del payload["entries"]
    with pytest.raises(CatalogValidationError, match="missing required"):
        validate_catalog(payload)


def test_unknown_entry_field_rejects() -> None:
    payload = _good_catalog()
    payload["entries"][0]["surprise"] = "no"
    with pytest.raises(CatalogValidationError, match="unknown field"):
        validate_catalog(payload)


def test_bad_id_pattern_rejects() -> None:
    payload = _good_catalog()
    payload["entries"][0]["id"] = "Bad ID!"
    with pytest.raises(CatalogValidationError, match="does not match pattern"):
        validate_catalog(payload)


def test_unknown_transport_rejects() -> None:
    payload = _good_catalog()
    payload["entries"][0]["transports"] = ["pigeons"]
    with pytest.raises(CatalogValidationError, match="unsupported value"):
        validate_catalog(payload)


def test_duplicate_id_rejects() -> None:
    payload = _good_catalog()
    payload["entries"].append(copy.deepcopy(payload["entries"][0]))
    with pytest.raises(CatalogValidationError, match="duplicates id"):
        validate_catalog(payload)


def test_unsupported_version_rejects() -> None:
    payload = _good_catalog()
    payload["version"] = 99
    with pytest.raises(CatalogValidationError, match="unsupported catalog schema"):
        validate_catalog(payload)


def test_optional_fields_accepted() -> None:
    payload = _good_catalog()
    payload["entries"][0].update(
        {
            "auto_upgrade": True,
            "signature": "0xdeadbeef",
            "command": "node",
            "args": ["./server.js"],
            "env": {"FS_ROOT": "/tmp/x"},
        }
    )
    catalog = validate_catalog(payload)
    entry = catalog.entries[0]
    assert entry.auto_upgrade is True
    assert entry.signature == "0xdeadbeef"
    assert entry.command == "node"
    assert entry.args == ("./server.js",)
    assert entry.env == {"FS_ROOT": "/tmp/x"}


def test_install_command_must_be_list() -> None:
    payload = _good_catalog()
    payload["entries"][0]["install_command"] = "rm -rf /"
    with pytest.raises(CatalogValidationError, match="must be a list"):
        validate_catalog(payload)
