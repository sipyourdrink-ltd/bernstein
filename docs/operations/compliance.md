# Compliance

Bernstein ships a compliance toolkit aimed at enterprise security
engineers and compliance officers who need to prove - to an auditor or
to internal reviewers - that the orchestrator's runtime configuration
matches a specific control framework. The CLI lives in
`cli/commands/compliance_cmd.py:26` (`@click.group("compliance")`) and
is a thin wrapper over two engines:

- **`bernstein.compliance.eu_ai_act.ComplianceEngine`** (EU AI Act:
  Annex III classification, Annex IV technical documentation, Article
  43 conformity assessment).
- **`bernstein.core.security.compliance_policies.CompliancePolicyLibrary`**
  (compliance-as-code policy evaluation against a runtime snapshot,
  with optional Rego export).

This page describes what the CLI does, where the artifacts land, and
how to map the output to an audit package. For provider-policy
enforcement (which provider can see which code) read
`operations/MODEL_POLICY.md`. For audit-log integrity and SOC 2
evidence specifically, read `security/AUDIT.md`.

---

## Frameworks supported

Two distinct surfaces, both reachable through the `bernstein
compliance` group:

| Framework        | Surface                                                              | Status                                |
| ---------------- | -------------------------------------------------------------------- | ------------------------------------- |
| **EU AI Act**    | `compliance assess` / `eu-ai-act` / `report` (Annex III + Annex IV)  | Shipped (Regulation (EU) 2024/1689)   |
| **HIPAA**        | `compliance: hipaa` mode (`core/security/hipaa.py`) + PHI detection  | Shipped (45 CFR §164.514(b))          |
| **SOC 2**        | Policy library + `security/AUDIT.md` (HMAC-chained audit log)        | Shipped                               |
| **ISO 27001**    | Policy library                                                       | Shipped                               |
| **PCI-DSS**      | Policy library                                                       | Shipped                               |
| **NIST 800-53**  | Policy library                                                       | Shipped                               |

The five framework values accepted by the policy commands are exactly
the members of `ComplianceFramework`
(`core/security/compliance_policies.py:54`): `soc2`, `iso27001`,
`pci_dss`, `nist_800_53`. (HIPAA is enforced by `compliance: hipaa` in
`bernstein.yaml` rather than by the policy library.)

---

## `bernstein compliance` group

All subcommands accept `--workdir` (default: `.`). Per-subcommand
notes below cite `cli/commands/compliance_cmd.py` line numbers.

### `eu-ai-act` - task-risk summary

```
bernstein compliance eu-ai-act [--workdir .] [--json-output]
```

Reads `<workdir>/.sdd/eu_ai_act/*.json` task assessments and prints
counts per risk level (`minimal`, `limited`, `high`, `unacceptable`)
plus the latest high-risk tasks (`compliance_cmd.py:31-68`). Use this
during a run to confirm that no task was flagged `high` or
`unacceptable` without the required approval.

### `assess` - generate the EU AI Act evidence package

```
bernstein compliance assess [--workdir .] [--output-dir DIR]
                            [--version 1.0.0] [--no-export]
                            [--json-output]
```

Runs the `ComplianceEngine` for the deployment described by
`bernstein_descriptor()`: classifies the system under Annex III,
generates the Annex IV technical document, executes the Article 43
conformity assessment, and writes the evidence package to
`<workdir>/.sdd/compliance/evidence_package.json`
(`compliance_cmd.py:71-113`).

The printed summary includes risk category, Annex III domain, conformity
status (pass/fail/partial), justification, mandatory gaps, and the
August 2027 compliance deadline (`compliance_cmd.py:141-182`).

### `report` - pretty-print an existing evidence package

```
bernstein compliance report [--evidence-package PATH] [--workdir .]
                            [--json-output]
```

Loads `<workdir>/.sdd/compliance/evidence_package.json` (or a path
given with `--evidence-package`) and re-renders the same summary table
(`compliance_cmd.py:116-138`). Useful for auditors who want a textual
view of an artifact already on disk.

### `enable` / `disable` - activate a framework

```
bernstein compliance enable  <soc2|iso27001|pci_dss|nist_800_53>
bernstein compliance disable <soc2|iso27001|pci_dss|nist_800_53>
```

Writes / removes a marker file under
`<workdir>/.sdd/compliance/enabled/<framework>.yaml` so the policy set
persists across restarts (`compliance_cmd.py:190-235`). After enabling,
the next `bernstein compliance check` (without `--framework`) evaluates
all enabled frameworks at once.

### `list` - list policies

```
bernstein compliance list [--framework <name>] [--json-output]
```

Prints policy id, framework, control id, severity, name. Without
`--framework` it lists every policy across every framework
(`compliance_cmd.py:238-278`).

### `check` - evaluate policies against a runtime snapshot

