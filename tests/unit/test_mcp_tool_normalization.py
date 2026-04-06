"""Tests for MCP tool normalization (MCP-003)."""

from __future__ import annotations

from bernstein.core.mcp_tool_normalization import (
    McpToolError,
    McpToolException,
    ToolNormalizer,
    normalize_tool_name,
    validate_tool_params,
    validate_tool_schema,
)

# ---------------------------------------------------------------------------
# normalize_tool_name
# ---------------------------------------------------------------------------


class TestNormalizeToolName:
    """Tests for snake_case normalization."""

    def test_camel_case(self) -> None:
        assert normalize_tool_name("searchIssues") == "search_issues"

    def test_pascal_case(self) -> None:
        assert normalize_tool_name("SearchIssues") == "search_issues"

    def test_kebab_case(self) -> None:
        assert normalize_tool_name("get-user-profile") == "get_user_profile"

    def test_dot_separated(self) -> None:
        assert normalize_tool_name("myServer.SearchIssues") == "my_server_search_issues"

    def test_slash_separated(self) -> None:
        assert normalize_tool_name("tools/call") == "tools_call"

    def test_already_snake(self) -> None:
        assert normalize_tool_name("already_snake_case") == "already_snake_case"

    def test_mixed_case_with_numbers(self) -> None:
        assert normalize_tool_name("getV2Users") == "get_v2_users"

    def test_empty_string(self) -> None:
        assert normalize_tool_name("") == ""

    def test_single_word_lower(self) -> None:
        assert normalize_tool_name("search") == "search"

    def test_single_word_upper(self) -> None:
        assert normalize_tool_name("SEARCH") == "search"

    def test_consecutive_uppercase(self) -> None:
        assert normalize_tool_name("getHTTPResponse") == "get_http_response"

    def test_underscores_in_input(self) -> None:
        assert normalize_tool_name("my__server") == "my_server"


# ---------------------------------------------------------------------------
# validate_tool_schema
# ---------------------------------------------------------------------------


class TestValidateToolSchema:
    """Tests for JSON Schema validation."""

    def test_valid_schema(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        }
        errors = validate_tool_schema(schema)
        assert errors == []

    def test_invalid_top_level_type(self) -> None:
        schema = {"type": "foobar"}
        errors = validate_tool_schema(schema)
        assert len(errors) == 1
        assert "foobar" in errors[0].message

    def test_invalid_property_type(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "bad_field": {"type": "unicorn"},
            },
        }
        errors = validate_tool_schema(schema)
        assert len(errors) == 1
        assert "unicorn" in errors[0].message
        assert "/properties/bad_field/type" in errors[0].path

    def test_required_references_missing_property(self) -> None:
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name", "missing_prop"],
        }
        errors = validate_tool_schema(schema)
        assert len(errors) == 1
        assert "missing_prop" in errors[0].message

    def test_properties_not_dict(self) -> None:
        schema = {"type": "object", "properties": "bad"}
        errors = validate_tool_schema(schema)
        assert len(errors) == 1
        assert "must be an object" in errors[0].message

    def test_required_not_list(self) -> None:
        schema = {"type": "object", "required": "oops"}
        errors = validate_tool_schema(schema)
        assert len(errors) == 1
        assert "must be an array" in errors[0].message

    def test_items_validation(self) -> None:
        schema = {
            "type": "array",
            "items": {"type": "badtype"},
        }
        errors = validate_tool_schema(schema)
        assert len(errors) == 1
        assert "/items/type" in errors[0].path

    def test_items_not_dict(self) -> None:
        schema = {"type": "array", "items": "string"}
        errors = validate_tool_schema(schema)
        assert len(errors) == 1
        assert "must be an object" in errors[0].message

    def test_no_type_is_ok(self) -> None:
        schema = {"properties": {"x": {"type": "string"}}}
        errors = validate_tool_schema(schema)
        assert errors == []

    def test_property_schema_not_dict(self) -> None:
        schema = {
            "type": "object",
            "properties": {"bad": "not a dict"},
        }
        errors = validate_tool_schema(schema)
        assert len(errors) == 1
        assert "must be an object" in errors[0].message


# ---------------------------------------------------------------------------
# validate_tool_params
# ---------------------------------------------------------------------------


