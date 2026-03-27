# JetBrains IDE Integration: Deferral Decision & Revisit Checklist

**Status**: Deferred (Q2 2026 tentative)
**Date**: 2026-03-29
**Related Task**: #413b (JetBrains Plugin)
**Dependencies**: #340b (VS Code extension), #380 (A2A protocol support)

---

## Executive Summary

JetBrains IDE integration is deferred in favor of **ACP (Agent Client Protocol)** implementation. Rather than building a custom JetBrains plugin (Kotlin + 40-60 hours), we implement ACP support in Bernstein (Python, 14-22 hours), which:

- ✅ Works with JetBrains, Zed, Neovim, and future editors without rewrites
- ✅ Is an open standard (not vendor-locked)
- ✅ Requires 50% less effort
- ✅ Positions Bernstein as "IDE-agnostic orchestrator"

**Revisit when**: JetBrains Central enters public early access (Q2 2026) and confirms ACP API stability.

---

## Why JetBrains Integration Matters

JetBrains IDEs (IntelliJ IDEA, PyCharm, WebStorm, GoLand, etc.) command ~20% of AI coding market share:
- Enterprise Java/Kotlin teams use IntelliJ (27% of enterprise developers)
- Python teams use PyCharm
- Node.js teams use WebStorm
- Ignoring JetBrains loses access to a significant developer segment

Without IDE integration, Bernstein requires users to:
- Run agents from terminal (`bernstein run`)
- Monitor task server dashboard separately
- Manually switch between editor and terminal

