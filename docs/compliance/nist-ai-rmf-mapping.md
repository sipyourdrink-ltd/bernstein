# NIST AI RMF — bernstein controls map

Spec: [NIST AI Risk Management Framework (AI RMF 1.0)](https://doi.org/10.6028/NIST.AI.100-1)
([NIST.AI.100-1](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf)),
January 2023. Subcategory ids follow the Playbook numbering
(`GOVERN 1.1` … `MANAGE 4.3`).

This document is the first slice of issue #4915: an honest cross-walk from
each Core subcategory to what bernstein already records or enforces in the
chain. It is **not** a certification claim and **not** the `ai-rmf` compliance
pack (that follows once this table is agreed).

The Generative AI Profile action items are out of scope for this PR; they will
fold onto these rows later rather than becoming a second document.

## Verdict vocabulary

| Verdict | Meaning |
|---------|---------|
| **Covered** | A concrete bernstein mechanism produces chain- or config-evident control for this subcategory. |
| **Partial** | Something in-tree advances the subcategory, but an operator still owes organisational process, human judgement, or evidence the product does not mint. |
| **Not-covered** | No bernstein mechanism satisfies this subcategory today. |

**Decision (stated for reviewers):** half-satisfied rows use **Partial**, not
"Not-covered with a note". Not-covered is reserved for rows with no mechanism
at all. Every Covered / Partial row cites at least one existing `src/` module;
a unit test fails if a cited path disappears.

## TL;DR

| Verdict | Count | Notes |
|---------|------:|-------|
| Covered | 16 | Auditability, approvals, retention, supply-chain attestation, access control, incident plumbing. |
| Partial | 34 | Mechanisms exist but stop short of the full organisational / socio-technical outcome. |
| Not-covered | 22 | Org culture, DEI, external engagement, fairness/bias evaluation, most MAP impact characterisation. |
| **Total** | **72** | All Core subcategories from AI RMF 1.0 Tables 1–4. |

Counts are deliberate and revisable — if a row looks wrong while you read it,
say so on the PR rather than forcing a Covered claim.

---

## 1. GOVERN

| Subcategory | Outcome (short) | bernstein mechanism | Module | Verdict |
|-------------|-----------------|---------------------|--------|---------|
| `GOVERN-1.1` | Legal / regulatory requirements understood and documented | Article 12 pack + compliance CLI project what the chain already holds; they do not invent a legal register. | `src/bernstein/core/security/article12_bundle.py`, `src/bernstein/cli/commands/compliance_cmd.py` | Partial |
| `GOVERN-1.2` | Trustworthy-AI characteristics in org policies | Capability matrix + default-deny role profiles encode technical policy; org charter is out of tree. | `src/bernstein/core/security/capability_matrix.py`, `src/bernstein/core/security/role_adapter_policy.py` | Partial |
| `GOVERN-1.3` | Risk-management intensity vs risk tolerance | No first-class risk-tolerance object; operators set budgets / gates ad hoc. | — | Not-covered |
| `GOVERN-1.4` | Transparent RM process and controls | HMAC audit chain + DSSE envelopes make technical decisions transparent; process docs are operator-owned. | `src/bernstein/core/security/audit.py`, `src/bernstein/core/security/audit_dsse.py`, `src/bernstein/core/security/audit_chain.py` | Partial |
| `GOVERN-1.5` | Ongoing monitoring / review cadence and roles | Compliance pack windows (`--since` / `--until`) support periodic review; cadence and RACI are organisational. | `src/bernstein/cli/commands/compliance_cmd.py` | Partial |
| `GOVERN-1.6` | AI system inventory | Adapter registry + endpoint certification inventory what bernstein can call; not a full enterprise AI inventory. | `src/bernstein/adapters/registry.py`, `src/bernstein/core/endpoints/certify.py` | Partial |
| `GOVERN-1.7` | Safe decommissioning | Retention / disk rotation and quarantine exist; no AI-system decommission workflow. | `src/bernstein/core/persistence/disk_retention.py`, `src/bernstein/core/security/quarantine.py` | Partial |
| `GOVERN-2.1` | Roles, responsibilities, communication lines | RBAC + permission graph document technical roles; org RACI is out of scope. | `src/bernstein/core/security/rbac.py`, `src/bernstein/core/security/permission_graph.py` | Partial |
| `GOVERN-2.2` | AI risk-management training | No training curriculum or completion evidence in-product. | — | Not-covered |
| `GOVERN-2.3` | Executive ownership of AI risk decisions | Dual / plan approval can require a named human; “executive” is an org mapping. | `src/bernstein/core/security/dual_approval.py`, `src/bernstein/core/security/plan_approval.py` | Partial |
| `GOVERN-3.1` | Diverse team informs RM decisions | No DEI / staffing controls in bernstein. | — | Not-covered |
| `GOVERN-3.2` | Roles for human–AI configurations and oversight | Approval gates + human-in-the-loop plan approval. | `src/bernstein/core/security/approval.py`, `src/bernstein/core/approval/gate.py`, `src/bernstein/core/security/plan_approval.py` | Covered |
| `GOVERN-4.1` | Safety-first / critical-thinking culture | Default-deny profiles and ASI detectors encode caution technically; culture is organisational. | `src/bernstein/core/security/owasp_asi_detectors.py`, `src/bernstein/core/security/capability_matrix.py` | Partial |
| `GOVERN-4.2` | Teams document and communicate AI risks / impacts | Audit + Article 12 export document *what ran*; impact narratives are not generated. | `src/bernstein/core/security/audit_chain.py`, `src/bernstein/core/security/article12_bundle.py` | Partial |
| `GOVERN-4.3` | Testing, incident identification, information sharing | Incident-response orchestrator + denial tracker + quarantine. | `src/bernstein/core/security/security_incident_response.py`, `src/bernstein/core/security/denial_tracker.py`, `src/bernstein/core/security/quarantine.py` | Covered |
| `GOVERN-5.1` | Collect / integrate external feedback on impacts | No external-stakeholder feedback loop in-product. | — | Not-covered |
| `GOVERN-5.2` | Regularly incorporate adjudicated external feedback | Same gap as 5.1. | — | Not-covered |
| `GOVERN-6.1` | Third-party / IP / supply-chain risk policies | Sigstore attestation, SBOM, license scanner, endpoint certification. | `src/bernstein/core/security/sigstore_attestation.py`, `src/bernstein/core/security/sbom.py`, `src/bernstein/core/endpoints/certify.py` | Covered |
| `GOVERN-6.2` | Contingency for third-party failures | Quarantine + incident response + adapter failover receipts on the chain. | `src/bernstein/core/security/quarantine.py`, `src/bernstein/core/security/security_incident_response.py`, `src/bernstein/core/security/audit_chain.py` | Partial |

---

## 2. MAP

| Subcategory | Outcome (short) | bernstein mechanism | Module | Verdict |
|-------------|-----------------|---------------------|--------|---------|
| `MAP-1.1` | Intended purpose, users, norms, settings documented | Run / task metadata and lineage capture *what* ran, not a purpose dossier. | `src/bernstein/core/persistence/lineage.py` | Partial |
| `MAP-1.2` | Interdisciplinary / diverse actors for context | No staffing or participation evidence. | — | Not-covered |
| `MAP-1.3` | Mission and AI goals documented | No mission register. | — | Not-covered |
| `MAP-1.4` | Business value / use context defined | No business-case artefact. | — | Not-covered |
| `MAP-1.5` | Organisational risk tolerances documented | Cost envelopes / budgets approximate spend tolerance only. | `src/bernstein/core/cost/cost_tracker.py` | Partial |
| `MAP-1.6` | System requirements elicited; socio-technical design | Capability / permission matrix encodes technical shalls; socio-technical requirements are operator-owned. | `src/bernstein/core/security/capability_matrix.py`, `src/bernstein/core/security/permission_graph.py` | Partial |
| `MAP-2.1` | Tasks and methods defined | Task graph + agent roles + model routing record task/method choices on the chain. | `src/bernstein/core/routing/model_routing.py`, `src/bernstein/core/persistence/lineage.py` | Covered |
| `MAP-2.2` | Knowledge limits and human oversight of outputs documented | Oversight gates exist; knowledge-limit docs are not a first-class artefact. | `src/bernstein/core/security/approval.py`, `src/bernstein/core/approval/gate.py` | Partial |
| `MAP-2.3` | Scientific integrity / TEVV considerations documented | No experimental-design / dataset-selection dossier. | — | Not-covered |
| `MAP-3.1` | Potential benefits examined and documented | Not in-product. | — | Not-covered |
| `MAP-3.2` | Potential costs (incl. non-monetary) documented | Monetary cost ledger is Covered-ish; non-monetary costs are not. | `src/bernstein/core/cost/cost_tracker.py`, `src/bernstein/core/agents/agent_cost_ledger.py` | Partial |
| `MAP-3.3` | Targeted application scope specified | Task scoping + role policy bound a run; not a product-scope statement. | `src/bernstein/core/security/role_adapter_policy.py` | Partial |
| `MAP-3.4` | Operator proficiency / certification processes | No operator-proficiency evidence. | — | Not-covered |
| `MAP-3.5` | Human-oversight processes defined and documented | Single / dual / plan approval and auto-approve policy. | `src/bernstein/core/security/approval.py`, `src/bernstein/core/security/dual_approval.py`, `src/bernstein/core/security/plan_approval.py`, `src/bernstein/core/security/auto_approve.py` | Covered |
| `MAP-4.1` | Map tech / legal risks of components (incl. third-party) | SBOM + license + Sigstore + endpoint cert map technical supply-chain risk; legal counsel is out of tree. | `src/bernstein/core/security/sbom.py`, `src/bernstein/core/security/sigstore_attestation.py`, `src/bernstein/core/endpoints/certify.py` | Partial |
| `MAP-4.2` | Internal risk controls for components documented | Capability matrix, residency, DLP, refusal. | `src/bernstein/core/security/capability_matrix.py`, `src/bernstein/core/security/data_residency.py`, `src/bernstein/core/security/dlp_scanner_v2.py`, `src/bernstein/core/security/input_refusal.py` | Covered |
| `MAP-5.1` | Likelihood / magnitude of impacts characterised | No impact-assessment model. | — | Not-covered |
| `MAP-5.2` | Engagement with relevant AI actors on impacts | No engagement workflow. | — | Not-covered |

---

## 3. MEASURE

| Subcategory | Outcome (short) | bernstein mechanism | Module | Verdict |
|-------------|-----------------|---------------------|--------|---------|
| `MEASURE-1.1` | Metrics selected for significant risks; gaps documented | Cost, denial, and gate metrics exist; no formal “unmeasured risks” register. | `src/bernstein/core/cost/cost_tracker.py`, `src/bernstein/core/security/denial_tracker.py` | Partial |
| `MEASURE-1.2` | Metrics / controls regularly assessed and updated | Compliance export windows support reassessment; no metric-lifecycle product. | `src/bernstein/cli/commands/compliance_cmd.py` | Partial |
| `MEASURE-1.3` | Independent assessors / domain experts involved | Offline verifiers exist; staffing of independent assessors is organisational. | `src/bernstein/core/security/article12_bundle.py`, `src/bernstein/core/security/audit_dsse.py` | Partial |
| `MEASURE-2.1` | TEVV tools / metrics documented | Eval / gate results land on the chain when used; no universal TEVV dossier. | `src/bernstein/core/security/audit_chain.py` | Partial |
| `MEASURE-2.2` | Human-subject protections / representative eval | Not applicable to bernstein’s orchestration surface. | — | Not-covered |
| `MEASURE-2.3` | Performance / assurance under deployment-like conditions | Quality gates and eval receipts when configured. | `src/bernstein/core/security/audit_chain.py` | Partial |
| `MEASURE-2.4` | Production monitoring of behaviour | Audit chain + cost ledger + denial tracker while runs execute. | `src/bernstein/core/security/audit_chain.py`, `src/bernstein/core/cost/cost_tracker.py`, `src/bernstein/core/security/denial_tracker.py` | Covered |
| `MEASURE-2.5` | Validity / reliability; generalisation limits documented | No model-validity programme. | — | Not-covered |
| `MEASURE-2.6` | Safety risks evaluated; fail-safe behaviour | Capability refusals + quarantine + incident path; not a full safety case. | `src/bernstein/core/security/input_refusal.py`, `src/bernstein/core/security/quarantine.py`, `src/bernstein/core/security/security_incident_response.py` | Partial |
| `MEASURE-2.7` | Security and resilience evaluated | Socket guard, state encryption, ASI detectors, SBOM / vuln disclosure. | `src/bernstein/core/security/socket_guard.py`, `src/bernstein/core/security/state_encryption.py`, `src/bernstein/core/security/owasp_asi_detectors.py`, `src/bernstein/core/security/sbom.py` | Covered |
| `MEASURE-2.8` | Transparency and accountability risks examined | Audit / DSSE / Article 12 make decisions inspectable. | `src/bernstein/core/security/audit.py`, `src/bernstein/core/security/audit_dsse.py`, `src/bernstein/core/security/article12_bundle.py` | Covered |
| `MEASURE-2.9` | Model explained / outputs interpreted in context | Orchestration is deterministic Python; model-output explainability is not bernstein’s job. | `src/bernstein/core/security/governance.py` | Partial |
| `MEASURE-2.10` | Privacy risk examined | DLP v2, PII output gate, data residency. | `src/bernstein/core/security/dlp_scanner_v2.py`, `src/bernstein/core/security/pii_output_gate.py`, `src/bernstein/core/security/data_residency.py` | Covered |
| `MEASURE-2.11` | Fairness and bias evaluated | No fairness / bias measurement harness. | — | Not-covered |
| `MEASURE-2.12` | Environmental impact of training / management | Token / $ cost is recorded; energy / carbon is not. | `src/bernstein/core/cost/cost_tracker.py`, `src/bernstein/core/agents/agent_cost_ledger.py` | Partial |
| `MEASURE-2.13` | Efficacy of TEVV metrics evaluated | No meta-evaluation of measurement quality. | — | Not-covered |
| `MEASURE-3.1` | Track existing / emergent risks over time | Denial tracker + incident correlation + chain history. | `src/bernstein/core/security/denial_tracker.py`, `src/bernstein/core/security/security_incident_response.py`, `src/bernstein/core/security/audit_chain.py` | Covered |
| `MEASURE-3.2` | Tracking where metrics are unavailable | Not modelled. | — | Not-covered |
| `MEASURE-3.3` | End-user / community feedback into metrics | No appeal / community-feedback channel. | — | Not-covered |
| `MEASURE-4.1` | Measurement approaches tied to deployment context via experts | Operator configures gates; no structured expert-consultation record. | — | Not-covered |
| `MEASURE-4.2` | Trustworthiness results validated with domain experts | Offline pack verify supports third-party check; expert validation process is organisational. | `src/bernstein/core/security/article12_bundle.py` | Partial |
| `MEASURE-4.3` | Performance changes from actor feedback documented | No feedback→metric loop. | — | Not-covered |

---

## 4. MANAGE

| Subcategory | Outcome (short) | bernstein mechanism | Module | Verdict |
|-------------|-----------------|---------------------|--------|---------|
| `MANAGE-1.1` | Go / no-go whether system meets purpose | Plan approval + gate refusals can halt work; “purpose achieved” is a human judgement. | `src/bernstein/core/security/plan_approval.py`, `src/bernstein/core/approval/gate.py` | Partial |
| `MANAGE-1.2` | Prioritise treatment by impact / likelihood / resources | Cost budgets and denial severity exist; no full risk register prioritisation. | `src/bernstein/core/cost/cost_tracker.py`, `src/bernstein/core/security/denial_tracker.py` | Partial |
| `MANAGE-1.3` | Responses for high-priority risks planned | Incident-response orchestrator documents response paths for security events. | `src/bernstein/core/security/security_incident_response.py` | Covered |
| `MANAGE-1.4` | Residual risk to acquirers / end users documented | Chain shows what was refused / accepted; residual-risk statements are operator-authored. | `src/bernstein/core/security/audit_chain.py`, `src/bernstein/core/security/input_refusal.py` | Partial |
| `MANAGE-2.1` | Resources and non-AI alternatives considered | Resource/cost controls exist; non-AI alternative analysis is not. | `src/bernstein/core/cost/cost.py`, `src/bernstein/core/cost/cost_tracker.py` | Partial |
| `MANAGE-2.2` | Sustain value of deployed systems | Retention pins + lineage keep evidence durable; “value sustainment” programme is organisational. | `src/bernstein/core/security/article12_bundle.py`, `src/bernstein/core/persistence/lineage.py`, `src/bernstein/core/lineage/v2_store.py` | Partial |
| `MANAGE-2.3` | Respond / recover from previously unknown risk | Incident response + quarantine + denial tracking. | `src/bernstein/core/security/security_incident_response.py`, `src/bernstein/core/security/quarantine.py` | Covered |
| `MANAGE-2.4` | Supersede / disengage / deactivate misbehaving systems | Quarantine and role/adapter deny can stop further action; full kill-switch productisation varies by deploy. | `src/bernstein/core/security/quarantine.py`, `src/bernstein/core/security/role_adapter_policy.py` | Covered |
| `MANAGE-3.1` | Monitor third-party risks and apply controls | Endpoint certification + Sigstore + residency / DLP on egress paths. | `src/bernstein/core/endpoints/certify.py`, `src/bernstein/core/security/sigstore_attestation.py`, `src/bernstein/core/security/data_residency.py` | Covered |
| `MANAGE-3.2` | Monitor pre-trained models used in development | Model routing records which model served a task; continuous upstream-model monitoring is not bernstein’s. | `src/bernstein/core/routing/model_routing.py` | Partial |
| `MANAGE-4.1` | Post-deployment monitoring, appeal, decommission, IR, change mgmt | Audit monitoring + IR + commit signing + retention; appeal / community override is thin. | `src/bernstein/core/security/audit_chain.py`, `src/bernstein/core/security/security_incident_response.py`, `src/bernstein/core/security/commit_signing.py`, `src/bernstein/core/persistence/disk_retention.py` | Partial |
| `MANAGE-4.2` | Continual improvement with interested parties | No structured continual-improvement / stakeholder cadence. | — | Not-covered |
| `MANAGE-4.3` | Incidents communicated; track / respond / recover documented | Incident-response + denial tracker + audit chain. | `src/bernstein/core/security/security_incident_response.py`, `src/bernstein/core/security/denial_tracker.py`, `src/bernstein/core/security/audit_chain.py` | Covered |

---

## 5. Cross-walk to existing bernstein packs

| Existing pack / map | Strongest AI RMF anchors |
|---------------------|--------------------------|
| Article 12 record-keeping | `GOVERN-1.4`, `MEASURE-2.8`, `MANAGE-2.2` |
| Retention | `GOVERN-1.7`, `MANAGE-4.1` |
| Incident (Art. 73-shaped) | `GOVERN-4.3`, `MANAGE-1.3`, `MANAGE-2.3`, `MANAGE-4.3` |
| Oversight (Art. 14-shaped) | `GOVERN-3.2`, `MAP-3.5` |
| FINOS AIGF map | Supply-chain / audit / oversight rows above; see `docs/compliance/finos-aigf-mapping.md` |

---

## 6. What this document deliberately does not claim

- Certification, authorisation, or third-party attestation under NIST AI RMF.
- Coverage of the NIST Generative AI Profile (deferred).
- That Partial rows are “good enough” for an audit — they flag work the operator still owns.
- Any runtime behaviour change; this is documentation of what the chain already can evidence.

## 7. References

- NIST AI RMF 1.0 — <https://doi.org/10.6028/NIST.AI.100-1>
- NIST AI RMF Playbook — <https://airc.nist.gov/AI_RMF_Knowledge_Base/Playbook>
- FINOS mapping (shape model) — `docs/compliance/finos-aigf-mapping.md`
- Issue #4915 — pack command and GenAI Profile rows follow this table.
