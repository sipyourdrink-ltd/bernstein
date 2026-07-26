# Enterprise evaluation guide

Audience: senior engineers, security reviewers, and platform teams evaluating Bernstein for production use.

This page is the short path through Bernstein's enterprise surface. It does not try to replace the deeper docs; it tells you what to verify, where the trust boundaries are, and what the real limitations are.

## Executive summary

Bernstein is a local-first, deterministic orchestrator for CLI coding agents. No model sits in its coordination loop, so parallel runs in per-task git worktrees replay byte-identically. In the default deployment model:

- orchestration state lives on disk under `.sdd/`
- agents run as separate local processes, usually in per-task git worktrees
- the API surface is local unless you explicitly expose it
- audit events are written to an HMAC-chained append-only log
- outbound network traffic depends on which adapters, model providers, and cloud features you enable

There is no separate enterprise edition. Auth, RBAC, audit logging, compliance tooling, model policy, and identity controls ship in-tree.

### What a reviewer can check, and what it costs them

The claim is that a reviewer checks a run offline, without rerunning it. That splits by key material, so budget for it before you plan a review:

| Verb | Needs | Checks |
|---|---|---|
| `bernstein artifact verify <task_id>` | on-disk artefacts only | re-derived content hash, Ed25519 signature on every lineage entry, parent-hash chain. The operator HMAC leg is skipped when no secret is supplied; the signature and chain legs still run. |
| `bernstein audit verify --merkle-only` | on-disk artefacts only | the Merkle seal over the daily `.sdd/audit/*.jsonl` files: a deleted, inserted, reordered, or byte-tampered file. |
| `bernstein audit verify`, `--hmac-only`, `bernstein audit verify-hmac` | the install's audit key | replays the per-line HMAC chain. The key sits outside the audit volume by design (`BERNSTEIN_AUDIT_KEY_PATH`, default `~/.local/state/bernstein/audit.key`), so it does not travel with the log. |
| `bernstein audit verify --receipt <file>` | the install's audit key | one automation receipt or status callback against the local chain. |

A third party who does not hold the audit key cannot replay the HMAC chain. Give them a pack exported with `bernstein audit export --signature-kind hmac-chain+pubkey` instead: it signs the chain head with the lineage Ed25519 key, so a key-less auditor authenticates the bundle against a public key.

## 1. Security model

Bernstein has two security planes:

### Human plane

Operators authenticate through local auth, OIDC, or SAML and are authorized through RBAC.

Read next:
- [Security & identity](operations/security-and-identity.md)
- [Secrets & credentials](operations/secrets.md)

What to verify:
- who can log in
- which routes each role can access
- whether auth is mandatory in your deployment
- where JWTs and session state are stored

### Agent plane

Each spawned agent can be treated as its own identity, with task-scoped permissions and audit history.

What to verify:
- whether agents can only mutate the tasks they were issued for
- how credentials are scoped into child processes
- whether your policy engine requires human approval for sensitive paths

### Isolation model

By default, Bernstein isolates agents by:

- separate processes
- separate git worktrees
- per-agent task ownership
- optional credential scoping

Important limitation: **git worktrees are not a security sandbox**. Agents can still execute arbitrary code with the privileges of the runtime unless you add a stronger sandbox backend or OS/container isolation.

For stronger isolation, evaluate:
- [Sandbox backends](architecture/sandbox.md)
- [Environment isolation](operations/env-isolation.md)
- [Deployment guide](operations/deployment-guide.md)

## 2. What leaves the machine

The safe answer is: **it depends on what you enable**.

### Default local orchestration

By default, Bernstein does not require a Bernstein-hosted control plane. Local orchestration state, task metadata, logs, and audit data stay on the machine under `.sdd/`.

### Typical outbound traffic

Outbound network traffic usually comes from one of these sources:

