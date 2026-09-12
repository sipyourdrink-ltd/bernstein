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

## Central Control Registry

Bernstein maintains a centralized taxonomy of compliance, security, and governance controls mapped across six major regulatory frameworks:
- **EU AI Act**: Regulation (EU) 2024/1689 (Articles 10, 11, 12, 13, 14, 15, 73, Annex IV).
- **OWASP ASI**: Top 10 for Agentic Applications (ASI01-ASI10).
- **OWASP Skills**: Top 10 for Agentic Skills (AST01-AST10).
- **NIST AI RMF**: AI Risk Management Framework 1.0.
- **ISO/IEC 42001**: Artificial Intelligence Management System (Annex A).
- **FINOS AIGF**: Open-source AI Governance Framework for Financial Services.

### Suite Control Declarations & Build-Time Enforcement

Every benchmark evaluation suite (`BenchSuite`) must declare the standard control IDs it measures. Any suite that omits controls or references unregistered control IDs fails build-time validation:

```python
from bernstein.eval.bench.suite import BenchSuite

suite = BenchSuite(
    version="golden-v1",
    tasks=tasks,
    controls=["CTL-ROB-01", "CTL-EVAL-01", "CTL-EVAL-02", "CTL-QUAL-02"],
)
suite.validate_controls()  # Fails build if invalid or unmapped
```

### CLI Inspection & Coverage

Inspect the control registry and evaluation suite coverage using the CLI:

```bash
# List all registered controls in text format
bernstein compliance controls

# Show benchmark suite coverage
bernstein compliance controls --coverage

# Filter by regulatory framework in JSON or Markdown format
bernstein compliance controls --framework eu_ai_act --format json
bernstein compliance controls --format markdown
```

### Registered Standard Controls & Benchmark Coverage

<!-- controls-table:start -- generated from ControlRegistry.to_markdown_table(); do not edit by hand,
     tests/unit/compliance/test_controls_registry.py fails when this drifts from the registry -->
| Control ID | Title | Frameworks | Evidence Kinds | Suites Covering |
| --- | --- | --- | --- | --- |
| CTL-GOV-01 | Policy as Code & Governance Boundary | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | audit_chain, policy, lineage_log | *(uncovered)* |
| CTL-GOV-02 | Agent Identity & System Card Declaration | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | agent_card, lineage_log | *(uncovered)* |
| CTL-AUD-01 | Tamper-Evident HMAC Audit Logging | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | audit_chain | *(uncovered)* |
| CTL-AUD-02 | Audit Chain Continuity & Retention Verification | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | audit_chain, retention_evidence | *(uncovered)* |
| CTL-LIN-01 | Artifact Lineage & Provenance Tracking | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | lineage_log, signatures | *(uncovered)* |
| CTL-OVS-01 | Human Oversight & Approval Gating | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | approval_receipt, audit_chain | *(uncovered)* |
| CTL-OVS-02 | Displayed vs Executed Action Equivalence | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF, OWASP_ASI | approval_receipt, oversight_evidence | *(uncovered)* |
| CTL-SEC-01 | Prompt Injection & Goal Hijack Defense | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF, OWASP_ASI | bench_bundle, audit_chain | *(uncovered)* |
| CTL-SEC-02 | Tool Execution Sandboxing & Authorization | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF, OWASP_ASI | audit_chain, bench_bundle | tool-surface-v1 |
| CTL-SEC-03 | Canary Token & Secret Leakage Prevention | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF, OWASP_ASI | bench_bundle, audit_chain | *(uncovered)* |
| CTL-SEC-04 | Gate Evasion & Adversarial Bypass Resistance | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF, OWASP_ASI | bench_bundle | *(uncovered)* |
| CTL-SEC-05 | Outbound Model Egress & Policy Boundary Checks | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF, OWASP_ASI | audit_chain, check_record | tool-surface-v1 |
| CTL-ROB-01 | Deterministic Execution & Offline Replay Verification | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | bench_bundle, verifier_receipt | golden-v1 |
| CTL-ROB-02 | Model Drift & Degradation Detection | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | bench_bundle, drift_report | *(uncovered)* |
| CTL-ROB-03 | Error Handling & Graceful Degradation | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | audit_chain, bench_bundle | *(uncovered)* |
| CTL-DATA-01 | Data Governance & Lineage Integrity | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | lineage_log, dataset_manifest | *(uncovered)* |
| CTL-DATA-02 | Confidential Information & PII Redaction | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF, OWASP_ASI | audit_chain, redaction_log | *(uncovered)* |
| CTL-INC-01 | Serious Incident Recording & Timeline Reconstruction | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | incident_pack, audit_chain | *(uncovered)* |
| CTL-COST-01 | Token Budget & Cost Allocation Controls | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | bench_bundle, cost_ledger | *(uncovered)* |
| CTL-EVAL-01 | Content-Addressed Benchmark Reproducibility | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | bench_bundle, suite_hash | golden-v1, tool-surface-v1 |
| CTL-EVAL-02 | Multi-Run Empirical Determinism Scoring | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | bench_bundle, reliability_report | golden-v1 |
| CTL-EVAL-03 | Quality Gate & Verification Adjudication | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | adjudication_record, bench_bundle | *(uncovered)* |
| CTL-QUAL-01 | Producing Identity & Independence Class Tracking | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | adjudication_record, audit_chain | *(uncovered)* |
| CTL-QUAL-02 | Automated Test Coverage & Static Verification | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | ci_run, sarif_report | golden-v1 |
| CTL-SKILL-01 | Agentic Skill Discovery & Verification | FINOS_AIGF, ISO_42001, NIST_AI_RMF, OWASP_SKILLS | skill_manifest, audit_chain | *(uncovered)* |
| CTL-SKILL-02 | Skill Execution Boundaries & Permissions | FINOS_AIGF, ISO_42001, NIST_AI_RMF, OWASP_SKILLS | audit_chain, policy | *(uncovered)* |
| CTL-SKILL-03 | Untrusted Skill Quarantine & Code Review | FINOS_AIGF, ISO_42001, NIST_AI_RMF, OWASP_SKILLS | audit_chain, approval_receipt | *(uncovered)* |
| CTL-MON-01 | Operational Health & Status Dashboarding | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | status_dashboard, metrics | *(uncovered)* |
| CTL-MON-02 | Anomaly Detection & Behavioral Alerts | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | audit_chain, alert_record | *(uncovered)* |
| CTL-DOC-01 | Technical Documentation & Compliance Evidence Packs | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | evidence_pack, tech_doc | *(uncovered)* |
| CTL-DOC-02 | Agent Capability & Limitation Declaration | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | agent_card, system_descriptor | *(uncovered)* |
| CTL-DEP-01 | Air-Gapped & Offline Verification Support | EU_AI_ACT, FINOS_AIGF, ISO_42001, NIST_AI_RMF | verifier_receipt | *(uncovered)* |
<!-- controls-table:end -->

