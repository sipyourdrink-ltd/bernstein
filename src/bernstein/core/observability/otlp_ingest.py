"""OTLP ingest boundary: accept externally-generated OTLP spans as governed activity.

Issue #4983. Operators who have already instrumented their agent workloads
with OpenTelemetry have the richest possible description of what their agents
did: spans carrying the GenAI semantic conventions, plus arbitrary untyped
activity. This module is the *receiver* mirror of :mod:`otlp_exporter` --
where the exporter maps the journal outward over OTLP, this module maps
incoming OTLP spans inward onto the chain.

Design:

* **OTLP/JSON wire format** is the ingest protocol: an operator points
  their existing collector at Bernstein and the spans their agents already
  emit become chain-anchored governed activity. This is the same shape
  ``otlp_exporter`` emits, so operators who run both directions get a
  symmetric pipeline.
* **GenAI conventions → typed activity.** Where a span carries
  ``gen_ai.system``, ``gen_ai.request.model``,
  ``gen_ai.usage.prompt_tokens`` / ``gen_ai.usage.completion_tokens``,
  ``gen_ai.tool.name`` / ``gen_ai.tool.call.id``, the ingest extracts
  those into a typed ``GenAIActivity`` record. Type is never inferred:
  what is not declared is not claimed.
* **No GenAI conventions → untyped.** A span without the convention
  attributes is recorded as ``UntypedActivity`` carrying only the raw
  trace/span identifiers. The chain receives it as untyped governance
  activity rather than a guessed-at type.
* **Malformed payload → rejection.** ``ingest_payload`` raises
  ``OTLPIngestError`` on bad input and appends nothing to any chain.
* **Trace/span ids preserved.** Ingesting a span records its original
  ``traceId`` and ``spanId`` verbatim; no re-derivation is applied to
  incoming spans (unlike the journal-anchored path where ids are derived
  from entry hashes).

This module is intentionally transport-agnostic: it parses OTLP/JSON span
payloads and produces typed/untyped activity records. The transport that
delivers those payloads (HTTP endpoint, gRPC receiver, file drop) is
plumbed by the calling context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "GenAIActivity",
    "IngestSpanResult",
    "OTLPIngestAdapter",
    "OTLPIngestError",
    "UntypedActivity",
    "ingest_payload",
]


# --------------------------------------------------------------------------- #
# Wire shape (mirrors otlp_exporter.py / otel_projection.py)                 #
# --------------------------------------------------------------------------- #


#: Attribute key carrying the OTLP trace id in a chain event.
ATTR_TRACE_ID = "otlp.trace_id"

#: Attribute key carrying the OTLP span id in a chain event.
ATTR_SPAN_ID = "otlp.span_id"

#: Attribute key carrying the span name.
ATTR_SPAN_NAME = "otlp.span_name"

#: Attribute key carrying the span kind (OTLP SpanKind string).
ATTR_SPAN_KIND = "otlp.span_kind"

#: Attribute key marking an untyped ingest record (distinguishes from
#: a journal-anchored span in the same chain namespace).
ATTR_INGEST_UNTYPED = "otlp.ingest_untyped"


# --------------------------------------------------------------------------- #
# Errors                                                                      #
# --------------------------------------------------------------------------- #


class OTLPIngestError(ValueError):
    """Raised when an OTLP ingest payload cannot be parsed or validated.

    The caller must not record any chain event when this is raised: the
    payload was rejected in its entirety.
    """


# --------------------------------------------------------------------------- #
# Activity records                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GenAIActivity:
    """A typed GenAI activity record extracted from an OTLP span.

    Produced when the incoming span carries the OpenTelemetry GenAI semantic
    conventions. Fields are extracted verbatim; no value is inferred.
    """

    trace_id: str
    span_id: str
    span_name: str
    span_kind: str
    system: str
    model: str
    operation: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    extra_attributes: dict[str, Any] = field(default_factory=dict)

    def to_chain_event(self, *, source: str = "otlp_ingest") -> dict[str, Any]:
        """Render this activity as a chain event payload.

        ``source`` defaults to ``"otlp_ingest"``; the adapter passes its
        configured ``source_label`` to override this for multi-collector
        deployments.
        """
        attrs: dict[str, Any] = {
            ATTR_TRACE_ID: self.trace_id,
            ATTR_SPAN_ID: self.span_id,
            ATTR_SPAN_NAME: self.span_name,
            ATTR_SPAN_KIND: self.span_kind,
            "gen_ai.activity_type": "typed",
            "gen_ai.system": self.system,
            "gen_ai.request.model": self.model,
            "gen_ai.operation.name": self.operation,
        }
        if self.prompt_tokens is not None:
            attrs["gen_ai.usage.prompt_tokens"] = self.prompt_tokens
        if self.completion_tokens is not None:
            attrs["gen_ai.usage.completion_tokens"] = self.completion_tokens
        if self.tool_name is not None:
            attrs["gen_ai.tool.name"] = self.tool_name
        if self.tool_call_id is not None:
            attrs["gen_ai.tool.call.id"] = self.tool_call_id
        if self.extra_attributes:
            for k, v in self.extra_attributes.items():
                attrs[f"gen_ai.extra.{k}"] = v
        return {
            "event": "otlp_ingest.genai_activity",
            "activity": "genai",
            "source": source,
            "attributes": attrs,
        }


@dataclass(frozen=True, slots=True)
class UntypedActivity:
    """An untyped activity record for spans without GenAI conventions.

    Produced when the incoming span carries none of the GenAI semantic
    convention attributes. The span id is preserved verbatim; no type is
    inferred from the name or any other field.
    """

    trace_id: str
    span_id: str
    span_name: str
    span_kind: str
    extra_attributes: dict[str, Any] = field(default_factory=dict)

    def to_chain_event(self, *, source: str = "otlp_ingest") -> dict[str, Any]:
        """Render this activity as a chain event payload.

        ``source`` defaults to ``"otlp_ingest"``; the adapter passes its
        configured ``source_label`` to override this.
        """
        attrs: dict[str, Any] = {
            ATTR_TRACE_ID: self.trace_id,
            ATTR_SPAN_ID: self.span_id,
            ATTR_SPAN_NAME: self.span_name,
            ATTR_SPAN_KIND: self.span_kind,
            "gen_ai.activity_type": "untyped",
            ATTR_INGEST_UNTYPED: True,
        }
        if self.extra_attributes:
            for k, v in self.extra_attributes.items():
                attrs[f"otlp.extra.{k}"] = v
        return {
            "event": "otlp_ingest.untyped_activity",
            "activity": "untyped",
            "source": source,
            "attributes": attrs,
        }


# --------------------------------------------------------------------------- #
# Result                                                                        #
# --------------------------------------------------------------------------- #


@dataclass
class IngestSpanResult:
    """Outcome of ingesting one OTLP span."""

    typed: GenAIActivity | None = None
    untyped: UntypedActivity | None = None
    parse_error: str | None = None

    @property
    def is_typed(self) -> bool:
        return self.typed is not None

    @property
    def is_untyped(self) -> bool:
        return self.untyped is not None

    @property
    def is_error(self) -> bool:
        return self.parse_error is not None


# --------------------------------------------------------------------------- #
# OTLP/JSON helpers                                                            #
# --------------------------------------------------------------------------- #


def _otlp_attr_value(value: Any) -> Any:
    """Unwrap an OTLP AnyValue dict to its scalar, or pass a plain value through."""
    if isinstance(value, dict):
        for key in (
            "stringValue",
            "intValue",
            "boolValue",
            "doubleValue",
            "arrayValue",
            "kvlistValue",
        ):
            if key in value:
                inner = value[key]
                if key == "intValue":
                    try:
                        return int(inner)
                    except (TypeError, ValueError):
                        return inner
                return inner
        return value
    return value


def _span_attributes(attributes: list[dict[str, Any]] | dict[str, Any] | None) -> dict[str, Any]:
    """Normalise an OTLP span attributes list/dict to a plain dict.

    Accepts the OTLP/JSON list form (``[{"key": k, "value": {...}}]``),
    the OTLP/protobuf dict form (``{k: v}``), or a plain Python dict.
    """
    if attributes is None:
        return {}
    if isinstance(attributes, dict):
        if not attributes:
            return {}
        # OTLP/protobuf wire: plain dict of scalars or AnyValue.
        # Check if it looks like a plain key→value map (no "key"/"value" keys).
        if not any(isinstance(v, dict) and "value" in v for v in attributes.values()):
            return {k: _otlp_attr_value(v) for k, v in attributes.items()}
        # Otherwise fall through to list form handling.
    if isinstance(attributes, list):
        out: dict[str, Any] = {}
        for item in attributes:
            if isinstance(item, dict):
                key = item.get("key")
                if key is not None:
                    out[str(key)] = _otlp_attr_value(item.get("value"))
        return out
    return {}


_GEN_AI_ATTRS = frozenset(
    {
        "gen_ai.system",
        "gen_ai.request.model",
        "gen_ai.operation.name",
        "gen_ai.usage.prompt_tokens",
        "gen_ai.usage.completion_tokens",
        "gen_ai.tool.name",
        "gen_ai.tool.call.id",
    }
)


def _is_genai_span(attrs: dict[str, Any]) -> bool:
    """True when ``attrs`` carries at least one GenAI semantic convention."""
    return any(k in _GEN_AI_ATTRS for k in attrs)


# --------------------------------------------------------------------------- #
# Main adapter                                                                  #
# --------------------------------------------------------------------------- #


class OTLPIngestAdapter:
    """OTLP ingest boundary: maps incoming OTLP/JSON spans to typed or untyped chain activity.

    Wire format mirrors the OTLP exporter output: each span is an object
    with ``traceId``, ``spanId``, ``name``, ``kind``, ``attributes``, and
    optionally ``parentSpanId``. Attributes are accepted in either the
    OTLP/JSON list form (``[{"key": "...", "value": {...}}]``) or the
    OTLP/protobuf dict form (``{key: value}``).

    The adapter is stateless: ``ingest_payload`` and ``ingest_span`` return
    activity records; the caller is responsible for recording them to the
    chain. ``ingest_payload`` raises ``OTLPIngestError`` for any malformed
    input and guarantees no partial state is recorded on error.

    Args:
        source_label: Value written to the ``source`` field of every chain
            event this adapter produces. Defaults to ``"otlp_ingest"``. Useful
            for distinguishing multiple ingest paths.
    """

    def __init__(self, *, source_label: str = "otlp_ingest") -> None:
        self._source = source_label

    def ingest_span(self, raw: dict[str, Any]) -> IngestSpanResult:
        """Ingest one OTLP/JSON span dict.

        Args:
            raw: A single span object. Must contain ``traceId`` and ``spanId``
                as hex strings. ``attributes`` may be in OTLP/JSON list form,
                OTLP/protobuf dict form, or a plain Python dict.

        Returns:
            An ``IngestSpanResult`` with either ``typed`` (a ``GenAIActivity``)
            or ``untyped`` (an ``UntypedActivity``). On a parsing error,
            ``parse_error`` is set and the caller must not record anything.

        Raises:
            OTLPIngestError: Never from this method. Parse errors are
                returned as ``IngestSpanResult.parse_error`` so the caller
                can make a policy decision. Use ``ingest_payload`` for
                atomic single-span ingestion that raises on error.
        """
        trace_id = raw.get("traceId") or raw.get("trace_id")
        span_id = raw.get("spanId") or raw.get("span_id")

        if not isinstance(trace_id, str) or not trace_id:
            return IngestSpanResult(parse_error=f"span has no traceId (got {type(trace_id).__name__!r})")
        if not isinstance(span_id, str) or not span_id:
            return IngestSpanResult(parse_error=f"span has no spanId (got {type(span_id).__name__!r})")

        # Normalise: OTLP/JSON uses camelCase; snake_case is also accepted.
        name = str(raw.get("name") or raw.get("span_name", ""))
        kind = str(raw.get("kind") or raw.get("span_kind", ""))
        raw_attrs = raw.get("attributes")

        try:
            attrs = _span_attributes(raw_attrs)
        except Exception as exc:
            return IngestSpanResult(parse_error=f"failed to parse attributes: {exc}")

        if _is_genai_span(attrs):
            return IngestSpanResult(
                typed=GenAIActivity(
                    trace_id=trace_id,
                    span_id=span_id,
                    span_name=name,
                    span_kind=kind,
                    system=str(attrs.get("gen_ai.system", "")),
                    model=str(attrs.get("gen_ai.request.model", "")),
                    operation=str(attrs.get("gen_ai.operation.name", "")),
                    prompt_tokens=int(attrs["gen_ai.usage.prompt_tokens"])
                    if "gen_ai.usage.prompt_tokens" in attrs
                    else None,
                    completion_tokens=int(attrs["gen_ai.usage.completion_tokens"])
                    if "gen_ai.usage.completion_tokens" in attrs
                    else None,
                    tool_name=str(attrs["gen_ai.tool.name"]) if "gen_ai.tool.name" in attrs else None,
                    tool_call_id=str(attrs["gen_ai.tool.call.id"]) if "gen_ai.tool.call.id" in attrs else None,
                    extra_attributes={k: v for k, v in attrs.items() if k not in _GEN_AI_ATTRS},
                )
            )

        # No GenAI conventions present: record as untyped, never infer.
        return IngestSpanResult(
            untyped=UntypedActivity(
                trace_id=trace_id,
                span_id=span_id,
                span_name=name,
                span_kind=kind,
                extra_attributes=dict(attrs),
            )
        )

    def ingest_payload(self, payload: list[dict[str, Any]] | dict[str, Any]) -> list[IngestSpanResult]:
        """Ingest an OTLP/JSON payload (atomic on parse errors).

        ``payload`` may be a single span dict or a list of span dicts.
        Parsing is all-or-nothing: if any span fails to parse, the method
        raises ``OTLPIngestError`` and the caller must not record anything.

        Args:
            payload: A list of OTLP/JSON span objects, or a single span.

        Returns:
            A list of ``IngestSpanResult`` in the same order as the input spans.

        Raises:
            OTLPIngestError: When the top-level payload is not a dict or list,
                or when a span within the list fails to parse.
        """
        if isinstance(payload, dict):
            spans: list[dict[str, Any]] = [payload]
        elif isinstance(payload, list):
            spans = payload
        else:
            raise OTLPIngestError(f"OTLP ingest payload must be a list or dict, got {type(payload).__name__!r}")

        if not spans:
            raise OTLPIngestError("OTLP ingest payload is an empty list")

        results: list[IngestSpanResult] = []
        for i, raw in enumerate(spans):
            if not isinstance(raw, dict):
                raise OTLPIngestError(f"span[{i}] is not a dict, got {type(raw).__name__!r}")
            result = self.ingest_span(raw)
            if result.parse_error:
                raise OTLPIngestError(f"span[{i}] parse error: {result.parse_error}")
            results.append(result)

        return results


# --------------------------------------------------------------------------- #
# Convenience                                                                  #
# --------------------------------------------------------------------------- #


def ingest_payload(
    payload: list[dict[str, Any]] | dict[str, Any],
    *,
    source_label: str = "otlp_ingest",
) -> list[IngestSpanResult]:
    """Ingest an OTLP/JSON payload with default configuration.

    Calls :meth:`OTLPIngestAdapter.ingest_payload` with default settings.
    See that method for the full contract.

    Raises:
        OTLPIngestError: When the payload is malformed or a span within it
            cannot be parsed.
    """
    adapter = OTLPIngestAdapter(source_label=source_label)
    return adapter.ingest_payload(payload)
