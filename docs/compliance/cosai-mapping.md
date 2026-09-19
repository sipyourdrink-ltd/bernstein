# CoSAI Secure-by-Design evidence pack

`bernstein audit export --standard cosai` maps the CoSAI (Coalition for Secure AI, OASIS Open Project) Secure-by-Design principles for agentic systems and Risk Map controls onto the same HMAC-chained audit events, lineage log, and cost ledger that back the `ai-act`, `owasp-asi`, `owasp-skills`, and `iso-42001` packs. Source: `src/bernstein/compliance/cosai.py`, registered in `src/bernstein/compliance/evidence_pack.py`.

## What this is not

Bernstein cannot certify anybody, and CoSAI guidance does not constitute a formal certification framework. This pack does not claim third-party compliance certification. It provides an operator with per-control evidence derived directly from their own run records, so compliance posture is demonstrated from verifiable execution evidence rather than asserted in spreadsheets.

## Three-state honesty rule

Every mapped control resolves to exactly one of three states. `todo` rows remain explicitly visible in the map so gaps are transparent to operators and auditors.

| Status | Meaning |
|---|---|
| `mapped` | The chain contains records that satisfy the control; the pack cites the concrete artefact and selector. |
| `partial` | The chain covers part of the control; the requirement states what is missing. |
| `todo` | The control is identified in the standard but not currently enforced or chained in Bernstein. |

A map that marked unimplemented controls `mapped` or omitted them entirely would mislead an auditor. Honest accounting is the deliverable.

## Coverage

The control map aligns with the three core principles of the CoSAI WS4 specification (*Secure Design Patterns for Agentic Systems*) and associates matching controls from the CoSAI Risk Map (`controls.yaml`).

### Principle 1: Human-governed and accountable

Agents operate under human oversight, verified cryptographic identities, and bounded user mandates.

| Control ID | Risk Map ID | Requirement (short) | Implementation module | Exercising test | Artefact | Status |
|---|---|---|---|---|---|---|
| `human-governed-accountable.oversight` | `controlHumanApprovalAndIntervention` | Human approval and override gates for privileged actions | `src/bernstein/core/security/approval.py` | `tests/unit/approval/test_card_verify.py` | `audit-chain/events.jsonl` | `mapped` |
| `human-governed-accountable.dual-control` | `controlDualAuthorization` | Dual-authorization and quorum enforcement for critical operations | `src/bernstein/core/security/dual_approval.py` | `tests/unit/approval/test_card_v2.py` | `audit-chain/events.jsonl` | `mapped` |
| `human-governed-accountable.identity` | `controlAgentIdentityAndSigning` | Cryptographic agent cards and signed delegation issuance | `src/bernstein/core/security/agent_card_signer.py` | `tests/unit/mcp/test_capability_card.py` | `audit-chain/events.jsonl` | `mapped` |
| `human-governed-accountable.mandate` | `controlMandateConsentGovernance` | Explicit user mandate and consent tracking | `src/bernstein/core/security/approval.py` | `tests/unit/approval/test_resolution_principal.py` | `audit-chain/events.jsonl` | `mapped` |

### Principle 2: Bounded and resilient

Agency is strictly bounded through capability controls, sandboxing, resource budgets, and supply chain verification.

| Control ID | Risk Map ID | Requirement (short) | Implementation module | Exercising test | Artefact | Status |
|---|---|---|---|---|---|---|
| `bounded-resilient.least-privilege` | `controlLeastPrivilegeCapabilityBounding` | Capability matrix bounding agency against the lethal trifecta | `src/bernstein/core/security/capability_matrix.py` | `tests/unit/security/test_capability_delta.py` | `audit-chain/events.jsonl` | `mapped` |
| `bounded-resilient.sandboxing` | `controlExecutionSandboxing` | Sandboxed execution environments and command allowlists | `src/bernstein/core/security/command_policy.py` | `tests/unit/security/test_hook_gate.py` | `audit-chain/events.jsonl` | `mapped` |
| `bounded-resilient.resource-bounds` | `controlResourceAndCostBounding` | Budget ceilings, token caps, and wall-clock deadlines | `src/bernstein/core/cost/cost_tracker.py` | `tests/unit/cost/test_budget_halt_receipt.py` | `costs/cost_history.jsonl` | `mapped` |
| `bounded-resilient.supply-chain` | `controlSupplyChainIntegrity` | Ed25519 detached signature verification for skill catalog entries | `src/bernstein/agents/catalog.py` | `tests/unit/skills/test_plugin_install_provenance.py` | `audit-chain/events.jsonl` | `mapped` |
| `bounded-resilient.codeguard` | `controlCodeGuardPreset` | Automated CoSAI CodeGuard rule set preset | `src/bernstein/core/security/owasp_asi_detectors.py` | `tests/unit/security/test_approval_gate_fail_closed.py` | n/a | `todo` |

### Principle 3: Transparent and verifiable

Every execution step, artifact state, and trajectory transition produces tamper-evident, verifiable proof.

| Control ID | Risk Map ID | Requirement (short) | Implementation module | Exercising test | Artefact | Status |
|---|---|---|---|---|---|---|
| `transparent-verifiable.audit-trail` | `controlTamperEvidentAuditLogging` | RFC 2104 HMAC-chained audit log across all lifecycle events | `src/bernstein/core/security/audit_chain.py` | `tests/unit/security/test_audit_pack_staged_write.py` | `audit-chain/events.jsonl` | `mapped` |
| `transparent-verifiable.lineage-provenance` | `controlModelAndDataIntegrityManagement` | Content-addressed artifact transparency log with tamper detection | `src/bernstein/core/lineage/spine.py` | `tests/unit/lineage/test_provenance.py` | `lineage/log.jsonl` | `mapped` |
| `transparent-verifiable.replay-reproducibility` | `controlTrajectoryReconstructionAndReplay` | Deterministic replay journal for offline trajectory reconstruction | `src/bernstein/core/replay/journal.py` | `tests/unit/core/replay/test_rederive.py` | `audit-chain/events.jsonl` | `mapped` |
| `transparent-verifiable.context-integrity` | `controlContextAndPromptIntegrity` | Context poisoning screening; multi-session semantic drift remains partial | `src/bernstein/core/security/owasp_asi_detectors.py` | `tests/unit/security/test_lineage_adversarial.py` | `audit-chain/events.jsonl` | `partial` |

11 `mapped`, 1 `partial`, 1 `todo` - 13 of 13 controls counted across the three principles.

## Licensing and Attribution

CoSAI documents are published under the Creative Commons Attribution 4.0 International License (CC BY 4.0). Principle titles are quoted with attribution to the OASIS Open CoSAI Project; requirement summaries are paraphrased.

## Building a pack

```bash
bernstein audit export --standard cosai --out cosai-evidence.zip
```

Produces the standard deterministic zip bundle (`manifest.json`, `controls.json`, `audit-chain/`, `lineage/`, `costs/`, `README.md`) with `manifest.json` reporting `controls_mapped`, `controls_partial`, and `controls_todo` matching the control map.