This friction is **not acceptable** once VS Code integration (#340b) launches. The competitive benchmark (Stoneforge, Cursor, Cline) all ship with IDE plugins. The question is not *whether* to support JetBrains, but *how* and *when*.

---

## Three Integration Paths Considered

### Path A: Custom JetBrains Plugin (Deferred)

**Technology**: Kotlin + IntelliJ Platform SDK
**Scope**: Tool window, status bar widget, cost tracking, agent status
**Effort**: 40-60 hours (Kotlin rewrite, IDE-specific APIs, testing, marketplace approval)
**Why we deferred it**:

1. **Narrow reach**: Only JetBrains IDEs. Custom plugin needed for Zed, Neovim, etc.
2. **API volatility**: JetBrains changes IntelliJ Platform APIs regularly. Ongoing maintenance burden.
3. **Distribution friction**: JetBrains Marketplace approval required (review time, version restrictions).
4. **Preview-only constraint**: JetBrains Air (the agent-aware IDE) is macOS-only preview as of March 2026 — no plugin API stabilized yet.
5. **Code sharing**: Zero code reuse with VS Code extension (#340b). Separate Kotlin codebase.
6. **Marketplace rules**: JetBrains Marketplace has stricter approval gates than npm/PyPI.

---

### Path B: ACP (Agent Client Protocol) — Chosen

**Technology**: Python (existing) + JSON-RPC 2.0 over stdio
**Scope**: JSON-RPC message handler, session manager, auth (env vars + terminal)
**Effort**: 14-22 hours (pure Python, no IDE SDKs)
**Why we chose it**:

1. **Broad IDE support**: ACP works with JetBrains Air, Zed, Neovim, and future editors without rewrites
2. **Open standard**: Backed by Zed, JetBrains, Anthropic, Google, OpenAI, Microsoft — not vendor-specific
3. **Simple implementation**: JSON-RPC messages, not IDE-specific APIs
4. **Distribution ease**: Register in public ACP registry (npm/PyPI/binary) — no marketplace approval needed
5. **Zero code sharing**: Implemented in Python; reusable across CLI agents
6. **Future-proofing**: If a new editor adopts ACP, Bernstein works there automatically
7. **Auth built-in**: ACP has standardized auth (env vars, terminal prompt, agent-managed)

**ACP workflow** (from user perspective):
1. User opens JetBrains Air
2. Searches for "Bernstein" in agent marketplace
3. IDE downloads bernstein-agent from npm/PyPI/binary registry
4. IDE spawns agent as subprocess (stdio-based JSON-RPC)
5. Agent polls Bernstein task server via HTTP
6. Results stream back to IDE chat panel

---

### Path C: Hybrid (Minimal)

Implement ACP but also support a lightweight tool window for status-only viewing (no plugin, just HTTP polling from IDE). **Not chosen**: adds complexity without much value beyond ACP alone.

---

## Key Blockers & Unblock Conditions

### Blocker 1: JetBrains Air API Stability

**Status**: macOS-only preview as of March 2026.

**Current constraint**: No stable extension API for custom plugins. JetBrains has not finalized how plugins integrate with agent orchestration.

**Unblock condition**: JetBrains Central (the IDE agent marketplace) opens public early access and publishes stable plugin API (estimated Q2 2026).

**How we monitor**: Watch for announcements at:
- https://www.jetbrains.com/ai/assistant/
- https://blog.jetbrains.com/ai/
- JetBrains Developer Community

---

### Blocker 2: ACP Specification Maturity

**Status**: v0.11.4 (actively maintained, 35+ releases), widely adopted (30+ agents).

**Current position**: ACP is stable enough to build on. No blocker here.

**Revisit if**: ACP undergoes breaking changes (unlikely given vendor backing) or is deprecated in favor of a successor standard (e.g., a Linux Foundation standard displaces ACP). Low probability.

---

## Dependencies

### Task #340b — VS Code Extension (Higher Priority)

VS Code has 73% market share in AI coding. JetBrains can only be justified *after* VS Code is launched. Building JetBrains first would:
- Waste time on low-reach platform
- Delay VS Code launch (the critical path)
- Create maintenance burden before revenue

**Dependency logic**:
```
#340b (VS Code) → #413b (JetBrains) in priority queue
#340b unblocks knowledge reuse:
  - Understand task server HTTP API
  - Document task lifecycle
  - Establish IDE ↔ agent ↔ server patterns
  - (Some patterns transfer to ACP, some are VS Code-specific)
```

---

### Task #380 — A2A Protocol Support

A2A (Agent-to-Agent Protocol, Linux Foundation standard) and ACP are complementary:
- **ACP**: Editor ↔ Agent
- **A2A**: Agent ↔ Agent (orchestration)

Implementing A2A first enables:
- Bernstein agents to discover other agents via A2A
- JetBrains Air to discover Bernstein agents via A2A (if Air implements A2A)
- Cross-editor agent composition

**Dependency logic**:
```
#380 (A2A) → ACP implementation (future)
A2A provides infrastructure for ACP session management.
But ACP can be built independently; A2A is additive.
```

---

## How to Revisit This Decision

### Checklist: When to Reconsider JetBrains Custom Plugin

Use this checklist to decide if the custom plugin path becomes justified:

#### 1. Market Signal (Required)
- [ ] JetBrains IDEs reach >25% of Bernstein's target market (currently ~20%)
- [ ] >100 Bernstein users report "blocking issue: can't use Bernstein from JetBrains"
- [ ] Paying customer explicitly asks for JetBrains plugin

**Owner**: Product (via user surveys, GitHub issues, sales)

#### 2. API Stability (Required)
- [ ] JetBrains Central has public API docs (not "preview" or "internal")
- [ ] IntelliJ Platform SDK v2025+ has no breaking changes for 2+ releases
- [ ] JetBrains Marketplace has published SLA for approval time (<7 days)

**Owner**: Engineering (via research, API review)

#### 3. ACP Viability (Required)
- [ ] ACP implementation (#380) is complete and tested with ≥3 editors (Zed, JetBrains, Neovim)
- [ ] ACP agent registered in public registry with 100+ downloads
- [ ] JetBrains Air officially supports ACP (not just experimental)
- [ ] User feedback shows ACP is "good enough" (no complaints about missing IDE features)

**Owner**: Engineering (via telemetry, user feedback)

#### 4. Resource Availability (Required)
- [ ] VS Code extension (#340b) is in maintenance mode (not active development)
- [ ] No other blockers on critical path (roadmap allows 40-60 hour sprint)
- [ ] Team has Kotlin expertise or willingness to hire/outsource

**Owner**: Engineering + Product (resource planning)

#### 5. Business Case (Required)
- [ ] Either:
  - **Market expansion**: JetBrains adoption is significant enough to justify engineering cost
  - **Revenue**: ≥5 paying customers specifically request JetBrains
  - **Competitive necessity**: Stoneforge/Cursor ship JetBrains and are materially winning market share

**Owner**: Product + Sales

---

## Revisit Process

### Phase 1: Assessment (2 weeks)
1. Check all five checklist items above
2. Document findings in `docs/jetbrains-revisit-2026-q2.md`
3. Decision: Proceed or defer further

### Phase 2: Planning (1 week, if proceeding)
1. Scope: Kotlin plugin, build system, testing
2. Team: Assign 1-2 engineers (ideally with Kotlin experience)
3. Timeline: 6-8 week sprint (40-60 hours ÷ 2 engineers)

### Phase 3: Execution (6-8 weeks, if proceeding)
1. Week 1-2: Scaffold plugin, wire HTTP client
2. Week 3-4: Tool window UI (agent status, task list, cost tracking)
3. Week 5-6: Status bar widget, auth flow
4. Week 7: Testing + marketplace submission
5. Week 8: Polish + monitoring

### Phase 4: Launch (if proceeding)
1. Beta on JetBrains Marketplace
2. Gather feedback from 10-20 beta users
3. Full release (EAP → stable)

---

## Risks if We Defer (and Mitigations)

| Risk | Probability | Mitigation |
|------|-------------|-----------|
| JetBrains launches disruptive agent feature (new IDE) | Low (2026) | Monitor announcements; ACP handles it automatically |
| Competitor ships JetBrains first, wins Java/Kotlin devs | Medium | ACP launch (14-22 hours) reaches JetBrains fast; custom plugin would still be slower |
| JetBrains closes ACP window (doesn't support ACP) | Very low | ACP is JetBrains' own standard; abandoning it would contradict their openness strategy |
| Users demand JetBrains plugin immediately | Low | ACP provides 80% of plugin UX; can escalate if demand spikes |

---

## Risks if We Proceed Early (Before Q2 2026)

| Risk | Probability | Mitigation |
|------|-------------|-----------|
| JetBrains ships breaking API change | Medium | Delay implementation until API is stable (Q2 2026) |
| Plugin sits unmaintained because team is busy | High | Don't start until VS Code is in maintenance mode |
| Marketplace approval is denied/delayed | Medium | Mitigate with clear compliance; study other plugins' patterns |
| Kotlin knowledge ramp is longer than expected | Medium | Hire contractor or outsource if timeline is critical |

---

## Competitive Context

### Current State (March 2026)

| Agent | IDE Support | Approach |
|-------|-------------|----------|
| **Cursor** | VS Code + macOS native | Custom plugins |
| **Cline** | VS Code | Extension (open source) |
| **Stoneforge** | VS Code + JetBrains plugin | Custom plugins |
| **Bernstein** | Terminal (MVP), VS Code (#340b planned) | Extension (open source) + ACP (future) |

**Gap**: Stoneforge has JetBrains. Bernstein doesn't (yet). **But**: Stoneforge uses custom plugins for both; we use ACP (lower maintenance, broader reach).

### Q2 2026 Projected State (if ACP launches)

| Agent | IDE Support |
|-------|-------------|
| **Bernstein** | VS Code + JetBrains + Zed + Neovim (via ACP) |
| **Stoneforge** | VS Code + JetBrains (custom plugins only) |

**Advantage**: Bernstein reaches more editors with less code.

---

## Summary

**Decision**: Defer custom JetBrains plugin. Invest in ACP (Python, 14-22 hours) instead.

**Timeline**:
1. **Now (Q1 2026)**: Finish #340b (VS Code extension)
2. **Q1-Q2 2026**: Implement ACP support, register in public registry
3. **Q2 2026**: Monitor JetBrains Central API stability
4. **Q2 2026+**: Revisit using the five-item checklist above

**Success metric**: By Q3 2026, Bernstein is discoverable in 4+ IDE agent marketplaces (VS Code, JetBrains Air, Zed, others) without maintaining separate plugins for each.

---

## Appendix: ACP Implementation Scope (For Reference)

### Phase 1: Protocol Handler (6-8 hours)
- `src/bernstein/acp/handler.py` — JSON-RPC message parser
- `src/bernstein/acp/session.py` — ACP session → Bernstein task mapping
- `src/bernstein/acp/auth.py` — Environment variable + terminal auth
- Message types: initialize, new, prompt, update, cancel

### Phase 2: CLI Agent (4-6 hours)
- `src/bernstein/cli/acp_agent.py` — Entry point
- Stdio transport (JSON-RPC over stdin/stdout)
- Task polling + streaming output
- Artifact handling

### Phase 3: Testing & Registry (2-3 hours)
- Unit + integration tests
- Registry submission (agent.json + SVG icon)
- Documentation

**Total**: 14-22 hours (vs. 40-60 hours for custom plugin).
