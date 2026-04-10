"""WEB-015: Tests for API client SDK generation."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.sdk_generator import (
    _method_name_from_operation,
    _python_type,
    _ts_type,
    generate_sdk,
    generate_sdk_to_file,
    generate_typescript_sdk,
    generate_typescript_sdk_to_file,
)


def _minimal_openapi_spec() -> dict:
    """Return a minimal OpenAPI spec for testing."""
    return {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": {
            "/tasks": {
                "get": {
                    "operationId": "list_tasks",
                    "summary": "List all tasks",
                    "parameters": [
                        {
                            "name": "status",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "OK"}},
                },
                "post": {
                    "operationId": "create_task",
                    "summary": "Create a new task",
                    "requestBody": {
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    },
                    "responses": {"201": {"description": "Created"}},
                },
            },
            "/tasks/{task_id}": {
                "get": {
                    "operationId": "get_task",
                    "summary": "Get a task by ID",
                    "parameters": [
                        {
                            "name": "task_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "OK"}},
                },
            },
            "/health": {
                "get": {
                    "summary": "Health check",
                    "responses": {"200": {"description": "OK"}},
                },
            },
        },
    }


class TestMethodNaming:
    """Test operation ID to method name conversion."""

    def test_simple_operation_id(self) -> None:
        result = _method_name_from_operation("get", "/tasks", {"operationId": "list_tasks"})
        assert result == "list_tasks"

    def test_camel_case_operation_id(self) -> None:
        result = _method_name_from_operation("post", "/tasks", {"operationId": "createTask"})
        assert result == "createtask"

    def test_no_operation_id_fallback(self) -> None:
        result = _method_name_from_operation("get", "/tasks/{task_id}", {})
        assert "get" in result
        assert "tasks" in result

    def test_hyphenated_operation_id(self) -> None:
        result = _method_name_from_operation("get", "/health", {"operationId": "health-check"})
        assert result == "health_check"


class TestPythonType:
    """Test OpenAPI type to Python type mapping."""

    def test_string(self) -> None:
        assert _python_type({"type": "string"}) == "str"

    def test_integer(self) -> None:
        assert _python_type({"type": "integer"}) == "int"

    def test_number(self) -> None:
        assert _python_type({"type": "number"}) == "float"

    def test_boolean(self) -> None:
        assert _python_type({"type": "boolean"}) == "bool"

    def test_array(self) -> None:
        assert _python_type({"type": "array"}) == "list[Any]"

    def test_object(self) -> None:
        assert _python_type({"type": "object"}) == "dict[str, Any]"

    def test_unknown(self) -> None:
        assert _python_type({}) == "Any"


class TestGenerateSDK:
    """Test full SDK generation."""

    def test_generates_valid_python(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_sdk(spec)
        # Should be valid Python
        compile(sdk, "<test>", "exec")

    def test_contains_client_class(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_sdk(spec)
        assert "class BernsteinClient:" in sdk

    def test_contains_error_class(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_sdk(spec)
        assert "class BernsteinAPIError" in sdk

    def test_generates_methods_for_endpoints(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_sdk(spec)
        assert "def list_tasks" in sdk
        assert "def create_task" in sdk
        assert "def get_task" in sdk

    def test_path_parameters_in_method(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_sdk(spec)
        # get_task should have task_id parameter
        assert "task_id: str" in sdk

    def test_query_parameters_optional(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_sdk(spec)
        # list_tasks has optional 'status' query param
        assert "status: str | None = None" in sdk

    def test_post_method_has_body(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_sdk(spec)
        assert "body: dict[str, Any]" in sdk

    def test_empty_spec(self) -> None:
        spec = {"openapi": "3.0.0", "info": {}, "paths": {}}
        sdk = generate_sdk(spec)
        assert "class BernsteinClient:" in sdk

    def test_includes_imports(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_sdk(spec)
        assert "from __future__ import annotations" in sdk
        assert "import json" in sdk


class TestGenerateSDKToFile:
    """Test SDK file generation."""

    def test_writes_file(self, tmp_path: Path) -> None:
        spec = _minimal_openapi_spec()
        output = tmp_path / "client.py"
        result = generate_sdk_to_file(str(output), spec)
        assert Path(result).exists()
        content = output.read_text()
        assert "class BernsteinClient:" in content

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        spec = _minimal_openapi_spec()
        output = tmp_path / "nested" / "dir" / "client.py"
        result = generate_sdk_to_file(str(output), spec)
        assert Path(result).exists()

    def test_generated_file_is_valid_python(self, tmp_path: Path) -> None:
        spec = _minimal_openapi_spec()
        output = tmp_path / "client.py"
        generate_sdk_to_file(str(output), spec)
        content = output.read_text()
        compile(content, str(output), "exec")


class TestTsType:
    """Test OpenAPI type to TypeScript type mapping."""

    def test_string(self) -> None:
        assert _ts_type({"type": "string"}) == "string"

    def test_integer(self) -> None:
        assert _ts_type({"type": "integer"}) == "number"

    def test_number(self) -> None:
        assert _ts_type({"type": "number"}) == "number"

    def test_boolean(self) -> None:
        assert _ts_type({"type": "boolean"}) == "boolean"

    def test_array(self) -> None:
        assert _ts_type({"type": "array"}) == "unknown[]"

    def test_object(self) -> None:
        assert _ts_type({"type": "object"}) == "Record<string, unknown>"

    def test_unknown(self) -> None:
        assert _ts_type({}) == "unknown"


class TestGenerateTypeScriptSDK:
    """Test TypeScript SDK generation."""

    def test_contains_client_class(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_typescript_sdk(spec)
        assert "class BernsteinClient" in sdk

    def test_contains_error_class(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_typescript_sdk(spec)
        assert "class BernsteinAPIError" in sdk

    def test_generates_methods_for_endpoints(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_typescript_sdk(spec)
        assert "listTasks" in sdk
        assert "createTask" in sdk
        assert "getTask" in sdk

    def test_path_parameters_in_method(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_typescript_sdk(spec)
        assert "task_id: string" in sdk

    def test_optional_query_parameters(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_typescript_sdk(spec)
        assert "status?: string" in sdk

    def test_post_method_has_body(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_typescript_sdk(spec)
        assert "body: Record<string, unknown>" in sdk

    def test_empty_spec(self) -> None:
        spec = {"openapi": "3.0.0", "info": {}, "paths": {}}
        sdk = generate_typescript_sdk(spec)
        assert "class BernsteinClient" in sdk

    def test_uses_fetch(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_typescript_sdk(spec)
        assert "fetch(" in sdk

    def test_async_methods(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_typescript_sdk(spec)
        assert "async " in sdk

    def test_promise_return_type(self) -> None:
        spec = _minimal_openapi_spec()
        sdk = generate_typescript_sdk(spec)
        assert "Promise<unknown>" in sdk


class TestGenerateTypeScriptSDKToFile:
    """Test TypeScript SDK file generation."""

    def test_writes_file(self, tmp_path: Path) -> None:
        spec = _minimal_openapi_spec()
        output = tmp_path / "client.ts"
        result = generate_typescript_sdk_to_file(str(output), spec)
        assert Path(result).exists()
        content = output.read_text()
        assert "class BernsteinClient" in content

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        spec = _minimal_openapi_spec()
        output = tmp_path / "nested" / "dir" / "client.ts"
        result = generate_typescript_sdk_to_file(str(output), spec)
        assert Path(result).exists()

    def test_has_ts_extension_content(self, tmp_path: Path) -> None:
        spec = _minimal_openapi_spec()
        output = tmp_path / "client.ts"
        generate_typescript_sdk_to_file(str(output), spec)
        content = output.read_text()
        assert "export class" in content