- the LLM provider used by your adapter (Anthropic, OpenAI, Google, etc.)
- package/install/update workflows you chose to run
- optional cloud features, webhooks, preview tunnels, cluster mode, or remote storage

### Local-only / air-gapped path

If you run only local models and local storage, Bernstein can operate without third-party model APIs. That is the deployment to validate for air-gapped or tightly regulated environments.

Bernstein ships first-class plumbing for this case: a pinned-dependency wheelhouse, signed `MANIFEST.json` plus per-wheel detached signatures, a `bernstein verify <wheelhouse>` checksum/signature pass (cosign or GPG), a `--profile airgap` runtime that flips the egress default to deny-all, a `--allow-network HOST|CIDR|none|any` per-destination override, and a `bernstein doctor airgap` self-check that confirms the perimeter before the first run.

Read next:
- [Air-gap installation](installation/air-gap.md) - wheelhouse build, signed verification, `--profile airgap`, and adapter network endpoint audit
- [Model policy](operations/MODEL_POLICY.md)
- [Compliance](operations/compliance.md)
- [Deployment guide](operations/deployment-guide.md)

## 3. Compliance considerations

Bernstein ships compliance tooling, but your deployment posture still depends on your model/provider choices and runtime environment.

### SOC 2 / ISO 27001 / PCI-DSS / NIST 800-53

Bernstein includes:
- policy evaluation
- evidence generation
- audit logging
- compliance-as-code surfaces

Read next:
- [Compliance](operations/compliance.md)
- `bernstein compliance check`
- `bernstein compliance assess`

### HIPAA / PHI

Bernstein has PHI detection and HIPAA-oriented controls, but HIPAA suitability still depends on the provider and contract boundary you choose.

Verify:
- which model provider sees PHI-bearing prompts, if any
- whether you have a BAA where required
- whether local-only or approved-provider mode is enforced

### Data residency

Bernstein's local state can remain local. Data residency risk usually comes from the model provider, remote artifact sink, or cloud deployment you enable.

Use [Model policy](operations/MODEL_POLICY.md) and deployment-level network controls to constrain this.

## 4. Network requirements

### Default local CLI mode

- inbound: none required
- localhost: Bernstein may expose local services for orchestration and dashboard flows
- outbound: only what your configured adapters/providers/features require

### Self-hosted shared server

- inbound: only the API/UI endpoints you intentionally expose
- outbound: model providers, optional storage sinks, optional identity providers

### Air-gapped / local-model mode

- inbound: none beyond your internal network policy
- outbound: none to external model providers if you use local models only

### Cloud / distributed mode

If you enable Cloudflare, cluster mode, preview tunnels, webhooks, or remote storage, re-do the network review. Those features are explicitly outside the minimal local-only trust boundary.

## 5. Cost controls

Bernstein has native cost-control primitives:

- per-run budgets
- token and policy limits
- model routing policy
- anomaly detection / burn-rate controls
- cost reporting

Read next:
- [Cost optimization](operations/cost-optimization.md)
- [Model policy](operations/MODEL_POLICY.md)
- `bernstein cost`

What to verify:
- hard budget caps actually stop runs
- cheap models are used for cheap work
- expensive models are reserved for high-value steps
- alerting and audit surfaces satisfy your approval workflow

## 6. Audit trail and forensics

Bernstein keeps a file-based audit trail designed for reconstruction and tamper evidence.

Expect to inspect:
- `.sdd/` runtime state
- task lifecycle records
- per-agent identity history
- HMAC-chained audit logs
- per-artefact lineage chain (output → prompt → inputs), exportable for regulator handoff

Read next:
- [Security & identity](operations/security-and-identity.md)
- [HMAC-chained audit log operator guide](security/audit-log.md)
- `security/AUDIT.md`
- [Regulatory lineage export (operator guide)](compliance/lineage-export.md)
- [Regulator-class lineage (schema reference)](compliance/regulatory-lineage.md)
- [Disaster recovery](operations/disaster-recovery.md)

