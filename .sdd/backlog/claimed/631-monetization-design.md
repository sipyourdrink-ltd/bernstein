# 631 — Monetization Design

**Role:** architect
**Priority:** 3 (medium)
**Scope:** small
**Depends on:** #600

## Problem

There is no monetization strategy for Bernstein. Sustainable open-source projects need revenue. Without a clear monetization plan, the project depends entirely on personal time and motivation, which does not scale.

## Design

Design monetization tiers following the open-core model. Free tier: full CLI orchestration, local execution, all adapters, community support. Pro tier ($20/month): hosted dashboard with team sharing, advanced analytics (cost trends, model performance comparisons), priority support, and managed MCP server hosting. Enterprise tier (custom pricing): SSO/SAML, audit log export, on-prem deployment support, SLA guarantees, and dedicated support. Usage-based add-on: managed execution where Bernstein runs agents in cloud sandboxes (per-minute billing). Document the boundary between open-source and commercial clearly — the CLI and core orchestration must always be free. Create a pricing page design and feature comparison matrix.

## Files to modify

- `.sdd/decisions/monetization.md` (new)
- `docs/pricing-design.md` (new)

## Completion signal

- Monetization strategy document with three tiers defined
- Clear boundary between free and paid features
- Revenue projections for first 12 months at various adoption levels
