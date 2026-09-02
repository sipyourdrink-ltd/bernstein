"""Governance surface inventory -- discover and catalog governable surfaces.

This module provides the discovery logic that feeds ``bernstein govern inventory``.
It scans the local project configuration to build a catalog of surfaces that
can be governed: MCP tools, API endpoints (OpenAPI spec), file paths
(bernstein.yaml worktree config), databases, and any other configured surfaces.

The catalog is consumed by ``bernstein govern propose`` which hands it to a
model for drafting governance playbooks.

Example output structure::

    inventory:
      surfaces:
        - kind: mcp_tool
          identifier: bernstein-mcp-status
          metadata:
            command: bernstein
            args: [mcp]
          source_ref: .mcp.json
        - kind: api_endpoint
          identifier: /api/v1/tasks
          metadata: {}
          source_ref: openapi.yaml
        - kind: file_path
          identifier: src/bernstein/core/orchestration/
          metadata: {}
          source_ref: bernstein.yaml
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Surface:
    """A governable surface discovered in the project.

    Attributes:
        kind: Category of surface (e.g., mcp_tool, api_endpoint, file_path).
        identifier: Unique identifier within the kind namespace.
        metadata: Additional structured metadata about the surface.
        source_ref: Path or reference to the file/config that defined this surface.
    """

    kind: str
    identifier: str
    metadata: dict[str, Any] = field(default_factory=dict)
    source_ref: str = ""


@dataclass
class SurfaceInventory:
    """Container for a discovered surface inventory.

    Attributes:
        surfaces: List of discovered :class:`Surface` objects.
        inventory_hash: SHA-256 digest of the serialized surface list for audit trail.
        timestamp: ISO 8601 timestamp of when the inventory was taken.
    """

    surfaces: list[Surface] = field(default_factory=list)
    inventory_hash: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize the inventory to a JSON-compatible dict."""
        return {
            "inventory": {
                "surfaces": [asdict(s) for s in self.surfaces],
                "inventory_hash": self.inventory_hash,
                "timestamp": self.timestamp,
            }
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SurfaceInventory:
        """Deserialize a JSON dict into a SurfaceInventory."""
        inv = data.get("inventory", data)
        return cls(
            surfaces=[Surface(**s) for s in inv.get("surfaces", [])],
            inventory_hash=inv.get("inventory_hash", ""),
            timestamp=inv.get("timestamp", ""),
        )


# ---------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def _sha256_digest(content: str) -> str:
    """Return the SHA-256 hex digest of a string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _discover_mcp_tools(workspace_root: Path) -> list[Surface]:
    """Discover MCP tools from .mcp.json configuration.

    Scans the workspace for ``.mcp.json`` at the root and any nested
    worktrees, extracting the ``mcpServers`` entries.
    """
    surfaces: list[Surface] = []
    mcp_paths = [workspace_root / ".mcp.json"]

    for mcp_path in mcp_paths:
        if not mcp_path.is_file():
            continue

        try:
            raw = mcp_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            continue

        if not isinstance(data, dict):
            continue

        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict):
            continue

        for name, config in servers.items():
            if isinstance(config, dict):
                command = config.get("command", "")
                args = config.get("args", [])
                metadata = {"command": command, "args": args}
            else:
                metadata = {}

            surfaces.append(
                Surface(
                    kind="mcp_tool",
                    identifier=name,
                    metadata=metadata,
                    source_ref=str(mcp_path.relative_to(workspace_root)),
                )
            )

    return surfaces


def _discover_api_endpoints(workspace_root: Path) -> list[Surface]:
    """Discover API endpoints from OpenAPI specification files.

    Scans the workspace for ``openapi.yaml``, ``openapi.yml``, ``openapi.json``,
    ``swagger.yaml``, and ``swagger.json`` at the root, extracting route paths.
    """
    surfaces: list[Surface] = []
    spec_names = [
        "openapi.yaml",
        "openapi.yml",
        "openapi.json",
        "swagger.yaml",
        "swagger.json",
    ]

    for spec_name in spec_names:
        spec_path = workspace_root / spec_name
        if not spec_path.is_file():
            continue

        try:
            raw = spec_path.read_text(encoding="utf-8")
            spec = json.loads(raw) if spec_path.suffix == ".json" else yaml.safe_load(raw)
        except (OSError, json.JSONDecodeError, yaml.YAMLError):
            continue

        if not isinstance(spec, dict):
            continue

        servers = spec.get("servers", [])
        base_url = ""
        if isinstance(servers, list) and servers and isinstance(servers[0], dict):
            base_url = str(servers[0].get("url", ""))

        paths = spec.get("paths", {})
        if not isinstance(paths, dict):
            continue

        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method in methods:
                if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}:
                    full_path = f"{base_url.rstrip('/')}{path}" if base_url else path
                    surfaces.append(
                        Surface(
                            kind="api_endpoint",
                            identifier=f"{method.upper()} {full_path}",
                            metadata={"method": method.upper(), "path": path, "base_url": base_url},
                            source_ref=spec_name,
                        )
                    )

    return surfaces


def _discover_file_paths(workspace_root: Path) -> list[Surface]:
    """Discover configured file paths from bernstein.yaml worktree config.

    Extracts file paths from the ``worktrees`` section of ``bernstein.yaml``.
    """
    surfaces: list[Surface] = []
    config_path = workspace_root / "bernstein.yaml"

    if not config_path.is_file():
        return surfaces

    try:
        raw = config_path.read_text(encoding="utf-8")
        config = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError):
        return surfaces

    if not isinstance(config, dict):
        return surfaces

    worktrees = config.get("worktrees", [])
    if not isinstance(worktrees, list):
        worktrees = []

    for wt in worktrees:
        if not isinstance(wt, dict):
            continue
        root = wt.get("root", "")
        if root:
            surfaces.append(
                Surface(
                    kind="file_path",
                    identifier=str(Path(root)),
                    metadata={},
                    source_ref="bernstein.yaml",
                )
            )

    return surfaces


def discover_surfaces(workspace_root: Path | str | None = None) -> SurfaceInventory:
    """Discover all governable surfaces in the workspace.

    Scans the workspace for:
    - MCP tools from ``.mcp.json``
    - API endpoints from OpenAPI/Swagger specs
    - File paths from ``bernstein.yaml`` worktree config

    Args:
        workspace_root: Path to the workspace root. Defaults to the current working directory.

    Returns:
        A :class:`SurfaceInventory` containing all discovered surfaces,
        with ``inventory_hash`` computed and ``timestamp`` set to now.
    """
    if workspace_root is None:
        root: Path = Path.cwd()
    elif isinstance(workspace_root, Path):
        root = workspace_root
    else:
        root = Path(workspace_root)

    all_surfaces: list[Surface] = []

    all_surfaces.extend(_discover_mcp_tools(root))
    all_surfaces.extend(_discover_api_endpoints(root))
    all_surfaces.extend(_discover_file_paths(root))

    surfaces_data = json.dumps([asdict(s) for s in all_surfaces], sort_keys=True)
    digest = _sha256_digest(surfaces_data)
    timestamp = datetime.now(UTC).isoformat()

    return SurfaceInventory(
        surfaces=all_surfaces,
        inventory_hash=digest,
        timestamp=timestamp,
    )


__all__ = [
    "Surface",
    "SurfaceInventory",
    "discover_surfaces",
]
