# E39 — JetBrains Plugin Foundation

**Priority:** P2
**Scope:** small (10-20 min)
**Wave:** 2 — Ecosystem & Integrations

## Problem
JetBrains IDE users (IntelliJ, PyCharm, WebStorm, etc.) have no plugin for viewing Bernstein run status within their IDE.

## Solution
- Create a JetBrains plugin skeleton at `editor-plugins/jetbrains/` as a Kotlin/Gradle project.
- Implement a tool window (side panel) that displays current Bernstein run status: task list, state, elapsed time.
- Read-only for the initial version (no run triggering).
- Use the IntelliJ Platform SDK tool window API.
- Include `plugin.xml` descriptor, Gradle build config, and project structure ready for JetBrains Marketplace publishing.

## Acceptance
- [ ] Plugin builds with Gradle without errors
- [ ] Tool window appears in the IDE showing Bernstein status
- [ ] Status updates when a bernstein run is active
- [ ] Plugin is structured for future JetBrains Marketplace publishing
