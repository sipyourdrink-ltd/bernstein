"""Tool-surface risk evaluation benchmark suite and replay adapter.

Evaluates an MCP server's declared and discoverable tool surface for security
risk factors (untrusted input, sensitive reach, egress channels, wildcard permissions,
auth postures) and validates the enforcement of approval gates for the "risky triple".

Controls covered:
  - CTRL-TOOL-INVENTORY: Complete inventory and classification of all tool surfaces
  - ASI02: Mitigation of tool misuse, over-privileged tool surface, and unauthorized egress
  - AST04: Deterministic approval gate enforcement on lethal tool combinations
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from bernstein.eval.bench.suite import BenchSuite, BenchTask
from bernstein.mcp.tool_surface import (
    ServerManifest,
    evaluate_tool_surface_risk,
    is_approval_forced,
    verify_capability_receipt,
)

CONTROLS_COVERED: tuple[str, ...] = ("CTRL-TOOL-INVENTORY", "ASI02", "AST04")

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "cases" / "tool_surface"


def get_tool_surface_fixtures() -> list[ServerManifest]:
    """Load all synthetic tool surface fixture manifests."""
    manifests: list[ServerManifest] = []
    if _FIXTURE_DIR.is_dir():
        for yaml_path in sorted(_FIXTURE_DIR.glob("*.yaml")):
            manifest = ServerManifest.from_yaml(yaml_path)
            manifests.append(manifest)
    return manifests


def build_tool_surface_suite() -> BenchSuite:
    """Build the canonical ``tool-surface-v1`` benchmark suite."""
    manifests = get_tool_surface_fixtures()
    tasks: list[BenchTask] = []

    for manifest in manifests:
        expected_receipt = evaluate_tool_surface_risk(manifest)
        is_risky_triple = expected_receipt.has_risky_triple
        is_read_only = (
            not expected_receipt.risk_factors.get("sensitive_reach", False)
            and not expected_receipt.risk_factors.get("egress_channel", False)
            and not expected_receipt.risk_factors.get("untrusted_input", False)
            and not expected_receipt.risk_factors.get("wildcard_permissions", False)
        )

        assertions: list[dict[str, Any]] = [
            {"kind": "risk_class_eq", "expected": expected_receipt.risk_class.value},
            {"kind": "risky_triple_eq", "expected": is_risky_triple},
            {"kind": "forced_approval_eq", "expected": expected_receipt.forced_approval},
            {"kind": "receipt_hash_valid", "expected": True},
            {"kind": "controls_present", "controls": list(CONTROLS_COVERED)},
        ]
        if is_risky_triple:
            assertions.append({"kind": "triple_detection_rate", "min_rate": 1.0})
            assertions.append({"kind": "deny_by_default_when_no_approver", "expected": True})
        if is_read_only:
            assertions.append({"kind": "read_only_never_force_approval", "expected": True})

        task = BenchTask(
            id=f"tool_surface_{manifest.server_id}",
            description=f"Evaluate tool surface risk and approval gating for server '{manifest.server_id}'.",
            steps=(
                f"parse manifest for {manifest.server_id}",
                "compute risk class and capability receipt",
                "evaluate risky triple and approval gate invariants",
                "verify deny-by-default when no approver is configured",
                "validate receipt cryptographic integrity",
            ),
            assertions=tuple(assertions),
            category="tool_surface",
        )
        tasks.append(task)

    return BenchSuite(version="tool-surface-v1", tasks=tasks)


class ToolSurfaceReplayAdapter:
    """In-process replay adapter for tool surface risk evaluation suite."""

    def __init__(self, fixture_manifests: dict[str, ServerManifest] | None = None) -> None:
        if fixture_manifests is not None:
            self._manifests = fixture_manifests
        else:
            manifest_list = get_tool_surface_fixtures()
            self._manifests = {f"tool_surface_{m.server_id}": m for m in manifest_list}
            # Also index by plain server_id
            for m in manifest_list:
                self._manifests[m.server_id] = m

    def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
        """Execute tool surface evaluation for the task and return the run receipt."""
        manifest = self._manifests.get(task.id)
        if manifest is None:
            # Fallback if task id starts with tool_surface_
            raw_id = task.id.removeprefix("tool_surface_")
            manifest = self._manifests.get(raw_id)

        if manifest is None:
            raise ValueError(f"Unknown fixture manifest for task {task.id}")

        receipt = evaluate_tool_surface_risk(manifest)
        task_hash = task.content_hash()
        journal_head = hashlib.sha256(f"journal:{task_hash}:{receipt.receipt_hash}".encode()).hexdigest()
        spine_head = hashlib.sha256(f"spine:{task_hash}:{receipt.receipt_hash}".encode()).hexdigest()

        # Evaluate approval enforcement with and without approver configured
        forced_with_approver, _reason_with_approver = is_approval_forced(receipt, approver_configured=True)
        _forced_no_approver, reason_no_approver = is_approval_forced(receipt, approver_configured=False)
        is_denied_when_no_approver = "DENIED:" in reason_no_approver if receipt.forced_approval else False

        events = [
            {
                "seq": 0,
                "kind": "tool_surface.evaluated",
                "server_id": manifest.server_id,
                "risk_class": receipt.risk_class.value,
                "has_risky_triple": receipt.has_risky_triple,
                "forced_approval": receipt.forced_approval,
            },
            {
                "seq": 1,
                "kind": "tool_surface.gate_checked",
                "forced_with_approver": forced_with_approver,
                "denied_when_no_approver": is_denied_when_no_approver,
            },
            {
                "seq": 2,
                "kind": "tool_surface.receipt_verified",
                "receipt_hash": receipt.receipt_hash,
                "valid": verify_capability_receipt(receipt),
            },
        ]

        return {
            "journal_head": journal_head,
            "spine_head": spine_head,
            "run_id": f"tool-surface-{task_hash[:12]}",
            "server_id": manifest.server_id,
            "risk_class": receipt.risk_class.value,
            "has_risky_triple": receipt.has_risky_triple,
            "forced_approval": receipt.forced_approval,
            "deny_by_default": is_denied_when_no_approver,
            "controls": list(CONTROLS_COVERED),
            "capability_receipt": receipt.to_dict(),
            "events": events,
        }

    def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        """Score task run receipt against assertions."""
        cap_receipt_dict = receipt.get("capability_receipt")
        if not cap_receipt_dict:
            return False, 0.0, {"error": "Missing capability_receipt in run receipt"}

        if not verify_capability_receipt(cap_receipt_dict):
            return False, 0.0, {"error": "Capability receipt cryptographic verification failed"}

        rc_val = receipt.get("risk_class")
        has_triple = receipt.get("has_risky_triple", False)
        forced_approval = receipt.get("forced_approval", False)
        deny_by_default = receipt.get("deny_by_default", False)

        for assertion in task.assertions:
            kind = assertion.get("kind")
            if kind == "risk_class_eq" and rc_val != assertion.get("expected"):
                return False, 0.0, {"error": f"RiskClass mismatch: {rc_val} != {assertion.get('expected')}"}
            if kind == "risky_triple_eq" and has_triple != assertion.get("expected"):
                return False, 0.0, {"error": f"Risky triple mismatch: {has_triple} != {assertion.get('expected')}"}
            if kind == "forced_approval_eq" and forced_approval != assertion.get("expected"):
                exp = assertion.get("expected")
                return False, 0.0, {"error": f"Forced approval mismatch: {forced_approval} != {exp}"}
            if kind == "triple_detection_rate" and has_triple is not True:
                return False, 0.0, {"error": "Risky triple not detected"}
            if kind == "deny_by_default_when_no_approver" and not deny_by_default:
                return False, 0.0, {"error": "Expected deny-by-default when no approver configured"}
            if kind == "read_only_never_force_approval" and forced_approval is True:
                return False, 0.0, {"error": "Read-only fixture must never force approval"}

        harness_output = {
            "status": "PASS",
            "server_id": receipt.get("server_id"),
            "risk_class": rc_val,
            "has_risky_triple": has_triple,
            "forced_approval": forced_approval,
            "receipt_hash": cap_receipt_dict.get("receipt_hash"),
            "controls": receipt.get("controls", list(CONTROLS_COVERED)),
        }
        return True, 1.0, harness_output
