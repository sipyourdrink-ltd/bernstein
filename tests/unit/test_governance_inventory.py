"""Unit tests for governance surface inventory discovery (#4973)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.governance.inventory import (
    Surface,
    SurfaceInventory,
    discover_surfaces,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A clean workspace directory for testing."""
    return tmp_path


def test_surface_dataclass_fields() -> None:
    """Surface has kind, identifier, metadata, source_ref."""
    surface = Surface(
        kind="mcp_tool",
        identifier="bernstein-mcp-status",
        metadata={"command": "bernstein", "args": ["mcp"]},
        source_ref=".mcp.json",
    )
    assert surface.kind == "mcp_tool"
    assert surface.identifier == "bernstein-mcp-status"
    assert surface.metadata == {"command": "bernstein", "args": ["mcp"]}
    assert surface.source_ref == ".mcp.json"


def test_surface_frozen() -> None:
    """Surface is immutable (frozen=True)."""
    from dataclasses import FrozenInstanceError

    surface = Surface(kind="mcp_tool", identifier="test", metadata={}, source_ref="")
    with pytest.raises(FrozenInstanceError):
        surface.kind = "changed"  # type: ignore[misc]


def test_surface_inventory_to_dict() -> None:
    """SurfaceInventory serializes to JSON-compatible dict."""
    surfaces = [
        Surface(kind="mcp_tool", identifier="tool-a", metadata={}, source_ref=".mcp.json"),
        Surface(kind="api_endpoint", identifier="GET /api/v1/tasks", metadata={}, source_ref="openapi.yaml"),
    ]
    inventory = SurfaceInventory(
        surfaces=surfaces,
        inventory_hash="abc123",
        timestamp="2024-01-01T00:00:00Z",
    )
    data = inventory.to_dict()
    assert "inventory" in data
    assert data["inventory"]["inventory_hash"] == "abc123"
    assert data["inventory"]["timestamp"] == "2024-01-01T00:00:00Z"
    assert len(data["inventory"]["surfaces"]) == 2


def test_surface_inventory_from_dict() -> None:
    """SurfaceInventory deserializes from JSON dict."""
    data = {
        "inventory": {
            "surfaces": [
                {"kind": "mcp_tool", "identifier": "tool-a", "metadata": {}, "source_ref": ".mcp.json"},
            ],
            "inventory_hash": "def456",
            "timestamp": "2024-01-02T00:00:00Z",
        }
    }
    inventory = SurfaceInventory.from_dict(data)
    assert len(inventory.surfaces) == 1
    assert inventory.surfaces[0].kind == "mcp_tool"
    assert inventory.inventory_hash == "def456"
    assert inventory.timestamp == "2024-01-02T00:00:00Z"


def test_discover_surfaces_empty_workspace(workspace: Path) -> None:
    """discover_surfaces returns empty inventory for workspace with no config."""
    inventory = discover_surfaces(workspace)
    assert len(inventory.surfaces) == 0
    assert inventory.inventory_hash != ""
    assert inventory.timestamp != ""


def test_discover_mcp_tools_from_mcp_json(workspace: Path) -> None:
    """discover_surfaces finds MCP tools in .mcp.json."""
    mcp_config = {
        "mcpServers": {
            "bernstein": {"command": "bernstein", "args": ["mcp"]},
            "other-tool": {"command": "python", "args": ["-m", "other"]},
        }
    }
    (workspace / ".mcp.json").write_text(json.dumps(mcp_config), encoding="utf-8")

    inventory = discover_surfaces(workspace)
    mcp_surfaces = [s for s in inventory.surfaces if s.kind == "mcp_tool"]
    assert len(mcp_surfaces) == 2

    identifiers = {s.identifier for s in mcp_surfaces}
    assert "bernstein" in identifiers
    assert "other-tool" in identifiers

    for s in mcp_surfaces:
        assert s.source_ref == ".mcp.json"


def test_discover_mcp_tools_handles_missing_mcp_json(workspace: Path) -> None:
    """discover_surfaces handles missing .mcp.json gracefully."""
    inventory = discover_surfaces(workspace)
    mcp_surfaces = [s for s in inventory.surfaces if s.kind == "mcp_tool"]
    assert len(mcp_surfaces) == 0


def test_discover_mcp_tools_handles_invalid_json(workspace: Path) -> None:
    """discover_surfaces handles malformed .mcp.json gracefully."""
    (workspace / ".mcp.json").write_text("not valid json", encoding="utf-8")
    inventory = discover_surfaces(workspace)
    mcp_surfaces = [s for s in inventory.surfaces if s.kind == "mcp_tool"]
    assert len(mcp_surfaces) == 0


