"""Data and endpoint conformance vectors (#5061, part of #5054): questions 8, 9 and 10.

Only exported bundle receipts are read through :class:`BundleReader`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from tests.integration.conformance.auditor import scenario

if TYPE_CHECKING:
    from tests.integration.conformance.auditor.bundle_reader import BundleReader

_SENSITIVITY_FIELDS = ("sensitivity", "classification", "data_classification", "sensitivity_label")


@pytest.mark.auditor_question(8)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "#5061 / #5042: docs/customer-list.csv carries sensitivity markers in its file bytes, "
        "but data.read and tool_call record only path and sha256 with no exported "
        "sensitivity / classification / data_classification field"
    ),
)
def test_question8_file_read_sensitivity_recorded(auditor_bundle: BundleReader) -> None:
    """Q8: was the file that was read marked sensitive, and does the record say so?"""
    audit_receipt = auditor_bundle.read_json(scenario.AUDIT_RECEIPT_NAME)
    run_receipt = auditor_bundle.read_json(scenario.RUN_RECEIPT_NAME)

    data_reads = [
        event
        for event in audit_receipt["events"]
        if event.get("event_type") == "data.read" and event.get("resource_id") == scenario.SENSITIVE_PATH
    ]
    assert data_reads, f"expected a data.read audit event for {scenario.SENSITIVE_PATH!r}"

    journal_tool_calls = [
        event
        for event in run_receipt["journal"]["events"]
        if event.get("event") in {"tool_call", "file_read"} and event.get("path") == scenario.SENSITIVE_PATH
    ]
    candidates: list[dict[str, Any]] = [
        {**event, **(event.get("details") if isinstance(event.get("details"), dict) else {})}
        for event in (data_reads + journal_tool_calls)
    ]
    labels = [candidate[field] for candidate in candidates for field in _SENSITIVITY_FIELDS if candidate.get(field)]
    assert labels, (
        f"exported read events for {scenario.SENSITIVE_PATH!r} omit sensitivity classification; "
        f"looked for {list(_SENSITIVITY_FIELDS)}"
    )
    assert any("restricted" in str(label).lower() for label in labels)


@pytest.mark.auditor_question(9)
def test_question9_model_and_endpoint_receiving_content(auditor_bundle: BundleReader) -> None:
    """Q9: which model and endpoint received content?

    Verifies actual content transmission in the bundle rather than spawn configuration
    alone: the ``model.request`` audit event and the ``model_call`` journal event both
    record agent B calling ``scenario.ENDPOINT_MODEL`` at ``scenario.ENDPOINT_BASE_URL``,
    with ``model_call.input_sha256`` matching the exported ``data.read`` file digest and
    matching ``agent_spawned`` endpoint metadata.
    """
    audit_receipt = auditor_bundle.read_json(scenario.AUDIT_RECEIPT_NAME)
    run_receipt = auditor_bundle.read_json(scenario.RUN_RECEIPT_NAME)

    data_reads = [
        event
        for event in audit_receipt["events"]
        if event.get("event_type") == "data.read" and event.get("resource_id") == scenario.SENSITIVE_PATH
    ]
    assert len(data_reads) == 1
    expected_input_sha = data_reads[0]["details"]["sha256"]

    audit_requests = [event for event in audit_receipt["events"] if event.get("event_type") == "model.request"]
    assert [
        (
            event.get("actor"),
            event.get("resource_id"),
            event.get("details", {}).get("adapter"),
            event.get("details", {}).get("model"),
        )
        for event in audit_requests
    ] == [
        (
            scenario.AGENT_B,
            scenario.ENDPOINT_BASE_URL,
            scenario.ENDPOINT_ADAPTER,
            scenario.ENDPOINT_MODEL,
        )
    ]

    journal_events = run_receipt["journal"]["events"]
    model_calls = [event for event in journal_events if event.get("event") == "model_call"]
    assert [
        (
            event.get("agent_id"),
            event.get("model"),
            event.get("base_url"),
            event.get("input_sha256"),
        )
        for event in model_calls
    ] == [
        (
            scenario.AGENT_B,
            scenario.ENDPOINT_MODEL,
            scenario.ENDPOINT_BASE_URL,
            expected_input_sha,
        )
    ]

    spawned_b = [
        event
        for event in journal_events
        if event.get("event") == "agent_spawned" and event.get("agent_id") == scenario.AGENT_B
    ]
    assert len(spawned_b) == 1
    assert (
        spawned_b[0].get("endpoint_adapter_name"),
        spawned_b[0].get("endpoint_model"),
        spawned_b[0].get("endpoint_base_url"),
        spawned_b[0].get("endpoint_profile_name"),
    ) == (
        scenario.ENDPOINT_ADAPTER,
        scenario.ENDPOINT_MODEL,
        scenario.ENDPOINT_BASE_URL,
        scenario.ENDPOINT_PROFILE,
    )


@pytest.mark.auditor_question(10)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "#5061 / #5038: the exported bundle carries no model.admitted/withdrawn or "
        "permitted_endpoints policy record proving the destination endpoint was "
        "permitted for this installation at request time"
    ),
)
def test_question10_endpoint_was_permitted_at_request_time(auditor_bundle: BundleReader) -> None:
    """Q10: was that endpoint one the installation is permitted to use?"""
    audit_receipt = auditor_bundle.read_json(scenario.AUDIT_RECEIPT_NAME)
    events = audit_receipt["events"]
    requests = [event for event in events if event.get("event_type") == "model.request"]
    assert requests, "expected at least one model.request audit event"

    for request in requests:
        req_model = request.get("details", {}).get("model")
        req_endpoint = request.get("resource_id")
        decisions = [
            event
            for event in events
            if event.get("event_type") in {"model.admitted", "model.withdrawn", "endpoint.permitted"}
            and event.get("timestamp", "") <= request.get("timestamp", "")
            and event.get("details", {}).get("model") == req_model
            and event.get("details", {}).get("endpoint") == req_endpoint
        ]
        assert decisions, (
            f"no exported admission decision permits {req_model!r} at {req_endpoint!r} prior to the request (#5038)"
        )
        latest = decisions[-1]
        assert latest.get("event_type") in {"model.admitted", "endpoint.permitted"}
        assert latest.get("details", {}).get("admitted_by"), "admission record must name its authorizer"
