"""Parity between the advertised MCP ``inputSchema`` and the enforced schema.

``src/bernstein/mcp/tool_schemas/*.json`` is the deny-by-default input
firewall. When the schema a client is shown is looser than the schema the
server enforces, a caller cannot satisfy a constrained argument on its first
call: it sends a plausible value, gets a rejection it had no way to predict,
and burns a turn.

These tests are the drift guard. They fail when enforcement is tightened
without the advertised schema following, which is how the gap appeared in the
first place. :func:`schema_disagreements` is the comparison used by the guard
and is itself covered by negative tests, so the guard is not a test that can
only pass.
"""

from __future__ import annotations

import asyncio
import copy
from functools import cache
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jsonschema
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bernstein.mcp.input_validation import (
    ValidatedPayload,
    get_registry,
    validate_tool_call,
)
from bernstein.mcp.server import create_mcp_server

# Top-level JSON Schema keywords that constrain a payload. Any disagreement on
# one of these between the advertised and the enforced schema is drift.
_CONSTRAINING_KEYWORDS = (
    "type",
    "required",
    "additionalProperties",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
)


@cache
def _advertised_schemas() -> dict[str, dict[str, Any]]:
    """Return ``{tool_name: inputSchema}`` exactly as a client would see it."""
    mcp = create_mcp_server(tier="all", lineage_enabled=True)
    tools = asyncio.run(mcp.list_tools())
    return {tool.name: copy.deepcopy(tool.inputSchema) for tool in tools}


def _enforced_schemas() -> dict[str, dict[str, Any]]:
    """Return ``{tool_name: schema}`` as ``validate_tool_call`` enforces it."""
    return dict(get_registry().schemas)


def schema_disagreements(
    tool_name: str,
    advertised: dict[str, Any],
    enforced: dict[str, Any],
) -> list[str]:
    """Return one message per constrained field the two schemas disagree on.

    An empty list means a caller that satisfies the advertised schema is
    accepted by the validator, and a caller the validator would refuse is
    refused up front by the advertised schema.
    """
    out: list[str] = []
    for keyword in _CONSTRAINING_KEYWORDS:
        adv: Any = advertised.get(keyword)
        enf: Any = enforced.get(keyword)
        if keyword == "required":
            adv = sorted(adv or [])
            enf = sorted(enf or [])
        if adv != enf:
            out.append(f"{tool_name}: top-level '{keyword}' advertised {adv!r}, enforced {enf!r}")

    adv_props: dict[str, Any] = advertised.get("properties", {})
    enf_props: dict[str, Any] = enforced.get("properties", {})
    for name in sorted(set(adv_props) | set(enf_props)):
        if name not in adv_props:
            out.append(f"{tool_name}.{name}: enforced but never advertised")
        elif name not in enf_props:
            out.append(f"{tool_name}.{name}: advertised but not enforced")
        elif adv_props[name] != enf_props[name]:
            out.append(f"{tool_name}.{name}: advertised {adv_props[name]!r}, enforced {enf_props[name]!r}")
    return out


# ---------------------------------------------------------------------------
# Seed payloads: one minimal accepted call per shape a tool supports. Used by
# the property tests to explore the constrained fields.
# ---------------------------------------------------------------------------

