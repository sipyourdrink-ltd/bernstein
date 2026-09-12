"""Data and endpoint questions answered only from the exported bundle (#5061)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.conformance.auditor import recorder

if TYPE_CHECKING:
    from tests.conformance.auditor.bundle import BundleReader


@pytest.mark.question(8)
def test_q8_the_record_states_the_sensitivity_of_the_file_read(bundle_reader: BundleReader) -> None:
    """Q8: the exported read names the file and its actual classification.

    Unlike the issue's initial expectation, the committed recording already
    exports ``restricted`` in both receipts. Pin that answer, not just the
    existence of a read. This says nothing about unrecorded production reads.
    """
    run = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    reads = [event for event in run["journal"]["events"] if event["event"] == "file_read"]
    assert [(event["agent_id"], event["path"], event.get("sensitivity")) for event in reads] == [
        ("agent-b", "config/customer_records.yaml", "restricted")
    ]
    audit = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    reads = [event for event in audit["events"] if event["event_type"] == "data.read"]
    assert [(event["resource_id"], event["details"].get("sensitivity")) for event in reads] == [
        ("config/customer_records.yaml", "restricted")
    ]


@pytest.mark.question(9)
def test_q9_the_record_names_the_model_and_endpoint_that_received_content(bundle_reader: BundleReader) -> None:
    """Q9: identify the delegated request, not the parent agent's endpoint."""
    run = bundle_reader.read_json(recorder.RUN_RECEIPT_NAME)
    events = run["journal"]["events"]
    requests = [event for event in events if event["event"] == "model_request"]
    assert [(event["agent_id"], event["model"], event["endpoint"]) for event in requests] == [
        ("agent-b", "delegated-worker", "https://models.example.invalid/v1")
    ]
    spawned = {event["agent_id"]: event for event in events if event["event"] == "agent_spawned"}
    for request in requests:
        agent = spawned[request["agent_id"]]
        assert (agent["endpoint_model"], agent["endpoint_base_url"]) == (request["model"], request["endpoint"])
    audit = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    requests = [event for event in audit["events"] if event["event_type"] == "model.request"]
    assert [(event["details"]["model"], event["resource_id"]) for event in requests] == [
        ("delegated-worker", "https://models.example.invalid/v1")
    ]


@pytest.mark.question(10)
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "the exported bundle has no model.admitted/withdrawn history binding the "
        "requested model and endpoint to an unexpired permission at request time (#5038)"
    ),
)
def test_q10_the_endpoint_was_permitted_when_content_was_sent(bundle_reader: BundleReader) -> None:
    """Q10: a configured or observed endpoint is not an admission decision.

    Read the exported chain only. A future permission record must identify
    the endpoint as well as the model, name the admitting principal and be
    live when the request occurred; a later admission cannot justify it.
    """
    audit = bundle_reader.read_json(recorder.AUDIT_RECEIPT_NAME)
    events = audit["events"]
    requests = [event for event in events if event["event_type"] == "model.request"]
    assert requests, "the bundle must record the request being assessed"
    for request in requests:
        decisions = [
            event
            for event in events
            if event["event_type"] in ("model.admitted", "model.withdrawn")
            and event["timestamp"] <= request["timestamp"]
            and event["details"].get("model") == request["details"]["model"]
            and event["details"].get("endpoint") == request["resource_id"]
        ]
        assert decisions, "no exported admission history for the requested model/endpoint (#5038)"
        latest = decisions[-1]
        assert latest["event_type"] == "model.admitted", "permission was withdrawn before the request"
        assert latest["details"].get("admitted_by"), "the admission does not name its principal"
        assert latest["details"].get("expires_at", "") > request["timestamp"], "permission expired before the request"
