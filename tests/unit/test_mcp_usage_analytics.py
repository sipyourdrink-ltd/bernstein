"""Tests for historical MCP usage analytics."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.mcp_registry import MCPServerEntry, save_catalog_entries
from bernstein.core.mcp_usage_analytics import analyze_mcp_usage, write_tool_inventory_snapshot
from bernstein.core.wal import WALWriter


def _make_sdd(tmp_path: Path) -> Path:
    sdd_dir = tmp_path / ".sdd"
    sdd_dir.mkdir()
    return sdd_dir


def _append_call(
    writer: WALWriter,
    *,
    server_name: str,
    tool_name: str,
    latency_ms: float,
    error: object | None = None,
) -> None:
    writer.append(
        decision_type="mcp_tool_call",
        inputs={
            "method": "tools/call",
            "server_name": server_name,
            "tool_name": tool_name,
            "arguments": {},
            "request_id": 1,
        },
        output={"result": {"ok": True}, "error": error, "latency_ms": latency_ms},
        actor="mcp_gateway",
    )


def test_analyze_mcp_usage_recommends_unused_servers_and_tools(tmp_path: Path) -> None:
    sdd_dir = _make_sdd(tmp_path)
    catalog_path = sdd_dir / "config" / "mcp_servers.yaml"
    save_catalog_entries(
        catalog_path,
        [
            MCPServerEntry(name="filesystem", package="@mcp/filesystem"),
            MCPServerEntry(name="github", package="@mcp/github"),
        ],
    )

    write_tool_inventory_snapshot(
        sdd_dir,
        "filesystem",
        [
            {"name": "read_file"},
            {"name": "write_file"},
            {"name": "delete_file"},
        ],
    )

    writer = WALWriter(run_id="usage", sdd_dir=sdd_dir)
    _append_call(writer, server_name="filesystem", tool_name="read_file", latency_ms=10.0)
    _append_call(writer, server_name="filesystem", tool_name="write_file", latency_ms=30.0)
    _append_call(
        writer,
        server_name="filesystem",
        tool_name="write_file",
        latency_ms=50.0,
        error={"code": -32603, "message": "boom"},
    )

    report = analyze_mcp_usage(sdd_dir, catalog_path=catalog_path)

    assert report.total_calls == 3
    assert [tool.tool_name for tool in report.top_tools[:2]] == ["write_file", "read_file"]

    filesystem = next(server for server in report.servers if server.server_name == "filesystem")
    github = next(server for server in report.servers if server.server_name == "github")

    assert filesystem.total_calls == 3
    assert filesystem.unused_tools == ("delete_file",)
    assert github.total_calls == 0

    recommendation_kinds = {(item.kind, item.server_name) for item in report.recommendations}
    assert ("unused_tools", "filesystem") in recommendation_kinds
    assert ("high_error_rate", "filesystem") in recommendation_kinds
    assert ("unused_server", "github") in recommendation_kinds
    assert ("missing_inventory", "github") in recommendation_kinds


def test_analyze_mcp_usage_skips_malformed_rows(tmp_path: Path) -> None:
    sdd_dir = _make_sdd(tmp_path)
    wal_dir = sdd_dir / "runtime" / "wal"
    wal_dir.mkdir(parents=True)
    wal_path = wal_dir / "broken.wal.jsonl"
    wal_path.write_text(
        "\n".join(
            [
                "not json",
                json.dumps({"decision_type": "mcp_tool_call", "inputs": [], "output": {}}),
                json.dumps(
                    {
                        "timestamp": 123.0,
                        "decision_type": "mcp_tool_call",
                        "inputs": {"server_name": "filesystem", "tool_name": "read_file"},
                        "output": {"latency_ms": 7.0, "error": None},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = analyze_mcp_usage(sdd_dir)

    assert report.total_calls == 1
    assert report.top_tools[0].server_name == "filesystem"
    assert report.top_tools[0].tool_name == "read_file"