_SEEDS: dict[str, list[dict[str, Any]]] = {
    "bernstein_health": [{}],
    "bernstein_status": [{}],
    "bernstein_cost": [{}],
    "bernstein_scenarios": [{}],
    "bernstein_run": [
        {"goal": "ship the thing", "scope": "medium", "complexity": "medium"},
        {"goal": "split work", "parent_task_id": "t-parent"},
    ],
    "bernstein_tasks": [{"status": "open"}],
    "bernstein_task_handle": [{"run_id": "run-1", "workdir": "."}],
    "bernstein_run_status": [{"run_id": "run-1", "workdir": "."}],
    "bernstein_context": [{"task_id": "t-1", "workdir": ".", "verify": True}],
    "bernstein_task_capsule": [{"task_id": "t-1", "workdir": ".", "verify": True}],
    "bernstein_stop": [{"workdir": "."}],
    "bernstein_shutdown_orchestrator": [{"workdir": "."}],
    "bernstein_cancel": [{"task_id": "t-1", "reason": "superseded"}],
    "bernstein_approve": [{"task_id": "t-1", "note": "ok"}],
    "bernstein_complete": [{"task_id": "t-1", "result_summary": "done"}],
    "bernstein_create_subtask": [
        {"parent_task_id": "t-1", "goal": "sub", "role": "backend", "scope": "small", "complexity": "low"},
    ],
    "bernstein_claim": [{"claimer_id": "worker-1", "completed_ids": []}],
    "bernstein_update": [{"task_id": "t-1", "body": "progress", "sender": "worker-1", "kind": "finding"}],
    "bernstein_post_message": [{"task_id": "t-1", "body": "progress", "sender": "worker-1", "kind": "finding"}],
    "bernstein_post_artifact": [
        {"task_id": "t-1", "key": "k1", "artifact_type": "report", "poster": "w", "body": "# hi"},
        {
            "task_id": "t-1",
            "key": "k1",
            "artifact_type": "table",
            "poster": "w",
            "columns": ["a"],
            "rows": [["1"]],
        },
        {
            "task_id": "t-1",
            "key": "k1",
            "artifact_type": "link",
            "poster": "w",
            "url": "https://example.invalid/x",
            "link_kind": "preview",
        },
    ],
    "load_skill": [{"name": "backend"}],
    "bernstein_scenario": [
        {"scenario_id": "pr-review", "context": "ctx"},
        {"action": "list"},
        {"action": "run", "scenario_id": "pr-review", "context": "ctx"},
        {"action": "status", "orchestration_id": "orch-1"},
    ],
    "bernstein_scenario_status": [{"orchestration_id": "orch-1"}],
    "verify_chain": [{"artefact_path": "src/bernstein/mcp/server.py"}],
    "bernstein_verify_lineage": [{"artefact_path": "src/bernstein/mcp/server.py"}],
}


#: Fields whose value selects which conditional branch a seed satisfies. They
#: are not redrawn, because swapping one invalidates the rest of the seed. Each
#: of their enum values gets its own seed above instead.
_DISCRIMINATOR_FIELDS: dict[str, frozenset[str]] = {
    "bernstein_post_artifact": frozenset({"artifact_type"}),
    "bernstein_scenario": frozenset({"action"}),
}


def _enum_fields(tool_name: str, schema: dict[str, Any], payload: dict[str, Any]) -> dict[str, list[Any]]:
    """Return the enum-constrained properties of ``schema`` present in ``payload``."""
    props: dict[str, Any] = schema.get("properties", {})
    pinned = _DISCRIMINATOR_FIELDS.get(tool_name, frozenset())
    return {name: list(props[name]["enum"]) for name in payload if name not in pinned and "enum" in props.get(name, {})}


# ---------------------------------------------------------------------------
# The registration invariant.
# ---------------------------------------------------------------------------


def test_schema_files_and_registered_tools_are_the_same_set() -> None:
    """Every registered tool has a schema file and every schema file a tool."""
    advertised = set(_advertised_schemas())
    enforced = set(_enforced_schemas())
    assert advertised - enforced == set(), "registered tools with no schema file"
    assert enforced - advertised == set(), "schema files with no registered tool"


def test_seeds_cover_every_registered_tool() -> None:
    """A new tool must bring a seed payload, or the property tests skip it."""
    assert set(_SEEDS) == set(_advertised_schemas())


# ---------------------------------------------------------------------------
# The drift guard.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", sorted(_SEEDS))
def test_advertised_schema_is_the_enforced_schema(tool_name: str) -> None:
    """What a client is shown is what ``validate_tool_call`` applies."""
    advertised = _advertised_schemas()[tool_name]
    enforced = _enforced_schemas()[tool_name]
    problems = schema_disagreements(tool_name, advertised, enforced)
    assert problems == [], "advertised/enforced schema drift:\n" + "\n".join(problems)


def test_drift_guard_catches_a_loosened_advertised_field() -> None:
    """Dropping an enum from the advertised copy must be reported."""
    enforced = _enforced_schemas()["bernstein_run"]
    loosened = copy.deepcopy(enforced)
    loosened["properties"]["scope"] = {"type": "string"}
    problems = schema_disagreements("bernstein_run", loosened, enforced)
    assert any("bernstein_run.scope" in p for p in problems), problems


