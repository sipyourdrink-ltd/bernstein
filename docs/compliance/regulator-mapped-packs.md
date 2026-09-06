# Regulator-mapped compliance packs

The EU AI Act Article 12 bundle (`bernstein compliance pack article-12`) covers
record-keeping. Operators with obligations beyond record-keeping needed three
more evidence shapes, and used to assemble them by hand from raw exports. The
`bernstein compliance pack` group now ships them as one-command packs, each a
deterministic projection of the audit chain and each offline-verifiable with the
standalone verifier.

| Pack | Command | Maps to | Proves |
|------|---------|---------|--------|
| Retention | `pack retention --since --until` | Article 12(3) | logs existed over the window and were not truncated or rewritten |
| Incident | `pack incident --run <id>` | Article 73 | the incident timeline joined with its audit slice, evidence bundles, and receipts |
| Oversight | `pack oversight --since --until` | Article 14 | who approved what, and that the displayed action equalled the executed action |

Every pack is sealed with the same operator-key signing, signed SLSA-style
provenance manifest, and canonical-bytes rule (`PACK_FORMAT_VERSION` 2) as the
Article 12 bundle. Two builds over the same chain window yield byte-identical
member content hashes; build timestamps live only in the manifest, isolated from
the member hashes so the comparison is mechanical.

## Retention pack

```
bernstein compliance pack retention \
  --since 2026-01-01 --until 2026-12-31 \
  --org "Acme GmbH" --output acme-retention-2026.zip
```

Contents:

- `retention-evidence.json` records the boundary head hashes at the window
  edges, the entry count, detected coverage gaps, and the retention parameters
  in force.
- `lineage-log.jsonl` embeds the signed entries in canonical bytes, with their
  per-entry Ed25519 signatures under `signatures/` and Agent Cards under
  `agent-cards/`.
- `retention-evidence.pdf` and `.csv` are the human- and machine-readable views.

The verifier recomputes the boundary head hashes and entry count from the actual
signed entries, so the continuity claim is checked against the chain, not
trusted from the report.

## Incident pack

```
bernstein compliance pack incident --run run-42 \
  --org "Acme GmbH" --output acme-incident-run-42.zip
```

Reads `.sdd/incidents/<run>/timeline.json` (with optional `evidence_bundle_refs`
and `receipt_refs`) and `.sdd/incidents/<run>/audit-slice.jsonl`, then joins:

- `incident-timeline.json` and a fixed regulator-readable `incident-report.pdf` /
  `.csv`.
- `audit-slice.jsonl`, the prev_hmac-chained audit events in canonical bytes.
- `evidence-bundles/` and `receipts/`, the referenced artefacts present in the
  store.
- `gaps.json`, an explicit entry for every referenced artefact missing from the
  store.

The pack never fabricates completeness: a missing bundle or receipt is recorded
as a gap, and verification reports the gap list rather than passing silently.

## Oversight pack

```
bernstein compliance pack oversight \
  --since 2026-01-01 --until 2026-12-31 \
  --org "Acme GmbH" --output acme-oversight-2026.zip
```

Reads resolved approvals from `.sdd/approvals/resolved.jsonl` (or `--approvals`).
Each in-window approval becomes a canonical receipt under `receipts/` carrying
the attested displayed-versus-executed binding (`displayed_hash`,
`executed_hash`), the approving principal, and the decision outcome, summarised
in `oversight-evidence.json` and rendered in `oversight-report.pdf` / `.csv`.
The verifier recomputes each binding from the receipt payloads, so an auditor
confirms displayed equalled executed decision by decision.

## Verifying a pack offline

All three kinds verify on a machine with no Bernstein install:

```
pip install bernstein-verify
python -m bernstein_verify pack ./acme-retention-2026.zip
```

Exit 0 with a one-line `PASS` summary means every member's sha256 matches the
signed manifest `input_hashes`, the manifest self-anchors (`output_hash`), and
the chained substrate re-verifies for the pack kind. Flipping a single byte in
any member, including a rendered PDF, fails verification and names the member.
Existing Article 12 packs (format v1 and v2) verify unchanged.

