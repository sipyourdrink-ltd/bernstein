# F95 — Zero-Trust Agent Networking

**Priority:** P5
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 5 — Future-Proofing 2030-2035

## Problem
Agent-orchestrator communication is not cryptographically authenticated, making it vulnerable to impersonation, tampering, and man-in-the-middle attacks in distributed deployments.

## Solution
- Implement mutual TLS (mTLS) between orchestrator and all agents
- Orchestrator acts as a lightweight CA, issuing short-lived certificates to registered agents
- Agents present client certificates on every connection; orchestrator verifies against its CA
- Task manifests are cryptographically signed by the orchestrator; agents verify signature before execution
- Agent identity verified via certificate subject matching registered agent ID
- Implement automatic certificate rotation with configurable TTL (default 24 hours)
- Add `bernstein security status` showing certificate health, expiry, and trust chain
- Revocation via CRL (Certificate Revocation List) distributed to all agents on sync

## Acceptance
- [ ] mTLS enforced between orchestrator and agents
- [ ] Orchestrator issues short-lived certificates to registered agents
- [ ] Task manifests signed by orchestrator and verified by agents before execution
- [ ] Agent identity verified via certificate subject
- [ ] Automatic certificate rotation with configurable TTL
- [ ] `bernstein security status` displays certificate health and expiry
- [ ] Certificate revocation via CRL supported