def test_drift_guard_catches_tightened_enforcement() -> None:
    """Tightening only the enforced copy must be reported."""
    advertised = _advertised_schemas()["bernstein_approve"]
    tightened = copy.deepcopy(advertised)
    tightened["required"] = ["task_id", "note"]
    tightened["properties"]["note"] = {"type": "string", "enum": ["approved"]}
    problems = schema_disagreements("bernstein_approve", advertised, tightened)
    assert any("'required'" in p for p in problems), problems
    assert any("bernstein_approve.note" in p for p in problems), problems


def test_post_artifact_advertises_its_conditional_shape() -> None:
    """A caller can see that report/table/link each need different fields."""
    advertised = _advertised_schemas()["bernstein_post_artifact"]
    conditionals = advertised["allOf"]
    required_by_type = {
        branch["if"]["properties"]["artifact_type"]["const"]: sorted(branch["then"]["required"])
        for branch in conditionals
    }
    assert required_by_type == {
        "report": ["body"],
        "table": ["columns", "rows"],
        "link": ["link_kind", "url"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", _SEEDS["bernstein_post_artifact"], ids=["report", "table", "link"])
async def test_a_call_matching_the_advertised_shape_reaches_the_task_server(seed: dict[str, Any]) -> None:
    """Filling the advertised conditional shape must not be refused up front.

    The handler defaults the fields of the other two artifact types to the
    empty string. Those defaults are absent from the advertised shape, so they
    must not be validated as if the caller had supplied them.
    """
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"key": seed["key"], "version": 1})

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    mcp = create_mcp_server(server_url="http://localhost:8052")
    with patch("bernstein.mcp.server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp.call_tool("bernstein_post_artifact", dict(seed))

    text = result[0][0].text  # type: ignore[index]
    assert "jsonrpc_error" not in text, text
    mock_client.post.assert_awaited_once()
    posted = mock_client.post.call_args.kwargs["json"]
    assert posted == {k: v for k, v in seed.items() if k != "task_id"}


def test_verify_chain_is_behind_the_input_firewall() -> None:
    """``verify_chain`` used to have no schema, so it bypassed validation."""
    assert "verify_chain" in _enforced_schemas()
    ok = validate_tool_call("verify_chain", {"artefact_path": "src/a.py"})
    assert isinstance(ok, ValidatedPayload)
    bad = validate_tool_call("verify_chain", {"artefact_path": 5})
    assert not isinstance(bad, ValidatedPayload)


# ---------------------------------------------------------------------------
# Property tests: advertised-valid is accepted, enforced-invalid is visible.
# ---------------------------------------------------------------------------


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_payloads_valid_under_the_advertised_schema_are_accepted(data: st.DataObject) -> None:
    """Anything the advertised schema permits, the validator must accept."""
    for tool_name, advertised in _advertised_schemas().items():
        for seed in _SEEDS[tool_name]:
            payload = dict(seed)
            for field, choices in _enum_fields(tool_name, advertised, seed).items():
                payload[field] = data.draw(st.sampled_from(choices), label=f"{tool_name}.{field}")
            # The server strips ``None`` args before validating, so a client
            # that picks the null branch of a nullable enum sends nothing.
            payload = {k: v for k, v in payload.items() if v is not None}
            jsonschema.Draft7Validator(advertised).validate(payload)
            result = validate_tool_call(tool_name, payload)
            assert isinstance(result, ValidatedPayload), (tool_name, payload, result)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(bad=st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=122), min_size=1, max_size=12))
def test_payloads_the_validator_rejects_are_rejected_by_the_advertised_schema(bad: str) -> None:
    """Anything the validator refuses, the advertised schema refuses too."""
    enforced_all = _enforced_schemas()
    for tool_name, advertised in _advertised_schemas().items():
        enforced = enforced_all[tool_name]
        for seed in _SEEDS[tool_name]:
            for field, choices in _enum_fields(tool_name, enforced, seed).items():
                if bad in choices:
                    continue
                payload = {**seed, field: bad}
                enforced_ok = not list(jsonschema.Draft7Validator(enforced).iter_errors(payload))
                advertised_ok = not list(jsonschema.Draft7Validator(advertised).iter_errors(payload))
                assert enforced_ok is False, (tool_name, field, bad)
                assert advertised_ok is enforced_ok, (tool_name, field, bad)
