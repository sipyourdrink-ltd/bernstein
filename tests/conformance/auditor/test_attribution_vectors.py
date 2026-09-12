"""Conformance vectors for the four attribution questions (#5058).

Q1  Which principal initiated the run.
Q2  Which agent performed each action.
Q7  Which identity was presented to the tool.
Q14 Which code, config and tool set actually ran.

Q1 and Q2 are answerable from today's bundle: the run receipt journal
carries the initiating principal in ``run_started`` and an ``agent_id``
on every action event.  Q7 and Q14 cannot be answered -- the tool-call
event records no credential and the bundle carries no code/config/toolset
digest -- and each is marked ``xfail(strict=True)`` naming the missing
field and the issue that would add it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conformance.auditor import recorder

if TYPE_CHECKING:
    from tests.conformance.auditor.bundle import BundleReader

# ---------------------------------------------------------------------------
# Q1 -- which principal initiated the run
# ---------------------------------------------------------------------------

#: Journal event kinds that carry the run-initiating principal.
_RUN_START_EVENT = "run_started"

#: Field on run_started that names the initiating principal.
_PRINCIPAL_FIELD = "principal"


@pytest.mark.question(1)
def test_q1_run_receipt_records_the_initiating_principal(bundle_reader: BundleReader) -> None:
    """Q1: which principal initiated the run?

    The ``run_started`` journal event carries a ``principal`` field naming
    the identity that submitted the run.  The bundle alone is sufficient;
    no external source is consulted.
    """
    receipt = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    events = receipt["journal"]["events"]

    start_events = [e for e in events if e.get("event") == _RUN_START_EVENT]
    assert start_events, f"no {_RUN_START_EVENT!r} event found in journal"

    start = start_events[0]
    assert _PRINCIPAL_FIELD in start, (
        f"{_RUN_START_EVENT!r} event is missing the {_PRINCIPAL_FIELD!r} field; "
        f"available keys: {list(start.keys())}"
    )

    principal = start[_PRINCIPAL_FIELD]
    assert isinstance(principal, str) and principal, (
        f"{_PRINCIPAL_FIELD!r} must be a non-empty string; got {principal!r}"
    )


def test_q1_principal_matches_expected_run(bundle_reader: BundleReader) -> None:
    """The initiating principal recorded in the bundle is the expected one."""
    receipt = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    events = receipt["journal"]["events"]
    start = next(e for e in events if e.get("event") == _RUN_START_EVENT)
    assert start[_PRINCIPAL_FIELD] == "user:operator@example.org"


# ---------------------------------------------------------------------------
# Q2 -- which agent performed each action
# ---------------------------------------------------------------------------

#: Journal event kinds that represent actions (excluding run-level bookends).
_ACTION_EVENT_KINDS = frozenset({
    "agent_spawned",
    "tool_called",
    "file_read",
    "model_request",
    "model_response",
    "artifact_written",
})

#: Field on each action event that names the performing agent.
_AGENT_ID_FIELD = "agent_id"


@pytest.mark.question(2)
def test_q2_every_action_event_carries_an_agent_id(bundle_reader: BundleReader) -> None:
    """Q2: which agent performed each action?

    Every action journal event -- ``agent_spawned``, ``tool_called``,
    ``file_read``, ``model_request``, ``model_response``,
    ``artifact_written`` -- carries an ``agent_id`` field naming the
    agent responsible.  The run-level bookends (``run_started``,
    ``run_completed``) are not actions and are excluded.
    """
    receipt = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    events = receipt["journal"]["events"]

    action_events = [e for e in events if e.get("event") in _ACTION_EVENT_KINDS]
    assert action_events, f"no action events found; looked for {sorted(_ACTION_EVENT_KINDS)}"

    missing = [
        f"{e['event']}[{e['index']}]"
        for e in action_events
        if not e.get(_AGENT_ID_FIELD)
    ]
    assert not missing, (
        f"action events without {_AGENT_ID_FIELD!r}: {missing}"
    )


def test_q2_agent_ids_are_non_empty_strings(bundle_reader: BundleReader) -> None:
    """Agent identifiers recorded in the bundle are non-empty strings."""
    receipt = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    events = receipt["journal"]["events"]
    action_events = [e for e in events if e.get("event") in _ACTION_EVENT_KINDS]

    for event in action_events:
        agent_id = event.get(_AGENT_ID_FIELD, "")
        assert isinstance(agent_id, str) and agent_id, (
            f"event {event['event']!r}[{event['index']}] has invalid agent_id: {agent_id!r}"
        )


# ---------------------------------------------------------------------------
# Q7 -- which identity was presented to the tool
# ---------------------------------------------------------------------------

#: Fields any of which would record the credential the agent presented.
_IDENTITY_FIELDS = ("credential_id", "presented_identity", "identity_token_id", "auth_principal")


@pytest.mark.question(7)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "the tool_called event records the calling agent and the server but "
        "carries no credential or presented-identity field; an auditor cannot "
        "determine which identity the agent assumed when calling the tool "
        "without a credential_id or equivalent on the event (#5058)"
    ),
)
def test_q7_tool_called_event_records_presented_identity(bundle_reader: BundleReader) -> None:
    """Q7: which identity was presented to the tool?

    The ``tool_called`` journal event names the tool and MCP server but
    does not record the credential or token the agent presented at the
    tool boundary.  An auditor cannot answer this question from the
    bundle alone.
    """
    receipt = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    events = receipt["journal"]["events"]

    tool_calls = [e for e in events if e.get("event") == "tool_called"]
    assert tool_calls, "no tool_called event found in journal"

    for call in tool_calls:
        details = {**call, **(call.get("details") or {})}
        found = [f for f in _IDENTITY_FIELDS if f in details]
        assert found, (
            f"tool_called event at index {call['index']} carries no presented-identity field; "
            f"looked for {list(_IDENTITY_FIELDS)}"
        )


# ---------------------------------------------------------------------------
# Q14 -- which code, config and tool set actually ran
# ---------------------------------------------------------------------------

#: Fields any of which would identify the code artifact that ran.
_CODE_DIGEST_FIELDS = ("code_digest", "code_sha256", "image_digest", "binary_hash")

#: Fields any of which would identify the configuration that was active.
_CONFIG_DIGEST_FIELDS = ("config_digest", "config_sha256", "config_hash", "playbook_digest")

#: Fields any of which would list the tool set that was available.
_TOOLSET_FIELDS = ("toolset_digest", "toolset_id", "tool_manifest_digest", "mcp_manifest_hash")


@pytest.mark.question(14)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "the bundle carries no code digest, config digest or toolset manifest: "
        "the spine records an artifact hash and a model name for one step, but "
        "neither the agent binary, the playbook config, nor the MCP tool "
        "manifest that was loaded at runtime are recorded in any bundle file; "
        "an auditor cannot determine which version of the code or tool set ran "
        "without these digests (#5058)"
    ),
)
def test_q14_bundle_records_code_config_and_toolset(bundle_reader: BundleReader) -> None:
    """Q14: which code, config and tool set actually ran?

    The bundle would need at least one of: a digest of the agent binary or
    container image, the playbook config that was active, and a manifest
    hash of the MCP tools that were loaded.  None of those appear in the
    run receipt, audit receipt, or bundle index today.
    """
    receipt = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)

    all_fields: dict[str, object] = {}
    all_fields.update(receipt)
    spine_entries = receipt.get("spine", {}).get("entries", [])
    for entry in spine_entries:
        all_fields.update(entry)

    has_code = any(f in all_fields for f in _CODE_DIGEST_FIELDS)
    has_config = any(f in all_fields for f in _CONFIG_DIGEST_FIELDS)
    has_toolset = any(f in all_fields for f in _TOOLSET_FIELDS)

    missing = []
    if not has_code:
        missing.append(f"code digest (looked for {list(_CODE_DIGEST_FIELDS)})")
    if not has_config:
        missing.append(f"config digest (looked for {list(_CONFIG_DIGEST_FIELDS)})")
    if not has_toolset:
        missing.append(f"toolset manifest (looked for {list(_TOOLSET_FIELDS)})")

    assert not missing, f"bundle is missing: {'; '.join(missing)}"