```
bernstein compliance check [--framework <name>] [--workdir .]
                           [--fail-on critical|high|medium|low|none]
                           [--audit-logging/--no-audit-logging]
                           [--audit-hmac-chain/--no-audit-hmac-chain]
                           [--sandbox-enabled/--no-sandbox-enabled]
                           [--seccomp-enabled/--no-seccomp-enabled]
                           [--tls-enforced/--no-tls-enforced]
                           [--mfa-enabled/--no-mfa-enabled]
                           [--rbac-enabled/--no-rbac-enabled]
                           [--encrypt-at-rest/--no-encrypt-at-rest]
                           [--vulnerability-scanning/--no-vuln…]
                           [--secrets-rotation-days 30]
                           [--json-output]
```

Every flag describes the *current* state of the deployment. The CLI
constructs a `PolicyInput` (defined at
`core/security/compliance_policies.py:79`), runs every enabled or
selected policy, and reports passing/failing counts and remediation
text for each failure (`compliance_cmd.py:281-406`).

`--fail-on` controls the exit code: by default the CLI exits non-zero
only when at least one *critical* policy fails. Use `--fail-on high`
in CI to fail builds on `high`-severity findings as well.

The full `PolicyInput` schema covers ~30 fields including
`audit_retention_days`, `network_isolation`, `read_only_rootfs`,
`sbom_enabled`, `phi_detection`, `data_residency_enforced`,
`backup_encryption`, `dr_rto_hours`, and others (see
`core/security/compliance_policies.py:79-160`). Many are not yet
exposed as flags; pipe `--json-output` and use the embedded library
when richer snapshots are needed.

### `rego` - export OPA / Rego rules

```
bernstein compliance rego <framework> [--output-dir DIR] [--workdir .]
```

Emits one `.rego` file per policy under
`<workdir>/.sdd/compliance/rego/<framework>/`
(`compliance_cmd.py:409-435`) so the same rules can be loaded into an
OPA server for live evaluation in front of the API gateway.

---

## EU AI Act specifics

The EU AI Act surface is implemented in `bernstein.compliance.eu_ai_act`
(see `src/bernstein/compliance/eu_ai_act.py:1`) and covers:

- **Article 5** - prohibited practices (unacceptable risk).
- **Article 6 + Annex III** - high-risk classification (eight domains:
  biometrics, critical infrastructure, education, employment, essential
  services, law enforcement, migration, justice).
- **Article 43** - conformity assessment (pass / fail / partial per
  control).
- **Article 50** - transparency obligations (limited risk).
- **Annex IV** - technical documentation (auto-generated as part of the
  evidence package).

What `bernstein compliance assess` writes:

- `evidence_package.json` containing:
  - `report.classification` - risk category + Annex III domain +
    plain-language justification.
  - `report.conformity` - overall status, per-control pass/fail/partial
    counts, mandatory gaps.
  - `report.compliance_summary` - next-step list and the August 2027
    deadline (`Article 111(2)`).
  - `tech_doc` - the Annex IV technical document (system description,
    intended use, data sets, risk-management measures, post-market
    monitoring).

Bernstein itself classifies as **limited risk** by default (transparency
obligations only) under `bernstein_descriptor()`. Override the
descriptor in code if your deployment falls into one of the Annex III
domains - for example, an HR-screening use case (Annex III §4) shifts
the classification to `high`, and the conformity assessment becomes
mandatory before the August 2027 deadline.

What is *audited*: the conformity assessment runs against the same
runtime snapshot consumed by `compliance check`. A policy gap (failing
control) becomes a `mandatory_gaps` entry in the evidence package.

What is *documented but not auto-verified*: the descriptor fields
(intended use, deployment context, training data sources). Those are
operator inputs; an auditor must read them and decide whether they are
accurate.

---

## HIPAA / PHI handling

HIPAA mode activates when `compliance: hipaa` is set in
`bernstein.yaml`. The implementation lives in
`core/security/hipaa.py:1`. It provides four enforcement layers:

1. **PHI detection** - regex-based scan of agent inputs and outputs
   for SSNs, MRNs, DOBs, phone numbers, emails, ICD codes, and the 18
   identifier categories listed in 45 CFR §164.514(b)
   (`core/security/hipaa.py:59`).
2. **File access controls** - agents are denied access to paths matching
   PHI globs (e.g. `*.phi`, `patient_records/**`).
3. **Encryption at rest** - `.sdd/` state files are AES-256-GCM
   encrypted via the `cryptography` package.
4. **BAA-ready report** - `core/security/hipaa.py` emits a structured
   compliance report suitable for inclusion in a Business Associate
   Agreement audit package.

Configure HIPAA mode end-to-end by combining:

- `compliance: hipaa` in `bernstein.yaml` (turns on the four layers
  above).
- `bernstein compliance check --phi-detection ...` is *not* yet exposed
  as a flag; pass the snapshot through the library directly when you
  need PHI-scoped policy evaluation.
- Environment-variable isolation
  (`operations/env-isolation.md`) so PHI never leaks via inherited
  shell state.
- Credential vault (`operations/secrets.md` once published) for any
  PHI-bearing credentials.

For provider-side data residency - "PHI never leaves Anthropic", "no
cloud APIs at all" - combine HIPAA mode with model policy
(`operations/MODEL_POLICY.md`) constraints.

---

## SOC 2 evidence export

SOC 2 control evidence comes from two complementary surfaces:

