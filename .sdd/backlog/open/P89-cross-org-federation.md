# P89 — Cross-Organization Federation

**Priority:** P4
**Scope:** medium (20 min for skeleton/foundation)
**Wave:** 4 — Platform & Network Effects

## Problem
Organizations working together cannot share workflow templates or agent catalogs across boundaries, forcing duplication and preventing ecosystem network effects.

## Solution
- Design a federation protocol where organizations share workflow templates and agent catalogs via signed manifests
- Manifest format: JSON document listing available workflows/agents, signed with org's private key
- Trust-on-first-use (TOFU) model: first connection to an org records its public key; subsequent connections verify against stored key
- Implement `bernstein federation join <org-url>` CLI command to subscribe to an org's manifest
- `bernstein federation list` shows all joined orgs and their available resources
- `bernstein federation sync` pulls latest manifests from all joined orgs
- Conflict resolution: local resources take precedence over federated ones with same name
- Revocation: `bernstein federation leave <org-url>` removes trust and cached resources

## Acceptance
- [ ] Signed manifest format defined with JSON schema
- [ ] TOFU key management: store on first use, verify on subsequent connections
- [ ] `bernstein federation join <org-url>` subscribes to remote org
- [ ] `bernstein federation list` shows joined orgs and available resources
- [ ] `bernstein federation sync` pulls latest manifests
- [ ] Local resources take precedence over federated resources with same name
- [ ] `bernstein federation leave <org-url>` removes trust relationship
