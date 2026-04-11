# Enterprise Evaluation Guide

This guide answers the questions security, compliance, and platform teams typically ask when evaluating Bernstein for enterprise adoption.

It covers the security model, compliance posture, deployment options, data handling, and operational requirements.

---

## What Bernstein is (and isn't)

Bernstein is an **on-premises orchestration layer** that coordinates CLI coding agents you already control. It is not a SaaS product, not a cloud service, and not a hosted AI platform.

- Bernstein runs **on your infrastructure** — laptop, VM, Kubernetes cluster
- Agent API calls go **directly from your machine to the AI provider** — not through Bernstein servers
- Source code, task state, and audit logs **never leave your environment**
- The Bernstein maintainers have **zero visibility** into your runs, your code, or your data

---

## Security model

### Components and trust boundaries

```
Your codebase (git repo)
  └── Bernstein orchestrator (Python process, port 8052)
        ├── Task server (HTTP API, localhost-only by default)
        ├── Agent spawner (forks CLI agent processes)
        │     └── Agent process (Claude Code / Codex / Gemini / etc.)
        │           └── Worktree (isolated git worktree per task)
        └── Janitor (verifies agent output before merge)
```

**Key isolation properties:**

| Property | Mechanism |
|----------|-----------|
| Agents cannot access each other's work | Each agent runs in a separate git worktree |
| Failed agent cannot corrupt main branch | Merge only happens after janitor verification |
| Agent commands are sandboxed | Permission rule engine with allowlist/denylist |
| Task server is not exposed to the internet | Bound to `127.0.0.1:8052` by default |
| Audit log is tamper-evident | HMAC-chained entries — any modification breaks the chain |

### Authentication and access control

The task server supports three authentication methods:

| Method | Use case | Config key |
|--------|----------|------------|
| Bearer token (agent tokens) | Agent-to-server communication | `auth.method: bearer` |
| JWT (stateless, expiring) | Human API access, CI pipelines | `auth.method: jwt` |
| OIDC / SSO | Multi-user enterprise deployments | `auth.method: oidc` |

OIDC supports Google Workspace, Okta, Azure AD, Ping Identity, and any standards-compliant IdP. Role mapping from IdP groups to Bernstein roles is configurable.

Authentication is disabled by default for local single-user use. For any deployment with network exposure or multiple users, enable authentication:

```yaml
# bernstein.yaml
auth:
  enabled: true
  method: oidc
  oidc:
    issuer_url: "https://idp.yourcompany.com"
    client_id: "bernstein-prod"
    client_secret: "${OIDC_CLIENT_SECRET}"
```

