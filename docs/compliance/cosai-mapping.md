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
| `human-governed-accountable.identity` | `controlAgentIntegrityManagement` | Cryptographic agent cards and signed delegation issuance | `src/bernstein/core/security/agent_card_signer.py` | `tests/unit/test_agent_card_signer.py` | `audit-chain/events.jsonl` | `mapped` |
| `human-governed-accountable.mandate` | n/a | Explicit payment mandate and consent tracking | `src/bernstein/core/payments/mandate.py` | `tests/unit/test_payment_mandate_audit_chain.py` | `audit-chain/events.jsonl` | `mapped` |

### Principle 2: Bounded and Resilient

Agency is strictly bounded through capability controls, sandboxing, resource budgets, and supply chain verification.

| Control ID | Risk Map ID | Requirement (short) | Implementation module | Exercising test | Artefact | Status |
|---|---|---|---|---|---|---|
| `bounded-resilient.least-privilege` | `controlAgentPluginPermissions` | Capability matrix bounding plugin permissions against the lethal trifecta | `src/bernstein/core/security/capability_matrix.py` | `tests/unit/test_capability_matrix.py` | `audit-chain/events.jsonl` | `mapped` |
| `bounded-resilient.sandboxing` | n/a | Sandboxed execution environments and command allowlists | `src/bernstein/core/security/command_policy.py` | `tests/unit/test_command_policy.py` | n/a | `partial` |
| `bounded-resilient.resource-bounds` | `controlAgentExecutionBounds` | Budget ceilings, token caps, and wall-clock deadlines | `src/bernstein/core/cost/cost_tracker.py` | `tests/unit/test_budget_killswitch.py` | `costs/cost_history.jsonl` | `mapped` |
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

8 `mapped`, 3 `partial`, 2 `todo` - 13 of 13 controls counted across the three principles.

## Licensing and Attribution

CoSAI Technical Steering Committee documents ([cosai-oasis/cosai-tsc](https://github.com/cosai-oasis/cosai-tsc)) are published under the [Creative Commons Attribution 4.0 International License (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/). Principle titles are quoted with attribution to the OASIS Open CoSAI Technical Steering Committee (*CoSAI Principles for Secure-by-Design Agentic Systems*); requirement summaries are paraphrased.

CoSAI Risk Map controls ([cosai-oasis/secure-ai-tooling](https://github.com/cosai-oasis/secure-ai-tooling)) are published under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0).

## Building a pack

```bash
bernstein audit export --standard cosai --out cosai-evidence.zip
```

Produces the standard deterministic zip bundle (`manifest.json`, `controls.json`, `audit-chain/`, `lineage/`, `costs/`, `README.md`) with `manifest.json` reporting `controls_mapped`, `controls_partial`, and `controls_todo` matching the control map.
