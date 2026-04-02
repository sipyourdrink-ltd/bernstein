# Decision: Switch to Apache 2.0 License

**Date:** 2026-03-28
**Status:** Accepted
**Ticket:** #600

## Context

Bernstein used PolyForm Noncommercial 1.0.0, which blocks enterprise adoption and discourages contributions. Every competitor in the multi-agent orchestration space uses permissive licenses: CrewAI (MIT), LangGraph (MIT), AutoGen (MIT).

## Options Considered

| License | Enterprise adoption | Patent protection | Contribution friction | Copyleft risk |
|---------|-------------------|-------------------|----------------------|---------------|
| MIT | High | None | Low | None |
| Apache 2.0 | High | Explicit grant | Low | None |
| AGPL v3 | Low (enterprise avoids) | Implicit | Medium (CLA needed) | High |

## Decision

**Apache 2.0.** Rationale:

1. **Patent grant** — explicit patent license protects both users and contributors. Important for a tool orchestrating AI agents where patent claims are increasingly common.
2. **Enterprise-friendly** — no copyleft concerns, widely approved by corporate legal teams.
3. **Contribution-friendly** — no CLA required, standard inbound=outbound model.
4. **Competitive parity** — on par with MIT competitors, slightly stronger contributor protections.

MIT was a close second but lacks explicit patent protection. AGPL was rejected — enterprise adoption is a non-starter with copyleft.

## Migration

1. Replace `LICENSE` with Apache 2.0 full text
2. Update `pyproject.toml` license field to `Apache-2.0`
3. No code changes required — no license headers in source files
