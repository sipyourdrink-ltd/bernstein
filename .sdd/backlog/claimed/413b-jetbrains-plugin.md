# 413b — JetBrains Plugin (Future — After VS Code)

**Role:** frontend
**Priority:** 4 (low)
**Scope:** large
**Depends on:** #340b, #380

## Problem

JetBrains IDEs (IntelliJ, PyCharm, WebStorm) have ~20% market share. JetBrains Air (March 2026) adds native multi-agent support with ACP protocol. A Bernstein plugin would reach enterprise Java/Kotlin developers.

## Why defer

1. VS Code/Cursor dominate AI coding (73% share) — build there first
2. JetBrains Air is macOS-only preview — no extension API yet
3. Plugin requires Kotlin rewrite (zero code sharing with VS Code)
4. ACP (Agent Client Protocol) is the better integration path — implement ACP in Bernstein and Air/JetBrains IDEs can use it natively without a custom plugin
5. Revisit when JetBrains Central opens early access (Q2 2026)

## Design (when ready)

### Option A: ACP integration (preferred)
Implement ACP protocol in Bernstein. JetBrains Air discovers ACP-compatible agents automatically. No plugin needed — just protocol compliance.

### Option B: JetBrains plugin
- Kotlin + IntelliJ Platform SDK
- Gradle build with IntelliJ Platform Gradle Plugin 2.x
- Tool window for dashboard (Swing/JB UI framework, not HTML)
- Status bar widget for cost tracking
- Same HTTP+SSE connection to localhost:8052

## Completion signal

- Bernstein accessible from JetBrains IDE (via ACP or plugin)
- Agent status visible in tool window
- Cost tracking in status bar