## Control registry

Every benchmark suite declares the control IDs it measures (`controls: [...]`).
The unified compliance control registry maps each control across frameworks:

| Control ID | Title | Framework References | Evidence Kinds | Status |
|---|---|---|---|---|
| `CTRL-AUDIT-TRAIL` | Audit Trail & Tamper-Evident Logging | `eu_ai_act: Article 12`, `finos_aigf: CTRL-AUDIT-TRAIL`, `iso42001: A.6.2.8`, `owasp_asi: ASI01` | audit_chain, receipt | mapped |
| `CTRL-DATA-LINEAGE` | Data & Artifact Provenance Lineage | `eu_ai_act: Article 10`, `finos_aigf: CTRL-DATA-LINEAGE`, `iso42001: A.7.2`, `owasp_skills: AST02` | lineage_log, receipt | mapped |
| `CTRL-MODEL-SUPPLY-CHAIN` | Model Supply Chain & Artifact Attestation | `finos_aigf: CTRL-MODEL-SUPPLY-CHAIN`, `iso42001: A.8.2`, `owasp_skills: AST01` | signature, attestation | mapped |
| `CTRL-TOOL-INVENTORY` | Tool & Adapter Capability Inventory | `finos_aigf: CTRL-TOOL-INVENTORY`, `owasp_asi: ASI02`, `owasp_skills: AST04` | audit_chain, capability_matrix | mapped |
| `CTRL-HUMAN-OVERSIGHT` | Human Oversight & Approval Gates | `eu_ai_act: Article 14`, `finos_aigf: CTRL-HUMAN-OVERSIGHT`, `iso42001: A.6.2.7`, `owasp_asi: ASI03` | approval_receipt, audit_chain | mapped |
| `CTRL-ACCESS-CONTROL` | Access Control & Privilege Scoping | `finos_aigf: CTRL-ACCESS-CONTROL`, `iso42001: A.6.2.2`, `owasp_asi: ASI03` | audit_chain, rbac_policy | mapped |
| `CTRL-DATA-RESIDENCY` | Data Residency & Sovereign Boundaries | `eu_ai_act: Article 10`, `finos_aigf: CTRL-DATA-RESIDENCY` | audit_chain, policy_receipt | mapped |
| `CTRL-PII-PROTECTION` | PII & Sensitive Data Protection | `eu_ai_act: Article 10`, `finos_aigf: CTRL-PII-PROTECTION`, `iso42001: A.7.4`, `owasp_skills: AST08` | dlp_scan, audit_chain | mapped |
| `CTRL-PROMPT-INJECTION-DEFENCE` | Prompt Injection & Instruction Defense | `eu_ai_act: Article 15`, `finos_aigf: CTRL-PROMPT-INJECTION-DEFENCE`, `owasp_asi: ASI01` | guardrail_event, audit_chain | mapped |
| `CTRL-INCIDENT-RESPONSE` | Incident Response & Quarantine | `eu_ai_act: Article 73`, `finos_aigf: CTRL-INCIDENT-RESPONSE`, `iso42001: A.6.2.6`, `owasp_asi: ASI08` | incident_timeline, audit_chain | mapped |
| `CTRL-SEGREGATION-OF-DUTIES` | Segregation of Duties & Isolation | `finos_aigf: CTRL-SEGREGATION-OF-DUTIES`, `iso42001: A.6.2.3` | role_policy, audit_chain | mapped |
| `CTRL-RETENTION` | Evidence Retention & Immutability | `eu_ai_act: Article 12(3)`, `finos_aigf: CTRL-RETENTION`, `iso42001: A.6.2.8` | retention_manifest, audit_chain | mapped |
| `CTRL-ENCRYPTION-AT-REST` | Encryption at Rest & Secret Storage | `finos_aigf: CTRL-ENCRYPTION-AT-REST`, `iso42001: A.6.2.4` | key_store, audit_chain | mapped |
| `CTRL-ENCRYPTION-IN-TRANSIT` | Encryption in Transit & mTLS | `finos_aigf: CTRL-ENCRYPTION-IN-TRANSIT`, `iso42001: A.6.2.4`, `owasp_asi: ASI07` | tls_receipt, audit_chain | mapped |
| `CTRL-DEPENDENCY-INTEGRITY` | Dependency Integrity & Software BOM | `finos_aigf: CTRL-DEPENDENCY-INTEGRITY`, `iso42001: A.8.4`, `owasp_skills: AST06` | sbom, audit_chain | mapped |
| `CTRL-CHANGE-MANAGEMENT` | Change Management & Commit Signing | `finos_aigf: CTRL-CHANGE-MANAGEMENT`, `iso42001: A.9.2` | wal, audit_chain | mapped |
| `ASI01` | Agent Goal & Instruction Hijack | `finos_aigf: CTRL-PROMPT-INJECTION-DEFENCE`, `owasp_asi: ASI01` | audit_chain, guardrail_event | mapped |
| `ASI02` | Tool Misuse & Excessive Agency | `finos_aigf: CTRL-TOOL-INVENTORY`, `owasp_asi: ASI02` | capability_matrix_refusal, audit_chain | mapped |
| `ASI03` | Identity & Privilege Abuse | `finos_aigf: CTRL-ACCESS-CONTROL`, `owasp_asi: ASI03` | approval_receipt, audit_chain | mapped |
| `ASI04` | Supply Chain & Skill Provenance | `finos_aigf: CTRL-MODEL-SUPPLY-CHAIN`, `owasp_asi: ASI04`, `owasp_skills: AST01` | signature, audit_chain | mapped |
| `ASI05` | Unexpected Code Execution | `owasp_asi: ASI05`, `owasp_skills: AST05` | sandbox_event, audit_chain | mapped |
| `ASI06` | Memory & Context Poisoning | `owasp_asi: ASI06` | lineage_log, audit_chain | mapped |
| `ASI07` | Insecure Inter-Agent Communication | `finos_aigf: CTRL-ENCRYPTION-IN-TRANSIT`, `owasp_asi: ASI07` | tls_receipt, audit_chain | mapped |
| `ASI08` | Cascading Agent Failure | `finos_aigf: CTRL-INCIDENT-RESPONSE`, `owasp_asi: ASI08` | denial_tracker, audit_chain | mapped |
| `ASI09` | Agent Impersonation | `finos_aigf: CTRL-ACCESS-CONTROL`, `owasp_asi: ASI09` | agent_card, audit_chain | mapped |
| `ASI10` | Rogue Agent Emergence | `finos_aigf: CTRL-HUMAN-OVERSIGHT`, `owasp_asi: ASI10` | deterministic_replay, audit_chain | mapped |
| `AST01` | Untrusted Skill Installation | `finos_aigf: CTRL-MODEL-SUPPLY-CHAIN`, `owasp_skills: AST01` | signature, audit_chain | mapped |
| `AST02` | Post-Publish Skill Tampering | `finos_aigf: CTRL-DATA-LINEAGE`, `owasp_skills: AST02` | lineage_log, audit_chain | mapped |
| `AST03` | Skill Provenance & Pinning | `owasp_skills: AST03` | audit_chain | mapped |
| `AST04` | Excessive Skill Capability | `owasp_asi: ASI02`, `owasp_skills: AST04` | capability_matrix_refusal, audit_chain | mapped |
| `AST05` | Unsafe Skill Execution | `owasp_asi: ASI05`, `owasp_skills: AST05` | sandbox_event, audit_chain | mapped |
| `AST06` | Skill Dependency Compromise | `finos_aigf: CTRL-DEPENDENCY-INTEGRITY`, `owasp_skills: AST06` | sbom, audit_chain | mapped |
| `AST07` | Skill Permission Creep | `finos_aigf: CTRL-ACCESS-CONTROL`, `owasp_skills: AST07` | rbac_policy, audit_chain | mapped |
| `AST08` | Skill Data Exfiltration | `finos_aigf: CTRL-PII-PROTECTION`, `owasp_skills: AST08` | dlp_scan, audit_chain | mapped |
| `AST09` | Skill Confused Deputy | `owasp_asi: ASI03`, `owasp_skills: AST09` | approval_receipt, audit_chain | mapped |
| `AST10` | Deprecated & Abandoned Skills | `owasp_skills: AST10` | audit_chain | mapped |
| `EU-AI-ACT-ART05` | Prohibited AI Practices Screening | `eu_ai_act: Article 5` | assessment_record, audit_chain | mapped |
| `EU-AI-ACT-ART09` | Risk Management System | `eu_ai_act: Article 9`, `iso42001: A.5.2` | risk_assessment, audit_chain | mapped |
| `EU-AI-ACT-ART10` | Data and Data Governance | `eu_ai_act: Article 10`, `iso42001: A.7.2` | lineage_log, audit_chain | mapped |
| `EU-AI-ACT-ART11` | Technical Documentation | `eu_ai_act: Article 11`, `iso42001: A.6.2.5` | evidence_pack | mapped |
| `EU-AI-ACT-ART12` | Automatic Event Recording (Article 12) | `eu_ai_act: Article 12`, `finos_aigf: CTRL-AUDIT-TRAIL`, `iso42001: A.6.2.8` | audit_chain, article12_bundle | mapped |
| `EU-AI-ACT-ART13` | Transparency and User Information | `eu_ai_act: Article 13` | system_description, audit_chain | mapped |
| `EU-AI-ACT-ART14` | Human Oversight (Article 14) | `eu_ai_act: Article 14`, `finos_aigf: CTRL-HUMAN-OVERSIGHT` | approval_receipt, audit_chain | mapped |
| `EU-AI-ACT-ART15` | Accuracy, Robustness and Cybersecurity | `eu_ai_act: Article 15`, `iso42001: A.6.2.6` | benchmark_bundle, audit_chain | mapped |
| `EU-AI-ACT-ART73` | Serious Incident Notification | `eu_ai_act: Article 73`, `finos_aigf: CTRL-INCIDENT-RESPONSE` | incident_pack, audit_chain | mapped |
| `ISO-42001-A628` | AI System Event Logging (A.6.2.8) | `finos_aigf: CTRL-AUDIT-TRAIL`, `iso42001: A.6.2.8` | audit_chain | mapped |
| `ISO-42001-A626` | AI System Operation & Monitoring (A.6.2.6) | `finos_aigf: CTRL-INCIDENT-RESPONSE`, `iso42001: A.6.2.6` | audit_chain, sla_report | mapped |
| `ISO-42001-A627` | Human Oversight of AI Systems (A.6.2.7) | `finos_aigf: CTRL-HUMAN-OVERSIGHT`, `iso42001: A.6.2.7` | approval_receipt, audit_chain | mapped |
| `ISO-42001-A72` | Data for AI Systems & Lineage (A.7.2) | `finos_aigf: CTRL-DATA-LINEAGE`, `iso42001: A.7.2` | lineage_log, audit_chain | mapped |
| `ISO-42001-A82` | Third-Party AI Components & Suppliers (A.8.2) | `finos_aigf: CTRL-MODEL-SUPPLY-CHAIN`, `iso42001: A.8.2` | attestation, audit_chain | mapped |

## Signed Benchmark Bundles & OSCAL Export

Evidence packs support embedding signed benchmark bundles (`bench_bundles`) keyed to compliance controls:

- **Empirical Measurement**: Controls measured by a signed benchmark suite are annotated in `controls.json` with `status: "measured"`, pass rate, score, and suite version. Unmeasured controls are marked `declared-not-measured` with explicit reasons.
- **NIST OSCAL 1.0.0 Export**: Packs automatically embed `oscal/assessment-results.json` containing deterministic NIST OSCAL 1.0.0 assessment-results objects mapping findings and states (`satisfied`, `not-satisfied`, `not-tested`, `not-applicable`) to regulatory controls.
- **Offline Pack Verification**: `verify_evidence_pack(zip_path)` re-hashes every bundle member, validates detached Ed25519 bundle signatures, and verifies per-task run receipts against stored hashes.


