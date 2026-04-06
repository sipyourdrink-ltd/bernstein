"""WEB-015: API client SDK generation script from OpenAPI spec.

Generates a Python client SDK module from the Bernstein OpenAPI spec.
Can be invoked as a CLI script or imported as a library function.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_SDK_HEADER = '''\
"""Auto-generated Bernstein API client SDK.

Generated from the Bernstein OpenAPI spec.  Do not edit manually.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError


class BernsteinAPIError(Exception):
    """Raised when the API returns an error status."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"API error {status}: {detail}")


class BernsteinClient:
    """Auto-generated API client for the Bernstein task server.

    Args:
        base_url: Base URL of the Bernstein server (e.g. ``http://127.0.0.1:8052``).
        auth_token: Optional bearer auth token.
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8052",
        auth_token: str | None = None,
        timeout: int = 30,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body else None
        req = Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
            raise BernsteinAPIError(exc.code, detail) from exc

'''


def _method_name_from_operation(method: str, path: str, operation: dict[str, Any]) -> str:
    """Derive a Python method name from an OpenAPI operation."""
    op_id = operation.get("operationId", "")
    if op_id:
        # Convert camelCase or snake-case operationId to snake_case
        name = op_id.replace("-", "_").replace(".", "_")
        # Strip duplicate underscores
        while "__" in name:
            name = name.replace("__", "_")
        return name.lower()

    # Fallback: method + path segments
    parts = [method.lower()]
    for segment in path.strip("/").split("/"):
        if segment.startswith("{"):
            parts.append("by_" + segment.strip("{}"))
        else:
            parts.append(segment.replace("-", "_"))
    return "_".join(parts)


def _python_type(schema: dict[str, Any]) -> str:
    """Map an OpenAPI type to a Python type hint."""
    type_map: dict[str, str] = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "array": "list[Any]",
        "object": "dict[str, Any]",
    }
    return type_map.get(schema.get("type", ""), "Any")


def _generate_method(method: str, path: str, operation: dict[str, Any]) -> str:
    """Generate a Python method for a single API operation."""
    name = _method_name_from_operation(method, path, operation)
    summary = operation.get("summary", operation.get("description", ""))
    http_method = method.upper()

    # Collect path parameters
    params: list[str] = ["self"]
    path_params: list[str] = []
    query_params: list[str] = []

    for param in operation.get("parameters", []):
        param_name = param.get("name", "")
        param_type = _python_type(param.get("schema", {}))
        required = param.get("required", False)
        if param.get("in") == "path":
            params.append(f"{param_name}: {param_type}")
            path_params.append(param_name)
        elif param.get("in") == "query":
            if required:
                params.append(f"{param_name}: {param_type}")
            else:
                params.append(f"{param_name}: {param_type} | None = None")
            query_params.append(param_name)

    # Body parameter
    has_body = http_method in ("POST", "PUT", "PATCH")
    request_body = operation.get("requestBody", {})
    if has_body and request_body:
        params.append("body: dict[str, Any]")
    elif has_body:
        params.append("body: dict[str, Any] | None = None")

    params_str = ", ".join(params)

    # Build path with f-string substitution
    py_path = path
    for p in path_params:
        py_path = py_path.replace(f"{{{p}}}", f"{{{p}}}")

    # Build method body
    lines: list[str] = []
    lines.append(f"    def {name}({params_str}) -> Any:")
    if summary:
        lines.append(f'        """{summary}"""')
    else:
        lines.append(f'        """{http_method} {path}"""')

    if query_params:
        lines.append("        _query_parts: list[str] = []")
        for qp in query_params:
            lines.append(f"        if {qp} is not None:")
            lines.append(f'            _query_parts.append(f"{qp}={{{qp}}}")')
        lines.append(f'        _path = f"{py_path}"')
        lines.append("        if _query_parts:")
        lines.append('            _path += "?" + "&".join(_query_parts)')
    else:
        lines.append(f'        _path = f"{py_path}"')

    body_arg = "body=body" if has_body else ""
    lines.append(f'        return self._request("{http_method}", _path{", " + body_arg if body_arg else ""})')

    return "\n".join(lines)


def generate_sdk(openapi_spec: dict[str, Any]) -> str:
    """Generate a Python SDK module from an OpenAPI spec dict.

    Args:
        openapi_spec: Parsed OpenAPI specification as a dict.

    Returns:
        Python source code string for the generated client module.
    """
    methods: list[str] = []
    paths = openapi_spec.get("paths", {})

    for path, path_item in sorted(paths.items()):
        for method in ("get", "post", "put", "patch", "delete"):
            if method not in path_item:
                continue
            operation = path_item[method]
            method_code = _generate_method(method, path, operation)
            methods.append(method_code)

    methods_block = "\n\n".join(methods)
    return f"{_SDK_HEADER}{methods_block}\n"


def generate_sdk_from_app(base_url: str = "http://127.0.0.1:8052") -> str:
    """Generate the SDK by fetching the OpenAPI spec from a running server.

    Args:
        base_url: URL of the running Bernstein server.

    Returns:
        Python source code for the generated client.
    """
    import urllib.request

    url = f"{base_url.rstrip('/')}/openapi.json"
    with urllib.request.urlopen(url) as resp:
        spec = json.loads(resp.read().decode("utf-8"))
    return generate_sdk(spec)


def generate_sdk_to_file(output_path: str, openapi_spec: dict[str, Any]) -> str:
    """Generate the SDK and write it to a file.

    Args:
        output_path: File path to write the generated SDK.
        openapi_spec: OpenAPI spec dict.

    Returns:
        The output file path.
    """
    from pathlib import Path

    sdk_code = generate_sdk(openapi_spec)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sdk_code, encoding="utf-8")
    return str(path)
