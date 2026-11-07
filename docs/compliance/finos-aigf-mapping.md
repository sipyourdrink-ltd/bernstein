# FINOS AI Governance Framework - Bernstein mitigation and risk crosswalk

Source: [FINOS AI Governance Framework](https://github.com/finos/ai-governance-framework),
Community Specification License v1.0, pinned at
[`aabbffbe02a4aae8e6d5f8534d63edf9248fa302`](https://github.com/finos/ai-governance-framework/tree/aabbffbe02a4aae8e6d5f8534d63edf9248fa302).
The authoritative inventories are `docs/_mitigations/` (23 `mi-N` mitigations) and
`docs/_risks/` (23 non-consecutive `ri-N` risks); their IDs, exact titles, and
types are recorded in `bernstein.compliance.finos_aigf.MITIGATIONS` and `RISKS`.

This is a code-evidence crosswalk, **not a certification or an assertion of
regulatory compliance**. **Covered** requires evidence of the mitigation's
whole upstream scope; **Partial** means only specified mechanisms exist;
**Not covered** means no directly applicable implementation was verified;
**Out of scope** denotes an obligation outside the shipped software. Risk
assessments in §2 are separate from the mitigation coverage denominator in §1.

## TL;DR

| Mitigation verdict | Count | Notes |
|--------------------|-------|-------|
| Covered | 0/23 | No implementation evidence here establishes every requirement of an upstream mitigation end to end. |
| Partial | 19/23 | Specific Bernstein safeguards exist, but important upstream requirements or deployment coverage remain unverified. |
| Not covered | 3/23 | External knowledge filtering, foundation-model pinning, and source-data entitlement inheritance lack verified implementations. |
| Out of scope | 1/23 | Legal/contractual vendor governance is an operator responsibility, not a Bernstein runtime safeguard. |

## 1. FINOS mitigation inventory

The rows below enumerate **every** `MITIGATIONS` entry, using its exact
upstream title and `PREV` (preventative) or `DET` (detective) type. File
paths are relative to the Bernstein repository; a mechanism's existence does
not mean it is enabled for every deployment or covers the complete FINOS
mitigation.

| FINOS mitigation | Upstream title | Type | Bernstein evidence and limits | Verdict |
|------------------|----------------|------|-------------------------------|---------|
| `mi-1` | AI Data Leakage Prevention and Detection | DET | `src/bernstein/core/security/dlp_scanner_v2.py` and `pii_output_gate.py` scan agent artefacts/diffs; they do not cover all hosted-model prompts, training data, or egress channels. | Partial |
| `mi-2` | Data Filtering From External Knowledge Bases | PREV | `src/bernstein/core/knowledge/rag.py` indexes project files, but no source-specific sensitivity filter at knowledge-base ingestion/retrieval was verified; output DLP is not a replacement. | Not covered |
| `mi-3` | User/App/Model Firewalling/Filtering | PREV | `src/bernstein/core/security/owasp_asi_detectors.py` and `pii_output_gate.py` detect selected agent threats; no mandatory firewall at every user/application/model boundary is established. | Partial |
| `mi-4` | AI System Observability | DET | `src/bernstein/core/security/audit.py` and `src/bernstein/core/agents/agent_cost_ledger.py` expose task audit and cost data, not complete model-input/output and quality telemetry. | Partial |
| `mi-5` | System Acceptance Testing | PREV | `src/bernstein/eval/judge.py` evaluates code changes and `src/bernstein/core/orchestration/approval_gate.py` gates workflows; neither constitutes business/user acceptance testing of a deployed AI system. | Partial |
| `mi-6` | Data Quality & Classification/Sensitivity | PREV | `src/bernstein/core/security/sensitive_data.py` and `src/bernstein/core/lineage/sensitivity.py` provide sensitivity handling; full training/retrieval-data accuracy and freshness validation is not shown. | Partial |
| `mi-7` | Legal and Contractual Frameworks for AI Systems | PREV | No Bernstein source path implements vendor contracts, licensing reviews, or legal allocation of AI responsibilities; these remain operator/legal obligations. | Out of scope |
| `mi-8` | Quality of Service (QoS) and DDoS Prevention for AI Systems | PREV | `src/bernstein/core/agents/spawn_rate_limiter.py` limits local provider spawns; it is not perimeter DDoS protection or full production inference QoS. | Partial |
| `mi-9` | AI System Alerting and Denial of Wallet (DoW) / Spend Monitoring | DET | `src/bernstein/core/agents/agent_cost_ledger.py` attributes task cost; that is narrower than end-to-end spend anomaly detection, alerting, and DoW prevention. | Partial |
| `mi-10` | AI Model Version Pinning | PREV | `src/bernstein/core/config/prompt_versions.py` hashes prompt versions and routes canaries, not immutable foundation-model release IDs or controlled provider model upgrades. | Not covered |
| `mi-11` | Human Feedback Loop for AI Systems | DET | `src/bernstein/core/security/approval.py` records individual human task approvals; it does not collect, analyze, and feed user/SME quality feedback into continual AI improvement. | Partial |
| `mi-12` | Role-Based Access Control for AI Data | PREV | `src/bernstein/core/security/rbac.py` and `auth_middleware.py` enforce API-route permissions; they do not establish per-dataset and per-model AI data entitlements. | Partial |
| `mi-13` | Providing Citations and Source Traceability for AI-Generated Information | DET | `src/bernstein/core/lineage/provenance.py` and `figure_grounding.py` track artefact lineage; they do not require source citations for every factual model-generated answer. | Partial |
| `mi-14` | Encryption of AI Data at Rest | PREV | `src/bernstein/core/security/state_encryption.py` offers AES-GCM for selected state files and `src/bernstein/core/security/vault/` protects credentials; blanket encryption of all model/vector data is not evidenced. | Partial |
| `mi-15` | Using Large Language Models for Automated Evaluation (LLM-as-a-Judge) | DET | `src/bernstein/eval/judge.py` implements an LLM judge with a code-review rubric and structured verdict; no calibrated, human-validated evaluation across general AI outputs or continuous monitoring is demonstrated. | Partial |
| `mi-16` | Preserving Source Data Access Controls in AI Systems | DET | `src/bernstein/core/knowledge/rag.py` indexes and searches workspace files, but propagation and audit of original source-document ACLs into retrieval/output are not implemented here. | Not covered |
| `mi-17` | AI Firewall Implementation and Management | PREV | `src/bernstein/core/security/owasp_asi_detectors.py` and `capability_matrix.py` provide specific detections and tool-path controls, not a universal inline AI request/response firewall. | Partial |
| `mi-18` | Agent Authority Least Privilege Framework | PREV | `src/bernstein/core/security/permission_matrix.py` resolves rule/approval precedence and `role_adapter_policy.py` restricts configured roles, but empty adapter allow-lists default to unrestricted; coverage is policy/deployment-dependent. | Partial |
| `mi-19` | Tool Chain Validation and Sanitization | PREV | `src/bernstein/core/security/capability_matrix.py` blocks unsafe declared tool combinations and `src/bernstein/mcp/tool_surface.py` scores manifests; full tool argument sanitization and chain-sequence validation are not proved. | Partial |
| `mi-20` | MCP Server Security Governance | PREV | `src/bernstein/core/protocols/mcp/mcp_verifier.py` checks signed manifests, `mcp_scanner.py` scans bundles, and `src/bernstein/mcp/tool_surface.py` scores tool risk; mandatory centralized proxy, continuous vendor vetting, and all FINOS security tiers are not provided. | Partial |
| `mi-21` | Agent Decision Audit and Explainability | DET | `src/bernstein/core/security/audit.py` and `src/bernstein/core/lineage/spine.py` record verifiable task actions; they do not establish complete semantic explanations or model reasoning for every decision. | Partial |
| `mi-22` | Multi-Agent Isolation and Segmentation | PREV | `src/bernstein/core/agents/spawner_sandbox_session.py` supports optional sandbox execution; its documented default worktree-direct path is not mandatory process/network isolation. | Partial |
| `mi-23` | Agentic System Credential Protection Framework | PREV | `src/bernstein/core/security/vault_injector.py`, `src/bernstein/core/credential_scoping.py` and the output PII gate provide scoped secret handling, not universally enforced credential isolation against autonomous agents. | Partial |

**Net result: 0 of 23 mitigations Covered; 19 Partial; 3 Not covered; 1 Out of scope.**
These are evidence-scoped verdicts, **not** a statement that no security
features exist: the HMAC audit chain, DSSE/in-toto bundle envelopes, per-role
policies, and Sigstore-backed release-artefact provenance are present, but
none alone proves all requirements of an unrelated upstream mitigation.
In particular, `actions/attest-build-provenance` publishes wheel/sdist provenance
via `.github/workflows/publish.yml` and `bernstein verify --sigstore` offers
consumer verification; this is not blanket model- or MCP-supply-chain assurance.

## 2. FINOS risk inventory

The risks below are the separate 23-entry `RISKS` catalogue from the same
pinned upstream commit. `RC`, `OP`, and `SEC` are FINOS's original risk types;
the `mi-N` links identify relevant upstream mitigations, **not** a claim that
Bernstein has fully implemented them. Risk verdicts describe Bernstein's
available response to each risk, not the §1 mitigation coverage count.

| FINOS risk | Upstream risk title | Type | Related mitigations | Bernstein response and limits | Verdict |
|------------|---------------------|------|---------------------|-------------------------------|---------|
| `ri-1` | Information Leaked To Hosted Model | RC | `mi-1`, `mi-2`, `mi-12` | Output DLP and role policies address some leaks, but do not guarantee filtering before data reaches a hosted model. | Partial |
| `ri-2` | Information Leaked to Vector Store | SEC | `mi-6`, `mi-14`, `mi-16` | Optional state encryption does not establish vector-store ingestion controls or inherited source ACLs. | Not covered |
| `ri-4` | Hallucination and Inaccurate Outputs | OP | `mi-13`, `mi-15` | The LLM judge reviews agent code changes, not the factual accuracy of all generated information. | Partial |
| `ri-5` | Foundation Model Versioning | OP | `mi-10`, `mi-15` | Prompt canary/version history exists, but model-provider release pinning is not verified. | Not covered |
| `ri-6` | Non-Deterministic Behaviour | OP | `mi-4`, `mi-10`, `mi-15` | Deterministic orchestration and task replay do not make external model responses deterministic. | Partial |
| `ri-7` | Availability of Foundational Model | OP | `mi-8`, `mi-9` | Spawn throttling and cost attribution are narrower than production provider resilience or DDoS defence. | Partial |
| `ri-8` | Tampering With the Foundational Model | SEC | `mi-12`, `mi-20` | Signed task/release artefacts and MCP verification do not verify the foundation-model weights or provider chain. | Partial |
| `ri-9` | Data Poisoning | SEC | `mi-2`, `mi-6`, `mi-12` | Output scanning is not source-dataset validation or knowledge-base poisoning prevention. | Not covered |
| `ri-10` | Prompt Injection | SEC | `mi-3`, `mi-17`, `mi-19` | OWASP detectors and capability combinations guard selected agent paths; unmediated model inputs remain outside those gates. | Partial |
| `ri-14` | Inadequate System Alignment | OP | `mi-5`, `mi-11`, `mi-15` | Approval and code review exist, but end-user/model objective alignment is not validated end to end. | Partial |
| `ri-16` | Bias and Discrimination | OP | `mi-6`, `mi-11`, `mi-15` | Model training and institutional fairness testing are not provided by the task orchestrator. | Out of scope |
| `ri-17` | Lack of Explainability | OP | `mi-13` | Provenance and audit logs trace artefacts, not every answer's source citations or the model's reasoning. | Partial |
| `ri-18` | Model Overreach / Expanded Use | OP | `mi-3`, `mi-11`, `mi-18` | Agent tool and adapter policies restrict configured roles, not every foundation-model use case. | Partial |
| `ri-19` | Data Quality and Drift | OP | `mi-4`, `mi-6`, `mi-15` | Task telemetry and sensitivity labels do not establish training/retrieval data quality or model drift monitoring. | Not covered |
| `ri-20` | Reputational Risk | OP | `mi-3`, `mi-11`, `mi-13` | Audit receipts support incident investigation; organizational reputation management is not a complete Bernstein service. | Partial |
| `ri-22` | Regulatory Compliance and Oversight | RC | `mi-7`, `mi-13`, `mi-21` | Evidence bundles, audit, and traceability are useful inputs, not proof of regulatory compliance or operator legal controls. | Partial |
| `ri-23` | Intellectual Property (IP) and Copyright | RC | `mi-6`, `mi-7` | Dependency licence checks do not establish copyright rights for foundation-model training or AI-generated content. | Out of scope |
| `ri-24` | Agent Action Authorization Bypass | SEC | `mi-18`, `mi-19`, `mi-21` | Permission resolution, tool capability checks, and approvals mitigate selected paths; default-open adapter roles remain configurable. | Partial |
| `ri-25` | Tool Chain Manipulation and Injection | SEC | `mi-4`, `mi-19`, `mi-21` | Tool-surface scoring and capability policies do not validate all tool parameters and command sequences. | Partial |
| `ri-26` | MCP Server Supply Chain Compromise | SEC | `mi-4`, `mi-20`, `mi-23` | MCP signed-manifest verification and static scanning cover select threats, not continuous supplier audits or every transport. | Partial |
| `ri-27` | Agent State Persistence Poisoning | SEC | `mi-4`, `mi-22` | State encryption and optional isolated runtimes do not prove every persistent agent-state write is poisoning-resistant. | Partial |
| `ri-28` | Multi-Agent Trust Boundary Violations | OP | `mi-4`, `mi-22` | Per-task worktrees and optional sandboxes limit some interference but do not enforce network/process segmentation by default. | Partial |
| `ri-29` | Agent-Mediated Credential Discovery and Harvesting | SEC | `mi-4`, `mi-23` | Vault injection, credential scoping, and output gates narrow exposure; agents with broad file/process access can exceed these controls. | Partial |

## 3. Cross-walk to other regulator anchors

These are **thematic** links to the evidence and limitations above, not
automatic satisfaction of any cited legal requirement. Every FINOS identifier
in this table appears in §1.

| Regulator | Anchor | Strongest Bernstein mappings (FINOS mitigation and actual function) |
|-----------|--------|----------------------------------------------------------------------|
| EU AI Act | Art. 12 record-keeping, Art. 19(1) automatically generated logs, Art. 26(5) high-risk monitoring | `mi-21` (task audit chain/DSSE evidence), `mi-4` (task observability), `mi-6` (sensitivity metadata); not complete model monitoring or legal retention assurance. |
| DORA | Art. 9(3) integrity, Art. 28 ICT third-party | `mi-21` (verifiable audit), `mi-20` (MCP supplier manifest checks), `mi-23` (credential scoping); third-party due diligence remains operator-owned. |
| SR 11-7 | §V model implementation / segregation of duties, §VII model monitoring | `mi-18` (role/tool permissions), `mi-21` (decision trace), `mi-15` (code-review LLM judge); not foundation-model validation or monitoring. |
| ISO 42001 | cl. 7.5.3 control of documented information, cl. 9 performance evaluation | `mi-21` (audit evidence), `mi-4` (task telemetry), `mi-14` (opt-in state encryption); organizational system controls still require separate assessment. |

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

- FINOS AI Governance Framework, [mitigation front matter](https://github.com/finos/ai-governance-framework/tree/aabbffbe02a4aae8e6d5f8534d63edf9248fa302/docs/_mitigations)
  and [risk front matter](https://github.com/finos/ai-governance-framework/tree/aabbffbe02a4aae8e6d5f8534d63edf9248fa302/docs/_risks),
  pinned to `aabbffbe02a4aae8e6d5f8534d63edf9248fa302`; Community Specification License v1.0.
- Local canonical transcription: `src/bernstein/compliance/finos_aigf.py`
  (mitigation and risk IDs, exact titles, types, and pinned commit).
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
