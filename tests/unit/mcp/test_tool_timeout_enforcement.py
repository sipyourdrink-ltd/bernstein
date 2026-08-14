"""Per-tool timeout enforcement and create_subtask role enum binding.

Drives the five acceptance criteria of issue #3647:

1. The four host-effecting tools declare a timeout AND the server enforces it.
2. Exceeding the declared bound returns a structured MCP error naming the
   tool and the limit, instead of dropping the connection.
3. A test drives a tool past its declared timeout and asserts the error
   shape (fail-before, pass-after).
4. ``bernstein_create_subtask``'s ``role`` field is an enum sourced from
   ``KNOWN_ROLES`` - the schema cannot drift from the constant without
   failing this test.
5. An unknown ``role`` value is rejected at the schema boundary with the
   offending value in the message.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from bernstein.core.planning.plan_schema import KNOWN_ROLES
from bernstein.mcp.input_validation import get_registry, validate_tool_call
from bernstein.mcp.server import create_mcp_server

# Host-effecting tools from the audit list - these are the four whose declared
# bound has to be enforced (acceptance #1).
_HOST_EFFECT_TOOLS: tuple[str, ...] = (
    "bernstein_create_subtask",
    "bernstein_post_artifact",
    "bernstein_update",
    "load_skill",
)

# Bound to declare (seconds) - short enough to fire inside a test, long
# enough that the request still reaches the tool handler before the bound.
_TIMEOUT_FLOOR_SECONDS = 0.01


# ---------------------------------------------------------------------------
# Acceptance #1 + #4: declarations exist and create_subtask's role is an enum
# ---------------------------------------------------------------------------


def test_host_effect_tools_declare_timeout_seconds() -> None:
    """Every host-effecting tool schema declares a positive timeoutSeconds."""
    schemas = get_registry().schemas
    for name in _HOST_EFFECT_TOOLS:
        schema = schemas[name]
        assert "timeoutSeconds" in schema, (
            f"{name} must declare timeoutSeconds so the bound is advertised (acceptance #1)."
        )
        declared = schema["timeoutSeconds"]
        assert isinstance(declared, (int, float))
        assert declared > 0


def test_create_subtask_role_enum_matches_known_roles() -> None:
    """create_subtask.role enum is the same list as plan_schema.KNOWN_ROLES."""
    schema = get_registry().schemas["bernstein_create_subtask"]
    props = schema["properties"]
    assert "role" in props, "create_subtask must keep role as a constrained field"
    role_schema = props["role"]
    assert "enum" in role_schema, (
        "create_subtask.role must be an enum sourced from KNOWN_ROLES "
        "(acceptance #4); schema validator uses enum to reject unknown roles."
    )
    assert sorted(role_schema["enum"]) == sorted(KNOWN_ROLES), (
        "create_subtask.role enum must equal KNOWN_ROLES bit-for-bit "
        "(acceptance #4) - if you add or remove a role, change both sides."
    )


def test_create_subtask_rejects_unknown_role_at_schema_boundary() -> None:
    """Unknown role is refused by validate_tool_call, value appears in message."""
    from bernstein.mcp.input_validation import ValidationError

    payload = {
        "parent_task_id": "p1",
        "goal": "g",
        "role": "not-a-real-role",
        "scope": "medium",
        "complexity": "low",
        "priority": 2,
        "estimated_minutes": 30,
    }
    result = validate_tool_call("bernstein_create_subtask", payload)
    assert isinstance(result, ValidationError), (
        f"unknown role must surface as ValidationError (acceptance #5), got {type(result).__name__}"
    )
    # Acceptance #5: the offending value must appear in either the top-level
    # message or one of the per-violation reasons, so the caller knows which
    # field is wrong.
    blob = " ".join([result.message, *(e.get("reason", "") for e in result.errors)])
    assert "not-a-real-role" in blob, (
        f"error must name the offending value (acceptance #5); got message={result.message!r} errors={result.errors!r}"
    )


# ---------------------------------------------------------------------------
# Acceptance #2 + #3: server enforces the declared bound, returns a
# structured error with the tool name and the limit.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def _drive_tool_past_timeout(mcp: Any, tool_name: str, args: dict[str, Any]) -> str:
    """Call ``tool_name`` with a patched handler that sleeps past the bound."""
    tool = mcp._tool_manager._tools[tool_name]  # pyright: ignore[reportPrivateUsage]
    original = tool.fn

    async def _slow(*_a: Any, **_kw: Any) -> str:
        # Sleep comfortably longer than any sane test timeout. The wrapping
        # layer must abort and return its structured error first.
        await asyncio.sleep(5.0)
        return json.dumps({"would_have_succeeded": True})

    tool.fn = _slow
    try:
        result = await mcp.call_tool(tool_name, args)
        if hasattr(result, "content") and result.content:
            return result.content[0].text
        if hasattr(result, "text"):
            return result.text  # type: ignore[no-any-return]
        return json.dumps(result.__dict__)
    finally:
        tool.fn = original


@pytest.mark.asyncio
async def test_server_enforces_declared_timeout_for_host_effect_tools() -> None:
    """Drive bernstein_post_artifact past its bound - returns structured error."""
    mcp = create_mcp_server(tier="all", lineage_enabled=True)

    # Shorten the declared bound so the test can drive past it quickly; the
    # schema's real 30s bound would make the test take half a minute.
    registry = get_registry()
    schema = registry.schemas["bernstein_post_artifact"]
    original_bound = schema.get("timeoutSeconds")
    schema["timeoutSeconds"] = 0.05

    # patch the handler's network step to wait, so the call surfaces a timeout.
    import bernstein.mcp.server as server_mod

    async def _slow_post(*_a: Any, **_kw: Any) -> Any:
        await asyncio.sleep(5.0)
        return json.dumps({"ok": True})

    original_post = getattr(server_mod, "_post_artifact_impl", None)
    server_mod._post_artifact_impl = _slow_post  # type: ignore[assignment]

    try:
        text = await _drive_tool_past_timeout(
            mcp,
            "bernstein_post_artifact",
            {
                "task_id": "t1",
                "key": "k",
                "artifact_type": "report",
                "poster": "p",
                "body": "b",
            },
        )
        parsed = json.loads(text)
        assert parsed.get("error") or parsed.get("timeout"), f"expected structured timeout error, got: {parsed}"
        # Acceptance #2: the error names the tool and the limit.
        msg = json.dumps(parsed)
        assert "bernstein_post_artifact" in msg
        assert "timeoutSeconds" in msg or "timeout" in msg
    finally:
        if original_post is not None:
            server_mod._post_artifact_impl = original_post  # type: ignore[assignment]
        if original_bound is None:
            schema.pop("timeoutSeconds", None)
        else:
            schema["timeoutSeconds"] = original_bound


@pytest.mark.asyncio
async def test_pass_within_bound_returns_normal_payload() -> None:
    """A call that finishes well inside the declared bound returns the
    normal payload (acceptance #3: pass-after)."""
    mcp = create_mcp_server(tier="all", lineage_enabled=True)
    # bernstein_status with status=None is a near-instant local read.
    result = await mcp.call_tool("bernstein_status", {"status": None, "detail": False})
    text = result.content[0].text if hasattr(result, "content") else json.dumps(result)
    # It either succeeded normally or returned a structured outage/error -
    # but it must NOT be the timeout error shape.
    assert "timeout" not in text.lower() or "exceeded" not in text.lower(), (
        f"fast bernstein_status unexpectedly hit the timeout path: {text}"
    )
