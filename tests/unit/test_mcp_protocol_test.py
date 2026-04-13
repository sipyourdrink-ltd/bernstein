"""Tests for MCP protocol validation helpers."""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

from bernstein.core.mcp_protocol_test import resolve_catalog_server, run_protocol_test, validate_tool_contracts
from bernstein.core.mcp_registry import MCPServerEntry, save_catalog_entries
from mcp.types import Tool


def _write_stdio_fixture_server(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            from mcp.server.fastmcp import FastMCP

            mcp = FastMCP("fixture")

            @mcp.tool()
            def ping() -> str:
                return "pong"

            @mcp.tool()
            def echo(message: str) -> str:
                return message

            if __name__ == "__main__":
                mcp.run("stdio")
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


class TestValidateToolContracts:
    def test_reports_invalid_schema_and_duplicate_names(self) -> None:
        tools = [
            Tool(name="dup", inputSchema={"type": "object", "properties": {}}),
            Tool(name="dup", inputSchema={"type": "definitely-not-a-real-jsonschema-type"}),
        ]

        reports, failures = validate_tool_contracts(tools)

        assert len(reports) == 2
        assert any("Duplicate tool name" in failure for failure in failures)
        assert any("invalid input schema" in failure for failure in failures)


class TestRunProtocolTest:
    def test_runs_against_real_stdio_server(self, tmp_path: Path) -> None:
        server_script = tmp_path / "fixture_server.py"
        _write_stdio_fixture_server(server_script)

        entry = MCPServerEntry(
            name="fixture",
            package="fixture-package",
            command=sys.executable,
            args=(str(server_script),),
        )

        report = asyncio.run(run_protocol_test(entry, cwd=tmp_path))

        assert report.passed is True
        assert report.tool_count == 2
        assert report.unknown_tool_rejected is True
        assert report.invalid_arguments_rejected is True
        assert report.empty_arguments_supported is True
        assert report.failures == ()

    def test_resolve_catalog_server_matches_case_insensitively(self, tmp_path: Path) -> None:
        catalog_path = tmp_path / ".sdd" / "config" / "mcp_servers.yaml"
        save_catalog_entries(
            catalog_path,
            [
                MCPServerEntry(
                    name="Fixture",
                    package="fixture-package",
                    command="python",
                    args=("fixture.py",),
                )
            ],
        )

        entry = resolve_catalog_server("fixture", catalog_path)

        assert entry is not None
        assert entry.name == "Fixture"
