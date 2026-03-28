# 340c — Extension Publish Pipeline + UX Polish

**Role:** frontend
**Priority:** 0 (urgent)
**Scope:** medium
**Depends on:** #340b

## Problem

Building the extension is only half the work. It must be published to BOTH VS Code Marketplace AND Open VSX (for Cursor), verified on Cursor's forum, and the UX must be premium — not "works but looks like a student project."

## Publishing Checklist

### 1. Accounts & credentials
- [ ] Azure DevOps org at dev.azure.com (for VS Code Marketplace PAT)
- [ ] Personal Access Token with scope "Marketplace > Manage", org "All accessible"
- [ ] Publisher created at marketplace.visualstudio.com/manage (ID: `bernstein-dev` or `chernistry`)
- [ ] Open VSX account at open-vsx.org (Eclipse identity)
- [ ] Open VSX namespace created matching publisher ID
- [ ] Open VSX access token generated from settings page
- [ ] Both tokens stored as GitHub secrets: `VS_MARKETPLACE_TOKEN`, `OPEN_VSX_TOKEN`

### 2. package.json required fields
```json
{
  "name": "bernstein",
  "displayName": "Bernstein — Multi-Agent Orchestration",
  "description": "Orchestrate parallel AI coding agents from your editor. Monitor tasks, agents, costs in real-time.",
  "version": "0.1.0",
  "publisher": "chernistry",
  "license": "Apache-2.0",
  "icon": "media/bernstein-icon.png",
  "engines": { "vscode": "^1.100.0" },
  "categories": ["AI", "Other"],
  "keywords": ["ai", "agents", "orchestration", "multi-agent", "claude", "codex", "gemini"],
  "repository": { "type": "git", "url": "https://github.com/chernistry/bernstein" },
  "homepage": "https://chernistry.github.io/bernstein/"
}
```

### 3. Required assets
- [ ] `media/bernstein-icon.png` — 128x128 and 256x256 (marketplace requires both)
- [ ] `README.md` in extension root (shown on marketplace page)
- [ ] `CHANGELOG.md` (shown on marketplace)
- [ ] Screenshots (at least 3): sidebar, TUI in action, status bar
- [ ] Demo GIF for marketplace hero image

### 4. CI/CD workflow
```yaml
# .github/workflows/publish-extension.yml
name: Publish Extension
on:
  push:
    tags: ["ext-v*"]
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: 20 }
      - run: cd packages/vscode && npm ci && npm run build
      - uses: HaaLeo/publish-vscode-extension@v1
        with:
          pat: ${{ secrets.VS_MARKETPLACE_TOKEN }}
          registryUrl: https://marketplace.visualstudio.com
      - uses: HaaLeo/publish-vscode-extension@v1
        with:
          pat: ${{ secrets.OPEN_VSX_TOKEN }}
          registryUrl: https://open-vsx.org
```

### 5. Cursor verification
- [ ] Host extension page at alexchernysh.com/bernstein/extension
- [ ] Link to Open VSX listing
- [ ] Ensure publisher ID matches across both marketplaces
- [ ] Post verification request on forum.cursor.com
- [ ] Include: publisher ID, extension ID, domain proof

### 6. Fallback distribution
- [ ] Release `.vsix` as GitHub Release asset on each tag
- [ ] README instructions: "Install from VSIX" for users who can't find it in marketplace

## UX Polish Requirements (must-have before v0.1.0)

### Visual design — "Decision-Grade Quiet Command"
Apply the same elite UI principles as the docs site:

**Sidebar:**
- Clean monochrome icons, no rainbow status indicators
- Restrained accent color (indigo, not saturated blue)
- Compact text: 13px, tight leading, tabular figures for numbers
- Status dots (●/○) not emoji or colored text
- Dark theme by default, respects VS Code theme

**Tree views (agents + tasks):**
```
▼ Agents (3)
  ● backend-abc   sonnet   $0.12   2m
  ● qa-def        sonnet   $0.08   1m
  ○ docs-ghi      flash    idle
▼ Tasks (7/12)
  ✓ Add JWT middleware
  ● Write auth tests  →  qa-def
  ○ Generate API docs
```

**Status bar:**
```
🎼 3 agents  ·  7/12 tasks  ·  $0.42
```
Not `$(zap) Bernstein: 3 agents running | Tasks: 7/12 done | Cost: $0.42` (too verbose).

**Dashboard webview:**
- Card-based layout (not table-first)
- 4 stat cards at top: agents, tasks done, success rate, cost
- Agent cards with progress bars
- Cost burn chart (sparkline, not full chart)
- Zero chrome: no borders, no shadows, just hierarchy via spacing
- Skeleton loading states (not "Loading...")

### Interaction patterns
- Click agent → output channel opens
- Click task → file diff opens
- Right-click agent → Kill / Inspect / Show Logs
- Right-click task → Prioritize / Cancel / Re-assign
- `Cmd+Shift+P` → "Bernstein: Start" works immediately
- Extension auto-connects when server detected on :8052
- Graceful "Server not running" state (not error, just info)

### Performance
- SSE connection, not polling
- Debounced UI updates (max 2/second)
- Lazy load webview (don't block VS Code startup)
- Extension size < 1MB packaged

## Completion signal

- Extension published on BOTH VS Code Marketplace and Open VSX
- Installable in Cursor via marketplace search
- Verified publisher on Cursor forum
- 3+ screenshots on marketplace page
- CI/CD auto-publishes on `ext-v*` tag
- All UX polish items checked off
- Extension size < 1MB
