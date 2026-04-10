"""Tests for custom tool injection per task (claude-017)."""

from __future__ import annotations

from bernstein.core.task_tools import (
    TaskToolConfig,
    TaskToolDefinition,
    generate_mcp_server_config,
    load_task_tools,
    merge_mcp_configs,
    validate_task_tools,
)

# ---------------------------------------------------------------------------
# TaskToolDefinition / TaskToolConfig creation
# ---------------------------------------------------------------------------


class TestTaskToolDefinition:
    """Tests for TaskToolDefinition frozen dataclass."""

    def test_basic_creation(self) -> None:
        tool = TaskToolDefinition(
            name="lint",
            description="Run linter",
            command="ruff check .",
        )
        assert tool.name == "lint"
        assert tool.description == "Run linter"
        assert tool.command == "ruff check ."
        assert tool.args_schema == {}
        assert tool.working_dir is None

    def test_full_creation(self) -> None:
        schema = {"type": "object", "properties": {"path": {"type": "string"}}}
        tool = TaskToolDefinition(
            name="test",
            description="Run tests",
            command="pytest -x",
            args_schema=schema,
            working_dir="/tmp/project",
        )
        assert tool.args_schema == schema
        assert tool.working_dir == "/tmp/project"

    def test_frozen(self) -> None:
        tool = TaskToolDefinition(name="a", description="b", command="c")
        try:
            tool.name = "changed"  # type: ignore[misc]
            raise AssertionError("Should have raised FrozenInstanceError")
        except AttributeError:
            pass


class TestTaskToolConfig:
    """Tests for TaskToolConfig frozen dataclass."""

    def test_creation(self) -> None:
        t1 = TaskToolDefinition(name="lint", description="", command="ruff .")
        t2 = TaskToolDefinition(name="test", description="", command="pytest")
        config = TaskToolConfig(task_id="task-42", tools=(t1, t2))
        assert config.task_id == "task-42"
        assert len(config.tools) == 2

    def test_empty_tools_default(self) -> None:
        config = TaskToolConfig(task_id="task-0")
        assert config.tools == ()

    def test_frozen(self) -> None:
        config = TaskToolConfig(task_id="t")
        try:
            config.task_id = "changed"  # type: ignore[misc]
            raise AssertionError("Should have raised FrozenInstanceError")
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# load_task_tools
# ---------------------------------------------------------------------------


class TestLoadTaskTools:
    """Tests for parsing task dicts into TaskToolConfig."""

    def test_with_tools_section(self) -> None:
        data = {
            "id": "task-1",
            "tools": [
                {
                    "name": "lint",
                    "description": "Run linter",
                    "command": "ruff check .",
                },
                {
                    "name": "test",
                    "description": "Run tests",
                    "command": "pytest -x -q",
                    "working_dir": "/project",
                },
            ],
        }
        config = load_task_tools(data)
        assert config is not None
        assert config.task_id == "task-1"
        assert len(config.tools) == 2
        assert config.tools[0].name == "lint"
        assert config.tools[1].working_dir == "/project"

    def test_without_tools_section(self) -> None:
        data = {"id": "task-2", "goal": "fix bug"}
        assert load_task_tools(data) is None

    def test_empty_tools_list(self) -> None:
        data = {"id": "task-3", "tools": []}
        assert load_task_tools(data) is None

    def test_tools_not_a_list(self) -> None:
        data = {"id": "task-4", "tools": "invalid"}
        assert load_task_tools(data) is None

    def test_invalid_tool_entry_not_dict(self) -> None:
        data = {"id": "task-5", "tools": ["not-a-dict"]}
        assert load_task_tools(data) is None

    def test_tool_missing_name(self) -> None:
        data = {"id": "task-6", "tools": [{"command": "echo hi"}]}
        assert load_task_tools(data) is None

    def test_tool_empty_name(self) -> None:
        data = {"id": "task-7", "tools": [{"name": "  ", "command": "echo"}]}
        assert load_task_tools(data) is None

    def test_tool_missing_command(self) -> None:
        data = {"id": "task-8", "tools": [{"name": "broken"}]}
        assert load_task_tools(data) is None

    def test_tool_empty_command(self) -> None:
        data = {"id": "task-9", "tools": [{"name": "broken", "command": "  "}]}
        assert load_task_tools(data) is None

    def test_invalid_args_schema_ignored(self) -> None:
        data = {
            "id": "task-10",
            "tools": [{"name": "ok", "command": "echo", "args_schema": "bad"}],
        }
        config = load_task_tools(data)
        assert config is not None
        assert config.tools[0].args_schema == {}

    def test_missing_id_uses_empty_string(self) -> None:
        data = {"tools": [{"name": "ok", "command": "echo hi"}]}
        config = load_task_tools(data)
        assert config is not None
        assert config.task_id == ""

    def test_partial_valid_tools(self) -> None:
        """Valid tools are kept; invalid ones are skipped."""
        data = {
            "id": "task-11",
            "tools": [
                {"name": "good", "command": "echo ok"},
                {"name": "", "command": "echo nope"},
                {"command": "echo missing-name"},
                {"name": "also-good", "command": "echo yes"},
            ],
        }
        config = load_task_tools(data)
        assert config is not None
        assert len(config.tools) == 2
        assert config.tools[0].name == "good"
        assert config.tools[1].name == "also-good"