## NIST OSCAL Assessment-Results Export

Bernstein supports automated export of evaluation benchmark assessment results in NIST OSCAL v1.1.0 format.

```bash
# Export OSCAL assessment-results to stdout
bernstein compliance oscal --standard ai-act

# Export OSCAL assessment-results to a JSON file
bernstein compliance oscal --standard ai-act --out oscal-assessment-results.json
```

The exported document models findings, benchmark observations, and control satisfaction state (`satisfied` vs `not-satisfied`) mapped to the central control registry and standard clauses.

`bernstein-bench run` writes its bundle wherever `--out` points (default `bundle.json` in the working directory); place the bundles a pack should carry under `.sdd/bench/bundles/` -- the file name is free, the pack keys them by bundle hash:

```bash
bernstein-bench run golden-v1 --out .sdd/bench/bundles/golden-v1.json
bernstein-bench run tool-surface-v1 --out .sdd/bench/bundles/tool-surface-v1.json
```

How bundles reach the pack and the OSCAL document, and what each does and does not assert:

- **Source.** Every `*.json` under `.sdd/bench/bundles/` is loaded through `SubmissionBundle.from_dict`, which recomputes every task's receipt hash and the bundle hash and raises on mismatch. The pack embeds each bundle that loads **byte-for-byte** under `bench-bundles/<bundle-hash>.json`; it is not re-serialised, so the embedded copy still passes `bernstein-bench verify`.
- **Unreadable bundles are recorded, not dropped.** `controls.json` lists them under `bench_bundles_unreadable` as `{"path", "reason"}`. `bernstein compliance oscal` goes further and refuses to export at all while any bundle under the directory does not load, because an assessment-results document that quietly omitted one would assert a coverage it did not check.
- **Control mapping goes through the suite.** A bundle names its suite (`suite_version`); the suite declares its controls (see the table above). A control is `measured` when a bundle from a suite declaring it is present, using the most recently listed such bundle. Bundles from a suite that is not built in cannot be mapped from here: the pack lists them in `bench_assessment._unresolvable_suites` and the OSCAL document names them in the result's `remarks`.
- **The standard selects the clause.** `--standard` must be one of the pack's supported standards; every finding carries a `clause` prop with the clause of that standard the control maps to in the registry (`unmapped` when it records none). Where several bundles measure a control, the one with the latest `submitted_at` is reported, and the observation's `collected` time is that bundle's own `submitted_at`.
- **`satisfied` threshold.** An OSCAL finding is `satisfied` when the bundle's mean task score is at or above `SATISFIED_SCORE_THRESHOLD` (0.99). This is the export's own policy -- the bench harness has no pass threshold -- and every finding's description records the score and the threshold it was judged against.
- **What is not verified.** Neither the pack verifier nor `bernstein-bench verify` checks a bundle's `signature`; only reliability receipts carry a trusted-key check today. Treat an embedded bundle as hash-consistent evidence of what was run, not as attested by a known key.

