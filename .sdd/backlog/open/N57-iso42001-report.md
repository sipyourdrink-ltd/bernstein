# N57 — ISO 42001 Report

**Priority:** P3
**Scope:** small (10-20 min)
**Wave:** 3 — Enterprise Readiness

## Problem
Organizations pursuing ISO 42001 certification need structured evidence of their AI management system, but Bernstein provides no automated way to generate compliant reports.

## Solution
- Implement `bernstein compliance report --standard iso42001`
- Generate a markdown report covering: AI risk assessment, model inventory, data handling practices, and human oversight evidence
- Pull data from `.sdd/` artifacts: run logs, model configs, approval records, audit trail
- Structure the report to align with ISO 42001 Annex A controls
- Support `--output` flag to write to a specific file path

## Acceptance
- [ ] `bernstein compliance report --standard iso42001` generates a markdown report
- [ ] Report covers AI risk assessment section
- [ ] Report covers model inventory section
- [ ] Report covers data handling practices section
- [ ] Report covers human oversight evidence section
- [ ] Report structure aligns with ISO 42001 Annex A controls
- [ ] `--output` flag writes report to specified path