class TestValidateToolParams:
    """Tests for parameter validation against schema."""

    def test_valid_params(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        }
        errors = validate_tool_params({"query": "test", "limit": 10}, schema)
        assert errors == []

    def test_missing_required(self) -> None:
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        errors = validate_tool_params({}, schema)
        assert len(errors) == 1
        assert "query" in errors[0].message

    def test_type_mismatch(self) -> None:
        schema = {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        }
        errors = validate_tool_params({"limit": "not_int"}, schema)
        assert len(errors) == 1
        assert "Type mismatch" in errors[0].message

    def test_extra_params_allowed(self) -> None:
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        }
        errors = validate_tool_params({"query": "test", "extra": 42}, schema)
        assert errors == []

    def test_boolean_not_integer(self) -> None:
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
        }
        # bool is a subclass of int in Python; we should reject it for "integer"
        errors = validate_tool_params({"count": True}, schema)
        assert len(errors) == 1

    def test_number_accepts_int_and_float(self) -> None:
        schema = {
            "type": "object",
            "properties": {"value": {"type": "number"}},
        }
        assert validate_tool_params({"value": 42}, schema) == []
        assert validate_tool_params({"value": 3.14}, schema) == []


# ---------------------------------------------------------------------------
# McpToolError
# ---------------------------------------------------------------------------


class TestMcpToolError:
    """Tests for McpToolError dataclass."""

    def test_to_dict(self) -> None:
        err = McpToolError(
            tool_name="search_issues",
            original_name="searchIssues",
            code="PARAM_VALIDATION_FAILED",
            message="Missing required parameter: 'query'",
            details={"path": "/params/query"},
        )
        d = err.to_dict()
        assert d["tool_name"] == "search_issues"
        assert d["code"] == "PARAM_VALIDATION_FAILED"
        assert d["details"] == {"path": "/params/query"}

    def test_to_dict_no_details(self) -> None:
        err = McpToolError(
            tool_name="x",
            original_name="X",
            code="ERR",
            message="fail",
        )
        d = err.to_dict()
        assert "details" not in d

    def test_exception_wraps_error(self) -> None:
        err = McpToolError(
            tool_name="t",
            original_name="T",
            code="ERR",
            message="something broke",
        )
        exc = McpToolException(err)
        assert exc.error is err
        assert str(exc) == "something broke"


# ---------------------------------------------------------------------------
# ToolNormalizer
# ---------------------------------------------------------------------------


class TestToolNormalizer:
    """Tests for the ToolNormalizer registry."""

    def test_register_and_lookup(self) -> None:
        n = ToolNormalizer()
        normalized = n.register_tool("SearchIssues", server_name="github")
        assert normalized == "search_issues"
        assert n.get_normalized_name("SearchIssues") == "search_issues"
        assert n.get_original_name("search_issues") == "SearchIssues"

    def test_unknown_lookup_returns_none(self) -> None:
        n = ToolNormalizer()
        assert n.get_normalized_name("unknown") is None
        assert n.get_original_name("unknown") is None

    def test_tool_count(self) -> None:
        n = ToolNormalizer()
        assert n.tool_count == 0
        n.register_tool("a")
        n.register_tool("b")
        assert n.tool_count == 2

    def test_list_tools(self) -> None:
        n = ToolNormalizer()
        n.register_tool("SearchIssues", server_name="github")
        tools = n.list_tools()
        assert len(tools) == 1
        assert tools[0]["original"] == "SearchIssues"
        assert tools[0]["normalized"] == "search_issues"
        assert tools[0]["server"] == "github"

    def test_normalize_call_validates_params(self) -> None:
        n = ToolNormalizer()
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        n.register_tool("SearchIssues", schema=schema)

        # Missing required param
        name, _params, errors = n.normalize_call("SearchIssues", {})
        assert name == "search_issues"
        assert len(errors) == 1
        assert errors[0].code == "PARAM_VALIDATION_FAILED"

    def test_normalize_call_valid_params(self) -> None:
        n = ToolNormalizer()
        schema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        n.register_tool("SearchIssues", schema=schema)

        name, _params, errors = n.normalize_call("SearchIssues", {"query": "bug"})
        assert name == "search_issues"
        assert errors == []

    def test_normalize_call_unregistered_tool(self) -> None:
        n = ToolNormalizer()
        name, _params, errors = n.normalize_call("UnknownTool", {"x": 1})
        assert name == "unknown_tool"
        assert errors == []

    def test_normalize_call_by_normalized_name(self) -> None:
        n = ToolNormalizer()
        schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
        n.register_tool("GetUsers", schema=schema)

        name, _params, errors = n.normalize_call("get_users", {"q": "test"})
        assert name == "get_users"
        assert errors == []

    def test_normalize_call_no_schema(self) -> None:
        n = ToolNormalizer()
        n.register_tool("MyTool")
        name, _params, errors = n.normalize_call("MyTool", {"anything": 42})
        assert name == "my_tool"
        assert errors == []
