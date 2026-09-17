"""Attribution questions 1, 2, 7 and 14 (#5058).

Each vector is checked from the exported bundle alone, driven through the
production writers (:mod:`tests.conformance.auditor.recorder`), not from
reading source. Where the answer depends on whether a field is a genuine
schema guarantee rather than a value one caller happened to pass, the
docstring says which was checked and how -- so a later change to the
scenario's scripted payload cannot quietly turn a real gap into a passing
vector.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conformance.auditor import recorder

if TYPE_CHECKING:
    from tests.conformance.auditor.bundle import BundleReader

#: Audit events that record an agent doing something, as opposed to a run
#: lifecycle boundary (``run.started``/``run.completed``). Restated from
#: :data:`tests.conformance.auditor.test_vectors.DECISION_EVENT_TYPES`
#: rather than imported, so this file's own claim about which events count
#: as "an action" does not silently drift if that module's list changes for
#: an unrelated reason.
_ACTION_EVENT_TYPES = ("agent.delegated", "tool.called", "data.read", "model.request", "repo.changed")

#: Fields that would make a tool call's actor a verifiable identity rather
#: than a bare label -- the shape :class:`~bernstein.core.security.toolcall_identity.ToolCallIdentityAttestation`
#: declares (``args_digest``, ``intent_digest``, ``identity_anchor_ref``,
#: ``tool_signing_kid``, among others).
_TOOL_IDENTITY_FIELDS = ("args_digest", "intent_digest", "identity_anchor_ref", "tool_signing_kid")

#: Fields that would bind an agent's identity to a content-addressed digest
#: of the code, config or tool set it actually ran with.
_RUNTIME_DIGEST_FIELDS = ("code_digest", "config_digest", "toolset_digest", "runtime_digest")


@pytest.mark.question(1)
def test_q1_the_journal_names_the_principal_who_started_the_run(bundle_reader: BundleReader) -> None:
    """Q1: which principal initiated the run?

    The run receipt's ``run_started`` event carries a ``principal`` field
    naming the human/service identity that opened the run, distinct in form
    (``user:...``) from every agent id the same journal records. That field
    is real production shape -- :func:`tests.conformance.auditor.recorder._drive_scenario`
    writes it through ``EventJournal.record``, the same generic writer the
    orchestrator itself calls for ``run_started`` -- but the writer accepts
    arbitrary keyword payload, so this vector proves the bundle *can* carry
    the answer, not that every caller populates it. ``orchestrator.py``'s own
    ``run_started`` call passes ``run_id``/``max_agents``/``budget_usd``/
    ``git_sha``/``git_branch``/``config_hash`` and no ``principal`` at all --
    a production run today would answer this question with the same gap
    Q7 and Q14 already xfail below, just for a different field. This vector
    is about the bundle format's evidence for the question, not a claim
    that production always exercises it; do not read it as more than that.
    """
    run = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    started = run["journal"]["events"][0]
    assert started["event"] == "run_started"

    principal = started.get("principal")
    assert isinstance(principal, str) and principal, "run_started carries no principal"

    agent_ids = {event["agent_id"] for event in run["journal"]["events"] if event["event"] == "agent_spawned"}
    assert principal not in agent_ids, "the recorded principal is indistinguishable from an agent id"


@pytest.mark.question(2)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "not every recorded action names the agent that performed it: the "
        "audit chain's data.read event's actor is the tool that did the "
        "read (repo.read_file), not the agent that invoked it, so 'which "
        "agent performed EACH action' has at least one recorded "
        "counterexample in the bundle's own action set (#5058)"
    ),
)
def test_q2_every_recorded_action_names_the_agent_that_performed_it(bundle_reader: BundleReader) -> None:
    """Q2: which agent performed each action?

    ``agent_spawned`` journal events reliably name the agent that exists --
    :func:`test_agent_spawned_events_reliably_name_a_real_agent` pins that
    half, and it is real: ``orchestrator.py``'s ``_record_spawned_events``
    passes ``agent_id=session.id`` unconditionally for every spawn. What
    does not hold is the stronger claim this question actually asks: that
    every recorded *action* (not just every agent's existence) names its
    actor. The audit chain's ``data.read`` event is the counterexample
    already in the bundle -- its actor is the tool, not the agent that
    called it.
    """
    audit = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    run = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    agent_ids = {event["agent_id"] for event in run["journal"]["events"] if event["event"] == "agent_spawned"}

    actions = [event for event in audit["events"] if event["event_type"] in _ACTION_EVENT_TYPES]
    assert actions, f"the recording holds no action to attribute; expected {list(_ACTION_EVENT_TYPES)}"

    unattributed = [
        f"{event['event_type']}(actor={event.get('actor')!r})"
        for event in actions
        if event.get("actor") not in agent_ids
    ]
    assert not unattributed, f"actions whose actor is not a real agent id: {unattributed}"


def test_agent_spawned_events_reliably_name_a_real_agent(bundle_reader: BundleReader) -> None:
    """The half of Q2 that does hold: an agent's own existence is attributed.

    Every ``agent_spawned`` event carries a non-empty ``agent_id``, and the
    two the fixture records are distinct. This is the real, unconditional
    half of the writer (``orchestrator.py``'s ``_record_spawned_events``
    always passes ``agent_id=session.id``); it is what
    :func:`test_q2_every_recorded_action_names_the_agent_that_performed_it`
    is comparing every other action's actor against.
    """
    run = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    spawned = [event for event in run["journal"]["events"] if event["event"] == "agent_spawned"]
    assert spawned, "the recording holds no agent_spawned event"

    agent_ids = [event["agent_id"] for event in spawned]
    assert all(isinstance(agent_id, str) and agent_id for agent_id in agent_ids), (
        f"an agent_spawned event carries no agent_id: {spawned}"
    )
    assert len(set(agent_ids)) == len(agent_ids), f"agent_spawned events do not name distinct agents: {agent_ids}"


@pytest.mark.question(7)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "no tool call carries a verifiable identity, only a bare actor "
        "label: mcp_gateway.py's real WAL write hardcodes actor='mcp_gateway' "
        "for every call regardless of the invoking agent (mcp_gateway.py:392), "
        "and ToolCallIdentityAttestation's shape (args_digest/intent_digest/ "
        "identity_anchor_ref/tool_signing_kid) never reaches the exported "
        "bundle at all -- the recorded actor is asserted, not attested (#5058)"
    ),
)
def test_q7_the_identity_presented_to_the_tool_can_be_verified(bundle_reader: BundleReader) -> None:
    """Q7: which identity was presented to the tool, and can it be checked?

    The bundle's ``tool.called`` events do carry a bare ``actor`` string --
    that alone is not what this question asks. A tool receiving a call has
    no way to verify that string is genuine; it is the caller's own
    unsigned assertion about itself, recorded by the gateway rather than
    presented to and checked by the tool. What would answer the question is
    a signed, per-call identity attestation the tool side could verify --
    :class:`~bernstein.core.security.toolcall_identity.ToolCallIdentityAttestation`
    declares exactly that shape -- and none of its fields reach this bundle.
    """
    audit = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    tool_calls = [event for event in audit["events"] if event["event_type"] == "tool.called"]
    assert tool_calls, "the recording holds no tool call to check"

    unverifiable = [
        event.get("resource_id", "")
        for event in tool_calls
        if not any(field in event or field in (event.get("details") or {}) for field in _TOOL_IDENTITY_FIELDS)
    ]
    assert not unverifiable, (
        f"tool call(s) with no verifiable identity attestation, only a bare actor label: {unverifiable}; "
        f"looked for {list(_TOOL_IDENTITY_FIELDS)}"
    )


@pytest.mark.question(14)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "nothing binds an agent's identity to a content-addressed digest "
        "of the code, config or tool set it ran with: agent_spawned "
        "records role/model/provider/agent_source/endpoint_* as plain "
        "descriptive strings, none of them a hash of anything, so a "
        "config edit after the fact leaves no detectable trace on the "
        "identity it was attributed to (#5058)"
    ),
)
def test_q14_the_agents_code_config_and_toolset_are_bound_to_its_identity(bundle_reader: BundleReader) -> None:
    """Q14: which code, config and tool set actually ran under this identity?

    ``agent_spawned`` events are rich in *description* -- adapter name,
    model, base URL, endpoint profile, role, provider -- and poor in
    *proof*: every one of those is a plain string the writer was handed,
    not a digest computed over the thing it names. A reader cannot tell a
    truthful description from an edited one without re-deriving it from
    somewhere the bundle does not point to.
    """
    run = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    spawned = [event for event in run["journal"]["events"] if event["event"] == "agent_spawned"]
    assert spawned, "the recording holds no agent_spawned event"

    unbound = [event["agent_id"] for event in spawned if not any(event.get(field) for field in _RUNTIME_DIGEST_FIELDS)]
    assert not unbound, (
        f"agent(s) with no code/config/toolset digest bound to their identity: {unbound}; "
        f"looked for {list(_RUNTIME_DIGEST_FIELDS)}"
    )
