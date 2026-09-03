"""Tests for the AuthZEN 1.0 shape at the permission decision boundary.

Each test is named for the property it protects; the numbering matches the
list in the pull request description.
"""

from __future__ import annotations

import json

import pytest

from bernstein.core.security.authzen import (
    AuthZenAction,
    AuthZenError,
    AuthZenRequest,
    AuthZenResource,
    AuthZenResponse,
    AuthZenSubject,
    Obligation,
    UnknownContextFieldError,
)
from bernstein.core.security.external_policy_hook import (
    ExternalPolicyHook,
    HookRequest,
    HookResponse,
    HookVerdict,
    PolicyHookRegistry,
)


def _request() -> AuthZenRequest:
    return AuthZenRequest(
        subject=AuthZenSubject(type="agent", id="agent-1", properties={"role": "reviewer"}),
        resource=AuthZenResource(type="resource", id="prod/db"),
        action=AuthZenAction(name="deploy"),
        context={"scope": "task-7", "metadata": {"ticket": "T-9"}},
    )


class _RecordingHook(ExternalPolicyHook):
    """Hook that records the request it was handed and answers with a fixed response."""

    def __init__(self, response: HookResponse) -> None:
        self._response = response
        self.seen: list[HookRequest] = []

    @property
    def name(self) -> str:
        return "recording"

    def evaluate(self, request: HookRequest) -> HookResponse:
        self.seen.append(request)
        return self._response


# 1
def test_authzen_request_round_trips_canonically() -> None:
    """The same request produces the same bytes, and survives payload round trips."""
    request = _request()
    first = request.canonical_bytes()

    parsed = AuthZenRequest.from_payload(json.loads(first.decode("utf-8")))

    assert parsed == request
    assert parsed.canonical_bytes() == first
    assert parsed.digest() == request.digest()
    assert first == (
        b'{"action":{"name":"deploy"},'
        b'"context":{"metadata":{"ticket":"T-9"},"scope":"task-7"},'
        b'"resource":{"id":"prod/db","type":"resource"},'
        b'"subject":{"id":"agent-1","properties":{"role":"reviewer"},"type":"agent"}}'
    )


# 2
def test_internal_decisions_use_the_same_request_shape_as_the_endpoint() -> None:
    """An internal hook request and an over-the-wire payload are one vocabulary."""
    internal = HookRequest(
        action="deploy",
        resource="prod/db",
        agent_id="agent-1",
        role="reviewer",
        scope="task-7",
        metadata={"ticket": "T-9"},
    )

    # What an AuthZEN endpoint would parse off the wire.
    from_wire = AuthZenRequest.from_payload(json.loads(internal.to_authzen().canonical_bytes().decode("utf-8")))

    assert from_wire == internal.to_authzen()
    assert HookRequest.from_authzen(from_wire) == internal

    hook = _RecordingHook(HookResponse(hook_name="recording", verdict=HookVerdict.ALLOW, reason="ok"))
    registry = PolicyHookRegistry()
    registry.register(hook)
    registry.first_decisive(internal)

    assert hook.seen == [HookRequest.from_authzen(from_wire)]


# 3
def test_permit_with_obligations_does_not_flatten_to_permit() -> None:
    """A conditional permit stays distinguishable from an unconditional one."""
    obligation = Obligation(id="redact_secrets", attributes={"fields": ["token"]})
    conditional = HookResponse(
        hook_name="cedar",
        verdict=HookVerdict.ALLOW,
        reason="permitted with conditions",
        obligations=(obligation,),
    )
    unconditional = HookResponse(
        hook_name="cedar",
        verdict=HookVerdict.ALLOW,
        reason="permitted with conditions",
    )

    conditional_authzen = conditional.to_authzen()
    unconditional_authzen = unconditional.to_authzen()

    assert conditional_authzen.decision is True
    assert conditional_authzen.obligations == (obligation,)
    assert not conditional_authzen.permits_unconditionally()
    assert unconditional_authzen.permits_unconditionally()
    assert conditional_authzen.canonical_bytes() != unconditional_authzen.canonical_bytes()

    registry = PolicyHookRegistry()
    registry.register(_RecordingHook(conditional))
    decisive = registry.first_decisive(HookRequest(action="deploy", resource="prod/db", agent_id="agent-1"))

    assert decisive.obligations == (obligation,)
    assert not decisive.to_authzen().permits_unconditionally()