def test_discover_api_endpoints_from_openapi_json(workspace: Path) -> None:
    """discover_surfaces finds API endpoints from openapi.json."""
    openapi_spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {
            "/api/v1/tasks": {
                "get": {"summary": "List tasks"},
                "post": {"summary": "Create task"},
            },
            "/api/v1/tasks/{id}": {
                "get": {"summary": "Get task"},
                "put": {"summary": "Update task"},
                "delete": {"summary": "Delete task"},
            },
        },
    }
    (workspace / "openapi.json").write_text(json.dumps(openapi_spec), encoding="utf-8")

    inventory = discover_surfaces(workspace)
    api_surfaces = [s for s in inventory.surfaces if s.kind == "api_endpoint"]
    assert len(api_surfaces) == 5

    identifiers = {s.identifier for s in api_surfaces}
    assert "GET /api/v1/tasks" in identifiers
    assert "POST /api/v1/tasks" in identifiers
    assert "GET /api/v1/tasks/{id}" in identifiers
    assert "PUT /api/v1/tasks/{id}" in identifiers
    assert "DELETE /api/v1/tasks/{id}" in identifiers


def test_discover_api_endpoints_from_openapi_yaml(workspace: Path) -> None:
    """discover_surfaces finds API endpoints from openapi.yaml."""
    yaml_content = """
openapi: "3.0.0"
info:
  title: Test API
  version: "1.0.0"
paths:
  /api/v1/health:
    get:
      summary: Health check
"""
    (workspace / "openapi.yaml").write_text(yaml_content, encoding="utf-8")

    inventory = discover_surfaces(workspace)
    api_surfaces = [s for s in inventory.surfaces if s.kind == "api_endpoint"]
    assert len(api_surfaces) == 1
    assert api_surfaces[0].identifier == "GET /api/v1/health"


def test_discover_api_endpoints_with_servers(workspace: Path) -> None:
    """discover_surfaces includes base URL from servers in identifier."""
    openapi_spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            "/tasks": {"get": {"summary": "List tasks"}},
        },
    }
    (workspace / "openapi.json").write_text(json.dumps(openapi_spec), encoding="utf-8")

    inventory = discover_surfaces(workspace)
    api_surfaces = [s for s in inventory.surfaces if s.kind == "api_endpoint"]
    assert len(api_surfaces) == 1
    assert api_surfaces[0].identifier == "GET https://api.example.com/tasks"
    assert api_surfaces[0].metadata["base_url"] == "https://api.example.com"


def test_discover_api_endpoints_handles_missing_spec(workspace: Path) -> None:
    """discover_surfaces handles missing OpenAPI spec gracefully."""
    inventory = discover_surfaces(workspace)
    api_surfaces = [s for s in inventory.surfaces if s.kind == "api_endpoint"]
    assert len(api_surfaces) == 0


def test_discover_file_paths_from_bernstein_yaml(workspace: Path) -> None:
    """discover_surfaces finds file paths from bernstein.yaml worktrees."""
    yaml_content = """
worktrees:
  - root: src/bernstein/core/
  - root: src/bernstein/cli/
"""
    (workspace / "bernstein.yaml").write_text(yaml_content, encoding="utf-8")

    inventory = discover_surfaces(workspace)
    file_surfaces = [s for s in inventory.surfaces if s.kind == "file_path"]
    assert len(file_surfaces) == 2

    identifiers = {s.identifier for s in file_surfaces}
    assert "src/bernstein/core" in identifiers or "src/bernstein/core/" in identifiers
    assert "src/bernstein/cli" in identifiers or "src/bernstein/cli/" in identifiers


def test_discover_file_paths_handles_missing_yaml(workspace: Path) -> None:
    """discover_surfaces handles missing bernstein.yaml gracefully."""
    inventory = discover_surfaces(workspace)
    file_surfaces = [s for s in inventory.surfaces if s.kind == "file_path"]
    assert len(file_surfaces) == 0


def test_inventory_hash_is_content_digest(workspace: Path) -> None:
    """inventory_hash is a SHA-256 digest of the surface list."""
    mcp_config = {"mcpServers": {"test": {"command": "test"}}}
    (workspace / ".mcp.json").write_text(json.dumps(mcp_config), encoding="utf-8")

    inventory = discover_surfaces(workspace)
    assert inventory.inventory_hash != ""
    assert len(inventory.inventory_hash) == 64  # SHA-256 hex digest


def test_inventory_hash_changes_with_content(workspace: Path) -> None:
    """inventory_hash changes when surfaces change."""
    mcp_config = {"mcpServers": {"tool-a": {"command": "a"}}}
    (workspace / ".mcp.json").write_text(json.dumps(mcp_config), encoding="utf-8")
    inventory1 = discover_surfaces(workspace)

    mcp_config["mcpServers"]["tool-b"] = {"command": "b"}
    (workspace / ".mcp.json").write_text(json.dumps(mcp_config), encoding="utf-8")
    inventory2 = discover_surfaces(workspace)

    assert inventory1.inventory_hash != inventory2.inventory_hash


