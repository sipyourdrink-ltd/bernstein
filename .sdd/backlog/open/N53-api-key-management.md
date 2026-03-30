# N53 — API Key Management

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
There is no way to generate scoped, expiring API keys for CI/CD pipelines or service accounts to authenticate against the Bernstein task server.

## Solution
- Implement `bernstein apikey create --name ci --scope run,status --expires 90d`
- Hash keys before storing them in `.sdd/config/apikeys.yaml`
- Display the plaintext key only once at creation time
- Validate API key on every task server request by comparing hashed values
- Support `bernstein apikey list` and `bernstein apikey revoke <name>`

## Acceptance
- [ ] `bernstein apikey create` generates a key and displays it once
- [ ] Keys are stored hashed in `.sdd/config/apikeys.yaml`
- [ ] `--scope` restricts which endpoints the key can access
- [ ] `--expires` sets an expiration date enforced at validation time
- [ ] Task server rejects requests with invalid, expired, or insufficient-scope keys
- [ ] `bernstein apikey list` shows names, scopes, and expiry (not the key itself)
