# FINOS AI Governance Framework - bernstein controls map

Spec: [FINOS AI Governance Framework](https://github.com/finos/ai-governance-framework)
       (`CONTROLS.md` + the rendered site at <https://air-governance-framework.finos.org>),
       Community Specification License v1.0.

For each FINOS AIGF control this document lists the bernstein subsystem(s) implementing it
and the specific source files.

The pairing also covers the AIGF risk inventory (`AIR-*` rows in `risks/`) so the
two sides of the framework are mapped end-to-end.

## TL;DR

| Status        | Count | Notes |
|---------------|-------|-------|
| Covered       | 16/16 | DSSE envelope on the audit chain, per-role adapter policy, and Sigstore-backed release attestation are wired in CI; release-artefact provenance ships via `actions/attest-build-provenance` on every published wheel + sdist. |

## 1. AIGF control inventory

Cross-walked against the 16 controls listed in `finos/ai-governance-framework`
(`CONTROLS.md` + the rendered site). Where the AIGF control title
differs slightly from what the upstream repo published, the closest-match
control id is cited.

| AIGF control | bernstein implementation | Files | Verdict |
|--------------|--------------------------|-------|---------|
| `CTRL-AUDIT-TRAIL` | HMAC-chained JSONL audit log + Article 12 evidence bundle (deterministic zip with manifest, clause map, retention pin) + DSSE/in-toto envelope wrapper. | `src/bernstein/core/security/audit.py`, `src/bernstein/core/security/article12_bundle.py`, `src/bernstein/core/security/audit_dsse.py` | Covered. |
| `CTRL-DATA-LINEAGE` | Per-artefact lineage WAL with `regulatory_class` field and customer-controlled Ed25519 detached signature (schema v2). Lineage v2 (two-layer parent ref + detached child body, HMAC-chained across both layers, SLSA v0.3 provenance export per child) lands as an opt-in writer via `BERNSTEIN_LINEAGE_V2=1` or `bernstein.yaml` `lineage.version: 2`; v1 stays default. | `src/bernstein/core/persistence/lineage.py`, `src/bernstein/core/persistence/lineage_signer.py`, `src/bernstein/core/lineage/v2_store.py` | Covered. |
| `CTRL-MODEL-SUPPLY-CHAIN` | Per-task Sigstore/Rekor keyless attestation with Ed25519 fallback; agent-card signer + JWKS rotation; release-artefact build provenance via `actions/attest-build-provenance` on every published wheel + sdist; `bernstein verify --sigstore` for consumers. | `src/bernstein/core/security/sigstore_attestation.py`, `src/bernstein/core/security/agent_card_signer.py`, `src/bernstein/core/security/agent_card_keystore.py`, `src/bernstein/core/distribution/sigstore_attestation_verify.py`, `.github/workflows/publish.yml` | Covered. Both halves of the supply chain are signed: per-task Sigstore for runtime artefacts, `actions/attest-build-provenance` (SLSA L3, keyless OIDC, Rekor public log) for release artefacts. Consumers verify with `gh attestation verify <file> --owner sipyourdrink-ltd` or, equivalently, `bernstein verify <wheelhouse> --sigstore --sigstore-owner sipyourdrink-ltd`. |
| `CTRL-TOOL-INVENTORY` | Adapter registry + capability-matrix yaml + per-role profile manager. | `src/bernstein/adapters/registry.py`, `src/bernstein/core/security/capability_matrix.py`, `src/bernstein/core/security/claude_permission_profiles.py` | Covered. |
| `CTRL-HUMAN-OVERSIGHT` | Single + dual approval gates, plan-approval workflow, per-role default deny. | `src/bernstein/core/security/approval.py`, `src/bernstein/core/security/dual_approval.py`, `src/bernstein/core/security/plan_approval.py`, `src/bernstein/core/security/auto_approve.py` | Covered. |
| `CTRL-ACCESS-CONTROL` | API-route RBAC (admin/operator/viewer) + per-role allowed/disallowed tools + permission-graph + delegation matrix. | `src/bernstein/core/security/rbac.py`, `src/bernstein/core/security/claude_permission_profiles.py`, `src/bernstein/core/security/permission_graph.py`, `src/bernstein/core/security/permission_delegation.py`, `src/bernstein/core/security/permission_matrix.py` | Covered. |
| `CTRL-DATA-RESIDENCY` | Per-tenant region policy with write-time check; EU-residency loopback test. | `src/bernstein/core/security/data_residency.py` | Covered. |
| `CTRL-PII-PROTECTION` | DLP scanner v2 + PII output gate + sensitive-data detector + secrets scanner. | `src/bernstein/core/security/dlp_scanner_v2.py`, `src/bernstein/core/security/pii_output_gate.py`, `src/bernstein/core/security/sensitive_data.py`, `src/bernstein/core/security/secrets.py`, `src/bernstein/core/security/sensitive_file_detector.py` | Covered. |
| `CTRL-PROMPT-INJECTION-DEFENCE` | OWASP Agentic Security Initiative (ASI) detector pack + lethal-trifecta capability matrix (PRIVATE_DATA × UNTRUSTED_INPUT × EXTERNAL_COMM). | `src/bernstein/core/security/owasp_asi_detectors.py`, `src/bernstein/core/security/capability_matrix.py` | Covered. |
| `CTRL-INCIDENT-RESPONSE` | Incident-response orchestrator + denial tracker + quarantine + correlation engine. | `src/bernstein/core/security/security_incident_response.py`, `src/bernstein/core/security/denial_tracker.py`, `src/bernstein/core/security/quarantine.py`, `src/bernstein/core/security/security_correlation.py` | Covered. |
| `CTRL-SEGREGATION-OF-DUTIES` | RBAC + per-role tool deny-lists + per-role adapter deny-list. | `src/bernstein/core/security/rbac.py`, `src/bernstein/core/security/claude_permission_profiles.py`, `src/bernstein/core/security/role_adapter_policy.py` | Covered. |
| `CTRL-RETENTION` | Article 12(3) retention pin (10y high-risk / 183d minimum) baked into the bundle manifest; calendar-day disk rotation. | `src/bernstein/core/security/article12_bundle.py:RetentionPin`, `src/bernstein/core/persistence/disk_retention.py` | Covered. |
| `CTRL-ENCRYPTION-AT-REST` | State-encryption module + credential vault (OS keychain transport) + injector. | `src/bernstein/core/security/state_encryption.py`, `src/bernstein/core/security/vault/`, `src/bernstein/core/security/vault_injector.py` | Covered. |
| `CTRL-ENCRYPTION-IN-TRANSIT` | mTLS cluster guard + TLS pinning + socket guard. | `src/bernstein/core/security/socket_guard.py`, `src/bernstein/adapters/clm_tls_launcher.py` | Covered. |
| `CTRL-DEPENDENCY-INTEGRITY` | SBOM generator + license scanner + vuln-disclosure pipeline + wheelhouse verify. | `src/bernstein/core/security/sbom.py`, `src/bernstein/core/security/license_scanner.py`, `src/bernstein/core/security/vuln_disclosure.py` | Covered. |
| `CTRL-CHANGE-MANAGEMENT` | WAL + audit chain + git provenance signing. | `src/bernstein/core/security/commit_signing.py`, `src/bernstein/core/persistence/wal.py` | Covered. |

**Net result: 16 of 16 controls covered.** Release-artefact provenance via
`actions/attest-build-provenance` is wired into `publish.yml` (the release
pipeline that `auto-release.yml` dispatches after it pushes a release tag;
container images are attested in `publish-docker.yml`); a `bernstein verify --sigstore` consumer-side checker
re-runs the Rekor inclusion proof + Fulcio cert-chain validation via the
official `gh attestation verify` CLI.

## 2. AIGF risk inventory

Same exercise on the AIR-* risk side.

| AIGF risk id | Risk title | bernstein mitigation | Verdict |
|--------------|-----------|----------------------|---------|
| `AIR-DA-001` | Inadequate data anonymisation | DLP scanner v2, PII output gate, differential-privacy module. | Covered. |
| `AIR-DA-002` | Cross-border data transfer | Per-tenant data-residency policy + write-time enforcement. | Covered. |
| `AIR-OP-001` | Tool-chain logic vulnerabilities | Lethal-trifecta capability matrix; refusal events emitted to the audit chain. | Covered. |
| `AIR-OP-002` | Inadequate record-keeping for AI decisions | HMAC-chained audit + Article 12 bundle + DSSE envelope. | Covered. |
| `AIR-OP-003` | Lack of explainability | Deterministic Python orchestration - coordination is zero-token, every decision is reproducible. The structural property that bernstein never delegates orchestration to an LLM is the basis for this mapping. | Covered (architectural). |
| `AIR-OP-004` | Model supply-chain compromise | Per-task Sigstore + agent-card JWKS + GitHub `actions/attest-build-provenance` build provenance on every released wheel + sdist + consumer-side `bernstein verify --sigstore`. | Covered. Both halves of the supply chain (runtime artefacts + release artefacts) carry Sigstore-backed provenance. |
| `AIR-OP-005` | Hallucination in production | Out of scope - bernstein operates at task-level, not model-level. | Out of scope. |
| `AIR-OP-006` | Inadequate human oversight | Approval, dual-approval, plan-approval, role-default deny. | Covered. |
| `AIR-OP-007` | Regulatory-violation risk via missing audit trails | Same chain as `AIR-OP-002`; DSSE envelope provides third-party-verifiability. | Covered. |
| `AIR-RC-001` | Bias amplification | Out of scope (model-level concern). | Out of scope. |
| `AIR-RC-002` | Sensitive-data leakage | DLP v2 + PII gate + sensitive-data + secrets. | Covered. |
| `AIR-RC-003` | Prompt injection | OWASP ASI detectors + lethal-trifecta capability matrix. | Covered. |
| `AIR-RC-004` | Unauthorised tool invocation | Command allowlist + command policy + per-role profile + per-role adapter policy. | Covered. |
| `AIR-RC-005` | Inadequate third-party evidence (vendor-DD) | Out of scope as a packaged artefact today; the underlying audit + attestation surfaces are available for operators assembling their own vendor-DD pack. | Out of scope (operator-assembled). |

## 3. Cross-walk to other regulator anchors

| Regulator | Anchor | Strongest bernstein mappings |
|-----------|--------|------------------------------|
| EU AI Act | Art. 12 record-keeping, Art. 19(1) automatically generated logs, Art. 26(5) high-risk monitoring | `CTRL-AUDIT-TRAIL`, `CTRL-RETENTION`, `CTRL-DATA-LINEAGE` |
| DORA | Art. 9(3) integrity, Art. 28 ICT third-party | `CTRL-AUDIT-TRAIL` (DSSE), `CTRL-MODEL-SUPPLY-CHAIN`, `CTRL-INCIDENT-RESPONSE` |
| SR 11-7 | §V model implementation / segregation of duties, §VII model monitoring | `CTRL-SEGREGATION-OF-DUTIES`, `CTRL-AUDIT-TRAIL`, `CTRL-CHANGE-MANAGEMENT` |
| ISO 42001 | cl. 7.5.3 control of documented information, cl. 9 performance evaluation | `CTRL-AUDIT-TRAIL`, `CTRL-RETENTION`, `CTRL-INCIDENT-RESPONSE` |

## 4. Operational notes

| Layer | Notes |
|-------|-------|
| HMAC-chained audit log | Daily rotation, key isolated outside `.sdd/`, mode-0600 enforced. |
| Article 12 bundle (deterministic zip + retention pin + clause map) | Shipped with in-tree tests. |
| DSSE/in-toto envelope on the bundle | Round-trip + tamper tests in place. v1 uses local Ed25519; Sigstore keyless variant is documented in the module docstring. |
| Standalone verifier | `tools/verify_audit_dsse.py`. Subprocess-isolated test enforces no `bernstein.*` import. Pure stdlib + `cryptography`. |
| Per-role adapter deny-list | Empty allow-list = all-allowed. Hooks `bernstein.adapters.registry.get_adapter` so every spawn site is covered. |
| Sigstore release attestation (SLSA L3) | `actions/attest-build-provenance` runs on every published wheel + sdist via `publish.yml`. Consumers verify with `gh attestation verify <file> --owner sipyourdrink-ltd` or `bernstein verify <wheelhouse> --sigstore`. Smoke test in `tests/unit/test_release_attestation_workflow.py` guards against a workflow refactor silently re-opening the gap. |

## 5. References

- FINOS AI Governance Framework - <https://github.com/finos/ai-governance-framework>,
  rendered at <https://air-governance-framework.finos.org>. Community
  Specification License v1.0.
- bernstein source tree - every file path above is relative to repo root.
- EU AI Act - Regulation (EU) 2024/1689,
  <https://eur-lex.europa.eu/eli/reg/2024/1689>.
- DORA - Regulation (EU) 2022/2554,
  <https://eur-lex.europa.eu/eli/reg/2022/2554>.
- US Federal Reserve SR 11-7, "Guidance on Model Risk Management".
- ISO/IEC 42001:2023, AI Management System.
- in-toto attestation v1.0 spec -
  <https://github.com/in-toto/attestation/blob/main/spec/v1/README.md>.
- DSSE - <https://github.com/secure-systems-lab/dsse>.
