"""Tests for the Hosted inference ingest boundary.

The acceptance criteria from the task:

1. test_ingest_preserves_principal_and_model
   - Principal (identity making the call) is preserved
   - Model identifier is preserved

2. test_ingest_preserves_request_response_digests
   - Request digest (SHA256 of request body) is preserved
   - Response digest (SHA256 of response body) is preserved

3. test_ingest_produces_genai_activity_with_certification_state
   - Endpoint fingerprint is included
   - Endpoint certification state is included

4. test_ingest_supports_gen_ai_activity_event_type
   - Adapter declares gen_ai_activity event type
   - Adapter validates declaration correctly

5. test_source_label_for_multi_collector_deployments
   - source_label parameter works correctly
   - Multiple collectors can be distinguished
"""

from __future__ import annotations

import pytest

from bernstein.core.observability.hosted_inference_ingest import (
    GenAIActivity,
    HostedInferenceIngestAdapter,
    HostedInferenceIngestError,
    HostedInferencePayload,
    ingest_payload,
)


@pytest.fixture
def adapter() -> HostedInferenceIngestAdapter:
    return HostedInferenceIngestAdapter()


def _hosted_inference_call(
    *,
    principal_id: str = "api_key_hash_123",
    endpoint_base_url: str = "https://api.openai.com/v1",
    model: str = "gpt-4o",
    request_digest: str = "abc123def456abc123def456abc123def456abc123def456abc123def456abc1",
    response_digest: str = "fedcba987654fedcba987654fedcba987654fedcba987654fedcba987654fedcba",
    prompt_tokens: int = 128,
    completion_tokens: int = 64,
    total_tokens: int = 192,
    operation: str = "chat",
    endpoint_fingerprint: str = "endpoint_fingerprint_123",
    timestamp_ms: int | None = None,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    """A fixture hosted inference call payload."""
    payload: dict[str, object] = {
        "principal_id": principal_id,
        "endpoint_base_url": endpoint_base_url,
        "model": model,
        "request_digest": request_digest,
        "response_digest": response_digest,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "operation": operation,
    }
    if endpoint_fingerprint is not None:
        payload["endpoint_fingerprint"] = endpoint_fingerprint
    if timestamp_ms is not None:
        payload["timestamp_ms"] = timestamp_ms
    if metadata is not None:
        payload["metadata"] = metadata
    return payload


def test_hosted_inference_payload_validation_principal_id_required() -> None:
    """A payload without principal_id raises HostedInferenceIngestError."""
    with pytest.raises(HostedInferenceIngestError, match="principal_id"):
        HostedInferencePayload.from_dict(
            {
                "endpoint_base_url": "https://api.openai.com/v1",
                "model": "gpt-4",
                "request_digest": "abc",
                "response_digest": "def",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "operation": "chat",
            }  # type: ignore[typeddict-item]
        )


def test_hosted_inference_payload_validation_endpoint_base_url_required() -> None:
    """A payload without endpoint_base_url raises HostedInferenceIngestError."""
    with pytest.raises(HostedInferenceIngestError, match="endpoint_base_url"):
        HostedInferencePayload.from_dict(
            {
                "principal_id": "user1",
                "model": "gpt-4",
                "request_digest": "abc",
                "response_digest": "def",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "operation": "chat",
            }  # type: ignore[typeddict-item]
        )


def test_hosted_inference_payload_validation_model_required() -> None:
    """A payload without model raises HostedInferenceIngestError."""
    with pytest.raises(HostedInferenceIngestError, match="model"):
        HostedInferencePayload.from_dict(
            {
                "principal_id": "user1",
                "endpoint_base_url": "https://api.openai.com/v1",
                "request_digest": "abc",
                "response_digest": "def",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "operation": "chat",
            }  # type: ignore[typeddict-item]
        )


def test_hosted_inference_payload_validation_request_digest_required() -> None:
    """A payload without request_digest raises HostedInferenceIngestError."""
    with pytest.raises(HostedInferenceIngestError, match="request_digest"):
        HostedInferencePayload.from_dict(
            {
                "principal_id": "user1",
                "endpoint_base_url": "https://api.openai.com/v1",
                "model": "gpt-4",
                "response_digest": "def",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "operation": "chat",
            }  # type: ignore[typeddict-item]
        )


def test_hosted_inference_payload_validation_response_digest_required() -> None:
    """A payload without response_digest raises HostedInferenceIngestError."""
    with pytest.raises(HostedInferenceIngestError, match="response_digest"):
        HostedInferencePayload.from_dict(
            {
                "principal_id": "user1",
                "endpoint_base_url": "https://api.openai.com/v1",
                "model": "gpt-4",
                "request_digest": "abc",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "operation": "chat",
            }  # type: ignore[typeddict-item]
        )


def test_hosted_inference_payload_validation_prompt_tokens_must_be_non_negative() -> None:
    """A payload with negative prompt_tokens raises HostedInferenceIngestError."""
    with pytest.raises(HostedInferenceIngestError, match="prompt_tokens"):
        HostedInferencePayload.from_dict(
            {
                "principal_id": "user1",
                "endpoint_base_url": "https://api.openai.com/v1",
                "model": "gpt-4",
                "request_digest": "abc",
                "response_digest": "def",
                "prompt_tokens": -1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "operation": "chat",
            }
        )


def test_hosted_inference_payload_validation_completion_tokens_must_be_non_negative() -> None:
    """A payload with negative completion_tokens raises HostedInferenceIngestError."""
    with pytest.raises(HostedInferenceIngestError, match="completion_tokens"):
        HostedInferencePayload.from_dict(
            {
                "principal_id": "user1",
                "endpoint_base_url": "https://api.openai.com/v1",
                "model": "gpt-4",
                "request_digest": "abc",
                "response_digest": "def",
                "prompt_tokens": 1,
                "completion_tokens": -1,
                "total_tokens": 2,
                "operation": "chat",
            }
        )


def test_hosted_inference_payload_validation_total_tokens_must_be_non_negative() -> None:
    """A payload with negative total_tokens raises HostedInferenceIngestError."""
    with pytest.raises(HostedInferenceIngestError, match="total_tokens"):
        HostedInferencePayload.from_dict(
            {
                "principal_id": "user1",
                "endpoint_base_url": "https://api.openai.com/v1",
                "model": "gpt-4",
                "request_digest": "abc",
                "response_digest": "def",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": -1,
                "operation": "chat",
            }
        )


def test_hosted_inference_payload_validation_operation_required() -> None:
    """A payload without operation raises HostedInferenceIngestError."""
    with pytest.raises(HostedInferenceIngestError, match="operation"):
        HostedInferencePayload.from_dict(
            {
                "principal_id": "user1",
                "endpoint_base_url": "https://api.openai.com/v1",
                "model": "gpt-4",
                "request_digest": "abc",
                "response_digest": "def",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            }  # type: ignore[typeddict-item]
        )


def test_hosted_inference_payload_validation_endpoint_fingerprint_must_be_string() -> None:
    """A payload with non-string endpoint_fingerprint raises HostedInferenceIngestError."""
    with pytest.raises(HostedInferenceIngestError, match="endpoint_fingerprint"):
        HostedInferencePayload.from_dict(
            {
                "principal_id": "user1",
                "endpoint_base_url": "https://api.openai.com/v1",
                "model": "gpt-4",
                "request_digest": "abc",
                "response_digest": "def",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "operation": "chat",
                "endpoint_fingerprint": 123,
            }
        )


def test_hosted_inference_payload_validation_timestamp_ms_must_be_int() -> None:
    """A payload with non-int timestamp_ms raises HostedInferenceIngestError."""
    with pytest.raises(HostedInferenceIngestError, match="timestamp_ms"):
        HostedInferencePayload.from_dict(
            {
                "principal_id": "user1",
                "endpoint_base_url": "https://api.openai.com/v1",
                "model": "gpt-4",
                "request_digest": "abc",
                "response_digest": "def",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "operation": "chat",
                "timestamp_ms": "not_an_int",
            }
        )


def test_hosted_inference_payload_validation_metadata_must_be_dict() -> None:
    """A payload with non-dict metadata raises HostedInferenceIngestError."""
    with pytest.raises(HostedInferenceIngestError, match="metadata"):
        HostedInferencePayload.from_dict(
            {
                "principal_id": "user1",
                "endpoint_base_url": "https://api.openai.com/v1",
                "model": "gpt-4",
                "request_digest": "abc",
                "response_digest": "def",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "operation": "chat",
                "metadata": "not_a_dict",
            }
        )


def test_hosted_inference_payload_from_dict_with_all_fields() -> None:
    """A payload with all fields is parsed correctly."""
    raw = _hosted_inference_call()
    payload = HostedInferencePayload.from_dict(raw)
    assert payload.principal_id == "api_key_hash_123"
    assert payload.endpoint_base_url == "https://api.openai.com/v1"
    assert payload.model == "gpt-4o"
    assert payload.request_digest == "abc123def456abc123def456abc123def456abc123def456abc123def456abc1"
    assert payload.response_digest == "fedcba987654fedcba987654fedcba987654fedcba987654fedcba987654fedcba"
    assert payload.prompt_tokens == 128
    assert payload.completion_tokens == 64
    assert payload.total_tokens == 192
    assert payload.operation == "chat"
    assert payload.endpoint_fingerprint == "endpoint_fingerprint_123"
    assert payload.timestamp_ms is None
    assert payload.metadata == {}


def test_hosted_inference_payload_from_dict_with_optional_fields() -> None:
    """A payload with optional fields is parsed correctly."""
    raw = _hosted_inference_call(
        endpoint_fingerprint="custom_fingerprint",
        timestamp_ms=1234567890,
        metadata={"custom_key": "custom_value"},
    )
    payload = HostedInferencePayload.from_dict(raw)
    assert payload.endpoint_fingerprint == "custom_fingerprint"
    assert payload.timestamp_ms == 1234567890
    assert payload.metadata == {"custom_key": "custom_value"}


def test_hosted_inference_payload_optional_fields_can_be_omitted() -> None:
    """Optional fields can be omitted and use default values."""
    raw = _hosted_inference_call(endpoint_fingerprint=None, timestamp_ms=None, metadata=None)
    # Remove optional fields entirely
    raw.pop("endpoint_fingerprint", None)
    raw.pop("timestamp_ms", None)
    raw.pop("metadata", None)
    payload = HostedInferencePayload.from_dict(raw)
    assert payload.endpoint_fingerprint is None
    assert payload.timestamp_ms is None
    assert payload.metadata == {}


# --------------------------------------------------------------------------- #
# AC1: Ingest preserves principal_id and model                                #
# --------------------------------------------------------------------------- #


def test_ingest_preserves_principal_id_and_model(adapter: HostedInferenceIngestAdapter) -> None:
    """Principal ID and model are preserved in the activity record."""
    payload = _hosted_inference_call(
        principal_id="user_abc123",
        model="claude-3-5-sonnet",
    )
    result = adapter.ingest_call(payload)
    assert result.is_typed
    assert result.typed is not None
    assert result.typed.principal_id == "user_abc123"
    assert result.typed.model == "claude-3-5-sonnet"


# --------------------------------------------------------------------------- #
# AC2: Ingest preserves request/response digests                              #
# --------------------------------------------------------------------------- #


def test_ingest_preserves_request_response_digests(adapter: HostedInferenceIngestAdapter) -> None:
    """Request and response digests are preserved in the activity record."""
    payload = _hosted_inference_call(
        request_digest="a" * 64,
        response_digest="b" * 64,
    )
    result = adapter.ingest_call(payload)
    assert result.is_typed
    activity = result.typed
    assert activity is not None
    assert activity.request_digest == "a" * 64
    assert activity.response_digest == "b" * 64


# --------------------------------------------------------------------------- #
# AC3: Ingest produces GenAI activity with certification state                #
# --------------------------------------------------------------------------- #


def test_ingest_produces_genai_activity_with_certification_state(adapter: HostedInferenceIngestAdapter) -> None:
    """A hosted inference call produces GenAI activity with endpoint certification state."""
    payload = _hosted_inference_call(
        endpoint_fingerprint="certified_endpoint_123",
    )
    result = adapter.ingest_call(payload)
    assert result.is_typed
    activity = result.typed
    assert activity is not None
    assert isinstance(activity, GenAIActivity)
    assert activity.endpoint_fingerprint == "certified_endpoint_123"
    # Without a certification resolver, role_certified should be False
    assert activity.role_certified is False
    assert activity.certified_roles == frozenset()


def test_ingest_produces_genai_activity_chain_event_with_certification(
    adapter: HostedInferenceIngestAdapter,
) -> None:
    """Chain event for a hosted inference call includes certification state."""
    payload = _hosted_inference_call(
        endpoint_fingerprint="fingerprint_abc",
    )
    result = adapter.ingest_call(payload)
    assert result.is_typed
    chain_event = result.typed.to_chain_event(source=adapter._source)
    assert chain_event is not None
    assert chain_event["event"] == "hosted_inference_ingest.genai_activity"
    assert chain_event["source"] == "hosted_inference_ingest"
    assert chain_event["activity"] == "genai"
    attrs = chain_event["attributes"]
    assert attrs["hosted_inference.endpoint_fingerprint"] == "fingerprint_abc"
    assert attrs["hosted_inference.role_certified"] is False
    assert attrs["hosted_inference.certified_roles"] == []
    assert attrs["gen_ai.activity_type"] == "hosted_inference"


def test_ingest_produces_genai_activity_chain_event_with_tokens(adapter: HostedInferenceIngestAdapter) -> None:
    """Chain event for a hosted inference call includes token counts when present."""
    payload = _hosted_inference_call(
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
    )
    result = adapter.ingest_call(payload)
    assert result.is_typed
    chain_event = result.typed.to_chain_event()
    assert chain_event is not None
    attrs = chain_event["attributes"]
    assert attrs["hosted_inference.prompt_tokens"] == 100
    assert attrs["hosted_inference.completion_tokens"] == 50
    assert attrs["hosted_inference.total_tokens"] == 150


def test_ingest_produces_genai_activity_trace_and_span_ids_from_digests(
    adapter: HostedInferenceIngestAdapter,
) -> None:
    """Trace ID and span ID are generated from request and response digests."""
    payload = _hosted_inference_call(
        request_digest="a" * 64,
        response_digest="b" * 64,
    )
    result = adapter.ingest_call(payload)
    assert result.is_typed
    activity = result.typed
    assert activity is not None
    # Trace ID should be first 32 chars of request digest
    assert activity.trace_id == "a" * 32
    # Span ID should be first 16 chars of response digest
    assert activity.span_id == "b" * 16


# --------------------------------------------------------------------------- #
# AC4: Adapter declares gen_ai_activity event type                            #
# --------------------------------------------------------------------------- #


def test_adapter_declares_gen_ai_activity_event_type(adapter: HostedInferenceIngestAdapter) -> None:
    """Adapter declares gen_ai_activity as a supported event type."""
    declared = adapter.declared_event_types
    assert "gen_ai_activity" in declared


def test_adapter_validate_declaration_accepts_gen_ai_activity(adapter: HostedInferenceIngestAdapter) -> None:
    """Adapter validates a declaration that declares gen_ai_activity."""
    from bernstein.core.observability.ingest_contract import IngestAdapterDeclaration

    declaration = IngestAdapterDeclaration(
        name="test-hosted-inference",
        version="1.0.0",
        declared_event_types=("gen_ai_activity",),
        summary="Test hosted inference adapter",
    )
    adapter.validate_declaration(declaration)


def test_adapter_validate_declaration_rejects_unsupported_event_type(
    adapter: HostedInferenceIngestAdapter,
) -> None:
    """Adapter rejects a declaration that names an unsupported event type."""
    from bernstein.core.observability.ingest_contract import IngestAdapterDeclaration

    declaration = IngestAdapterDeclaration(
        name="test-hosted-inference",
        version="1.0.0",
        declared_event_types=("gen_ai_activity", "unsupported_type"),
        summary="Test hosted inference adapter",
    )
    with pytest.raises(ValueError, match="unsupported_type"):
        adapter.validate_declaration(declaration)


def test_adapter_validate_declaration_rejects_missing_event_type(
    adapter: HostedInferenceIngestAdapter,
) -> None:
    """Adapter rejects a declaration that omits gen_ai_activity."""
    from bernstein.core.observability.ingest_contract import IngestAdapterDeclaration

    declaration = IngestAdapterDeclaration(
        name="test-hosted-inference",
        version="1.0.0",
        declared_event_types=(),
        summary="Test hosted inference adapter",
    )
    with pytest.raises(ValueError, match="gen_ai_activity"):
        adapter.validate_declaration(declaration)


# --------------------------------------------------------------------------- #
# AC5: source_label for multi-collector deployments                           #
# --------------------------------------------------------------------------- #


def test_source_label_for_multi_collector_deployments() -> None:
    """source_label parameter allows distinguishing multiple collectors."""
    adapter1 = HostedInferenceIngestAdapter(source_label="collector-prod")
    adapter2 = HostedInferenceIngestAdapter(source_label="collector-staging")

    payload = _hosted_inference_call()

    result1 = adapter1.ingest_call(payload)
    assert result1.is_typed
    chain_event1 = result1.typed.to_chain_event(source=adapter1._source)
    assert chain_event1 is not None
    assert chain_event1["source"] == "collector-prod"

    result2 = adapter2.ingest_call(payload)
    assert result2.is_typed
    chain_event2 = result2.typed.to_chain_event(source=adapter2._source)
    assert chain_event2 is not None
    assert chain_event2["source"] == "collector-staging"


# --------------------------------------------------------------------------- #
# Malformed payload rejection                                                 #
# --------------------------------------------------------------------------- #


def test_non_dict_payload_raises(adapter: HostedInferenceIngestAdapter) -> None:
    """A top-level payload that is not a dict or list raises HostedInferenceIngestError."""
    with pytest.raises(HostedInferenceIngestError, match="must be a list or dict"):
        adapter.ingest_payload("not a call")  # type: ignore[arg-type]


def test_non_list_with_non_dict_element_raises(adapter: HostedInferenceIngestAdapter) -> None:
    """A list containing a non-dict element raises HostedInferenceIngestError."""
    with pytest.raises(HostedInferenceIngestError, match="is not a dict"):
        adapter.ingest_payload(["not a dict", "also not a dict"])  # type: ignore[list-item]


def test_empty_list_raises(adapter: HostedInferenceIngestAdapter) -> None:
    """An empty list payload raises HostedInferenceIngestError."""
    with pytest.raises(HostedInferenceIngestError, match="empty list"):
        adapter.ingest_payload([])


def test_missing_principal_id_returns_error_result(adapter: HostedInferenceIngestAdapter) -> None:
    """A call missing principal_id returns an error result."""
    result = adapter.ingest_call(
        {
            "endpoint_base_url": "https://api.openai.com/v1",
            "model": "gpt-4",
            "request_digest": "abc",
            "response_digest": "def",
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "operation": "chat",
        }
    )
    assert result.is_error
    assert "principal_id" in result.parse_error


def test_missing_model_returns_error_result(adapter: HostedInferenceIngestAdapter) -> None:
    """A call missing model returns an error result."""
    result = adapter.ingest_call(
        {
            "principal_id": "user1",
            "endpoint_base_url": "https://api.openai.com/v1",
            "request_digest": "abc",
            "response_digest": "def",
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "operation": "chat",
        }
    )
    assert result.is_error
    assert "model" in result.parse_error


def test_no_partial_state_on_call_in_list_error(adapter: HostedInferenceIngestAdapter) -> None:
    """When call[2] in a list is malformed, no results for calls[0:2] are returned."""
    payload = [
        _hosted_inference_call(principal_id="user1"),
        _hosted_inference_call(principal_id="user2"),
        {"bad": "call"},  # Missing required fields
    ]
    with pytest.raises(HostedInferenceIngestError, match="call\\[2\\]"):
        adapter.ingest_payload(payload)


def test_ingest_payload_function_raises_on_bad_input() -> None:
    """The module-level ingest_payload convenience raises on bad input."""
    with pytest.raises(HostedInferenceIngestError):
        ingest_payload([])  # type: ignore[arg-type]


def test_ingest_payload_function_ok() -> None:
    """The module-level ingest_payload returns results for valid input."""
    payload = _hosted_inference_call()
    results = ingest_payload([payload])
    assert len(results) == 1
    assert results[0].is_typed


# --------------------------------------------------------------------------- #
# Extra attributes preserved                                                  #
# --------------------------------------------------------------------------- #


def test_extra_attributes_preserved_in_activity(adapter: HostedInferenceIngestAdapter) -> None:
    """Extra attributes beyond the known fields are preserved in the activity."""
    payload = _hosted_inference_call(
        metadata={
            "custom_field": "custom_value",
            "another_field": 42,
        }
    )
    result = adapter.ingest_call(payload)
    assert result.is_typed
    activity = result.typed
    assert activity is not None
    assert activity.extra_attributes.get("custom_field") == "custom_value"
    assert activity.extra_attributes.get("another_field") == 42


def test_extra_attributes_in_chain_event(adapter: HostedInferenceIngestAdapter) -> None:
    """Extra attributes appear in the chain event with hosted_inference.extra prefix."""
    payload = _hosted_inference_call(metadata={"custom_field": "custom_value"})
    result = adapter.ingest_call(payload)
    assert result.is_typed
    chain_event = result.typed.to_chain_event()
    assert chain_event is not None
    attrs = chain_event["attributes"]
    assert attrs["hosted_inference.extra.custom_field"] == "custom_value"