def test_discover_surfaces_defaults_to_cwd(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """discover_surfaces uses cwd when workspace_root is None."""
    mcp_config = {"mcpServers": {"test": {"command": "test"}}}
    (workspace / ".mcp.json").write_text(json.dumps(mcp_config), encoding="utf-8")
    monkeypatch.chdir(workspace)

    inventory = discover_surfaces(None)
    assert len(inventory.surfaces) >= 1


def test_discover_surfaces_accepts_string_path(workspace: Path) -> None:
    """discover_surfaces accepts string path in addition to Path."""
    mcp_config = {"mcpServers": {"test": {"command": "test"}}}
    (workspace / ".mcp.json").write_text(json.dumps(mcp_config), encoding="utf-8")

    inventory = discover_surfaces(str(workspace))
    assert len(inventory.surfaces) >= 1


def test_inventory_roundtrip_dict(workspace: Path) -> None:
    """SurfaceInventory can roundtrip through to_dict/from_dict."""
    mcp_config = {"mcpServers": {"test": {"command": "test", "args": ["--verbose"]}}}
    (workspace / ".mcp.json").write_text(json.dumps(mcp_config), encoding="utf-8")

    inventory1 = discover_surfaces(workspace)
    data = inventory1.to_dict()
    inventory2 = SurfaceInventory.from_dict(data)

    assert len(inventory1.surfaces) == len(inventory2.surfaces)
    assert inventory1.inventory_hash == inventory2.inventory_hash
    assert inventory1.timestamp == inventory2.timestamp


def test_discover_surfaces_combines_all_sources(workspace: Path) -> None:
    """discover_surfaces combines MCP tools, API endpoints, and file paths."""
    mcp_config = {"mcpServers": {"test-tool": {"command": "test"}}}
    (workspace / ".mcp.json").write_text(json.dumps(mcp_config), encoding="utf-8")

    openapi_spec = {
        "openapi": "3.0.0",
        "info": {"title": "API", "version": "1.0"},
        "paths": {"/health": {"get": {"summary": "Health"}}},
    }
    (workspace / "openapi.json").write_text(json.dumps(openapi_spec), encoding="utf-8")

    yaml_content = "worktrees:\n  - root: src/\n"
    (workspace / "bernstein.yaml").write_text(yaml_content, encoding="utf-8")

    inventory = discover_surfaces(workspace)
    kinds = {s.kind for s in inventory.surfaces}
    assert "mcp_tool" in kinds
    assert "api_endpoint" in kinds
    assert "file_path" in kinds


# ---------------------------------------------------------------------------
# A config file the scan cannot interpret must not abort the whole scan
# ---------------------------------------------------------------------------


def test_malformed_openapi_yaml_does_not_abort_the_scan(workspace: Path) -> None:
    """An unparseable OpenAPI YAML is skipped, not raised out of discovery.

    ``bernstein.yaml`` already survived its own YAML errors while the OpenAPI
    scanner let ``yaml.YAMLError`` escape, so one broken spec in a workspace
    took down the inventory of every other source.
    """
    (workspace / ".mcp.json").write_text(json.dumps({"mcpServers": {"kept": {"command": "kept"}}}), encoding="utf-8")
    (workspace / "openapi.yaml").write_text("paths: {\n  broken", encoding="utf-8")

    inventory = discover_surfaces(workspace)

    assert [s.identifier for s in inventory.surfaces] == ["kept"]


def test_mcp_config_that_is_not_a_mapping_is_skipped(workspace: Path) -> None:
    """A ``.mcp.json`` holding a JSON list parses but has no ``mcpServers``."""
    (workspace / ".mcp.json").write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")

    inventory = discover_surfaces(workspace)

    assert inventory.surfaces == []


def test_mcp_servers_that_is_not_a_mapping_is_skipped(workspace: Path) -> None:
    """``mcpServers`` holding a list has no name/config pairs to enumerate."""
    (workspace / ".mcp.json").write_text(json.dumps({"mcpServers": []}), encoding="utf-8")

    inventory = discover_surfaces(workspace)

    assert inventory.surfaces == []


def test_openapi_paths_that_is_not_a_mapping_is_skipped(workspace: Path) -> None:
    """A spec whose ``paths`` is not a mapping yields no endpoints."""
    (workspace / "openapi.json").write_text(json.dumps({"openapi": "3.0.0", "paths": ["/health"]}), encoding="utf-8")

    inventory = discover_surfaces(workspace)

    assert inventory.surfaces == []


def test_openapi_servers_entry_without_a_url_leaves_the_path_unprefixed(workspace: Path) -> None:
    """A ``servers`` list whose first entry is not a mapping is not read as one."""
    (workspace / "openapi.json").write_text(
        json.dumps(
            {
                "openapi": "3.0.0",
                "servers": ["https://example.invalid"],
                "paths": {"/health": {"get": {}}},
            }
        ),
        encoding="utf-8",
    )

    inventory = discover_surfaces(workspace)

    assert [s.identifier for s in inventory.surfaces] == ["GET /health"]
