"""MCP protocol validation helpers for ``bernstein mcp test``.

Runs a bounded protocol smoke suite against a stdio MCP server:
tool discovery, JSON Schema validation, error-path handling, and
empty-argument edge cases.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, cast

from jsonschema import Draft202012Validator
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import McpError

from bernstein.core.mcp_registry import MCPServerEntry, load_catalog_entries

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mcp.types import CallToolResult, Tool

_UNKNOWN_TOOL_NAME = "__bernstein_protocol_test_missing__"


@dataclass(frozen=True)
class ToolValidationReport:
    """Schema validation outcome for a single MCP tool."""

    name: str
    required_arguments: tuple[str, ...]
    input_schema_valid: bool
    output_schema_valid: bool


@dataclass(frozen=True)
class MCPProtocolTestResult:
    """Aggregate result for one protocol validation run."""

    server_name: str
    transport: str
    tool_reports: tuple[ToolValidationReport, ...]
    unknown_tool_rejected: bool
    invalid_arguments_rejected: bool | None
    empty_arguments_supported: bool | None
    failures: tuple[str, ...]
    warnings: tuple[str, ...]
    duration_seconds: float

    @property
    def passed(self) -> bool:
        """Return True when the suite found no protocol failures."""
        return not self.failures

    @property
    def tool_count(self) -> int:
        """Return the number of validated tools."""
        return len(self.tool_reports)

    def to_dict(self) -> dict[str, object]:
        """Serialize the report to JSON-friendly primitives."""
        return {
            "server_name": self.server_name,
            "transport": self.transport,
            "tool_count": self.tool_count,
            "tool_reports": [asdict(report) for report in self.tool_reports],
            "unknown_tool_rejected": self.unknown_tool_rejected,
            "invalid_arguments_rejected": self.invalid_arguments_rejected,
            "empty_arguments_supported": self.empty_arguments_supported,
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "duration_seconds": self.duration_seconds,
            "passed": self.passed,
        }


def resolve_catalog_server(server_name: str, catalog_path: Path) -> MCPServerEntry | None:
    """Return a catalog entry by case-insensitive name."""
    normalized = server_name.strip().lower()
    for entry in load_catalog_entries(catalog_path):
        if entry.name.lower() == normalized or entry.namespaced_name.lower() == normalized:
            return entry
    return None


def validate_tool_contracts(tools: Sequence[Tool]) -> tuple[tuple[ToolValidationReport, ...], tuple[str, ...]]:
    """Validate tool names and schemas returned by ``tools/list``."""
    failures: list[str] = []
    reports: list[ToolValidationReport] = []
    seen_names: set[str] = set()

    if not tools:
        failures.append("Server returned no tools.")
        return (), tuple(failures)

    for tool in tools:
        if not tool.name.strip():
            failures.append("Encountered tool with an empty name.")
        elif tool.name in seen_names:
            failures.append(f"Duplicate tool name returned by server: {tool.name}")
        seen_names.add(tool.name)

        required_arguments = _required_arguments(tool.inputSchema)
        input_schema_error = _schema_error(tool.inputSchema)
        output_schema_error = _schema_error(tool.outputSchema) if tool.outputSchema is not None else None

        if input_schema_error is not None:
            failures.append(f"{tool.name}: invalid input schema: {input_schema_error}")
        if output_schema_error is not None:
            failures.append(f"{tool.name}: invalid output schema: {output_schema_error}")

        reports.append(
            ToolValidationReport(
                name=tool.name,
                required_arguments=required_arguments,
                input_schema_valid=input_schema_error is None,
                output_schema_valid=output_schema_error is None,
            )
        )

    return tuple(reports), tuple(failures)


async def run_protocol_test(
    entry: MCPServerEntry,
    *,
    cwd: Path | None = None,
) -> MCPProtocolTestResult:
    """Run a bounded MCP protocol test suite against one stdio server."""
    started_at = time.monotonic()
    failures: list[str] = []
    warnings: list[str] = []

    try:
        async with (
            stdio_client(_build_stdio_parameters(entry, cwd=cwd)) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            listed_tools = (await session.list_tools()).tools

            tool_reports, schema_failures = validate_tool_contracts(listed_tools)
            failures.extend(schema_failures)

            unknown_tool_rejected, unknown_detail = await _call_rejected(session, _UNKNOWN_TOOL_NAME, {})
            if not unknown_tool_rejected:
                failures.append("Server accepted an unknown tool call instead of rejecting it.")
            elif unknown_detail:
                warnings.append(f"Unknown tool rejection detail: {unknown_detail}")

            invalid_arguments_rejected: bool | None = None
            required_tool = _first_tool_with_required_arguments(listed_tools)
            if required_tool is None:
                warnings.append("No tool with required arguments was available for invalid-arguments validation.")
            else:
                invalid_arguments_rejected, invalid_detail = await _call_rejected(session, required_tool.name, {})
                if not invalid_arguments_rejected:
                    failures.append(
                        f"Tool {required_tool.name!r} accepted an empty argument object despite required fields."
                    )
                elif invalid_detail:
                    warnings.append(f"Invalid-argument rejection detail for {required_tool.name}: {invalid_detail}")

            empty_arguments_supported: bool | None = None
            empty_args_tool = _first_tool_without_required_arguments(listed_tools)
            if empty_args_tool is None:
                warnings.append("No zero-required-argument tool was available for empty-argument validation.")
            else:
                empty_arguments_supported, empty_detail = await _call_succeeds(session, empty_args_tool.name, {})
                if not empty_arguments_supported:
                    detail = f": {empty_detail}" if empty_detail else ""
                    failures.append(
                        f"Tool {empty_args_tool.name!r} rejected empty args despite no required fields{detail}"
                    )

            return MCPProtocolTestResult(
                server_name=entry.name,
                transport="stdio",
                tool_reports=tool_reports,
                unknown_tool_rejected=unknown_tool_rejected,
                invalid_arguments_rejected=invalid_arguments_rejected,
                empty_arguments_supported=empty_arguments_supported,
                failures=tuple(failures),
                warnings=tuple(warnings),
                duration_seconds=time.monotonic() - started_at,
            )
    except Exception as exc:
        failures.append(f"Protocol session failed before validation completed: {exc}")
        return MCPProtocolTestResult(
            server_name=entry.name,
            transport="stdio",
            tool_reports=(),
            unknown_tool_rejected=False,
            invalid_arguments_rejected=None,
            empty_arguments_supported=None,
            failures=tuple(failures),
            warnings=tuple(warnings),
            duration_seconds=time.monotonic() - started_at,
        )


def _build_stdio_parameters(entry: MCPServerEntry, *, cwd: Path | None) -> StdioServerParameters:
    """Create stdio spawn parameters from a catalog entry."""
    args = list(entry.args) if entry.args is not None else ["-y", entry.package]
    env = {name: value for name in entry.env_required if (value := os.environ.get(name))}
    return StdioServerParameters(
        command=entry.command,
        args=args,
        env=env or None,
        cwd=str(cwd) if cwd is not None else None,
    )


def _schema_error(schema: dict[str, Any] | None) -> str | None:
    """Return a human-readable schema error, if any."""
    if schema is None:
        return None
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        return str(exc)
    return None


def _required_arguments(schema: dict[str, Any]) -> tuple[str, ...]:
    """Return the required argument names from a JSON Schema object."""
    raw_required = schema.get("required")
    if not isinstance(raw_required, list):
        return ()
    required_names: list[str] = []
    for item in cast("list[object]", raw_required):
        if isinstance(item, str) and item.strip():
            required_names.append(item)
    return tuple(required_names)


def _first_tool_with_required_arguments(tools: Sequence[Tool]) -> Tool | None:
    """Return the first tool whose input schema has required fields."""
    for tool in tools:
        if _required_arguments(tool.inputSchema):
            return tool
    return None


def _first_tool_without_required_arguments(tools: Sequence[Tool]) -> Tool | None:
    """Return the first tool whose input schema can be called with ``{}``."""
    for tool in tools:
        if not _required_arguments(tool.inputSchema):
            return tool
    return None


async def _call_rejected(
    session: ClientSession,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[bool, str]:
    """Return True when a tool call is rejected via MCP error or ``isError`` result."""
    try:
        result = await session.call_tool(tool_name, arguments)
    except McpError as exc:
        return True, exc.error.message
    if result.isError:
        return True, _tool_result_text(result)
    return False, _tool_result_text(result)


async def _call_succeeds(
    session: ClientSession,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[bool, str]:
    """Return True when a tool call succeeds without protocol-level error."""
    try:
        result = await session.call_tool(tool_name, arguments)
    except McpError as exc:
        return False, exc.error.message
    if result.isError:
        return False, _tool_result_text(result)
    return True, _tool_result_text(result)


def _tool_result_text(result: CallToolResult) -> str:
    """Extract a compact text summary from a ``CallToolResult``."""
    parts: list[str] = []
    for block in result.content:
        text = getattr(block, "text", "")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return " ".join(parts)
