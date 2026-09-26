# CISA / Five Eyes Agentic AI Risk Crosswalk

**Source:** CISA / NSA / ACSC / Five Eyes, *Careful adoption of agentic AI services*  
**First published:** 1 May 2026  
**Evidence-pack standard:** `cisa-agentic`

This crosswalk maps the 23 risks identified in the May 2026 joint
guidance to Bernstein mechanisms and audit-chain evidence.

Verdicts use the issue terminology:

- **Covered** — Bernstein has an existing mechanism and a chain event
  that can evidence the control.
- **Partial** — Bernstein has related mechanisms, but the implementation
  does not fully address the published risk.
- **Not covered** — Bernstein has no existing mechanism for the risk.

The implementation uses `mapped`, `partial`, and `todo` internally.
A `todo` row is intentionally non-selectable in evidence packs.

| ID | CISA / Five Eyes risk | Bernstein mechanism | Implementing module | Chain event type | Verdict |
|---|---|---|---|---|---|
| CISA-01 | Inherited risks of LLMs | Input refusal and runtime evaluation guardrails | `bernstein.core.security.audit_chain` | `input.refusal_receipt` | Partial |
| CISA-02 | Increased attack surface | Capability selection and MCP capability-drift evidence | `bernstein.core.security.audit_chain` | `mcp.capability_drift` | Partial |
| CISA-03 | Increased complexity | Sealed run graph and subagent delegation evidence | `bernstein.core.security.audit_chain` | `run_graph.sealed` | Partial |
| CISA-04 | Evolving security as technology matures | Model drift and update-advisory evidence | `bernstein.core.security.audit_chain` | `model.drift_observation` | Partial |
| CISA-05 | Privilege compromise and scope creep | Capability deltas detect widening grants and record authorization | `bernstein.core.security.capability_delta` | `capability.delta_recorded`, `capability.authorization` | Covered |
| CISA-06 | Identity spoofing and agent impersonation | SPIFFE/SVID binding and signed agent identity anchoring | `bernstein.core.identity.spiffe.binding` | `spiffe.svid_binding`, `identity.spawn_attestation` | Covered |
| CISA-07 | Unvetted third-party components | Adapter/plugin admission and conformance receipts | `bernstein.core.security.audit_chain` | `adapter.admission_receipt` | Partial |
| CISA-08 | Static role or permission checks | Runtime capability authorization and enforced tool dispatch | `bernstein.core.security.audit_chain` | `toolcall.enforced_dispatch` | Partial |
| CISA-09 | Poor segmentation | Per-task sandbox host isolation | `bernstein.core.security.audit_chain` | `sandbox.host_isolation_declared` | Covered |
| CISA-10 | Incomplete or outdated allow lists | Capability manifests and capability-drift detection | `bernstein.core.security.audit_chain` | `mcp.capability_drift` | Partial |
| CISA-11 | Goal misalignment and unintended behaviour | Intent capsules, intent-drift evidence and evaluation gates | `bernstein.core.security.audit_chain` | `intent.drift` | Partial |
| CISA-12 | Deceptive behaviour | Evaluation gate verdicts and clean-run attestations | `bernstein.core.security.audit_chain` | `eval.gate_verdict` | Partial |
| CISA-13 | Emergent capabilities and unpredictable behaviour | Capability evaluation and capability-delta evidence | `bernstein.core.security.audit_chain` | `capability.delta_recorded` | Partial |
| CISA-14 | Malicious exploitation and behaviour | Input refusal and guarded tool dispatch | `bernstein.core.security.audit_chain` | `input.refusal_receipt` | Partial |
| CISA-15 | Orchestration and resources | Task lifecycle, resource release and cost budget controls | `bernstein.core.security.audit_chain` | `task.suspend_resource_release` | Partial |
| CISA-16 | Tool use | Tool-call attestation and enforced dispatch | `bernstein.core.security.audit_chain` | `toolcall.attestation`, `toolcall.enforced_dispatch` | Covered |
| CISA-17 | Third-party components | Adapter admission, version posture and capability selection | `bernstein.core.security.audit_chain` | `adapter.admission_receipt` | Partial |
| CISA-18 | Data | Provenance decisions and quarantine evidence | `bernstein.core.security.audit_chain` | `provenance.taint_decision`, `provenance.quarantine` | Partial |
| CISA-19 | Rogue agents | Identity anchoring, delegation receipts and revocation | `bernstein.core.security.audit_chain` | `subagent.delegation`, `identity.revoked` | Partial |
| CISA-20 | Communication | Agent-to-agent message receipts and identity binding | `bernstein.core.security.audit_chain` | `a2a.message_receipt` | Partial |
| CISA-21 | Actions and processes | HMAC audit chain records agent actions and process receipts | `bernstein.core.security.audit_chain` | `run.lifecycle` | Covered |
| CISA-22 | Accuracy | Evaluation gates and trajectory receipts | `bernstein.core.security.audit_chain` | `eval.trajectory_receipt` | Partial |
| CISA-23 | Visibility | Audit receipts and observability projections | `bernstein.core.security.audit_chain` | `otel.projection` | Covered |

## Evidence-pack registration

The `cisa-agentic` standard is registered in
`bernstein.compliance.evidence_pack`.

Controls marked internally as `todo` remain non-selectable, consistent
with the existing handling of standards whose mappings are not yet
reviewed.

## Source

CISA / NSA / Australian Cyber Security Centre and Five Eyes partners,
*Careful adoption of agentic AI services*, first published 1 May 2026.

Official publication:

https://www.cyber.gov.au/business-government/secure-design/artificial-intelligence/careful-adoption-of-agentic-ai-services