See [Security Hardening Guide](security-hardening.md#api-authentication) for the full configuration reference.

### Permission model

Bernstein enforces a two-layer permission model:

**Layer 1 — Permission rules** (`action: deny | ask | allow`):
Rules control individual tool calls at runtime. Rules are stored in `.bernstein/rules.yaml` and evaluated for every agent action. The first matching rule wins.

**Layer 2 — Role-based file permissions**:
Each agent role (`backend`, `qa`, `docs`, etc.) has a built-in matrix of allowed and denied filesystem paths. Agents cannot write outside their role's allowed paths.

Both layers enforce path traversal protection unconditionally: paths containing `..`, null bytes, URL-encoded sequences, or symlinks escaping the project root are blocked regardless of rules or role permissions.

### Audit logging

Every task lifecycle event is written to `.sdd/metrics/audit.jsonl` with:

- Timestamp (ISO 8601, microsecond precision)
- Event type and task ID
- Agent identifier
- File paths affected
- HMAC chain hash linking to the previous entry

**Chain verification:**

```bash
bernstein admin verify-audit-log
```

Any insertion, deletion, or modification of a log entry breaks the HMAC chain and is immediately detectable. The audit log can be exported to Splunk, Elasticsearch, or CloudWatch in real time.

Retention is configurable:

```yaml
audit:
  enabled: true
  retention_days: 365
  archive_compressed: true
```

### PII and secret detection

Before agent output is merged, Bernstein scans diffs for:

- Hardcoded secrets (AWS keys, API tokens, password assignments)
- Private key material (RSA, EC, OpenSSH)
- Unsafe `eval` / `exec` patterns
- Shell injection vectors
- PII patterns (SSN, email, credit card numbers — configurable)

The scanner runs entirely locally with static regex patterns — no LLM, no network call.

```yaml
pii_gate:
  enabled: true
  action: block   # block | redact | warn
```

---

## Data handling

### What data stays on your infrastructure

| Data | Location | Leaves your environment? |
|------|----------|--------------------------|
| Source code | Your git repo | Never |
| Task state | `.sdd/backlog/`, `.sdd/runtime/` | Never |
| Audit logs | `.sdd/metrics/audit.jsonl` | Only if you configure SIEM export |
| Agent prompts | Constructed in memory, sent to AI provider | Only to AI provider (Anthropic, OpenAI, Google) |
| Agent outputs | Written to worktree files | Never |
| Cost metrics | `.sdd/metrics/` | Never |

### What is sent to AI providers

Bernstein sends task prompts and codebase context to the AI provider configured for each agent (Anthropic, OpenAI, Google, etc.). This is the same data you would send if using those agents directly. Bernstein does not add telemetry to these requests.

If you use local models (Ollama + Aider), no data leaves your machine at all.

### Data residency

For multi-region or multi-tenant deployments, Bernstein supports tenant-level data residency policies:

```yaml
data_residency:
  tenant_policies:
    eu-tenant:
      allowed_regions: ["eu-west", "eu-central"]
      enforce_strict: true
```

---

## Compliance

### Framework applicability

| Framework | Applicability | Notes |
|-----------|--------------|-------|
| SOC 2 Type II | Applicable if Bernstein is in scope of your audit | Audit log, access controls, and encryption at rest support SOC 2 evidence collection |
| GDPR / CCPA | Applicable if agents process personal data | PII gate, data residency controls, and audit log support GDPR data mapping |
| HIPAA | Self-hosted with appropriate controls | No PHI should be passed to external AI providers without a BAA |
| ISO 27001 | Applicable | Audit log, access controls, and change management features support 27001 |
| FedRAMP | Not directly applicable | Bernstein is not FedRAMP authorized; cloud AI providers may have separate FedRAMP offerings |

### Compliance tooling

```bash
# Run a full security posture check
bernstein doctor --security

# Generate a structured compliance report
bernstein admin compliance-report --format json > compliance-$(date +%Y%m%d).json
```

The compliance report covers: authentication status, audit log integrity, network isolation, PII detection, dependency vulnerability scan, and access control configuration.

### Dependency scanning

Bernstein scans agent-introduced dependencies before merge:

```yaml
dependency_scan:
  enabled: true
  block_on_critical: true
  allowed_licenses:
    - "MIT"
    - "Apache-2.0"
    - "BSD-2-Clause"
    - "BSD-3-Clause"
```

---

## Deployment options

### Single-machine (developer workstation)

The default mode. Bernstein runs as a local Python process alongside the developer's terminal. No network exposure, no persistent daemon.

```bash
pip install bernstein
bernstein -g "Add rate limiting"
```

### Team / shared server

Deploy Bernstein on a shared Linux server. Enable authentication, TLS termination via reverse proxy, and IP allowlisting.

Minimum recommended server: 4 vCPU, 8 GB RAM, 50 GB SSD.

See [Deployment Guide](deployment-guide.md) for nginx / Caddy configuration.

### Kubernetes / container

Official Docker images are published at `ghcr.io/chernistry/bernstein`:

```bash
docker pull ghcr.io/chernistry/bernstein:latest
docker pull ghcr.io/chernistry/bernstein:sandbox   # Research sandbox image
```

Helm chart: see [Helm Deployment Guide](HELM_DEPLOYMENT.md).

### Cluster (distributed)

For large organizations running many parallel agents:

```yaml
# bernstein.yaml
cluster:
  enabled: true
  coordinator: "http://bernstein-coordinator:8052"
  worker_count: 10
  auth:
    enabled: true
    secret: "${BERNSTEIN_CLUSTER_SECRET}"
```

See [Cluster Guide](CLUSTER.md) for network topology, worker authentication, and failure handling.

---

## Enterprise evaluation checklist

Use this checklist to assess Bernstein for your organization. Each item links to the relevant documentation.

### Security

- [ ] **Authentication** — Is agent-to-server and human-to-server authentication enabled?
  - Default: disabled (local single-user). Enable for any shared or networked deployment.
  - Supported methods: bearer token, JWT, OIDC/SSO
  - Docs: [Security Hardening — API authentication](security-hardening.md#api-authentication)

- [ ] **Authorization** — Are role-based file permissions and permission rules configured?
  - Review the default role matrix and tighten paths for your codebase layout
  - Docs: [Security Hardening — Role-based file permissions](security-hardening.md#role-based-file-permissions)

- [ ] **Network exposure** — Is the task server bound to localhost or a private network only?
  - Default: `127.0.0.1:8052`. Never expose directly to the internet.
  - Docs: [Security Hardening — Network isolation](security-hardening.md#network-isolation)

- [ ] **TLS** — Is HTTPS enforced for non-localhost access?
  - Bernstein does not terminate TLS; use nginx, Caddy, or similar
  - Docs: [Security Hardening — TLS termination](security-hardening.md#tls-termination)

- [ ] **Secrets management** — Are API keys and secrets in environment variables, not config files?
  - Use `${VAR_NAME}` references in `bernstein.yaml`
  - Docs: [Security Hardening — Secret management](security-hardening.md#secret-management)

- [ ] **Audit log** — Is the tamper-evident audit log enabled and being exported to your SIEM?
  - Docs: [Security Hardening — Audit mode](security-hardening.md#audit-mode)

- [ ] **PII gate** — Is PII detection enabled and configured for your data sensitivity?
  - Docs: [Security Hardening — PII detection](security-hardening.md#pii-detection)

- [ ] **Dependency scanning** — Are agent-introduced dependencies scanned for vulnerabilities and license compliance?
  - Docs: [Security Hardening — Dependency scanning](security-hardening.md#dependency-scanning)

- [ ] **Vulnerability disclosure** — Have you reviewed the bug bounty scope and safe harbor terms?
  - Docs: [Bug Bounty Program](bug-bounty.md)

### Data and privacy

- [ ] **Data residency** — Is Bernstein deployed in the correct region for your data residency requirements?

- [ ] **AI provider data handling** — Have you reviewed the data processing terms for each AI provider your agents use?
  - Anthropic: [Usage Policy](https://www.anthropic.com/legal/usage-policy)
  - OpenAI: [Enterprise Privacy](https://openai.com/enterprise-privacy)
  - Google: [Gemini API Terms](https://ai.google.dev/gemini-api/terms)

- [ ] **Local models** — For air-gapped or strict no-cloud requirements, are local models (Ollama) configured?

- [ ] **Encryption at rest** — Is the `.sdd/` state directory on an encrypted volume?
  - Docs: [Security Hardening — Encryption at rest](security-hardening.md#encryption-at-rest)

### Compliance

- [ ] **Compliance report** — Have you run `bernstein admin compliance-report` and reviewed the output?

- [ ] **`bernstein doctor --security`** — Does the security check pass clean?

- [ ] **Policy-as-code** — Are YAML or Rego policies configured for your compliance requirements?
  - Example: block merges that add source files without tests
  - Docs: [Security Hardening — Policy-as-code engine](security-hardening.md#policy-as-code-engine)

- [ ] **License compliance** — Is the Apache 2.0 license compatible with your organization's open-source policy?
  - License: [Apache 2.0](../LICENSE)

### Operations

- [ ] **Backup and restore** — Is the `.sdd/` directory included in your backup policy?
  - Crash recovery is WAL-based; no silent data loss. But backups are still needed for disaster recovery.

- [ ] **Monitoring** — Are Prometheus metrics and/or the Grafana dashboard configured?
  - Endpoint: `GET /metrics` on the task server
  - Docs: [Deployment Guide](deployment-guide.md)

- [ ] **Log forwarding** — Are Bernstein logs forwarded to your centralized logging platform?

- [ ] **Incident response** — Do you have a runbook for Bernstein failures?
  - Docs: [Runbooks](runbooks.md)

- [ ] **Update policy** — Are you tracking Bernstein releases and applying security patches?
  - Security patches are backported to the current minor version only. Always run the latest.
  - Supported versions: see [SECURITY.md](../SECURITY.md#supported-versions)

### Access and identity

- [ ] **SSO integration** — Is Bernstein integrated with your identity provider?
  - Supports OIDC, OAuth 2.0 PKCE, SAML (via OIDC bridge)
  - Docs: [Security Hardening — OIDC / SSO](security-hardening.md#oidc--sso-recommended-for-multi-user-or-enterprise)

- [ ] **MFA** — Is multi-factor authentication enforced at the IdP level for Bernstein access?

- [ ] **Least privilege** — Are agent roles scoped to the minimum paths needed for each task type?

- [ ] **Token rotation** — Are JWT secrets and audit log HMAC keys rotated on a schedule?

### Vendor and support

- [ ] **Open-source license** — Bernstein is Apache 2.0. You may fork, modify, and self-host without restriction.

- [ ] **Support channel** — Community support via GitHub Issues. No SLA-backed enterprise support tier at this time.

- [ ] **Security disclosures** — Subscribe to GitHub security advisories for the `chernistry/bernstein` repository.

- [ ] **Roadmap** — Review [CHANGELOG.md](CHANGELOG.md) and the public roadmap for planned features.

---

## Security contacts

| Channel | Use |
|---------|-----|
| HackerOne — https://hackerone.com/bernstein | Vulnerability reports |
| security@bernstein.dev | Security questions outside HackerOne scope |
| GitHub Issues — https://github.com/chernistry/bernstein/issues | Non-security bugs and feature requests |

For vulnerability reports: **do not open a public GitHub issue.** Use HackerOne or the security email.

---

## Further reading

- [Security Hardening Guide](security-hardening.md) — Detailed configuration reference
- [Bug Bounty Program](bug-bounty.md) — Researcher sandbox and disclosure policy
- [Architecture](ARCHITECTURE.md) — How Bernstein works internally
- [Deployment Guide](deployment-guide.md) — Production deployment patterns
- [Cluster Guide](CLUSTER.md) — Distributed execution