# ---------------------------------------------------------------------------
# generate_mcp_server_config
# ---------------------------------------------------------------------------


class TestGenerateMCPServerConfig:
    """Tests for MCP server config generation."""

    def test_basic_structure(self) -> None:
        config = TaskToolConfig(
            task_id="t1",
            tools=(TaskToolDefinition(name="lint", description="Lint code", command="ruff check ."),),
        )
        result = generate_mcp_server_config(config)

        assert "mcpServers" in result
        assert "task-tool-lint" in result["mcpServers"]

        server = result["mcpServers"]["task-tool-lint"]
        assert server["command"] == "ruff"
        assert server["args"] == ["check", "."]

    def test_description_in_env(self) -> None:
        config = TaskToolConfig(
            task_id="t2",
            tools=(TaskToolDefinition(name="test", description="Run tests", command="pytest"),),
        )
        result = generate_mcp_server_config(config)
        server = result["mcpServers"]["task-tool-test"]
        assert server["env"]["TOOL_DESCRIPTION"] == "Run tests"

    def test_args_schema_in_env(self) -> None:
        schema = {"type": "object"}
        config = TaskToolConfig(
            task_id="t3",
            tools=(
                TaskToolDefinition(
                    name="scan",
                    description="",
                    command="trivy scan",
                    args_schema=schema,
                ),
            ),
        )
        result = generate_mcp_server_config(config)
        server = result["mcpServers"]["task-tool-scan"]
        assert "TOOL_ARGS_SCHEMA" in server["env"]

    def test_working_dir_sets_cwd(self) -> None:
        config = TaskToolConfig(
            task_id="t4",
            tools=(
                TaskToolDefinition(
                    name="build",
                    description="Build project",
                    command="make all",
                    working_dir="/src",
                ),
            ),
        )
        result = generate_mcp_server_config(config)
        server = result["mcpServers"]["task-tool-build"]
        assert server["cwd"] == "/src"

    def test_no_cwd_when_no_working_dir(self) -> None:
        config = TaskToolConfig(
            task_id="t5",
            tools=(TaskToolDefinition(name="echo", description="", command="echo hi"),),
        )
        result = generate_mcp_server_config(config)
        server = result["mcpServers"]["task-tool-echo"]
        assert "cwd" not in server

    def test_multiple_tools(self) -> None:
        config = TaskToolConfig(
            task_id="t6",
            tools=(
                TaskToolDefinition(name="a", description="", command="cmd-a"),
                TaskToolDefinition(name="b", description="", command="cmd-b --flag"),
            ),
        )
        result = generate_mcp_server_config(config)
        assert len(result["mcpServers"]) == 2
        assert "task-tool-a" in result["mcpServers"]
        assert "task-tool-b" in result["mcpServers"]
        assert result["mcpServers"]["task-tool-b"]["args"] == ["--flag"]

    def test_empty_tools(self) -> None:
        config = TaskToolConfig(task_id="t7", tools=())
        result = generate_mcp_server_config(config)
        assert result == {"mcpServers": {}}

    def test_no_env_when_no_description_or_schema(self) -> None:
        config = TaskToolConfig(
            task_id="t8",
            tools=(TaskToolDefinition(name="bare", description="", command="true"),),
        )
        result = generate_mcp_server_config(config)
        server = result["mcpServers"]["task-tool-bare"]
        assert "env" not in server


# ---------------------------------------------------------------------------
# merge_mcp_configs
# ---------------------------------------------------------------------------