# 4 (acceptance item 6)
def test_unknown_context_field_is_rejected_not_ignored() -> None:
    """Context this engine does not evaluate is refused, never silently dropped."""
    with pytest.raises(UnknownContextFieldError, match="source_ip"):
        AuthZenRequest(
            subject=AuthZenSubject(type="agent", id="agent-1"),
            resource=AuthZenResource(type="resource", id="prod/db"),
            action=AuthZenAction(name="deploy"),
            context={"source_ip": "203.0.113.7"},
        )

    payload = _request().to_payload()
    payload["context"]["source_ip"] = "203.0.113.7"
    with pytest.raises(UnknownContextFieldError, match="source_ip"):
        AuthZenRequest.from_payload(payload)

    stray_top_level = _request().to_payload()
    stray_top_level["environment"] = {"tier": "prod"}
    with pytest.raises(AuthZenError, match="environment"):
        AuthZenRequest.from_payload(stray_top_level)


# 5
def test_entity_property_that_cannot_be_carried_is_rejected_not_dropped() -> None:
    """Converting to the internal request refuses attributes it would have to discard."""
    request = AuthZenRequest(
        subject=AuthZenSubject(type="agent", id="agent-1", properties={"role": "reviewer", "department": "ops"}),
        resource=AuthZenResource(type="resource", id="prod/db"),
        action=AuthZenAction(name="deploy"),
    )

    with pytest.raises(AuthZenError, match="department"):
        HookRequest.from_authzen(request)


# 6
def test_registry_refuses_a_request_the_standard_shape_cannot_express() -> None:
    """A malformed decision request never reaches a policy engine."""
    hook = _RecordingHook(HookResponse(hook_name="recording", verdict=HookVerdict.ALLOW, reason="ok"))
    registry = PolicyHookRegistry()
    registry.register(hook)

    with pytest.raises(AuthZenError):
        registry.first_decisive(HookRequest(action="", resource="prod/db", agent_id="agent-1"))

    assert hook.seen == []


# 7
def test_canonical_bytes_ignore_construction_order() -> None:
    """Requests that mean the same thing hash the same, whatever order built them."""
    forward = AuthZenRequest.from_payload(
        {
            "subject": {"type": "agent", "id": "agent-1", "properties": {"role": "reviewer"}},
            "resource": {"type": "resource", "id": "prod/db"},
            "action": {"name": "deploy"},
            "context": {"scope": "task-7", "metadata": {"ticket": "T-9"}},
        },
    )
    reversed_order = AuthZenRequest.from_payload(
        {
            "context": {"metadata": {"ticket": "T-9"}, "scope": "task-7"},
            "action": {"name": "deploy"},
            "resource": {"id": "prod/db", "type": "resource"},
            "subject": {"properties": {"role": "reviewer"}, "id": "agent-1", "type": "agent"},
        },
    )

    assert forward.canonical_bytes() == reversed_order.canonical_bytes()
    assert forward.digest() == reversed_order.digest()


# 8
def test_response_round_trips_through_its_payload() -> None:
    """A response payload carries its obligations and its bernstein verdict back."""
    response = AuthZenResponse(
        decision=True,
        obligations=(Obligation(id="notify_owner", attributes={"channel": "email"}),),
        reason="permitted with conditions",
        verdict="allow",
        hook_name="cedar",
    )

    parsed = AuthZenResponse.from_payload(json.loads(response.canonical_bytes().decode("utf-8")))

    assert parsed == response
    assert parsed.canonical_bytes() == response.canonical_bytes()
