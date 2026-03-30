# P84 — Verified Publisher Badges

**Priority:** P4
**Scope:** small (10 min for skeleton/foundation)
**Wave:** 4 — Platform & Network Effects

## Problem
Enterprises cannot distinguish trusted, reviewed plugins and agents from unvetted community submissions, creating a trust barrier to adoption.

## Solution
- Create a verified publisher program: publishers submit a request with org details and plugin/agent list
- Manual review process: check code quality, security scan, publisher identity verification
- Approved publishers receive a "Verified" badge stored as a flag in the catalog/marketplace index
- Display badge in marketplace web UI, CLI search results (`bernstein agents browse`, `bernstein plugin search`), and web dashboard
- Add `verified: true` field to catalog JSON schema
- Publish verification criteria and application form on the website
- Revocation process for badge if publisher violates guidelines

## Acceptance
- [ ] Verification request form available on website with required fields
- [ ] Review workflow documented with criteria (code quality, security, identity)
- [ ] `verified` flag in catalog JSON schema for agents and plugins
- [ ] Badge displayed in marketplace web UI next to verified entries
- [ ] CLI search results indicate verified status
- [ ] Revocation process documented and enforceable
