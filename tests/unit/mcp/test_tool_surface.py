"""Tests for tool surface risk classification and capability receipts."""

from __future__ import annotations

from bernstein.mcp.capability import build_capability_card
from bernstein.mcp.tool_surface import (
    AuthPosture,
    CapabilityReceipt,
    RiskClass,
    ServerManifest,
    evaluate_tool_surface_risk,
    is_approval_forced,
    verify_capability_receipt,
)


def test_risky_triple_scores_critical_and_forces_approval() -> None:
    manifest = ServerManifest(
        server_id="risky_test",
        name="Risky Server",
        auth_posture=AuthPosture.STATIC_BEARER_TOKEN.value,
        tools=[
            {"name": "read_secrets", "sensitive_reach": True},
            {"name": "eval_prompt", "untrusted_input": True},
            {"name": "post_webhook", "egress": True},
        ],
    )
    receipt = evaluate_tool_surface_risk(manifest)
    assert receipt.risk_class == RiskClass.CRITICAL
    assert receipt.has_risky_triple is True
    assert receipt.forced_approval is True
    assert verify_capability_receipt(receipt) is True


def test_wildcard_no_auth_scores_critical() -> None:
    manifest = ServerManifest(
        server_id="wildcard_anon",
        name="Wildcard Anonymous",
        auth_posture=AuthPosture.NONE.value,
        has_wildcard_permissions=True,
    )
    receipt = evaluate_tool_surface_risk(manifest)
    assert receipt.risk_class == RiskClass.CRITICAL
    assert receipt.forced_approval is True
    assert receipt.has_risky_triple is False


def test_wildcard_oauth_scores_high() -> None:
    manifest = ServerManifest(
        server_id="wildcard_oauth",
        name="Wildcard OAuth",
        auth_posture=AuthPosture.OAUTH2_PKCE.value,
        has_wildcard_permissions=True,
    )
    receipt = evaluate_tool_surface_risk(manifest)
    assert receipt.risk_class == RiskClass.HIGH
    assert receipt.forced_approval is True


def test_read_only_local_scores_minimal_and_never_forces_approval() -> None:
    manifest = ServerManifest(
        server_id="local_read",
        name="Local Reader",
        auth_posture=AuthPosture.STATIC_BEARER_TOKEN.value,
        tools=[{"name": "read_file", "read_only": True}],
    )
    receipt = evaluate_tool_surface_risk(manifest)
    assert receipt.risk_class == RiskClass.MINIMAL
    assert receipt.forced_approval is False
    assert receipt.has_risky_triple is False


def test_read_only_anonymous_scores_low() -> None:
    manifest = ServerManifest(
        server_id="public_read",
        name="Public Reader",
        auth_posture=AuthPosture.ANONYMOUS.value,
        tools=[{"name": "fetch_doc", "read_only": True}],
    )
    receipt = evaluate_tool_surface_risk(manifest)
    assert receipt.risk_class == RiskClass.LOW
    assert receipt.forced_approval is False


def test_is_approval_forced_deny_by_default_when_no_approver() -> None:
    manifest = ServerManifest(
        server_id="risky_server",
        name="Risky Server",
        has_sensitive_reach=True,
        has_untrusted_input=True,
        has_egress_channel=True,
    )
    receipt = evaluate_tool_surface_risk(manifest)

    # Approver configured: forces approval
    forced, reason = is_approval_forced(receipt, approver_configured=True)
    assert forced is True
    assert "APPROVAL_REQUIRED" in reason

    # No approver configured: denies by default
    forced_denied, deny_reason = is_approval_forced(receipt, approver_configured=False)
    assert forced_denied is True
    assert "DENIED" in deny_reason


def test_capability_receipt_hash_determinism_and_tamper_detection() -> None:
    manifest = ServerManifest(
        server_id="det_server",
        name="Deterministic Server",
        auth_posture=AuthPosture.STATIC_BEARER_TOKEN.value,
        tools=[{"name": "query_db", "sensitive_reach": True}],
    )
    receipt1 = evaluate_tool_surface_risk(manifest)
    receipt2 = evaluate_tool_surface_risk(manifest)

    assert receipt1.receipt_hash == receipt2.receipt_hash
    assert verify_capability_receipt(receipt1) is True

    # Tampered receipt
    tampered = CapabilityReceipt(
        server_id=receipt1.server_id,
        risk_class=RiskClass.MINIMAL,  # tampered risk class
        has_risky_triple=receipt1.has_risky_triple,
        forced_approval=receipt1.forced_approval,
        risk_factors=receipt1.risk_factors,
        auth_posture=receipt1.auth_posture,
        receipt_hash=receipt1.receipt_hash,
    )
    assert verify_capability_receipt(tampered) is False


def test_build_capability_card_includes_tool_surface() -> None:
    card = build_capability_card()
    assert "toolSurface" in card
    tool_surface = card["toolSurface"]
    assert tool_surface["server_id"] == "bernstein"
    assert "receipt_hash" in tool_surface
    assert verify_capability_receipt(tool_surface) is True
