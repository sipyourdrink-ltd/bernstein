"""AuthZEN 1.0 request and response shapes for the permission decision boundary.

The decision boundary previously answered over request shapes invented here, so
a decision could only be re-evaluated by code that already knew those shapes.
These types express the same decision in the standard vocabulary - subject,
resource, action, context on the way in; decision plus obligations on the way
out - so one request shape serves both an internal caller and a foreign policy
enforcement point.

Two rules make the shape usable as evidence rather than only as transport:

- Canonicalisation is byte-stable. ``canonical_bytes`` uses the same RFC 8785
  JCS encoder the signed surfaces use (``agent_card_signer.canonicalize_jcs``),
  so the same request digests identically on every host and every run.
- Nothing is silently ignored. A context field this engine does not evaluate is
  refused at construction, because a decision that quietly dropped one of its
  inputs is not the decision the requester asked for.

The bridge between these types and the internal :mod:`external_policy_hook`
request lives in that module, so this one stays a plain expression of the
standard and can be parsed without pulling the hook machinery in.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, cast

from bernstein.core.security.agent_card_signer import canonicalize_jcs

#: Version of the AuthZEN evaluation shape these types implement.
AUTHZEN_SPEC_VERSION = "1.0"

#: Subject type used when the requester is a bernstein agent.
SUBJECT_TYPE_AGENT = "agent"

#: Resource type used for the opaque resource string an internal request carries.
RESOURCE_TYPE_OPAQUE = "resource"

#: Context keys this decision boundary evaluates.
#:
#: ``scope`` is the task scope the request is bound to. ``metadata`` is the
#: declared passthrough object for attributes bernstein does not itself evaluate
#: but hands to the external engine unchanged. Any other top-level context key
#: is refused rather than dropped: a request carrying a field we neither
#: evaluate nor forward means something the answer would not have accounted for.
KNOWN_CONTEXT_FIELDS = frozenset({"metadata", "scope"})

_REQUEST_FIELDS = frozenset({"action", "context", "resource", "subject"})
_RESPONSE_FIELDS = frozenset({"context", "decision", "obligations"})
_RESPONSE_CONTEXT_FIELDS = frozenset({"hook", "reason", "verdict"})
_ENTITY_FIELDS = frozenset({"id", "properties", "type"})
_ACTION_FIELDS = frozenset({"name", "properties"})
_OBLIGATION_FIELDS = frozenset({"attributes", "id"})

type _JsonObject = dict[str, Any]


class AuthZenError(ValueError):
    """Raised when a decision cannot be expressed in the AuthZEN shape."""


class UnknownContextFieldError(AuthZenError):
    """Raised when a request carries context this engine does not evaluate."""


def _reject_unknown(payload: _JsonObject, allowed: frozenset[str], where: str) -> None:
    """Refuse payload keys outside *allowed* instead of dropping them."""
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise AuthZenError(f"unknown {where} field(s): {', '.join(unknown)}")


def _as_object(value: object, where: str) -> _JsonObject:
    """Return *value* as a JSON object, or raise."""
    if not isinstance(value, dict):
        raise AuthZenError(f"{where} must be a JSON object")
    return cast("_JsonObject", value)


def _as_text(value: object, where: str) -> str:
    """Return *value* as a string, or raise."""
    if not isinstance(value, str):
        raise AuthZenError(f"{where} must be a string")
    return value


@dataclass(frozen=True, slots=True)
class AuthZenSubject:
    """The entity a decision is requested for.

    ``type`` names the kind of entity and is required. ``id`` may be empty: an
    unidentified subject is a fact a policy can decide on, and blanking it here
    would hide it from the engine.
    """

    type: str
    id: str = ""
    properties: _JsonObject = field(default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        if not self.type.strip():
            raise AuthZenError("AuthZEN subject requires a non-empty type")
        object.__setattr__(self, "properties", dict(self.properties))

    def to_payload(self) -> _JsonObject:
        """Return the AuthZEN payload for this subject."""
        payload: _JsonObject = {"id": self.id, "type": self.type}
        if self.properties:
            payload["properties"] = dict(self.properties)
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> AuthZenSubject:
        """Parse an AuthZEN subject payload."""
        obj = _as_object(payload, "subject")
        _reject_unknown(obj, _ENTITY_FIELDS, "subject")
        return cls(
            type=_as_text(obj.get("type", ""), "subject.type"),
            id=_as_text(obj.get("id", ""), "subject.id"),
            properties=_as_object(obj.get("properties", {}), "subject.properties"),
        )


@dataclass(frozen=True, slots=True)
class AuthZenResource:
    """The thing being acted upon."""

    type: str
    id: str = ""
    properties: _JsonObject = field(default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        if not self.type.strip():
            raise AuthZenError("AuthZEN resource requires a non-empty type")
        object.__setattr__(self, "properties", dict(self.properties))

    def to_payload(self) -> _JsonObject:
        """Return the AuthZEN payload for this resource."""
        payload: _JsonObject = {"id": self.id, "type": self.type}
        if self.properties:
            payload["properties"] = dict(self.properties)
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> AuthZenResource:
        """Parse an AuthZEN resource payload."""
        obj = _as_object(payload, "resource")
        _reject_unknown(obj, _ENTITY_FIELDS, "resource")
        return cls(
            type=_as_text(obj.get("type", ""), "resource.type"),
            id=_as_text(obj.get("id", ""), "resource.id"),
            properties=_as_object(obj.get("properties", {}), "resource.properties"),
        )


@dataclass(frozen=True, slots=True)
class AuthZenAction:
    """The operation being requested."""

    name: str
    properties: _JsonObject = field(default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise AuthZenError("AuthZEN action requires a non-empty name")
        object.__setattr__(self, "properties", dict(self.properties))

    def to_payload(self) -> _JsonObject:
        """Return the AuthZEN payload for this action."""
        payload: _JsonObject = {"name": self.name}
        if self.properties:
            payload["properties"] = dict(self.properties)
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> AuthZenAction:
        """Parse an AuthZEN action payload."""
        obj = _as_object(payload, "action")
        _reject_unknown(obj, _ACTION_FIELDS, "action")
        return cls(
            name=_as_text(obj.get("name", ""), "action.name"),
            properties=_as_object(obj.get("properties", {}), "action.properties"),
        )


@dataclass(frozen=True, slots=True)
class Obligation:
    """A condition attached to a decision.

    An obligation is part of the answer, not commentary on it: a permit that
    carries one has not permitted the request as asked.
    """

    id: str
    attributes: _JsonObject = field(default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise AuthZenError("AuthZEN obligation requires a non-empty id")
        object.__setattr__(self, "attributes", dict(self.attributes))

    def to_payload(self) -> _JsonObject:
        """Return the AuthZEN payload for this obligation."""
        payload: _JsonObject = {"id": self.id}
        if self.attributes:
            payload["attributes"] = dict(self.attributes)
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> Obligation:
        """Parse an AuthZEN obligation payload."""
        obj = _as_object(payload, "obligation")
        _reject_unknown(obj, _OBLIGATION_FIELDS, "obligation")
        return cls(
            id=_as_text(obj.get("id", ""), "obligation.id"),
            attributes=_as_object(obj.get("attributes", {}), "obligation.attributes"),
        )


@dataclass(frozen=True, slots=True)
class AuthZenRequest:
    """A decision request in the AuthZEN evaluation shape."""

    subject: AuthZenSubject
    resource: AuthZenResource
    action: AuthZenAction
    context: _JsonObject = field(default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        unknown = sorted(set(self.context) - KNOWN_CONTEXT_FIELDS)
        if unknown:
            raise UnknownContextFieldError(
                f"decision context field(s) this engine does not evaluate: {', '.join(unknown)}",
            )
        object.__setattr__(self, "context", dict(self.context))

    def to_payload(self) -> _JsonObject:
        """Return the AuthZEN evaluation-request payload."""
        payload: _JsonObject = {
            "action": self.action.to_payload(),
            "resource": self.resource.to_payload(),
            "subject": self.subject.to_payload(),
        }
        if self.context:
            payload["context"] = dict(self.context)
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> AuthZenRequest:
        """Parse an AuthZEN evaluation-request payload."""
        obj = _as_object(payload, "request")
        _reject_unknown(obj, _REQUEST_FIELDS, "request")
        return cls(
            subject=AuthZenSubject.from_payload(obj.get("subject", {})),
            resource=AuthZenResource.from_payload(obj.get("resource", {})),
            action=AuthZenAction.from_payload(obj.get("action", {})),
            context=_as_object(obj.get("context", {}), "context"),
        )

    def canonical_bytes(self) -> bytes:
        """Return the RFC 8785 canonical encoding of this request."""
        return canonicalize_jcs(self.to_payload())

    def digest(self) -> str:
        """Return the digest of the canonical request bytes."""
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthZenResponse:
    """A decision answer in the AuthZEN evaluation shape.

    ``decision`` is the standard boolean. ``verdict`` retains the bernstein
    verdict behind it, because AuthZEN's boolean cannot tell a denial apart from
    an engine that abstained or one that could not be reached, and those have
    opposite safety properties at this boundary.
    """

    decision: bool
    obligations: tuple[Obligation, ...] = ()
    reason: str = ""
    verdict: str = ""
    hook_name: str = ""

    def permits_unconditionally(self) -> bool:
        """Whether this answer permits the request exactly as it was asked.

        A permit carrying obligations is not an unconditional permit; callers
        that only look at ``decision`` would treat the two the same.
        """
        return self.decision and not self.obligations

    def to_payload(self) -> _JsonObject:
        """Return the AuthZEN evaluation-response payload."""
        payload: _JsonObject = {"decision": self.decision}
        if self.obligations:
            payload["obligations"] = [obligation.to_payload() for obligation in self.obligations]
        context: _JsonObject = {}
        if self.hook_name:
            context["hook"] = self.hook_name
        if self.reason:
            context["reason"] = self.reason
        if self.verdict:
            context["verdict"] = self.verdict
        if context:
            payload["context"] = context
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> AuthZenResponse:
        """Parse an AuthZEN evaluation-response payload."""
        obj = _as_object(payload, "response")
        _reject_unknown(obj, _RESPONSE_FIELDS, "response")
        decision = obj.get("decision", False)
        if not isinstance(decision, bool):
            raise AuthZenError("response decision must be a boolean")
        raw_obligations = obj.get("obligations", [])
        if not isinstance(raw_obligations, list):
            raise AuthZenError("response obligations must be a JSON array")
        context = _as_object(obj.get("context", {}), "response context")
        _reject_unknown(context, _RESPONSE_CONTEXT_FIELDS, "response context")
        return cls(
            decision=decision,
            obligations=tuple(Obligation.from_payload(item) for item in cast("list[object]", raw_obligations)),
            reason=_as_text(context.get("reason", ""), "response context reason"),
            verdict=_as_text(context.get("verdict", ""), "response context verdict"),
            hook_name=_as_text(context.get("hook", ""), "response context hook"),
        )

    def canonical_bytes(self) -> bytes:
        """Return the RFC 8785 canonical encoding of this response."""
        return canonicalize_jcs(self.to_payload())

    def digest(self) -> str:
        """Return the digest of the canonical response bytes."""
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()
