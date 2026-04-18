"""Historical MCP usage analytics and optimization recommendations."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.protocols.mcp_registry import load_catalog_entries

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_DICT_STR_OBJ = "dict[str, object]"


@dataclass(frozen=True)
class _UsageRow:
    """Normalized historical MCP tool call extracted from the WAL."""

    timestamp: float
    server_name: str
    tool_name: str
    latency_ms: float
    error: bool


@dataclass(frozen=True)
class _ToolInventory:
    """Catalog of known tools for a specific MCP server."""

    server_name: str
    tool_names: tuple[str, ...]


@dataclass(frozen=True)
class MCPToolUsageSummary:
    """Aggregated usage for a single MCP tool."""

    server_name: str
    tool_name: str
    total_calls: int
    error_count: int
    avg_latency_ms: float
    last_used_at: float | None

    @property
    def error_rate(self) -> float:
        """Return the tool error rate."""
        if self.total_calls == 0:
            return 0.0
        return self.error_count / self.total_calls

    def to_dict(self) -> dict[str, object]:
        """Serialize the summary to JSON-compatible data."""
        return {
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "total_calls": self.total_calls,
            "error_count": self.error_count,
            "error_rate": round(self.error_rate, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "last_used_at": self.last_used_at,
        }


@dataclass(frozen=True)
class MCPServerUsageSummary:
    """Aggregated usage for one MCP server."""

    server_name: str
    installed: bool
    total_calls: int
    distinct_tools_used: int
    error_count: int
    avg_latency_ms: float
    last_used_at: float | None
    known_tools: tuple[str, ...]
    used_tools: tuple[str, ...]
    unused_tools: tuple[str, ...]

    @property
    def error_rate(self) -> float:
        """Return the server error rate."""
        if self.total_calls == 0:
            return 0.0
        return self.error_count / self.total_calls

    def to_dict(self) -> dict[str, object]:
        """Serialize the summary to JSON-compatible data."""
        return {
            "server_name": self.server_name,
            "installed": self.installed,
            "total_calls": self.total_calls,
            "distinct_tools_used": self.distinct_tools_used,
            "error_count": self.error_count,
            "error_rate": round(self.error_rate, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "last_used_at": self.last_used_at,
            "known_tools": list(self.known_tools),
            "used_tools": list(self.used_tools),
            "unused_tools": list(self.unused_tools),
        }


@dataclass(frozen=True)
class MCPUsageRecommendation:
    """One optimization recommendation derived from usage analytics."""

    kind: str
    server_name: str
    message: str

    def to_dict(self) -> dict[str, str]:
        """Serialize the recommendation to a small JSON object."""
        return {"kind": self.kind, "server_name": self.server_name, "message": self.message}


@dataclass(frozen=True)
class MCPUsageAnalyticsReport:
    """Historical MCP usage report."""

    generated_at: float
    total_calls: int
    installed_servers: tuple[str, ...]
    servers: tuple[MCPServerUsageSummary, ...]
    top_tools: tuple[MCPToolUsageSummary, ...]
    recommendations: tuple[MCPUsageRecommendation, ...]

    def to_dict(self) -> dict[str, object]:
        """Serialize the report to JSON-compatible data."""
        return {
            "generated_at": self.generated_at,
            "total_calls": self.total_calls,
            "installed_servers": list(self.installed_servers),
            "servers": [server.to_dict() for server in self.servers],
            "top_tools": [tool.to_dict() for tool in self.top_tools],
            "recommendations": [recommendation.to_dict() for recommendation in self.recommendations],
        }


def write_tool_inventory_snapshot(
    sdd_dir: Path,
    server_name: str,
    tool_rows: Sequence[dict[str, object]],
) -> Path:
    """Persist the tool inventory learned during ``bernstein mcp test``."""
    inventory_dir = sdd_dir / "mcp" / "tool_catalog"
    inventory_dir.mkdir(parents=True, exist_ok=True)
    path = inventory_dir / f"{_inventory_key(server_name)}.json"
    payload = {
        "server_name": server_name,
        "captured_at": time.time(),
        "tools": list(tool_rows),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _build_server_summary(
    server_name: str,
    top_tools: tuple[MCPToolUsageSummary, ...],
    tool_inventory: dict[str, Any],
    installed_servers: tuple[str, ...],
) -> MCPServerUsageSummary:
    """Build a usage summary for a single MCP server."""
    server_tools = [tool for tool in top_tools if tool.server_name == server_name]
    used_tools = sorted(tool.tool_name for tool in server_tools)
    inventory = tool_inventory.get(server_name)
    known_tools = sorted(inventory.tool_names) if inventory is not None else []
    total_calls = sum(tool.total_calls for tool in server_tools)
    error_count = sum(tool.error_count for tool in server_tools)
    avg_latency_ms = 0.0
    if total_calls > 0:
        avg_latency_ms = sum(tool.avg_latency_ms * tool.total_calls for tool in server_tools) / total_calls
    last_used_candidates = [tool.last_used_at for tool in server_tools if tool.last_used_at]
    last_used_at = max(last_used_candidates) if last_used_candidates else None
    unused_tools = tuple(tool for tool in known_tools if tool not in set(used_tools))

    return MCPServerUsageSummary(
        server_name=server_name,
        installed=server_name in installed_servers,
        total_calls=total_calls,
        distinct_tools_used=len(used_tools),
        error_count=error_count,
        avg_latency_ms=avg_latency_ms,
        last_used_at=last_used_at,
        known_tools=tuple(known_tools),
        used_tools=tuple(used_tools),
        unused_tools=unused_tools,
    )


def _server_recommendations(server: MCPServerUsageSummary) -> list[MCPUsageRecommendation]:
    """Generate usage recommendations for a single MCP server."""
    recs: list[MCPUsageRecommendation] = []
    if server.installed and server.total_calls == 0:
        recs.append(
            MCPUsageRecommendation(
                kind="unused_server",
                server_name=server.server_name,
                message=f"Installed server {server.server_name!r} has no recorded tool calls. Consider removing it.",
            )
        )
    if server.known_tools and server.unused_tools:
        unused_count = len(server.unused_tools)
        recs.append(
            MCPUsageRecommendation(
                kind="unused_tools",
                server_name=server.server_name,
                message=(
                    f"Server {server.server_name!r} has {unused_count} cataloged tool(s) with zero recorded usage: "
                    f"{', '.join(server.unused_tools[:5])}"
                ),
            )
        )
    if server.total_calls > 0 and server.error_rate >= 0.25:
        recs.append(
            MCPUsageRecommendation(
                kind="high_error_rate",
                server_name=server.server_name,
                message=(
                    f"Server {server.server_name!r} is erroring on {server.error_rate:.0%} of recorded calls. "
                    "Retest or sandbox it before keeping it enabled."
                ),
            )
        )
    if server.installed and not server.known_tools:
        recs.append(
            MCPUsageRecommendation(
                kind="missing_inventory",
                server_name=server.server_name,
                message=(
                    f"Server {server.server_name!r} has no tool inventory snapshot yet. "
                    f"Run `bernstein mcp test {server.server_name}` to catalog its tools."
                ),
            )
        )
    return recs


def analyze_mcp_usage(
    sdd_dir: Path,
    *,
    catalog_path: Path | None = None,
) -> MCPUsageAnalyticsReport:
    """Analyze historical MCP usage from gateway WAL files and tool inventories."""
    installed_servers = (
        tuple(sorted(entry.name for entry in load_catalog_entries(catalog_path))) if catalog_path else ()
    )
    tool_inventory = _load_tool_inventory(sdd_dir)
    usage_rows = _load_usage_rows(sdd_dir)

    tool_counts: dict[tuple[str, str], int] = {}
    tool_errors: dict[tuple[str, str], int] = {}
    tool_latency_totals: dict[tuple[str, str], float] = {}
    tool_last_used: dict[tuple[str, str], float] = {}

    for row in usage_rows:
        key = (row.server_name, row.tool_name)
        tool_counts[key] = tool_counts.get(key, 0) + 1
        tool_errors[key] = tool_errors.get(key, 0) + (1 if row.error else 0)
        tool_latency_totals[key] = tool_latency_totals.get(key, 0.0) + row.latency_ms
        tool_last_used[key] = max(tool_last_used.get(key, 0.0), row.timestamp)

    top_tools = tuple(
        sorted(
            (
                MCPToolUsageSummary(
                    server_name=server_name,
                    tool_name=tool_name,
                    total_calls=count,
                    error_count=tool_errors[(server_name, tool_name)],
                    avg_latency_ms=(tool_latency_totals[(server_name, tool_name)] / count),
                    last_used_at=tool_last_used.get((server_name, tool_name)),
                )
                for (server_name, tool_name), count in tool_counts.items()
            ),
            key=lambda item: (-item.total_calls, item.server_name, item.tool_name),
        )
    )

    all_server_names = {
        *installed_servers,
        *tool_inventory,
        *(tool.server_name for tool in top_tools),
    }
    server_summaries: list[MCPServerUsageSummary] = []
    for server_name in sorted(all_server_names):
        summary = _build_server_summary(
            server_name,
            top_tools,
            tool_inventory,
            installed_servers,
        )
        server_summaries.append(summary)

    recommendations: list[MCPUsageRecommendation] = []
    for server in server_summaries:
        recommendations.extend(_server_recommendations(server))

    return MCPUsageAnalyticsReport(
        generated_at=time.time(),
        total_calls=sum(tool.total_calls for tool in top_tools),
        installed_servers=installed_servers,
        servers=tuple(server_summaries),
        top_tools=top_tools,
        recommendations=tuple(recommendations),
    )


def _inventory_key(server_name: str) -> str:
    """Return a filesystem-safe inventory key."""
    lowered = server_name.strip().lower()
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in lowered)


def _parse_inventory_file(path: Path) -> _ToolInventory | None:
    """Parse a single tool inventory JSON file, returning None on failure."""
    try:
        raw_obj: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw_obj, dict):
        return None
    raw = cast(_CAST_DICT_STR_OBJ, raw_obj)
    server_name = str(raw.get("server_name", "")).strip()
    if not server_name:
        return None
    tools_raw_obj = raw.get("tools", [])
    if not isinstance(tools_raw_obj, list):
        return None
    tool_names: list[str] = []
    for tool in cast("list[object]", tools_raw_obj):
        if not isinstance(tool, dict):
            continue
        tool_name = str(cast(_CAST_DICT_STR_OBJ, tool).get("name", "")).strip()
        if tool_name:
            tool_names.append(tool_name)
    return _ToolInventory(server_name=server_name, tool_names=tuple(sorted(set(tool_names))))


def _load_tool_inventory(sdd_dir: Path) -> dict[str, _ToolInventory]:
    """Load tool inventory snapshots captured during MCP protocol tests."""
    inventory_dir = sdd_dir / "mcp" / "tool_catalog"
    inventories: dict[str, _ToolInventory] = {}
    if not inventory_dir.exists():
        return inventories
    for path in sorted(inventory_dir.glob("*.json")):
        inv = _parse_inventory_file(path)
        if inv is not None:
            inventories[inv.server_name] = inv
    return inventories


def _parse_wal_line(stripped: str) -> _UsageRow | None:
    """Parse a single WAL JSONL line into a _UsageRow, or None if not applicable."""
    try:
        raw_obj: object = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw_obj, dict):
        return None
    raw = cast(_CAST_DICT_STR_OBJ, raw_obj)
    if raw.get("decision_type") != "mcp_tool_call":
        return None
    inputs_obj = raw.get("inputs", {})
    output_obj = raw.get("output", {})
    if not isinstance(inputs_obj, dict) or not isinstance(output_obj, dict):
        return None
    inputs = cast(_CAST_DICT_STR_OBJ, inputs_obj)
    output = cast(_CAST_DICT_STR_OBJ, output_obj)
    tool_name = str(inputs.get("tool_name", "")).strip()
    if not tool_name:
        return None
    latency_raw = output.get("latency_ms", 0.0)
    latency_ms = float(latency_raw) if isinstance(latency_raw, (int, float)) else 0.0
    timestamp_raw = raw.get("timestamp", 0.0)
    timestamp = float(timestamp_raw) if isinstance(timestamp_raw, (int, float)) else 0.0
    return _UsageRow(
        timestamp=timestamp,
        server_name=str(inputs.get("server_name", "unknown")).strip() or "unknown",
        tool_name=tool_name,
        latency_ms=latency_ms,
        error=output.get("error") is not None,
    )


def _load_usage_rows(sdd_dir: Path) -> list[_UsageRow]:
    """Load all historical ``mcp_tool_call`` rows from gateway WAL files."""
    rows: list[_UsageRow] = []
    wal_dir = sdd_dir / "runtime" / "wal"
    if not wal_dir.exists():
        return rows

    for wal_path in sorted(wal_dir.glob("*.wal.jsonl")):
        try:
            lines = wal_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            row = _parse_wal_line(stripped)
            if row is not None:
                rows.append(row)
    return rows