class TestMergeMCPConfigs:
    """Tests for merging task tools into base MCP config."""

    def test_preserves_base_servers(self) -> None:
        base = {"mcpServers": {"github": {"command": "gh-mcp"}}}
        task = {"mcpServers": {"task-tool-lint": {"command": "ruff"}}}
        merged = merge_mcp_configs(base, task)

        assert "github" in merged["mcpServers"]
        assert "task-tool-lint" in merged["mcpServers"]

    def test_none_base(self) -> None:
        task = {"mcpServers": {"task-tool-x": {"command": "x"}}}
        merged = merge_mcp_configs(None, task)
        assert merged == task

    def test_empty_base(self) -> None:
        task = {"mcpServers": {"task-tool-y": {"command": "y"}}}
        merged = merge_mcp_configs({}, task)
        assert merged["mcpServers"] == {"task-tool-y": {"command": "y"}}

    def test_base_without_mcpservers_key(self) -> None:
        base = {"otherKey": "value"}
        task = {"mcpServers": {"task-tool-z": {"command": "z"}}}
        merged = merge_mcp_configs(base, task)
        assert "task-tool-z" in merged["mcpServers"]

    def test_task_tools_override_on_collision(self) -> None:
        base = {"mcpServers": {"task-tool-x": {"command": "old"}}}
        task = {"mcpServers": {"task-tool-x": {"command": "new"}}}
        merged = merge_mcp_configs(base, task)
        assert merged["mcpServers"]["task-tool-x"]["command"] == "new"

    def test_does_not_mutate_base(self) -> None:
        base_servers = {"github": {"command": "gh"}}
        base = {"mcpServers": base_servers}
        task = {"mcpServers": {"task-tool-a": {"command": "a"}}}
        merge_mcp_configs(base, task)
        # Original base dict should be unchanged
        assert "task-tool-a" not in base_servers

    def test_empty_task_tools(self) -> None:
        base = {"mcpServers": {"existing": {"command": "e"}}}
        merged = merge_mcp_configs(base, {"mcpServers": {}})
        assert merged["mcpServers"] == {"existing": {"command": "e"}}


# ---------------------------------------------------------------------------
# validate_task_tools
# ---------------------------------------------------------------------------


class TestValidateTaskTools:
    """Tests for tool definition validation."""

    def test_valid_config(self) -> None:
        config = TaskToolConfig(
            task_id="t",
            tools=(
                TaskToolDefinition(name="lint", description="Lint", command="ruff ."),
                TaskToolDefinition(name="test", description="Test", command="pytest"),
            ),
        )
        errors = validate_task_tools(config)
        assert errors == []

    def test_empty_name(self) -> None:
        config = TaskToolConfig(
            task_id="t",
            tools=(TaskToolDefinition(name="", description="", command="echo"),),
        )
        errors = validate_task_tools(config)
        assert len(errors) == 1
        assert "empty name" in errors[0]

    def test_whitespace_only_name(self) -> None:
        config = TaskToolConfig(
            task_id="t",
            tools=(TaskToolDefinition(name="   ", description="", command="echo"),),
        )
        errors = validate_task_tools(config)
        assert len(errors) == 1
        assert "empty name" in errors[0]

    def test_name_with_spaces(self) -> None:
        config = TaskToolConfig(
            task_id="t",
            tools=(TaskToolDefinition(name="my tool", description="", command="echo"),),
        )
        errors = validate_task_tools(config)
        assert any("whitespace" in e for e in errors)

    def test_empty_command(self) -> None:
        config = TaskToolConfig(
            task_id="t",
            tools=(TaskToolDefinition(name="broken", description="", command=""),),
        )
        errors = validate_task_tools(config)
        assert len(errors) == 1
        assert "empty command" in errors[0]

    def test_whitespace_only_command(self) -> None:
        config = TaskToolConfig(
            task_id="t",
            tools=(TaskToolDefinition(name="broken", description="", command="   "),),
        )
        errors = validate_task_tools(config)
        assert len(errors) == 1
        assert "empty command" in errors[0]

    def test_unparseable_command(self) -> None:
        config = TaskToolConfig(
            task_id="t",
            tools=(TaskToolDefinition(name="bad", description="", command="echo 'unterminated"),),
        )
        errors = validate_task_tools(config)
        assert len(errors) == 1
        assert "unparseable" in errors[0]

    def test_duplicate_names(self) -> None:
        config = TaskToolConfig(
            task_id="t",
            tools=(
                TaskToolDefinition(name="dup", description="", command="echo 1"),
                TaskToolDefinition(name="dup", description="", command="echo 2"),
            ),
        )
        errors = validate_task_tools(config)
        assert any("Duplicate" in e for e in errors)

    def test_multiple_errors(self) -> None:
        config = TaskToolConfig(
            task_id="t",
            tools=(
                TaskToolDefinition(name="", description="", command="echo"),
                TaskToolDefinition(name="ok", description="", command=""),
                TaskToolDefinition(name="has space", description="", command="echo"),
            ),
        )
        errors = validate_task_tools(config)
        assert len(errors) == 3

    def test_empty_config(self) -> None:
        config = TaskToolConfig(task_id="t", tools=())
        errors = validate_task_tools(config)
        assert errors == []
