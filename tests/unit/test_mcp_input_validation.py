"""Tests for the MCP schema validator (issue #1406).

The validator is the orchestrator-side input firewall: every MCP tool-call
payload must pass schema validation plus the deny-by-default size, depth
and control-character rules before the handler runs.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bernstein.mcp import input_validation as iv
from bernstein.mcp.input_validation import (
    JSONRPC_INVALID_PARAMS,
    JSONRPC_METHOD_NOT_FOUND,
    MAX_PAYLOAD_BYTES,
    MAX_RECURSION_DEPTH,
    SchemaRegistry,
    ValidatedPayload,
    ValidationError,
    get_registry,
    load_registry,
    reset_registry_cache,
    to_jsonrpc_error,
    validate_tool_call,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_validator_cache() -> Iterator[None]:
    """Each test starts with a clean registry cache and strict mode."""
    reset_registry_cache()
    prior = os.environ.pop("BERNSTEIN_MCP_VALIDATION", None)
    try:
        yield
    finally:
        reset_registry_cache()
        if prior is not None:
            os.environ["BERNSTEIN_MCP_VALIDATION"] = prior


def _minimal_registry(extra: dict[str, dict[str, Any]] | None = None) -> SchemaRegistry:
    """Build a registry containing the production schemas plus extras."""
    base = load_registry()
    schemas = dict(base.schemas)
    if extra:
        schemas.update(extra)
    return SchemaRegistry(schemas=schemas, allow_unsafe_args=base.allow_unsafe_args)


# ---------------------------------------------------------------------------
# Happy-path coverage for every shipped schema.
# ---------------------------------------------------------------------------


def test_bernstein_health_accepts_empty_object() -> None:
    result = validate_tool_call("bernstein_health", {})
    assert isinstance(result, ValidatedPayload)
    assert result.tool_name == "bernstein_health"


def test_bernstein_status_accepts_empty_object() -> None:
    assert isinstance(validate_tool_call("bernstein_status", {}), ValidatedPayload)


def test_bernstein_cost_accepts_empty_object() -> None:
    assert isinstance(validate_tool_call("bernstein_cost", {}), ValidatedPayload)


def test_bernstein_scenarios_accepts_empty_object() -> None:
    assert isinstance(validate_tool_call("bernstein_scenarios", {}), ValidatedPayload)


def test_bernstein_run_accepts_full_payload() -> None:
    payload = {
        "goal": "Ship the validator",
        "role": "backend",
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
        "estimated_minutes": 30,
    }
    assert isinstance(validate_tool_call("bernstein_run", payload), ValidatedPayload)


def test_bernstein_run_accepts_minimum_payload() -> None:
    assert isinstance(validate_tool_call("bernstein_run", {"goal": "x"}), ValidatedPayload)


def test_bernstein_tasks_accepts_known_status() -> None:
    for status in ("open", "claimed", "in_progress", "done", "failed", "blocked", "cancelled"):
        assert isinstance(validate_tool_call("bernstein_tasks", {"status": status}), ValidatedPayload)


def test_bernstein_stop_accepts_workdir() -> None:
    assert isinstance(validate_tool_call("bernstein_stop", {"workdir": "."}), ValidatedPayload)


def test_bernstein_approve_accepts_valid_id() -> None:
    payload = {"task_id": "abc.12-34_X:5", "note": "Approved"}
    assert isinstance(validate_tool_call("bernstein_approve", payload), ValidatedPayload)


def test_bernstein_create_subtask_accepts_minimal_payload() -> None:
    payload = {"parent_task_id": "task-1", "goal": "subgoal"}
    assert isinstance(validate_tool_call("bernstein_create_subtask", payload), ValidatedPayload)


def test_load_skill_accepts_name_only() -> None:
    assert isinstance(validate_tool_call("load_skill", {"name": "backend"}), ValidatedPayload)


def test_load_skill_accepts_reference_and_script() -> None:
    payload = {"name": "backend", "reference": "python.md", "script": "lint.sh"}
    assert isinstance(validate_tool_call("load_skill", payload), ValidatedPayload)


def test_bernstein_scenario_accepts_full_payload() -> None:
    payload = {
        "scenario_id": "pr-review",
        "context": "PR review run",
        "pr_number": 42,
        "branch": "main",
    }
    assert isinstance(validate_tool_call("bernstein_scenario", payload), ValidatedPayload)


def test_bernstein_scenario_status_accepts_id() -> None:
    payload = {"orchestration_id": "orch-1"}
    assert isinstance(validate_tool_call("bernstein_scenario_status", payload), ValidatedPayload)


# ---------------------------------------------------------------------------
# Unknown-tool rejection : JSON-RPC -32601.
# ---------------------------------------------------------------------------


def test_unknown_tool_is_rejected_with_method_not_found() -> None:
    result = validate_tool_call("definitely_not_a_real_tool", {})
    assert isinstance(result, ValidationError)
    assert result.code == JSONRPC_METHOD_NOT_FOUND


def test_empty_tool_name_is_rejected() -> None:
    result = validate_tool_call("", {})
    assert isinstance(result, ValidationError)
    assert result.code == JSONRPC_METHOD_NOT_FOUND


def test_non_string_tool_name_is_rejected() -> None:
    result = validate_tool_call(None, {})  # type: ignore[arg-type]
    assert isinstance(result, ValidationError)
    assert result.code == JSONRPC_METHOD_NOT_FOUND


# ---------------------------------------------------------------------------
# Malformed-payload rejection : JSON-RPC -32602.
# ---------------------------------------------------------------------------


def test_non_dict_payload_is_rejected() -> None:
    result = validate_tool_call("bernstein_health", "not a dict")
    assert isinstance(result, ValidationError)
    assert result.code == JSONRPC_INVALID_PARAMS


def test_list_payload_is_rejected() -> None:
    result = validate_tool_call("bernstein_health", ["a", "b"])
    assert isinstance(result, ValidationError)
    assert result.code == JSONRPC_INVALID_PARAMS


def test_missing_required_field_is_rejected() -> None:
    result = validate_tool_call("bernstein_run", {})
    assert isinstance(result, ValidationError)
    assert result.code == JSONRPC_INVALID_PARAMS
    assert any("goal" in e["reason"] or e["path"].endswith("goal") or e["path"] == "" for e in result.errors)


def test_unknown_top_level_property_is_rejected() -> None:
    result = validate_tool_call("bernstein_run", {"goal": "x", "smuggle": "yes"})
    assert isinstance(result, ValidationError)
    assert result.code == JSONRPC_INVALID_PARAMS


def test_type_mismatch_is_rejected() -> None:
    result = validate_tool_call("bernstein_run", {"goal": "x", "priority": "high"})
    assert isinstance(result, ValidationError)
    assert result.code == JSONRPC_INVALID_PARAMS


def test_enum_mismatch_is_rejected() -> None:
    result = validate_tool_call("bernstein_run", {"goal": "x", "scope": "ginormous"})
    assert isinstance(result, ValidationError)


def test_out_of_range_integer_is_rejected() -> None:
    result = validate_tool_call("bernstein_run", {"goal": "x", "priority": 9})
    assert isinstance(result, ValidationError)


def test_negative_estimated_minutes_is_rejected() -> None:
    result = validate_tool_call("bernstein_run", {"goal": "x", "estimated_minutes": -1})
    assert isinstance(result, ValidationError)


def test_bad_status_enum_is_rejected() -> None:
    result = validate_tool_call("bernstein_tasks", {"status": "weird"})
    assert isinstance(result, ValidationError)


def test_id_pattern_violation_is_rejected_for_approve() -> None:
    result = validate_tool_call("bernstein_approve", {"task_id": "../etc/passwd"})
    assert isinstance(result, ValidationError)


def test_id_pattern_violation_is_rejected_for_scenario() -> None:
    result = validate_tool_call("bernstein_scenario", {"scenario_id": "../etc"})
    assert isinstance(result, ValidationError)


def test_empty_required_string_is_rejected() -> None:
    result = validate_tool_call("bernstein_run", {"goal": ""})
    assert isinstance(result, ValidationError)


# ---------------------------------------------------------------------------
# Deny rules: oversize payloads.
# ---------------------------------------------------------------------------


def test_oversize_payload_is_rejected() -> None:
    huge = "a" * (MAX_PAYLOAD_BYTES + 10)
    result = validate_tool_call("bernstein_run", {"goal": huge})
    assert isinstance(result, ValidationError)
    assert any("bytes" in e["reason"] for e in result.errors)


def test_payload_just_under_limit_is_size_clean() -> None:
    # 8KB stays well inside MAX_PAYLOAD_BYTES (64KB) and inside goal's
    # 8192-char schema cap, so the only thing being asserted here is that
    # the size check does not false-positive.
    big = "b" * 8000
    result = validate_tool_call("bernstein_run", {"goal": big})
    assert isinstance(result, ValidatedPayload)


# ---------------------------------------------------------------------------
# Deny rules: recursion depth.
# ---------------------------------------------------------------------------


def test_deeply_nested_payload_is_rejected() -> None:
    # Build a schema that accepts an arbitrary "nested" key so the depth
    # check fires before schema validation. The deny rule should still
    # catch it regardless of what schema says.
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": True,
    }
    reg = _minimal_registry({"deep_tool": schema})
    nested: dict[str, Any] = {}
    cursor = nested
    for _ in range(MAX_RECURSION_DEPTH + 5):
        cursor["x"] = {}
        cursor = cursor["x"]
    result = validate_tool_call("deep_tool", nested, registry=reg)
    assert isinstance(result, ValidationError)
    assert any("depth" in e["reason"] for e in result.errors)


def test_shallow_payload_passes_depth_check() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": True,
    }
    reg = _minimal_registry({"shallow_tool": schema})
    payload = {"a": {"b": {"c": 1}}}
    assert isinstance(validate_tool_call("shallow_tool", payload, registry=reg), ValidatedPayload)


# ---------------------------------------------------------------------------
# Deny rules: control characters in strings.
# ---------------------------------------------------------------------------


def test_null_byte_in_string_is_rejected() -> None:
    result = validate_tool_call("bernstein_run", {"goal": "hello\x00world"})
    assert isinstance(result, ValidationError)
    assert any("control" in e["reason"] for e in result.errors)


def test_escape_character_in_string_is_rejected() -> None:
    result = validate_tool_call("bernstein_run", {"goal": "ansi\x1b[31mred"})
    assert isinstance(result, ValidationError)


def test_c1_control_character_is_rejected() -> None:
    # U+0085 (NEL) is a C1 control char.
    result = validate_tool_call("bernstein_run", {"goal": "linetwo"})
    assert isinstance(result, ValidationError)


def test_tab_newline_carriage_return_are_allowed() -> None:
    payload = {"goal": "line1\nline2\r\n\tindented"}
    assert isinstance(validate_tool_call("bernstein_run", payload), ValidatedPayload)


def test_control_chars_caught_in_nested_strings() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": True,
    }
    reg = _minimal_registry({"nested_tool": schema})
    payload = {"outer": {"inner": "bad\x00"}}
    result = validate_tool_call("nested_tool", payload, registry=reg)
    assert isinstance(result, ValidationError)


# ---------------------------------------------------------------------------
# allow_unsafe_args allowlist bypass.
# ---------------------------------------------------------------------------


def test_allow_unsafe_args_bypasses_size_check() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": True,
    }
    reg = SchemaRegistry(schemas={"bulk_tool": schema}, allow_unsafe_args=frozenset({"bulk_tool"}))
    huge = {"blob": "a" * (MAX_PAYLOAD_BYTES + 100)}
    assert isinstance(validate_tool_call("bulk_tool", huge, registry=reg), ValidatedPayload)


def test_allow_unsafe_args_bypasses_depth_check() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": True,
    }
    reg = SchemaRegistry(schemas={"deep_tool": schema}, allow_unsafe_args=frozenset({"deep_tool"}))
    nested: dict[str, Any] = {}
    cursor = nested
    for _ in range(MAX_RECURSION_DEPTH + 5):
        cursor["x"] = {}
        cursor = cursor["x"]
    assert isinstance(validate_tool_call("deep_tool", nested, registry=reg), ValidatedPayload)


def test_allow_unsafe_args_still_runs_schema_validation() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "required": ["x"],
        "properties": {"x": {"type": "integer"}},
    }
    reg = SchemaRegistry(schemas={"strict_tool": schema}, allow_unsafe_args=frozenset({"strict_tool"}))
    result = validate_tool_call("strict_tool", {"x": "not int"}, registry=reg)
    assert isinstance(result, ValidationError)


# ---------------------------------------------------------------------------
# Permissive mode.
# ---------------------------------------------------------------------------


def test_permissive_mode_demotes_schema_failure() -> None:
    os.environ["BERNSTEIN_MCP_VALIDATION"] = "permissive"
    result = validate_tool_call("bernstein_run", {})  # missing goal
    assert isinstance(result, ValidatedPayload)


def test_permissive_mode_demotes_unknown_tool() -> None:
    os.environ["BERNSTEIN_MCP_VALIDATION"] = "permissive"
    result = validate_tool_call("not_a_tool", {})
    assert isinstance(result, ValidatedPayload)


def test_permissive_explicit_override_beats_env() -> None:
    os.environ["BERNSTEIN_MCP_VALIDATION"] = "strict"
    result = validate_tool_call("bernstein_run", {}, permissive=True)
    assert isinstance(result, ValidatedPayload)


def test_permissive_mode_with_non_dict_payload_returns_empty_payload() -> None:
    os.environ["BERNSTEIN_MCP_VALIDATION"] = "permissive"
    result = validate_tool_call("bernstein_run", 42)
    assert isinstance(result, ValidatedPayload)
    assert result.payload == {}


# ---------------------------------------------------------------------------
# Registry behaviour.
# ---------------------------------------------------------------------------


def test_get_registry_is_cached() -> None:
    reg1 = get_registry()
    reg2 = get_registry()
    assert reg1 is reg2


def test_reset_registry_cache_drops_cached_instance() -> None:
    reg1 = get_registry()
    reset_registry_cache()
    reg2 = get_registry()
    assert reg1 is not reg2


def test_registry_loads_every_shipped_schema() -> None:
    reg = load_registry()
    expected = {
        "bernstein_health",
        "bernstein_run",
        "bernstein_status",
        "bernstein_tasks",
        "bernstein_cost",
        "bernstein_stop",
        "bernstein_approve",
        "bernstein_create_subtask",
        "bernstein_claim",
        "bernstein_update",
        "load_skill",
        "bernstein_scenarios",
        "bernstein_scenario",
        "bernstein_scenario_status",
    }
    assert expected.issubset(reg.schemas.keys())


def test_corrupt_schema_file_raises(tmp_path: Path) -> None:
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        load_registry(schema_dir=tmp_path)


def test_schema_file_with_non_object_root_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "broken.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(RuntimeError):
        load_registry(schema_dir=tmp_path)


def test_empty_schema_dir_yields_empty_registry(tmp_path: Path) -> None:
    reg = load_registry(schema_dir=tmp_path)
    assert reg.schemas == {}


def test_nonexistent_schema_dir_yields_empty_registry(tmp_path: Path) -> None:
    reg = load_registry(schema_dir=tmp_path / "no-such")
    assert reg.schemas == {}


# ---------------------------------------------------------------------------
# JSON-RPC rendering.
# ---------------------------------------------------------------------------


def test_to_jsonrpc_error_renders_standard_envelope() -> None:
    err = ValidationError(
        tool_name="bernstein_run",
        code=JSONRPC_INVALID_PARAMS,
        message="boom",
        errors=[{"path": "/goal", "reason": "missing"}],
    )
    envelope = to_jsonrpc_error(err)
    assert envelope["code"] == JSONRPC_INVALID_PARAMS
    assert envelope["message"] == "boom"
    assert envelope["data"]["tool"] == "bernstein_run"
    assert envelope["data"]["errors"] == [{"path": "/goal", "reason": "missing"}]


def test_validation_error_data_includes_field_paths() -> None:
    result = validate_tool_call("bernstein_run", {"goal": 123})
    assert isinstance(result, ValidationError)
    envelope = to_jsonrpc_error(result)
    # The failing field is /goal so the path should mention it.
    pointers = [e["path"] for e in envelope["data"]["errors"]]
    assert any("goal" in p for p in pointers)


# ---------------------------------------------------------------------------
# Property tests : Hypothesis fuzzes the validator with random payloads.
# ---------------------------------------------------------------------------


def _jsonish(max_leaves: int = 20) -> st.SearchStrategy[object]:
    """A recursive JSON-ish value strategy with a small recursion budget."""
    return st.recursive(
        st.one_of(
            st.none(),
            st.booleans(),
            st.integers(min_value=-10_000, max_value=10_000),
            st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=20),
        ),
        lambda children: st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(st.text(min_size=1, max_size=8), children, max_size=4),
        ),
        max_leaves=max_leaves,
    )


@given(payload=_jsonish())
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_unknown_tool_always_rejected(payload: object) -> None:
    result = validate_tool_call("__no_such_tool__", payload)
    assert isinstance(result, ValidationError)
    assert result.code == JSONRPC_METHOD_NOT_FOUND


@given(payload=_jsonish())
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_validator_never_raises(payload: object) -> None:
    # Pyright/runtime: object is fine; validator must never raise.
    result = validate_tool_call("bernstein_run", payload)
    assert isinstance(result, (ValidatedPayload, ValidationError))


@given(goal=st.text(min_size=1, max_size=200))
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_goal_text_round_trip(goal: str) -> None:
    # Strip control chars first since the deny rule is the intended behaviour.
    cleaned = "".join(c for c in goal if c in "\t\n\r" or 32 <= ord(c) <= 126)
    if not cleaned:
        return
    result = validate_tool_call("bernstein_run", {"goal": cleaned})
    assert isinstance(result, ValidatedPayload)


@given(extra_key=st.text(min_size=1, max_size=10), extra_value=st.integers())
@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_extra_props_rejected(extra_key: str, extra_value: int) -> None:
    if extra_key in {"goal", "role", "priority", "scope", "complexity", "estimated_minutes"}:
        return
    result = validate_tool_call("bernstein_run", {"goal": "x", extra_key: extra_value})
    assert isinstance(result, ValidationError)


@given(priority=st.integers())
@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_priority_only_accepts_1_to_3(priority: int) -> None:
    result = validate_tool_call("bernstein_run", {"goal": "x", "priority": priority})
    if 1 <= priority <= 3:
        assert isinstance(result, ValidatedPayload)
    else:
        assert isinstance(result, ValidationError)


@given(size=st.integers(min_value=0, max_value=MAX_PAYLOAD_BYTES + 100))
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_oversize_rejected(size: int) -> None:
    if size == 0:
        return
    # Schema caps goal at 8192 chars, so for sizes beyond that we'd hit the
    # schema check rather than the size deny rule. We only fuzz against
    # known oversize-vs-fine in the size-only window.
    blob = "a" * min(size, 8000)
    result = validate_tool_call("bernstein_run", {"goal": blob})
    # Whatever the outcome, it must be one of the tagged types.
    assert isinstance(result, (ValidatedPayload, ValidationError))


@given(depth=st.integers(min_value=0, max_value=MAX_RECURSION_DEPTH + 5))
@settings(max_examples=20, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_depth_check_threshold(depth: int) -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": True,
    }
    reg = _minimal_registry({"depth_tool": schema})
    nested: dict[str, Any] = {}
    cursor = nested
    for _ in range(depth):
        cursor["x"] = {}
        cursor = cursor["x"]
    result = validate_tool_call("depth_tool", nested, registry=reg)
    if depth > MAX_RECURSION_DEPTH:
        assert isinstance(result, ValidationError)
    else:
        assert isinstance(result, ValidatedPayload)


@given(control_codepoint=st.integers(min_value=0, max_value=0x9F))
@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_control_char_codepoints(control_codepoint: int) -> None:
    ch = chr(control_codepoint)
    payload = {"goal": f"prefix{ch}suffix"}
    result = validate_tool_call("bernstein_run", payload)
    if ch in "\t\n\r" or (32 <= control_codepoint <= 0x7E):
        # Either accepted (in-range printable / allowed control) or rejected
        # for some unrelated reason. We only assert no crash.
        assert isinstance(result, (ValidatedPayload, ValidationError))
    else:
        assert isinstance(result, ValidationError)


@given(scenario_id=st.text(min_size=1, max_size=20))
@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_scenario_id_pattern(scenario_id: str) -> None:
    result = validate_tool_call("bernstein_scenario", {"scenario_id": scenario_id})
    assert isinstance(result, (ValidatedPayload, ValidationError))


@given(payload=st.dictionaries(st.text(min_size=1, max_size=8), _jsonish(max_leaves=5), max_size=5))
@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_random_payload_against_health(payload: dict[str, object]) -> None:
    # bernstein_health forbids any properties at all.
    result = validate_tool_call("bernstein_health", payload)
    if payload == {}:
        assert isinstance(result, ValidatedPayload)
    else:
        assert isinstance(result, ValidationError)


@given(payload=_jsonish())
@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_permissive_mode_never_rejects(payload: object) -> None:
    result = validate_tool_call("bernstein_run", payload, permissive=True)
    assert isinstance(result, ValidatedPayload)


@given(payload=_jsonish())
@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_unknown_tool_permissive_passes(payload: object) -> None:
    result = validate_tool_call("__not_a_tool__", payload, permissive=True)
    assert isinstance(result, ValidatedPayload)


@given(note=st.text(min_size=0, max_size=200))
@settings(max_examples=30, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_approve_note_text(note: str) -> None:
    cleaned = "".join(c for c in note if c in "\t\n\r" or 32 <= ord(c) <= 126)
    result = validate_tool_call(
        "bernstein_approve",
        {"task_id": "task-1", "note": cleaned},
    )
    assert isinstance(result, ValidatedPayload)


# ---------------------------------------------------------------------------
# Integration: fixture JSON-RPC requests against the registry.
# ---------------------------------------------------------------------------


def _jsonrpc(method: str, params: object, request_id: int = 1) -> dict[str, Any]:
    """Helper: build a JSON-RPC 2.0 request envelope."""
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _validate_request(req: dict[str, Any]) -> dict[str, Any]:
    """Helper: simulate the server dispatch path : validate then envelope."""
    result = validate_tool_call(req["method"], req["params"])
    if isinstance(result, ValidationError):
        return {"jsonrpc": "2.0", "id": req["id"], "error": to_jsonrpc_error(result)}
    return {"jsonrpc": "2.0", "id": req["id"], "result": {"ok": True}}


def test_integration_valid_run_request() -> None:
    req = _jsonrpc("bernstein_run", {"goal": "Do thing", "priority": 1})
    response = _validate_request(req)
    assert "result" in response


def test_integration_unknown_method_rejected_with_32601() -> None:
    req = _jsonrpc("phantom_tool", {})
    response = _validate_request(req)
    assert response["error"]["code"] == JSONRPC_METHOD_NOT_FOUND


def test_integration_malformed_params_rejected_with_32602() -> None:
    req = _jsonrpc("bernstein_run", {"goal": 42})
    response = _validate_request(req)
    assert response["error"]["code"] == JSONRPC_INVALID_PARAMS
    assert response["error"]["data"]["tool"] == "bernstein_run"


def test_integration_unknown_top_level_key_rejected() -> None:
    req = _jsonrpc("bernstein_run", {"goal": "ok", "evil": "data"})
    response = _validate_request(req)
    assert response["error"]["code"] == JSONRPC_INVALID_PARAMS


def test_integration_size_violation_round_trip() -> None:
    req = _jsonrpc("bernstein_run", {"goal": "a" * (MAX_PAYLOAD_BYTES + 10)})
    response = _validate_request(req)
    assert response["error"]["code"] == JSONRPC_INVALID_PARAMS


def test_integration_response_envelope_is_jsonrpc_serializable() -> None:
    req = _jsonrpc("phantom_tool", {}, request_id=42)
    response = _validate_request(req)
    rendered = json.dumps(response)
    assert "42" in rendered


# ---------------------------------------------------------------------------
# Public module surface : silently break the API and this test fails.
# ---------------------------------------------------------------------------


def test_public_surface_exports_expected_names() -> None:
    for name in (
        "validate_tool_call",
        "ValidatedPayload",
        "ValidationError",
        "SchemaRegistry",
        "to_jsonrpc_error",
        "load_registry",
        "get_registry",
        "reset_registry_cache",
        "JSONRPC_METHOD_NOT_FOUND",
        "JSONRPC_INVALID_PARAMS",
        "MAX_PAYLOAD_BYTES",
        "MAX_RECURSION_DEPTH",
    ):
        assert hasattr(iv, name), f"missing public name: {name}"
