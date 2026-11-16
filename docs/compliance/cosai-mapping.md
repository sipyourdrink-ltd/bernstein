# CoSAI Principles for Secure-by-Design Agentic Systems evidence pack

`bernstein audit export --standard cosai` maps the CoSAI (Coalition for Secure AI, OASIS Open Project) Principles for Secure-by-Design Agentic Systems and Risk Map controls onto the same HMAC-chained audit events, lineage log, and cost ledger that back the `ai-act`, `owasp-asi`, `owasp-skills`, and `iso-42001` packs. Source: `src/bernstein/compliance/cosai.py`, registered in `src/bernstein/compliance/evidence_pack.py`.

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

The control map is organized under the three core principles published by the CoSAI Technical Steering Committee (TSC) in *CoSAI Principles for Secure-by-Design Agentic Systems* ([cosai-oasis/cosai-tsc](https://github.com/cosai-oasis/cosai-tsc), `security-principles-for-agentic-systems.md`). Where the CoSAI Risk Map ([cosai-oasis/secure-ai-tooling](https://github.com/cosai-oasis/secure-ai-tooling), `risk-map/yaml/controls.yaml`) defines a corresponding control, its upstream identifier is listed in the Risk Map ID column; where no direct Risk Map control exists, `n/a` is stated. Sub-clause IDs (e.g. `human-governed-accountable.oversight`) are Bernstein's internal control identifiers mapping against the TSC principles.

### Principle 1: Human-governed and Accountable

Agents operate under human oversight, verified cryptographic identities, and bounded user mandates.

| Control ID | Risk Map ID | Requirement (short) | Implementation module | Exercising test | Artefact | Status |
|---|---|---|---|---|---|---|
| `human-governed-accountable.oversight` | `controlAgentPluginUserControl` | Human approval and user control for sensitive actions | `src/bernstein/core/security/approval.py` | `tests/unit/test_approval.py` | `audit-chain/events.jsonl` | `mapped` |
| `human-governed-accountable.dual-control` | n/a | Multi-party quorum enforcement for critical operations | n/a | n/a | n/a | `todo` |
| `human-governed-accountable.identity` | `controlAgentIntegrityManagement` | Delegation minting is chained; agent card signing is not (no audit event) | `src/bernstein/core/security/agent_card_signer.py` | `tests/unit/test_agent_card_signer.py` | `audit-chain/events.jsonl` | `partial` |
| `human-governed-accountable.mandate` | n/a | Explicit payment mandate and consent tracking | `src/bernstein/core/payments/mandate.py` | `tests/unit/test_payment_mandate_audit_chain.py` | `audit-chain/events.jsonl` | `mapped` |

### Principle 2: Bounded and Resilient

Agency is strictly bounded through capability controls, sandboxing, resource budgets, and supply chain verification.

| Control ID | Risk Map ID | Requirement (short) | Implementation module | Exercising test | Artefact | Status |
|---|---|---|---|---|---|---|
| `bounded-resilient.least-privilege` | `controlAgentPluginPermissions` | Capability matrix bounding plugin permissions against the lethal trifecta | `src/bernstein/core/security/capability_matrix.py` | `tests/unit/test_capability_matrix.py` | `audit-chain/events.jsonl` | `mapped` |
| `bounded-resilient.sandboxing` | n/a | Sandboxed execution environments and command allowlists | `src/bernstein/core/security/command_policy.py` | `tests/unit/test_command_policy.py` | n/a | `partial` |
| `bounded-resilient.resource-bounds` | `controlAgentExecutionBounds` | Recorded spend vs budget | `src/bernstein/core/cost/cost_tracker.py` | `tests/unit/test_budget_killswitch.py` | `costs/cost_history.jsonl` | `mapped` |
| `bounded-resilient.codeguard` | n/a | Automated CoSAI CodeGuard rule set preset | n/a | n/a | n/a | `todo` |

### Principle 3: Transparent and Verifiable

Every execution step, artifact state, and trajectory transition produces tamper-evident, verifiable proof.

| Control ID | Risk Map ID | Requirement (short) | Implementation module | Exercising test | Artefact | Status |
|---|---|---|---|---|---|---|
| `transparent-verifiable.supply-chain` | n/a | Ed25519 signature verification for skill packages | `src/bernstein/core/skills/catalog/signature.py` | `tests/unit/core/skills/test_catalog_verify.py` | `audit-chain/events.jsonl` | `mapped` |
| `transparent-verifiable.audit-trail` | `controlAgentObservability` | RFC 2104 HMAC-chained audit log across all lifecycle events | `src/bernstein/core/security/audit_chain.py` | `tests/unit/test_audit_chain_memory_write.py` | `audit-chain/events.jsonl` | `mapped` |
| `transparent-verifiable.lineage-provenance` | `controlModelAndDataIntegrityManagement` | Content-addressed artifact transparency log with tamper detection | `src/bernstein/core/lineage/store.py` | `tests/unit/lineage/test_store.py` | `lineage/log.jsonl` | `mapped` |
| `transparent-verifiable.replay-reproducibility` | n/a | Trajectory logging in audit chain; step journal local to .sdd/runtime/ | `src/bernstein/core/replay/journal.py` | `tests/unit/core/replay/test_journal_identity.py` | `audit-chain/events.jsonl` | `partial` |
| `transparent-verifiable.context-integrity` | `controlInputValidationAndSanitization` | Input validation and context capsule logging; semantic drift remains partial | `src/bernstein/core/security/owasp_asi_detectors.py` | `tests/unit/test_owasp_asi_detectors.py` | `audit-chain/events.jsonl` | `partial` |

7 `mapped`, 4 `partial`, 2 `todo` - 13 of 13 controls counted across the three principles.

## MCP threat classes (#6217)

The same `cosai` standard also includes the twelve classes in section 6.2 of
*Model Context Protocol (MCP) Security*, dated **2026-01-08**, at source revision
[`7ec1306f2f55563f6eeef9d36a6bb2b531491ceb`](https://github.com/cosai-oasis/ws4-secure-design-agentic-systems/blob/7ec1306f2f55563f6eeef9d36a6bb2b531491ceb/model-context-protocol-security.md#62-threat-details).
IDs and class names follow that pinned section, including its
“Input/Instruction” title for MCP-T4. The paper title, revision, date, section
and class name are retained in every MCP row's exported `requirement` string.
They add no pack schema fields or second standard.

**Covered** corresponds to `mapped`, **Partial** to `partial`, and **Not covered**
to `todo`. No whole MCP threat class is marked Covered: the pre-load gate
provides narrower evidence. A successful signature or scan is not proof of
runtime authentication, confidentiality, transport isolation or comprehensive
monitoring. The table states the demonstrated control and its remaining scope.

| Control ID | Risk Map ID | Requirement (class and bounded control) | Implementation module | Exercising test | Artefact | Status | Gate chain events | Verdict |
|---|---|---|---|---|---|---|---|---|
| `MCP-T1` | n/a | Improper Authentication and Identity Management: The stock managed path does not supply bundle source for the optional scanner. Gap: Runtime authentication and credential protection are not established by manifest signing. | n/a | n/a | n/a | `todo` | n/a | Not covered |
| `MCP-T2` | n/a | Missing or Improper Access Control: The load gate records server admission, not per-tool or per-object access control. Gap: The stock managed path does not supply bundle source for optional scope-pattern checks. | n/a | n/a | n/a | `todo` | n/a | Not covered |
| `MCP-T3` | n/a | Input Validation/Sanitization Failures: The stock managed path does not supply bundle source for the optional scanner. Gap: Runtime input validation and injection-context controls need separate evidence. | n/a | n/a | n/a | `todo` | n/a | Not covered |
| `MCP-T4` | n/a | Input/Instruction Boundary Distinction Failure: Manifest signatures do not separate instructions from tool or resource data. Gap: No instruction/data boundary control is established by the pre-load signing gate. | n/a | n/a | n/a | `todo` | n/a | Not covered |
| `MCP-T5` | n/a | Inadequate Data Protection and Confidentiality Controls: The stock managed path does not supply bundle source for the optional scanner. Gap: Data classification, encryption and runtime confidentiality need separate evidence. | n/a | n/a | n/a | `todo` | n/a | Not covered |
| `MCP-T6` | n/a | Missing Integrity/Verification Controls: Manifest signature, trusted-publisher policy and load admission verdicts are recorded. Gap: The stock path supplies no bundle bytes; bundle-hash verification is not evidenced, nor is continuous behavioral attestation. | `src/bernstein/core/protocols/mcp/mcp_signing_policy.py` | `tests/unit/compliance/test_cosai_mcp_map.py` | `audit-chain/events.jsonl` | `partial` | `mcp.signature_verification,mcp.load_decision` | Partial |
| `MCP-T7` | n/a | Session and Transport Security Failures: The pre-load signing gate supplies no session or transport security evidence. Gap: Session binding, transport encryption and session termination need separate evidence. | n/a | n/a | n/a | `todo` | n/a | Not covered |
| `MCP-T8` | n/a | Network Binding/Isolation Failures: The pre-load signing gate supplies no network binding or isolation evidence. Gap: Interface binding, DNS rebinding defenses and network segmentation need separate evidence. | n/a | n/a | n/a | `todo` | n/a | Not covered |
| `MCP-T9` | n/a | Trust Boundary and Privilege Design Failures: Trusted-publisher policy and strict or warning admission decisions are recorded. Gap: Server admission does not prove transitive trust boundaries or runtime privilege separation. | `src/bernstein/core/protocols/mcp/mcp_signing_policy.py` | `tests/unit/compliance/test_cosai_mcp_map.py` | `audit-chain/events.jsonl` | `partial` | `mcp.signature_verification,mcp.load_decision` | Partial |
| `MCP-T10` | n/a | Resource Management/Rate Limiting Absence: The pre-load signing gate supplies no runtime resource or rate-limit evidence. Gap: Token, request and payload quotas need separate runtime evidence. | n/a | n/a | n/a | `todo` | n/a | Not covered |
| `MCP-T11` | n/a | Supply Chain and Lifecycle Security Failures: Manifest signature, trusted-publisher policy and load admission verdicts are recorded. Gap: The stock path does not establish bundle-hash verification, completed scanning or the complete acquisition-to-retirement lifecycle. | `src/bernstein/core/protocols/mcp/mcp_signing_policy.py` | `tests/unit/compliance/test_cosai_mcp_map.py` | `audit-chain/events.jsonl` | `partial` | `mcp.signature_verification,mcp.load_decision` | Partial |
| `MCP-T12` | n/a | Insufficient Logging, Monitoring, and Auditability: The enabled gate chains verification, scan status and linked admission decisions with server identity. Gap: These load events do not establish comprehensive tool-call, data-access or anomaly monitoring coverage. | `src/bernstein/core/protocols/mcp/mcp_signing_policy.py` | `tests/unit/compliance/test_cosai_mcp_map.py` | `audit-chain/events.jsonl` | `partial` | `mcp.signature_verification,mcp.bundle_scan,mcp.load_decision` | Partial |

4 MCP rows are `partial`; 8 are `todo`. Combined with the unchanged thirteen
principle rows: 7 `mapped`, 8 `partial`, 10 `todo` - 25 of 25 controls counted.

The stock manager supplies neither bundle source nor bundle bytes to the gate
([existing limitation](../security/mcp-signing.md)). Scan-backed classes are
therefore Not covered here, even if a caller supplies source to the optional
scanner in an isolated fixture. Manifest-signature success does not establish
bundle-hash verification or instruction/data separation.

Events come from the existing enabled signing gate. `mcp.signature_verification`
records its verification verdict; `mcp.bundle_scan` records completed or skipped
scan status, reasons and findings; `mcp.load_decision` links both records and
states strict/warn policy and admission. A skipped scan is not a successful
scan, and a warn-mode admission does not mean verification passed. Selectors
identify evidence to inspect; their presence alone is not a compliance verdict.

The regression fixture runs the real gate with a signed clean server and a
signed server refused by the static scan, verifies the chain and builds the
same deterministic ZIP twice. It checks the exported IDs, names, pinned
provenance and both servers' actual gate events. No server process or model
call is required.

The MCP class names are attributed to OASIS Open, *Model Context Protocol (MCP)
Security*, Workstream 4: Secure Design Patterns for Agentic Systems. Copyright
© OASIS Open 2025. All Rights Reserved. Descriptions above are Bernstein's
bounded control mapping, not a copy of the paper's threat descriptions. See
the pinned paper's [copyright notice and permissions](https://github.com/cosai-oasis/ws4-secure-design-agentic-systems/blob/7ec1306f2f55563f6eeef9d36a6bb2b531491ceb/model-context-protocol-security.md#66-copyright-notice).

## MCP paper copyright notice

The following section is reproduced from the pinned paper under its stated
permission for copies and explanatory derivative works.

Copyright © OASIS Open 2025. All Rights Reserved. This document has been produced under the process and license terms stated in the OASIS Open Project rules: [https://www.oasis-open.org/policies-guidelines/open-projects-process](https://www.oasis-open.org/policies-guidelines/open-projects-process).

This document and translations of it may be copied and furnished to others, and derivative works that comment on or otherwise explain it or assist in its implementation may be prepared, copied, published, and distributed, in whole or in part, without restriction of any kind, provided that the above copyright notice and this section are included on all such copies and derivative works. The limited permissions granted above are perpetual and will not be revoked by OASIS or its successors or assigns. This document and the information contained herein is provided on an "AS IS" basis and OASIS DISCLAIMS ALL WARRANTIES, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTY THAT THE USE OF THE INFORMATION HEREIN WILL NOT INFRINGE ANY OWNERSHIP RIGHTS OR ANY IMPLIED WARRANTIES OF MERCHANTABILITY OR FITNESS FOR A PARTICULAR PURPOSE. OASIS AND ITS MEMBERS WILL NOT BE LIABLE FOR ANY DIRECT, INDIRECT, SPECIAL OR CONSEQUENTIAL DAMAGES ARISING OUT OF ANY USE OF THIS DOCUMENT OR ANY PART THEREOF. The name "OASIS" is a trademark of OASIS, the owner and developer of this document, and should be used only to refer to the organization and its official outputs. OASIS welcomes reference to, and implementation and use of, documents, while reserving the right to enforce its marks against misleading uses. Please see [https://www.oasis-open.org/policies-guidelines/trademark/](https://www.oasis-open.org/policies-guidelines/trademark/) for above guidance.

This is a Non-Standards Track Work Product. The patent provisions of the OASIS IPR Policy do not apply.

## Licensing and Attribution

CoSAI Technical Steering Committee documents ([cosai-oasis/cosai-tsc](https://github.com/cosai-oasis/cosai-tsc)) are published under the [Creative Commons Attribution 4.0 International License (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/). Principle titles are quoted with attribution to the OASIS Open CoSAI Technical Steering Committee (*CoSAI Principles for Secure-by-Design Agentic Systems*); requirement summaries are paraphrased.

CoSAI Risk Map controls ([cosai-oasis/secure-ai-tooling](https://github.com/cosai-oasis/secure-ai-tooling)) are published under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0).

## Building a pack

```bash
bernstein audit export --standard cosai --out cosai-evidence.zip
```

Produces the standard deterministic zip bundle (`manifest.json`, `controls.json`, `audit-chain/`, `lineage/`, `costs/`, `README.md`) with `manifest.json` reporting `controls_mapped`, `controls_partial`, and `controls_todo` matching the control map.
