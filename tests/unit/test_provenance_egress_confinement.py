"""Egress confinement: propagated taint feeds the trifecta gate and auto-approve.

The capability matrix carries UNTRUSTED_INPUT by *data* (a propagated trust
class), not only by static tool tags; auto-approve never returns APPROVE for a
command whose text derives from an untrusted-origin artefact.
"""

from __future__ import annotations

from bernstein.core.lineage.provenance import TrustClass
from bernstein.core.security.auto_approve import Decision, classify_command, classify_tool_call
from bernstein.core.security.capability_matrix import (
    Capability,
    CapabilityRegistry,
    EnforcementMode,
    ToolCapabilities,
)

# ---------------------------------------------------------------------------
# capability_matrix: taint carried by data, not by static tags
# ---------------------------------------------------------------------------


def _registry() -> CapabilityRegistry:
    reg = CapabilityRegistry(mode=EnforcementMode.ENFORCE)
    # Three individually-safe tools: read private data, transform, post out.
    reg.register(ToolCapabilities("fs.read_secret", frozenset({Capability.PRIVATE_DATA})))
    reg.register(ToolCapabilities("text.transform", frozenset()))
    reg.register(ToolCapabilities("github.post_comment", frozenset({Capability.EXTERNAL_COMM})))
    return reg


def test_static_tags_alone_pass_without_operand_trust() -> None:
    reg = _registry()
    decision = reg.evaluate_chain(["fs.read_secret", "text.transform", "github.post_comment"])
    # No tool statically carries UNTRUSTED_INPUT, so the trifecta is incomplete.
    assert decision.allowed is True
    assert Capability.UNTRUSTED_INPUT not in decision.triggered


def test_tainted_operand_completes_trifecta_and_denies() -> None:
    reg = _registry()
    decision = reg.evaluate_chain(
        ["fs.read_secret", "text.transform", "github.post_comment"],
        operand_trust=[TrustClass.THIRD_PARTY],
    )
    # The data path now carries UNTRUSTED_INPUT even though no tool tag does.
    assert Capability.UNTRUSTED_INPUT in decision.triggered
    assert decision.allowed is False
    assert decision.triggered >= frozenset(Capability)


def test_trusted_operand_does_not_add_untrusted_input() -> None:
    reg = _registry()
    decision = reg.evaluate_chain(
        ["fs.read_secret", "text.transform", "github.post_comment"],
        operand_trust=[TrustClass.OPERATOR, TrustClass.WORKSPACE],
    )
    assert Capability.UNTRUSTED_INPUT not in decision.triggered
    assert decision.allowed is True


def test_unknown_operand_trust_fails_closed() -> None:
    reg = _registry()
    # ``None`` inside the operand list means provenance could not be resolved:
    # fail closed by treating it as untrusted input.
    decision = reg.evaluate_chain(
        ["fs.read_secret", "text.transform", "github.post_comment"],
        operand_trust=[None],
    )
    assert Capability.UNTRUSTED_INPUT in decision.triggered
    assert decision.allowed is False


def test_operand_trust_backward_compatible_default() -> None:
    reg = _registry()
    a = reg.evaluate_chain(["fs.read_secret", "github.post_comment"])
    b = reg.evaluate_chain(["fs.read_secret", "github.post_comment"], operand_trust=None)
    assert a == b


# ---------------------------------------------------------------------------
# auto_approve: derived taint downgrades APPROVE -> ASK
# ---------------------------------------------------------------------------


def test_safe_command_still_approves_without_taint() -> None:
    result = classify_command("ls -la")
    assert result.decision is Decision.APPROVE


def test_safe_command_downgraded_to_ask_when_derived_from_taint() -> None:
    result = classify_command("ls -la", derived_trust=TrustClass.THIRD_PARTY)
    assert result.decision is Decision.ASK
    assert "untrust" in result.reason.lower() or "taint" in result.reason.lower()


def test_taint_never_upgrades_a_deny() -> None:
    result = classify_command("rm -rf /", derived_trust=TrustClass.PUBLIC)
    assert result.decision is Decision.DENY


def test_trusted_derivation_leaves_approve_intact() -> None:
    result = classify_command("ls -la", derived_trust=TrustClass.OPERATOR)
    assert result.decision is Decision.APPROVE


def test_classify_tool_call_downgrades_safe_tool_when_tainted() -> None:
    approved = classify_tool_call("Read", {"file_path": "x"})
    assert approved.decision is Decision.APPROVE
    downgraded = classify_tool_call("Read", {"file_path": "x"}, derived_trust=TrustClass.PUBLIC)
    assert downgraded.decision is Decision.ASK
