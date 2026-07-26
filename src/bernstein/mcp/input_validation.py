"""Schema-validated MCP tool-call inputs with deny-by-default.

This module is the orchestrator-side input firewall for the Bernstein MCP
server. Every tool-call payload arriving over the MCP transport must be
shape-validated before the tool handler runs. The validator is deliberately
strict:

* **Unknown tools are rejected**: only tools with a registered schema may run.
* **Unknown top-level properties are rejected** (``additionalProperties: false``).
* **Oversize payloads are rejected** before JSON Schema even sees them.
* **Recursive / deeply nested payloads are rejected** to bound work per call.
* **Control characters in string args are rejected** to block prompt-injected
  newlines and ANSI escapes that downstream agents would render verbatim.

The validator returns a tagged ``ValidatedPayload`` on success, or a
``ValidationError`` on failure. Callers translate ``ValidationError`` into a
JSON-RPC 2.0 error response: ``-32601`` for unknown tools, ``-32602`` for
malformed params.

Schemas live under ``src/bernstein/mcp/tool_schemas/<tool>.json`` and are
loaded once at process start. Operators may opt into permissive mode via
``BERNSTEIN_MCP_VALIDATION=permissive`` (transitional only) or extend the
deny rules' allowlist via ``mcp.allow_unsafe_args`` in ``bernstein.yaml``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import jsonschema

if TYPE_CHECKING:
    from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables: deny-by-default thresholds. Operators that need to push past
# them must explicitly opt in via ``mcp.allow_unsafe_args`` in bernstein.yaml.
# ---------------------------------------------------------------------------

#: Maximum total serialized payload size in bytes. 64 KiB is enough for every
#: legitimate Bernstein tool call we ship; bigger means something is off.
MAX_PAYLOAD_BYTES: Final[int] = 64 * 1024

#: Maximum recursion depth for nested containers (dicts / lists). 10 is far
#: more than any legitimate tool needs and far less than CPython's recursion
#: limit, so we fail fast on cyclic / pathological payloads.
MAX_RECURSION_DEPTH: Final[int] = 10

#: Control-character ranges rejected in string args. We allow ordinary TAB
#: (U+0009), LF (U+000A) and CR (U+000D) because tools legitimately accept
#: multi-line free-form text (e.g. ``goal=``). Everything else in the C0/C1
#: control planes is treated as adversarial.
_ALLOWED_CONTROL_CHARS: Final[frozenset[str]] = frozenset({"\t", "\n", "\r"})

#: JSON-RPC error codes (see the spec section 5.1).
JSONRPC_METHOD_NOT_FOUND: Final[int] = -32601
JSONRPC_INVALID_PARAMS: Final[int] = -32602

#: Environment variable that flips the validator into log-and-pass mode.
#: ``strict`` (default) rejects; ``permissive`` logs the rejection but lets
#: the call through. ``permissive`` exists only as a migration aid.
_MODE_ENV: Final[str] = "BERNSTEIN_MCP_VALIDATION"

#: Path to the bundled schema directory.
_SCHEMA_DIR: Final[Path] = Path(__file__).resolve().parent / "tool_schemas"


# ---------------------------------------------------------------------------
# Result types: a discriminated union over success / failure.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidatedPayload:
    """Successful validation result.

    Attributes:
        tool_name: The tool the payload was validated against.
        payload: The validated payload (shallow-copied to defeat caller
            mutation between validation and dispatch).
    """

    tool_name: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ValidationError:
    """Failed validation result.

    Attributes:
        tool_name: The tool the payload targeted.
        code: JSON-RPC error code (``-32601`` for unknown tool, ``-32602``
            for invalid params).
        message: One-line human-readable reason.
        errors: Detailed per-violation list. Each entry has a ``path`` (JSON
            pointer of the offending field, ``""`` for the document root)
            and a ``reason``.
    """

    tool_name: str
    code: int
    message: str
    errors: list[dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Schema registry: loaded once at first call, cached for the process.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SchemaRegistry:
    """A loaded set of per-tool schemas."""

    schemas: dict[str, dict[str, Any]]
    allow_unsafe_args: frozenset[str]

    def has(self, tool_name: str) -> bool:
        """Return ``True`` when a schema is registered for ``tool_name``."""
        return tool_name in self.schemas

    def get(self, tool_name: str) -> dict[str, Any] | None:
        """Return the registered schema (or ``None`` if unknown)."""
        return self.schemas.get(tool_name)


_registry_cache: SchemaRegistry | None = None


def _load_schema_file(path: Path) -> dict[str, Any]:
    """Load and JSON-parse one schema file, with a friendly error on failure."""
    raw = path.read_text(encoding="utf-8")
    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"Schema file {path.name} is not valid JSON: {exc}"
        raise RuntimeError(msg) from exc
    if not isinstance(data, dict):
        msg = f"Schema file {path.name} must contain a JSON object at the root"
        raise RuntimeError(msg)
    return cast("dict[str, Any]", data)


def load_registry(
    schema_dir: Path | None = None,
    *,
    allow_unsafe_args: frozenset[str] | None = None,
) -> SchemaRegistry:
    """Build a fresh ``SchemaRegistry`` by reading every JSON file in ``schema_dir``.

    Args:
        schema_dir: Directory to scan. Defaults to the bundled
            ``tool_schemas/`` next to this module.
        allow_unsafe_args: Tool names exempt from the deny rules (size /
            depth / control chars). Schema validation still runs.

    Returns:
        A populated ``SchemaRegistry``.
    """
    directory = schema_dir if schema_dir is not None else _SCHEMA_DIR
    schemas: dict[str, dict[str, Any]] = {}
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            tool = path.stem
            schemas[tool] = _load_schema_file(path)
            # Validate the schema itself: a corrupt schema file is an
            # operator bug we want to surface at startup, not on the first
            # incoming call.
            jsonschema.Draft7Validator.check_schema(schemas[tool])
    return SchemaRegistry(
        schemas=schemas,
        allow_unsafe_args=allow_unsafe_args or frozenset(),
    )


def get_registry() -> SchemaRegistry:
    """Return the cached registry, loading it on first access."""
    global _registry_cache
    if _registry_cache is None:
        _registry_cache = load_registry()
    return _registry_cache


def reset_registry_cache() -> None:
    """Clear the cached registry: useful in tests."""
    global _registry_cache
    _registry_cache = None


# ---------------------------------------------------------------------------
# Mode resolution.
# ---------------------------------------------------------------------------


def _is_permissive() -> bool:
    """Return ``True`` when the env var requests permissive mode."""
    return os.environ.get(_MODE_ENV, "strict").strip().lower() == "permissive"


# ---------------------------------------------------------------------------
# Deny-by-default rules: these run before JSON Schema validation so the
# Schema validator never sees pathological inputs.
# ---------------------------------------------------------------------------


def _payload_size_bytes(payload: dict[str, Any]) -> int:
    """Return the serialized size of ``payload`` in UTF-8 bytes."""
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _check_recursion_depth(value: object, *, limit: int, depth: int = 0) -> int | None:
    """Return the depth at which ``value`` first exceeds ``limit``, or ``None``.

    A return of ``None`` means the structure is within bounds; an integer is
    the depth at which the limit was breached (used in error messages).
    """
    if depth > limit:
        return depth
    if isinstance(value, dict):
        dict_value = cast("dict[Any, Any]", value)
        for sub in dict_value.values():
            sub_any: object = sub
            breach = _check_recursion_depth(sub_any, limit=limit, depth=depth + 1)
            if breach is not None:
                return breach
    elif isinstance(value, list):
        list_value = cast("list[Any]", value)
        for sub in list_value:
            sub_any2: object = sub
            breach = _check_recursion_depth(sub_any2, limit=limit, depth=depth + 1)
            if breach is not None:
                return breach
    return None


def _has_disallowed_control_chars(s: str) -> bool:
    """Return ``True`` when ``s`` contains a disallowed control character."""
    for ch in s:
        if ch in _ALLOWED_CONTROL_CHARS:
            continue
        code = ord(ch)
        # C0 controls: U+0000..U+001F; DEL: U+007F; C1 controls: U+0080..U+009F.
        if code <= 0x1F or code == 0x7F or 0x80 <= code <= 0x9F:
            return True
    return False


def _collect_control_char_violations(value: object, path: str = "") -> list[dict[str, str]]:
    """Walk ``value`` and emit one violation per string with bad control chars."""
    out: list[dict[str, str]] = []
    if isinstance(value, str):
        if _has_disallowed_control_chars(value):
            out.append({"path": path or "/", "reason": "string contains disallowed control character"})
    elif isinstance(value, dict):
        dict_value = cast("dict[Any, Any]", value)
        for key_obj, sub in dict_value.items():
            key = str(key_obj)
            sub_obj: object = sub
            out.extend(_collect_control_char_violations(sub_obj, f"{path}/{key}"))
    elif isinstance(value, list):
        list_value = cast("list[Any]", value)
        for idx, sub in enumerate(list_value):
            sub_obj2: object = sub
            out.extend(_collect_control_char_violations(sub_obj2, f"{path}/{idx}"))
    return out


def _apply_deny_rules(
    tool_name: str,
    payload: dict[str, Any],
    registry: SchemaRegistry,
) -> list[dict[str, str]]:
    """Run the deny-by-default rules. Returns an empty list when clean.

    Tools listed in ``registry.allow_unsafe_args`` skip these rules; schema
    validation still runs on them. This is the escape valve for the rare
    legitimately-huge tool input.
    """
    if tool_name in registry.allow_unsafe_args:
        return []

    violations: list[dict[str, str]] = []

    size = _payload_size_bytes(payload)
    if size > MAX_PAYLOAD_BYTES:
        violations.append(
            {
                "path": "",
                "reason": f"payload exceeds {MAX_PAYLOAD_BYTES} bytes (got {size})",
            }
        )

    breach = _check_recursion_depth(payload, limit=MAX_RECURSION_DEPTH)
    if breach is not None:
        violations.append(
            {
                "path": "",
                "reason": f"payload nesting exceeds depth {MAX_RECURSION_DEPTH} (got {breach})",
            }
        )

    violations.extend(_collect_control_char_violations(payload))
    return violations


# ---------------------------------------------------------------------------
# JSON Schema validation.
# ---------------------------------------------------------------------------


def _format_pointer(absolute_path: list[object]) -> str:
    """Render a jsonschema absolute path as a slash-pointer.

    ``jsonschema`` exposes errors with a ``deque`` of path components. We
    keep things human-readable by joining them as ``/a/0/b``.
    """
    parts: list[str] = [str(component) for component in absolute_path]
    return "/" + "/".join(parts) if parts else ""


def _schema_violations(payload: dict[str, Any], schema: dict[str, Any]) -> list[dict[str, str]]:
    """Validate ``payload`` against ``schema`` and return all violations."""
    validator: Any = jsonschema.Draft7Validator(schema)
    raw_errors = cast("list[JsonSchemaValidationError]", list(validator.iter_errors(payload)))
    raw_errors.sort(key=lambda e: list(e.absolute_path))
    return [
        {
            "path": _format_pointer(list(err.absolute_path)),
            "reason": str(err.message),
        }
        for err in raw_errors
    ]


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def validate_tool_call(
    tool_name: str,
    payload: object,
    *,
    registry: SchemaRegistry | None = None,
    permissive: bool | None = None,
) -> ValidatedPayload | ValidationError:
    """Validate a single MCP tool-call payload.

    Args:
        tool_name: The advertised tool name.
        payload: The raw params dict from the JSON-RPC ``params`` field.
            Must be a JSON object: anything else is rejected.
        registry: Optional registry override. Default: the cached registry
            loaded from the bundled schema directory.
        permissive: Optional override for the env-controlled mode. When
            ``True``, validation runs but failures are demoted to a logged
            warning and a ``ValidatedPayload`` is returned anyway.

    Returns:
        ``ValidatedPayload`` on success, ``ValidationError`` on failure
        (unless permissive mode demotes it).
    """
    reg = registry if registry is not None else get_registry()
    is_permissive = _is_permissive() if permissive is None else permissive

    # ``tool_name`` is statically typed ``str`` but callers may pass arbitrary
    # JSON values from network deserialization, so the runtime guard stays.
    name_obj = cast("object", tool_name)
    if not isinstance(name_obj, str) or not name_obj:
        err = ValidationError(
            tool_name=str(name_obj),
            code=JSONRPC_METHOD_NOT_FOUND,
            message="tool name must be a non-empty string",
            errors=[{"path": "", "reason": "tool name missing or empty"}],
        )
        return _maybe_demote(err, payload, is_permissive)

    if not reg.has(tool_name):
        err = ValidationError(
            tool_name=tool_name,
            code=JSONRPC_METHOD_NOT_FOUND,
            message=f"unknown tool: {tool_name}",
            errors=[{"path": "", "reason": "no schema registered for tool"}],
        )
        return _maybe_demote(err, payload, is_permissive)

    if not isinstance(payload, dict):
        err = ValidationError(
            tool_name=tool_name,
            code=JSONRPC_INVALID_PARAMS,
            message="params must be a JSON object",
            errors=[{"path": "", "reason": "expected object payload"}],
        )
        return _maybe_demote(err, payload, is_permissive)

    typed_payload: dict[str, Any] = cast("dict[str, Any]", cast("dict[Any, Any]", payload).copy())

    deny_violations = _apply_deny_rules(tool_name, typed_payload, reg)
    if deny_violations:
        err = ValidationError(
            tool_name=tool_name,
            code=JSONRPC_INVALID_PARAMS,
            message="payload rejected by deny-by-default rules",
            errors=deny_violations,
        )
        return _maybe_demote(err, typed_payload, is_permissive)

    schema = reg.get(tool_name)
    if schema is None:
        # Defensive: registry.has confirmed the entry exists, but a race in
        # a custom registry could conceivably remove it. Treat as unknown.
        err = ValidationError(
            tool_name=tool_name,
            code=JSONRPC_METHOD_NOT_FOUND,
            message=f"unknown tool: {tool_name}",
            errors=[{"path": "", "reason": "schema disappeared between has/get"}],
        )
        return _maybe_demote(err, typed_payload, is_permissive)
    schema_errors = _schema_violations(typed_payload, schema)
    if schema_errors:
        err = ValidationError(
            tool_name=tool_name,
            code=JSONRPC_INVALID_PARAMS,
            message="payload failed schema validation",
            errors=schema_errors,
        )
        return _maybe_demote(err, typed_payload, is_permissive)

    return ValidatedPayload(tool_name=tool_name, payload=typed_payload)


def _maybe_demote(
    err: ValidationError,
    payload: object,
    is_permissive: bool,
) -> ValidatedPayload | ValidationError:
    """In permissive mode, log the error and return a best-effort success.

    Permissive mode exists only to ease migration; production should always
    run in strict mode. Note: we still wrap the payload in a
    ``ValidatedPayload`` so the dispatch path stays uniform, but downstream
    handlers should treat it as untrusted.
    """
    if not is_permissive:
        return err
    logger.warning(
        "MCP input validation rejected payload but permissive mode is on: tool=%s code=%d errors=%s",
        err.tool_name,
        err.code,
        err.errors,
    )
    if isinstance(payload, dict):
        dict_payload: dict[str, Any] = cast("dict[str, Any]", cast("dict[Any, Any]", payload).copy())
        return ValidatedPayload(tool_name=err.tool_name, payload=dict_payload)
    return ValidatedPayload(tool_name=err.tool_name, payload={})


def validate_or_error(tool_name: str, params: dict[str, Any]) -> ValidationError | None:
    """Validate ``params`` against ``tool_name``'s schema, for tool handlers.

    Returns ``None`` when the call is allowed, or a ``ValidationError`` the
    caller should render via :func:`validation_error_response`. ``None``
    values are stripped first, which keeps every tool's optional-argument
    convention working: a Python default of ``None`` means "argument not
    supplied", not "argument supplied as null". A schema can still mark a
    property nullable when accepting an explicit null is the contract.
    """
    cleaned = {k: v for k, v in params.items() if v is not None}
    result = validate_tool_call(tool_name, cleaned)
    if isinstance(result, ValidatedPayload):
        return None
    return result


def validation_error_response(err: ValidationError) -> str:
    """Render a validation failure as the JSON string an MCP tool returns.

    Carries the full structured error so MCP clients can show users which
    field failed and why, without leaking server internals.
    """
    payload = {"error": err.message, "jsonrpc_error": to_jsonrpc_error(err)}
    return json.dumps(payload)


def to_jsonrpc_error(err: ValidationError) -> dict[str, Any]:
    """Render a ``ValidationError`` as a JSON-RPC 2.0 ``error`` object.

    The shape matches the spec: ``{code, message, data}`` where ``data``
    carries the structured per-violation list. Callers wrap this in a full
    JSON-RPC envelope (with ``jsonrpc: "2.0"`` and the request id).
    """
    return {
        "code": err.code,
        "message": err.message,
        "data": {
            "tool": err.tool_name,
            "errors": err.errors.copy(),
        },
    }
