"""Webhook payload-to-event templates, content-addressed first (#2548).

Covers the acceptance criterion: webhook payload bytes are content-addressed
into the chain before template rendering; re-rendering from the recorded bytes
is byte-identical; a render failure produces a diagnostic feed event carrying
the payload digest and never the payload.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.events.webhook_template import (
    WebhookTemplate,
    canonical_event_json,
    content_address,
    render,
)
from bernstein.core.security.audit_chain import (
    EVENT_FEED_RENDER_FAILURE,
    EVENT_WEBHOOK_PAYLOAD_ANCHOR,
    AuditChainStore,
    record_render_failure,
    record_webhook_payload_anchor,
)

_TEMPLATE = WebhookTemplate(
    template_id="ci_run",
    label="external.ci_run",
    resource_path="run.id",
    related_paths=("run.repo", "run.commit"),
)


def test_content_address_is_deterministic() -> None:
    payload = b'{"run": {"id": "r-42"}}'
    assert content_address(payload) == content_address(payload)
    assert content_address(payload).startswith("sha256:")


def test_render_is_byte_identical_from_recorded_bytes() -> None:
    payload = json.dumps({"run": {"id": "r-42", "repo": "acme/app", "commit": "abc"}}).encode("utf-8")
    first = render(_TEMPLATE, payload)
    second = render(_TEMPLATE, payload)
    assert first.ok
    assert first.event is not None
    assert canonical_event_json(first.event) == canonical_event_json(second.event or {})
    assert first.event["resource_id"] == "r-42"
    assert first.event["related_resource_ids"] == ["abc", "acme/app"]
    assert first.event["payload_digest"] == content_address(payload)


def test_anchor_precedes_render_and_carries_only_digest(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    payload = json.dumps({"run": {"id": "r-7"}}).encode("utf-8")
    digest = content_address(payload)

    # Content-address the raw bytes into the chain BEFORE rendering.
    anchor = record_webhook_payload_anchor(chain=chain, payload_digest=digest, source="acme", template_id="ci_run")
    assert anchor.event_type == EVENT_WEBHOOK_PAYLOAD_ANCHOR
    assert anchor.details["payload_digest"] == digest
    # The anchor records the digest only, never payload content.
    assert "r-7" not in json.dumps(anchor.details)

    result = render(_TEMPLATE, payload)
    assert result.ok


def test_render_failure_emits_diagnostic_without_payload(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    secret_payload = b'{"broken": "s3cr3t-value" '  # invalid JSON (unterminated)
    result = render(_TEMPLATE, secret_payload)
    assert not result.ok
    assert result.error_kind == "invalid_json"
    assert result.event is None

    diag = record_render_failure(
        chain=chain,
        payload_digest=result.payload_digest,
        template_id=_TEMPLATE.template_id,
        error_kind=result.error_kind or "unknown",
        source="acme",
    )
    assert diag.event_type == EVENT_FEED_RENDER_FAILURE
    assert diag.details["payload_digest"] == result.payload_digest
    # The diagnostic must never carry the payload content.
    assert "s3cr3t-value" not in json.dumps(diag.details)
    ok, errors = chain.verify()
    assert ok, errors


def test_missing_required_path_fails_with_digest() -> None:
    payload = json.dumps({"run": {"repo": "acme/app"}}).encode("utf-8")  # no run.id
    result = render(_TEMPLATE, payload)
    assert not result.ok
    assert result.error_kind == "missing_resource"
    assert result.payload_digest == content_address(payload)


def test_non_scalar_resource_does_not_leak_payload() -> None:
    # run.id resolves to a nested object; str()-ing it would serialise the whole
    # subtree (including the secret) into resource_id (#2653).
    payload = json.dumps({"run": {"id": {"nested": "s3cr3t-value"}}}).encode("utf-8")
    result = render(_TEMPLATE, payload)
    assert not result.ok
    assert result.error_kind == "invalid_resource"
    assert result.event is None
    assert result.payload_digest == content_address(payload)


def test_boolean_and_empty_resource_are_rejected() -> None:
    for raw in (True, False, "", [1, 2], None):
        payload = json.dumps({"run": {"id": raw}}).encode("utf-8")
        result = render(_TEMPLATE, payload)
        assert not result.ok, raw
        assert result.error_kind == "invalid_resource", raw


def test_integer_resource_is_accepted() -> None:
    payload = json.dumps({"run": {"id": 4242}}).encode("utf-8")
    result = render(_TEMPLATE, payload)
    assert result.ok
    assert result.event is not None
    assert result.event["resource_id"] == "4242"
