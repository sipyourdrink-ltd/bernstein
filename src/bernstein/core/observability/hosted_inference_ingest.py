"""Hosted inference ingest boundary: accept externally-generated OpenAI-compatible
inference calls as governed activity.

This module mirrors the OTLP ingest pattern but for hosted inference APIs
(OpenAI-compatible endpoints). Operators who instrument their agent workloads
with direct API calls to hosted providers can send the request/response
metadata to Bernstein for governance and audit.

Design:

* **Hosted inference wire format** is the ingest protocol: an operator points
  their existing API client wrapper at Bernstein and the inference calls their
  agents already make become chain-anchored governed activity.
* **Endpoint certification state** is embedded in every activity record: the
  adapter looks up the endpoint's certification receipt and includes the
  certified roles and fingerprint.
* **Typed GenAI activity only.** Unlike OTLP (which produces untyped for
  non-GenAI spans), hosted inference is by definition GenAI activity.
* **Malformed payload → rejection.** ``ingest_payload`` raises
  ``HostedInferenceIngestError`` on bad input and appends nothing to any chain.
* **Principal identity preserved.** The calling principal (API key hash,
  service account, etc.) is recorded verbatim.

This module is intentionally transport-agnostic: it parses hosted inference
JSON payloads and produces typed activity records. The transport that delivers
those payloads (HTTP endpoint, message queue, file drop) is plumbed by the
calling context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.observability.ingest_contract import IngestAdapterDeclaration

__all__ = [
    "GenAIActivity",
    "HostedInferenceIngestAdapter",
    "HostedInferenceIngestError",
    "HostedInferencePayload",
    "ingest_payload",
]


# --------------------------------------------------------------------------- #
# Wire shape constants                                                        #
# --------------------------------------------------------------------------- #


#: Attribute key carrying the principal identifier (API key hash, etc.)
ATTR_PRINCIPAL_ID = "hosted_inference.principal_id"

#: Attribute key carrying the endpoint base URL
ATTR_ENDPOINT_BASE_URL = "hosted_inference.endpoint_base_url"

#: Attribute key carrying the model name
ATTR_MODEL = "hosted_inference.model"

#: Attribute key carrying the endpoint fingerprint
ATTR_ENDPOINT_FINGERPRINT = "hosted_inference.endpoint_fingerprint"

#: Attribute key carrying certified roles from endpoint certification
ATTR_CERTIFIED_ROLES = "hosted_inference.certified_roles"

#: Attribute key marking if endpoint is certified for the requested role
ATTR_ROLE_CERTIFIED = "hosted_inference.role_certified"

#: Attribute key carrying the request digest (SHA-256)
ATTR_REQUEST_DIGEST = "hosted_inference.request_digest"

#: Attribute key carrying the response digest (SHA-256)
ATTR_RESPONSE_DIGEST = "hosted_inference.response_digest"

#: Attribute key carrying prompt token count
ATTR_PROMPT_TOKENS = "hosted_inference.prompt_tokens"

#: Attribute key carrying completion token count
ATTR_COMPLETION_TOKENS = "hosted_inference.completion_tokens"

#: Attribute key carrying total token count
ATTR_TOTAL_TOKENS = "hosted_inference.total_tokens"

#: Attribute key carrying the operation name (chat, completion, embedding)
ATTR_OPERATION = "hosted_inference.operation"


# --------------------------------------------------------------------------- #
# Errors                                                                      #
# --------------------------------------------------------------------------- #


class HostedInferenceIngestError(ValueError):
    """Raised when a hosted inference ingest payload cannot be parsed or validated.

    The caller must not record any chain event when this is raised: the
    payload was rejected in its entirety.
    """


# --------------------------------------------------------------------------- #
# Payload shape                                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class HostedInferencePayload:
    """Validated hosted inference call payload.

    This is the expected JSON structure for a single hosted inference call.
    All fields are required unless marked optional.
    """

    # Identity
    principal_id: str  # Hash of API key or service account identifier
    endpoint_base_url: str  # e.g., "https://api.openai.com/v1"
    model: str  # Model identifier as sent to the endpoint

    # Request/Response metadata
    request_digest: str  # SHA-256 hex of the request body
    response_digest: str  # SHA-256 hex of the response body

    # Token accounting
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    # Operation classification
    operation: str  # "chat", "completion", "embedding", "moderation", etc.

    # Optional: endpoint fingerprint (derived from base_url + model if not provided)
    endpoint_fingerprint: str | None = None

    # Optional: timestamp in milliseconds since epoch
    timestamp_ms: int | None = None

    # Optional: additional metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> HostedInferencePayload:
        """Parse and validate a raw dict into a HostedInferencePayload.

        Args:
            raw: Raw dict from JSON payload.

        Returns:
            Validated HostedInferencePayload.

        Raises:
            HostedInferenceIngestError: When required fields are missing or
                have invalid types.
        """
        # Required fields
        required_str_fields = {
            "principal_id": "principal_id",
            "endpoint_base_url": "endpoint_base_url",
            "model": "model",
            "request_digest": "request_digest",
            "response_digest": "response_digest",
            "operation": "operation",
        }
        required_int_fields = {
            "prompt_tokens": "prompt_tokens",
            "completion_tokens": "completion_tokens",
            "total_tokens": "total_tokens",
        }

        missing = [field for field in required_str_fields if field not in raw]
        if missing:
            raise HostedInferenceIngestError(f"Missing required fields: {', '.join(missing)}")

        # Validate string fields
        parsed_str = {}
        for json_key, attr_name in required_str_fields.items():
            value = raw[json_key]
            if not isinstance(value, str) or not value:
                raise HostedInferenceIngestError(
                    f"Field {json_key!r} must be a non-empty string, got {type(value).__name__!r}"
                )
            parsed_str[attr_name] = value

        # Validate int fields
        parsed_int = {}
        for json_key, attr_name in required_int_fields.items():
            value = raw[json_key]
            if not isinstance(value, int) or value < 0:
                raise HostedInferenceIngestError(f"Field {json_key!r} must be a non-negative integer, got {value!r}")
            parsed_int[attr_name] = value

        # Optional fields
        endpoint_fingerprint = raw.get("endpoint_fingerprint")
        if endpoint_fingerprint is not None and not isinstance(endpoint_fingerprint, str):
            raise HostedInferenceIngestError(
                f"endpoint_fingerprint must be a string if provided, got {type(endpoint_fingerprint).__name__!r}"
            )

        timestamp_ms = raw.get("timestamp_ms")
        if timestamp_ms is not None and not isinstance(timestamp_ms, int):
            raise HostedInferenceIngestError(
                f"timestamp_ms must be an integer if provided, got {type(timestamp_ms).__name__!r}"
            )

        metadata = raw.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise HostedInferenceIngestError(f"metadata must be a dict if provided, got {type(metadata).__name__!r}")

        return cls(
            principal_id=parsed_str["principal_id"],
            endpoint_base_url=parsed_str["endpoint_base_url"],
            model=parsed_str["model"],
            request_digest=parsed_str["request_digest"],
            response_digest=parsed_str["response_digest"],
            prompt_tokens=parsed_int["prompt_tokens"],
            completion_tokens=parsed_int["completion_tokens"],
            total_tokens=parsed_int["total_tokens"],
            operation=parsed_str["operation"],
            endpoint_fingerprint=endpoint_fingerprint,
            timestamp_ms=timestamp_ms,
            metadata=metadata or {},
        )


# --------------------------------------------------------------------------- #
# Activity records                                                            #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GenAIActivity:
    """A typed GenAI activity record extracted from a hosted inference call.

    Includes endpoint certification state for governance.
    """

    trace_id: str  # Generated from request_digest for traceability
    span_id: str  # Generated from response_digest for traceability
    principal_id: str
    endpoint_base_url: str
    model: str
    operation: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    endpoint_fingerprint: str
    certified_roles: frozenset[str]
    role_certified: bool  # True if the operation's role is certified
    request_digest: str
    response_digest: str
    extra_attributes: dict[str, Any] = field(default_factory=dict)

    def to_chain_event(self, *, source: str = "hosted_inference_ingest") -> dict[str, Any]:
        """Render this activity as a chain event payload.

        ``source`` defaults to ``"hosted_inference_ingest"``; the adapter passes
        its configured ``source_label`` to override this for multi-collector
        deployments.
        """
        attrs: dict[str, Any] = {
            ATTR_PRINCIPAL_ID: self.principal_id,
            ATTR_ENDPOINT_BASE_URL: self.endpoint_base_url,
            ATTR_MODEL: self.model,
            ATTR_OPERATION: self.operation,
            ATTR_ENDPOINT_FINGERPRINT: self.endpoint_fingerprint,
            ATTR_CERTIFIED_ROLES: sorted(self.certified_roles),
            ATTR_ROLE_CERTIFIED: self.role_certified,
            ATTR_REQUEST_DIGEST: self.request_digest,
            ATTR_RESPONSE_DIGEST: self.response_digest,
            ATTR_PROMPT_TOKENS: self.prompt_tokens,
            ATTR_COMPLETION_TOKENS: self.completion_tokens,
            ATTR_TOTAL_TOKENS: self.total_tokens,
            "gen_ai.activity_type": "hosted_inference",
            "gen_ai.system": "openai_compatible",
            "gen_ai.request.model": self.model,
            "gen_ai.operation.name": self.operation,
            "gen_ai.usage.prompt_tokens": self.prompt_tokens,
            "gen_ai.usage.completion_tokens": self.completion_tokens,
            "gen_ai.usage.total_tokens": self.total_tokens,
        }
        if self.extra_attributes:
            for k, v in self.extra_attributes.items():
                attrs[f"hosted_inference.extra.{k}"] = v

        return {
            "event": "hosted_inference_ingest.genai_activity",
            "activity": "genai",
            "source": source,
            "attributes": attrs,
        }


# --------------------------------------------------------------------------- #
# Result                                                                      #
# --------------------------------------------------------------------------- #


@dataclass
class IngestCallResult:
    """Outcome of ingesting one hosted inference call."""

    typed: GenAIActivity | None = None
    parse_error: str | None = None

    @property
    def is_typed(self) -> bool:
        return self.typed is not None

    @property
    def is_error(self) -> bool:
        return self.parse_error is not None


# --------------------------------------------------------------------------- #
# Main adapter                                                                #
# --------------------------------------------------------------------------- #


class HostedInferenceIngestAdapter:
    """Hosted inference ingest boundary: maps incoming OpenAI-compatible
    inference calls to typed chain activity with endpoint certification state.

    Wire format is a JSON object (or list of objects) with fields matching
    ``HostedInferencePayload``.

    The adapter is stateless: ``ingest_payload`` and ``ingest_call`` return
    activity records; the caller is responsible for recording them to the
    chain. ``ingest_payload`` raises ``HostedInferenceIngestError`` for any
    malformed input and guarantees no partial state is recorded on error.

    Args:
        source_label: Value written to the ``source`` field of every chain
            event this adapter produces. Defaults to ``"hosted_inference_ingest"``.
            Useful for distinguishing multiple ingest paths.
        certification_resolver: Optional callable that resolves an
            ``EndpointCertification`` for a given ``(base_url, model)`` pair.
            If not provided, the adapter produces activity with empty
            certification (uncertified). Signature:
            ``(base_url: str, model: str) -> EndpointCertification | None``.
    """

    def __init__(
        self,
        *,
        source_label: str = "hosted_inference_ingest",
        certification_resolver: Any = None,  # Callable[[str, str], EndpointCertification | None] | None
    ) -> None:
        self._source = source_label
        self._cert_resolver = certification_resolver

    @property
    def declared_event_types(self) -> tuple[str, ...]:
        """Return the event types this adapter declares it can receive."""
        return ("gen_ai_activity",)

    def validate_declaration(self, declaration: IngestAdapterDeclaration) -> None:
        """Validate a plugin's ingest adapter declaration.

        The declaration must name every event type this adapter can
        produce: an adapter may not quietly narrow its declared surface
        below what it emits.

        Raises:
            ValueError: When the declaration names an event type this
                adapter does not support, or omits one it does.
        """
        from bernstein.core.observability.ingest_contract import VALID_INGEST_EVENT_TYPES

        valid = set(VALID_INGEST_EVENT_TYPES)
        for et in declaration.declared_event_types:
            if et not in valid:
                raise ValueError(
                    f"adapter {declaration.name!r} declared unsupported event type {et!r}; "
                    f"supported types are {sorted(valid)}"
                )
        missing = set(self.declared_event_types) - set(declaration.declared_event_types)
        if missing:
            raise ValueError(
                f"adapter {declaration.name!r} does not declare event types {sorted(missing)} "
                f"which this adapter can produce; supported types are {sorted(valid)}"
            )

    def _resolve_certification(self, base_url: str, model: str) -> tuple[str, frozenset[str], bool]:
        """Resolve endpoint certification for the given endpoint/model.

        Returns:
            Tuple of (endpoint_fingerprint, certified_roles, role_certified).
            If no resolver is configured or no cert found, returns computed
            fingerprint, empty frozenset, and False.
        """
        from bernstein.core.endpoints.certification import endpoint_fingerprint

        fingerprint = endpoint_fingerprint(base_url, model)
        if self._cert_resolver is None:
            return fingerprint, frozenset(), False

        cert = self._cert_resolver(base_url, model)
        if cert is None:
            return fingerprint, frozenset(), False

        certified_roles = cert.certified_roles()
        # For now, we consider the call "role certified" if ANY gated role is
        # certified for this endpoint. A more sophisticated policy could
        # check specific role requirements per operation.
        role_certified = bool(certified_roles)
        return fingerprint, certified_roles, role_certified

    def _generate_trace_id(self, request_digest: str) -> str:
        """Generate a trace ID from the request digest."""
        # Use first 32 chars of request digest as trace_id
        return request_digest[:32]

    def _generate_span_id(self, response_digest: str) -> str:
        """Generate a span ID from the response digest."""
        # Use first 16 chars of response digest as span_id
        return response_digest[:16]

    def ingest_call(self, raw: dict[str, Any]) -> IngestCallResult:
        """Ingest one hosted inference call dict.

        Args:
            raw: A single call object matching HostedInferencePayload fields.

        Returns:
            An ``IngestCallResult`` with either ``typed`` (a ``GenAIActivity``).
            On a parsing error, ``parse_error`` is set and the caller must not
            record anything.

        Raises:
            HostedInferenceIngestError: Never from this method. Parse errors are
                returned as ``IngestCallResult.parse_error`` so the caller
                can make a policy decision. Use ``ingest_payload`` for
                atomic single-call ingestion that raises on error.
        """
        try:
            payload = HostedInferencePayload.from_dict(raw)
        except HostedInferenceIngestError as exc:
            return IngestCallResult(parse_error=str(exc))

        # Resolve certification state
        fingerprint, certified_roles, role_certified = self._resolve_certification(
            payload.endpoint_base_url, payload.model
        )

        # Use provided fingerprint or computed one
        effective_fingerprint = payload.endpoint_fingerprint or fingerprint

        # Generate trace/span IDs from digests for traceability
        trace_id = self._generate_trace_id(payload.request_digest)
        span_id = self._generate_span_id(payload.response_digest)

        activity = GenAIActivity(
            trace_id=trace_id,
            span_id=span_id,
            principal_id=payload.principal_id,
            endpoint_base_url=payload.endpoint_base_url,
            model=payload.model,
            operation=payload.operation,
            prompt_tokens=payload.prompt_tokens,
            completion_tokens=payload.completion_tokens,
            total_tokens=payload.total_tokens,
            endpoint_fingerprint=effective_fingerprint,
            certified_roles=certified_roles,
            role_certified=role_certified,
            request_digest=payload.request_digest,
            response_digest=payload.response_digest,
            extra_attributes=dict(payload.metadata),
        )

        return IngestCallResult(typed=activity)

    def ingest_payload(self, payload: list[dict[str, Any]] | dict[str, Any]) -> list[IngestCallResult]:
        """Ingest a hosted inference payload (atomic on parse errors).

        ``payload`` may be a single call dict or a list of call dicts.
        Parsing is all-or-nothing: if any call fails to parse, the method
        raises ``HostedInferenceIngestError`` and the caller must not record
        anything.

        Args:
            payload: A list of hosted inference call objects, or a single call.

        Returns:
            A list of ``IngestCallResult`` in the same order as the input calls.

        Raises:
            HostedInferenceIngestError: When the top-level payload is not a
                dict or list, or when a call within the list fails to parse.
        """
        if isinstance(payload, dict):
            calls: list[dict[str, Any]] = [payload]
        elif isinstance(payload, list):
            calls = payload
        else:
            raise HostedInferenceIngestError(
                f"Hosted inference ingest payload must be a list or dict, got {type(payload).__name__!r}"
            )

        if not calls:
            raise HostedInferenceIngestError("Hosted inference ingest payload is an empty list")

        results: list[IngestCallResult] = []
        for i, raw in enumerate(calls):
            if not isinstance(raw, dict):
                raise HostedInferenceIngestError(f"call[{i}] is not a dict, got {type(raw).__name__!r}")
            result = self.ingest_call(raw)
            if result.parse_error:
                raise HostedInferenceIngestError(f"call[{i}] parse error: {result.parse_error}")
            results.append(result)

        return results


# --------------------------------------------------------------------------- #
# Convenience                                                                 #
# --------------------------------------------------------------------------- #


def ingest_payload(
    payload: list[dict[str, Any]] | dict[str, Any],
    *,
    source_label: str = "hosted_inference_ingest",
    certification_resolver: Any = None,
) -> list[IngestCallResult]:
    """Ingest a hosted inference payload with default configuration.

    Calls :meth:`HostedInferenceIngestAdapter.ingest_payload` with default
    settings. See that method for the full contract.

    Raises:
        HostedInferenceIngestError: When the payload is malformed or a call
            within it cannot be parsed.
    """
    adapter = HostedInferenceIngestAdapter(source_label=source_label, certification_resolver=certification_resolver)
    return adapter.ingest_payload(payload)
