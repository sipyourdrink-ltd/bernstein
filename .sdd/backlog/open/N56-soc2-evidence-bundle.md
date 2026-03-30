# N56 — SOC 2 Evidence Bundle

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Preparing SOC 2 audit evidence is a manual, error-prone process — teams must gather logs, manifests, and configs from scattered locations for each audit cycle.

## Solution
- Implement `bernstein compliance export --format soc2`
- Generate a ZIP archive containing: audit log, run manifests, policy configs, test results, and access logs
- Organize the ZIP with clear directory structure (e.g., `audit-log/`, `manifests/`, `policies/`, `tests/`, `access/`)
- Include a manifest.json listing all included files with SHA-256 hashes for integrity verification
- Output is ready for direct handoff to auditors

## Acceptance
- [ ] `bernstein compliance export --format soc2` produces a ZIP file
- [ ] ZIP contains audit log, run manifests, policy configs, test results, and access logs
- [ ] ZIP has a clear, auditor-friendly directory structure
- [ ] manifest.json with SHA-256 hashes is included for integrity verification
- [ ] Command works with date range filters (e.g., `--from 2026-01-01 --to 2026-03-31`)