What to verify:
- you can reconstruct who started a run
- you can reconstruct which agent touched which task
- audit retention matches policy
- HMAC key handling is externalized appropriately for your environment

## 7. Deployment patterns

### Developer laptop

Best when:
- a single engineer owns the workflow
- local provider/API key use is acceptable
- you want the smallest operational footprint

### CI / ephemeral runner

Best when:
- every run should be reproducible
- credentials are short-lived and centrally managed
- artifact retention and logs are already part of CI

### Shared internal server

Best when:
- multiple operators need one controlled Bernstein instance
- you want SSO, RBAC, centralized audit, and stronger policy enforcement

### Air-gapped / local-model deployment

Best when:
- code cannot be sent to third-party providers
- you can accept local-model quality/performance tradeoffs

## 8. Honest limitations

These are the bits a security team should hear plainly:

- Bernstein orchestrates tools that can execute arbitrary code. If you need strong containment, you must provide it.
- Git worktree isolation is operational isolation, not a security boundary.
- "No external traffic" is only true for the deployment you actually configure. Cloud features, remote storage, webhooks, and hosted model providers change the answer.
- Compliance tooling helps produce evidence; it does not make an unsafe deployment safe by itself.
- Provider risk is real. Model policy reduces it, but cannot change a provider's underlying trust model.
- Prompt-injection exfiltration is structurally refused at spawn time by the [lethal-trifecta capability gate](security/lethal-trifecta.md) - tool chains that combine private data, untrusted input, and external comm are denied before the agent process starts. This narrows the prompt-injection surface but does not remove it.

## 9. Evaluation checklist

Use this as a practical sign-off sheet.

### Identity and access

- [ ] Verified which auth mode is enabled (OIDC, SAML, or local)
- [ ] Confirmed auth is not disabled in production
- [ ] Reviewed RBAC roles and route permissions
- [ ] Tested agent identity scoping and revocation

### Data flow

- [ ] Mapped all outbound destinations for the chosen deployment
- [ ] Confirmed which model/provider can receive source code
- [ ] Confirmed whether any remote artifact sink or cloud feature is enabled
- [ ] Validated data residency requirements against provider reality

### Isolation and execution

- [ ] Tested agent isolation at the process/worktree level
- [ ] Decided whether stronger sandboxing is required
- [ ] Verified secret scoping into child agents
- [ ] Reviewed policy engine rules for sensitive paths/commands
- [ ] Reviewed the [lethal-trifecta capability gate](security/lethal-trifecta.md) and confirmed the default capability matrix matches deployment expectations

### Audit and recovery

- [ ] Inspected `.sdd/` layout and retention plan
- [ ] Reviewed HMAC audit-chain handling - see [audit-log.md](security/audit-log.md)
- [ ] Reconstructed a sample run from logs and task state
- [ ] Tested stop/restart / crash-recovery behavior

### Cost and governance

- [ ] Confirmed budget caps work
- [ ] Confirmed model routing policy matches approved providers
- [ ] Reviewed anomaly/cost reporting surfaces
- [ ] Validated approval workflow for high-risk tasks

### Compliance

- [ ] Ran `bernstein compliance check` for the required framework(s)
- [ ] Generated an evidence package where applicable
- [ ] Reviewed HIPAA / PHI posture if regulated data is in scope
- [ ] Walked the [regulatory lineage export](compliance/lineage-export.md) for a sample run and verified the chain with `bernstein lineage verify`
- [ ] Documented remaining gaps and compensating controls

## Recommended evaluation flow

1. Start with [Security & identity](operations/security-and-identity.md).
2. Review [Compliance](operations/compliance.md) and [Model policy](operations/MODEL_POLICY.md).
3. Decide whether default worktree isolation is sufficient.
4. Run a small pilot on a non-sensitive repo.
5. Validate audit reconstruction, cost limits, and restart behavior.
6. Only then move to sensitive code or shared-server deployment.
