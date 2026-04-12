"""Custom tool injection per task (claude-017).

Allow tasks to declare MCP tools in their definition so that the spawned
agent receives a tailored set of subprocess-based MCP servers.  Each tool
is mapped to a stdio-transport MCP server entry that executes the tool's
shell command, making it available via the ``--mcp-config`` flag.

Usage::

    task_data = {
        "id": "task-1",
        "tools": [
            {
                "name": "lint",
                "description": "Run project linter",
                "command": "ruff check .",
            }
        ],
    }
    config = load_task_tools(task_data)
    if config:
        mcp = generate_mcp_server_config(config)
        merged = merge_mcp_configs(base_mcp, mcp)
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, field
from typing import Any, cast

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskToolDefinition:
    """A single custom tool attached to a task.

    Attributes:
        name: Unique tool name (used as MCP server key).
        description: Human-readable purpose of the tool.
        command: Shell command executed when the tool is invoked.
        args_schema: JSON Schema dict describing accepted arguments.
        working_dir: Optional working directory override for the command.
    """

    name: str
    description: str
    command: str
    args_schema: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    working_dir: str | None = None


@dataclass(frozen=True)
class TaskToolConfig:
    """Collection of custom tools associated with a specific task.

    Attributes:
        task_id: Identifier of the task that owns these tools.
        tools: Ordered list of tool definitions.
    """

    task_id: str
    tools: tuple[TaskToolDefinition, ...] = ()


def load_task_tools(task_data: dict[str, Any]) -> TaskToolConfig | None:
    """Parse a task dict's ``tools`` section into a TaskToolConfig.

    Args:
        task_data: Raw task definition dict (e.g. from YAML plan or API).

    Returns:
        A TaskToolConfig if the task declares tools, otherwise None.
    """
    task_id: str = str(task_data.get("id", ""))
    raw_tools = task_data.get("tools")

    if not raw_tools or not isinstance(raw_tools, list):
        return None

    definitions: list[TaskToolDefinition] = []
    typed_tools = cast("list[Any]", raw_tools)
    for idx, entry in enumerate(typed_tools):
        if not isinstance(entry, dict):
            logger.warning("Task %s: tools[%d] is not a dict, skipping", task_id, idx)
            continue

        tool_entry = cast("dict[str, Any]", entry)
        name = str(tool_entry.get("name", "")).strip()
        if not name:
            logger.warning("Task %s: tools[%d] has no name, skipping", task_id, idx)
            continue

        command = str(tool_entry.get("command", "")).strip()
        if not command:
            logger.warning("Task %s: tool '%s' has no command, skipping", task_id, name)
            continue

        description = str(tool_entry.get("description", ""))
        raw_schema: Any = tool_entry.get("args_schema", {})
        args_schema: dict[str, Any] = cast("dict[str, Any]", raw_schema) if isinstance(raw_schema, dict) else {}
        working_dir: str | None = tool_entry.get("working_dir")
        if working_dir is not None:
            working_dir = str(working_dir)

        definitions.append(
            TaskToolDefinition(
                name=name,
                description=description,
                command=command,
                args_schema=args_schema,
                working_dir=working_dir,
            )
        )

    if not definitions:
        return None

    return TaskToolConfig(task_id=task_id, tools=tuple(definitions))


def generate_mcp_server_config(config: TaskToolConfig) -> dict[str, Any]:
    """Generate an MCP-compatible server config from task tool definitions.

    Each tool becomes a stdio-transport MCP server entry whose command
    is a shell invocation of the tool's command string.  The server key
    is prefixed with ``task-tool-`` to avoid collisions with user-defined
    or system MCP servers.

    Args:
        config: Validated task tool configuration.

    Returns:
        Dict with ``mcpServers`` key containing per-tool server entries.
    """
    servers: dict[str, Any] = {}

    for tool in config.tools:
        parts = shlex.split(tool.command)
        if not parts:
            continue

        server_key = f"task-tool-{tool.name}"
        server_entry: dict[str, Any] = {
            "command": parts[0],
            "args": parts[1:],
        }

        if tool.working_dir:
            server_entry["cwd"] = tool.working_dir

        # Embed description and args_schema as metadata so downstream
        # consumers can display tool documentation.
        env: dict[str, str] = {}
        if tool.description:
            env["TOOL_DESCRIPTION"] = tool.description
        if tool.args_schema:
            import json

            env["TOOL_ARGS_SCHEMA"] = json.dumps(tool.args_schema)
        if env:
            server_entry["env"] = env

        servers[server_key] = server_entry

    return {"mcpServers": servers}


def merge_mcp_configs(
    base: dict[str, Any] | None,
    task_tools: dict[str, Any],
) -> dict[str, Any]:
    """Merge task-specific tool servers into an existing MCP config.

    The base config is preserved; task tool servers are added alongside
    existing entries.  If a key collision occurs (unlikely due to the
    ``task-tool-`` prefix), the task tool entry wins so that per-task
    overrides take precedence.

    Args:
        base: Existing MCP config dict (may be None or empty).
        task_tools: MCP config generated by :func:`generate_mcp_server_config`.

    Returns:
        Merged MCP config dict with ``mcpServers`` key.
    """
    if not base:
        return dict(task_tools)

    base_servers: dict[str, Any] = dict(base.get("mcpServers", {}))
    task_servers: dict[str, Any] = task_tools.get("mcpServers", {})

    merged_servers = {**base_servers, **task_servers}
    return {"mcpServers": merged_servers}


def validate_task_tools(config: TaskToolConfig) -> list[str]:
    """Validate tool definitions and return a list of error messages.

    Checks performed:
    - Tool name is non-empty and contains no whitespace.
    - Command is non-empty.
    - Command can be parsed by shlex (no unterminated quotes).
    - No duplicate tool names within the config.

    Args:
        config: Task tool configuration to validate.

    Returns:
        List of human-readable error strings.  Empty list means valid.
    """
    errors: list[str] = []
    seen_names: set[str] = set()

    for tool in config.tools:
        if not tool.name or not tool.name.strip():
            errors.append("Tool has empty name")
            continue

        if " " in tool.name or "\t" in tool.name:
            errors.append(f"Tool name '{tool.name}' contains whitespace")

        if tool.name in seen_names:
            errors.append(f"Duplicate tool name '{tool.name}'")
        seen_names.add(tool.name)

        if not tool.command or not tool.command.strip():
            errors.append(f"Tool '{tool.name}' has empty command")
            continue

        try:
            parts = shlex.split(tool.command)
            if not parts:
                errors.append(f"Tool '{tool.name}' command parses to empty list")
        except ValueError as exc:
            errors.append(f"Tool '{tool.name}' has unparseable command: {exc}")

    return errors