- **`bernstein compliance check --framework soc2`** evaluates the
  configuration controls (audit logging, MFA, RBAC, TLS, secrets
  rotation, etc.) against the SOC 2 policy set in
  `core/security/compliance_policies.py`. The pass/fail report becomes
  one component of the evidence package.
- **HMAC-chained audit log** is the second component. Every state
  transition is signed and chained, so an auditor can verify that the
  sequence has not been tampered with after the fact. Setup, key
  rotation, and verification commands are documented in detail in
  [`security/AUDIT.md`](../security/AUDIT.md).

A typical SOC 2 evidence-export workflow:

1. Run `bernstein compliance enable soc2` once during deployment.
2. Run `bernstein compliance check --framework soc2 --json-output`
   periodically (recommended: nightly in CI) and archive the output.
3. Export the audit log via `POST /export/tasks` and `POST /export/agents`
   (auth-gated; see [`security/AUDIT.md`](../security/AUDIT.md)).
4. Bundle (1) `policy check` JSON, (2) audit-log export, (3) SBOM
   (`POST /sbom`, `GET /sbom`). The bundle is the SOC 2 evidence
   package.

---

## Cross-link: provider-policy enforcement

Compliance is more than the framework checklist. Bernstein's *runtime*
constraint on which provider can see which code lives in **Model
Policy** (`operations/MODEL_POLICY.md`). Examples:

- "Code never leaves Anthropic" → `model_policy.allowed_providers:
  [anthropic]`.
- "No cloud APIs at all" → `model_policy.allowed_providers: [ollama]`.
- "SOC 2 certified providers only" → curated allow-list per the SOC 2
  evidence package above.

The policy engine in `MODEL_POLICY.md` runs *before* the routing layer,
so denied providers are never even offered to the cascade router. This
is the recommended way to enforce data-residency constraints inside an
EU AI Act high-risk classification or a HIPAA covered entity.

---

## Code pointers

- `cli/commands/compliance_cmd.py:26` - `@click.group("compliance")`
  entry point.
- `cli/commands/compliance_cmd.py:31-68` - `eu-ai-act` summary.
- `cli/commands/compliance_cmd.py:71-113` - `assess` (writes evidence
  package).
- `cli/commands/compliance_cmd.py:116-138` - `report` (re-renders an
  existing package).
- `cli/commands/compliance_cmd.py:190-235` - `enable` / `disable`
  framework.
- `cli/commands/compliance_cmd.py:238-278` - `list` policies.
- `cli/commands/compliance_cmd.py:281-406` - `check` (PolicyInput
  evaluation, exit-code threshold).
- `cli/commands/compliance_cmd.py:409-435` - `rego` export.
- `src/bernstein/compliance/eu_ai_act.py:1` - `ComplianceEngine`,
  `RiskCategory`, `AnnexIIIDomain`, `bernstein_descriptor()`.
- `core/security/compliance_policies.py:54` - `ComplianceFramework`
  enum (soc2 / iso27001 / pci_dss / nist_800_53).
- `core/security/compliance_policies.py:79` - `PolicyInput` dataclass
  (full ~30-field runtime snapshot schema).
- `core/security/compliance_policies.py:1233` -
  `CompliancePolicyLibrary` (enable/disable, evaluate, export Rego).
- `core/security/compliance_policies.py:166` - `CompliancePolicy`
  dataclass.
- `core/security/compliance_policies.py:198` - `PolicyResult`
  dataclass.
- `core/security/hipaa.py:1` - HIPAA mode (PHI detection, file ACL,
  encryption, BAA report).
- `core/security/hipaa.py:59` - `PHICategory` (45 CFR §164.514(b)
  identifier categories).
- `core/security/eu_ai_act.py` - task-level risk assessment store consumed by
  `bernstein compliance eu-ai-act`.
- `[security/AUDIT.md](../security/AUDIT.md)` - HMAC-chained audit log
  (SOC 2 evidence component).
- `[operations/MODEL_POLICY.md](MODEL_POLICY.md)` - provider-policy
  enforcement (CISO-level constraints on which provider sees which
  code).
- [`compliance/lineage-export.md`](../compliance/lineage-export.md) -
  operator guide to `bernstein lineage export` / `verify`, including
  worked DORA / SOC 2 / EU AI Act / HIPAA workflows.
- [`compliance/regulatory-lineage.md`](../compliance/regulatory-lineage.md)
  - schema-v2 reference for the per-artefact lineage trail, customer-
  key signing, KMS-adapter dispatch, and SIEM-webhook tamper-loud
  configuration.
- [`compliance/eu-ai-act-article-12-bundle.md`](../compliance/eu-ai-act-article-12-bundle.md)
  - operator guide for the deterministic Article 12 evidence pack.
- [`security/audit-dsse-envelope.md`](../security/audit-dsse-envelope.md)
  - DSSE / in-toto v1 envelope wrapper plus the standalone stdlib-only
  verifier.
- [`compliance/finos-aigf-mapping.md`](../compliance/finos-aigf-mapping.md)
  - bernstein's coverage of the 16 AIGF controls and 14 AIR risks.
