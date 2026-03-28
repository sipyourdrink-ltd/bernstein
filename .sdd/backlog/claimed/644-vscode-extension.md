# 644 — VS Code Extension

**Role:** frontend
**Priority:** 5 (low)
**Scope:** large
**Depends on:** #615

## Problem

Bernstein has no IDE integration. Developers spend most of their time in VS Code, and switching to a terminal for orchestration monitoring is friction. JetBrains Central validates that IDE-integrated agent monitoring is a valued capability.

## Design

Create a VS Code extension for Bernstein orchestration monitoring. The extension provides: a sidebar panel showing active agents with status indicators, a task list view with drag-and-drop priority ordering, a cost summary widget in the status bar, and an output channel per agent for log viewing. The extension connects to the Bernstein task server API (and WebSocket for real-time updates). Commands: start orchestration run, stop run, view agent output, open cost dashboard. Use the VS Code Webview API for rich visualizations (task timeline, cost chart). Build with TypeScript, package as a .vsix. Support both local and remote task server connections for team use. Publish to VS Code Marketplace.

## Files to modify

- `vscode-extension/package.json` (new)
- `vscode-extension/src/extension.ts` (new)
- `vscode-extension/src/panels/` (new — Webview panels)
- `vscode-extension/src/providers/` (new — tree data providers)
- `vscode-extension/README.md` (new)

## Completion signal

- VS Code extension installs and connects to Bernstein task server
- Sidebar shows active agents with real-time status
- Status bar shows current run cost
