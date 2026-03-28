# 631 — Sustainability Model Design

**Role:** architect
**Priority:** 3 (medium)
**Scope:** small
**Depends on:** #600

## Problem

Long-term project sustainability requires a clear model for funding development. The open-core model used by CrewAI, LangChain, and similar projects balances open-source accessibility with funded development of enterprise features.

## Design

Design an open-core model. Free tier: full CLI orchestration, local execution, all adapters, community support. Pro tier: hosted dashboard with team sharing, advanced analytics (cost trends, model performance comparisons), priority support, and managed MCP server hosting. Enterprise tier: SSO/SAML, audit log export, on-prem deployment support, SLA guarantees. Document the boundary between open-source and commercial clearly — the CLI and core orchestration must always be free.

## Files to modify

- `.sdd/decisions/sustainability.md` (new)

## Completion signal

- Sustainability strategy document with tier definitions
- Clear boundary between free and paid features
