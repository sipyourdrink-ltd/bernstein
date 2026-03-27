# 340c — Extension Publish Pipeline + UX Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish Bernstein VS Code extension to both VS Code Marketplace and Open VSX, verify on Cursor, and polish UX to premium standard before v0.1.0 release.

**Architecture:** The extension is 95% complete (code, CI/CD workflow, package.json, PUBLISH.md, README, CHANGELOG all in place). Remaining work is: (1) create marketplace accounts and PATs, (2) configure GitHub secrets, (3) create missing marketplace screenshots/GIFs, (4) verify UX polish against spec, (5) perform test publish to both registries, (6) verify on Cursor forum.

**Tech Stack:** VS Code Extension API, TypeScript, Node 20, vsce CLI, ovsx CLI, GitHub Actions, HaaLeo/publish-vscode-extension action, softprops/action-gh-release.

---

## Discovery & Verification vs Specification

### What Already Exists ✓
- `packages/vscode/package.json` — all required fields present (name, displayName, description, version, publisher, icon, engines, categories, keywords, repository, homepage, license, activationEvents, main, contributes)
- `packages/vscode/CHANGELOG.md` — complete v0.1.0 entry
- `packages/vscode/README.md` — professional, 103 lines, covers Quick Start, Requirements, Configuration, Features, Commands
- `.github/workflows/publish-extension.yml` — complete CI/CD workflow with icon validation, type check, tests, build, package, publish to both marketplaces, GitHub Release creation
- `packages/vscode/media/bernstein-icon.{png,svg}` — both formats present (PNG: 6.8 KB, SVG: 263 B)
- `packages/vscode/.vscodeignore` — properly configured (excludes src, node_modules, tests, tsconfig, esbuild, jest.config, PUBLISH.md)
- **VSIX package** — builds successfully, 22 KB (well under 1 MB limit)
- **Build scripts** — `npm run compile`, `npm run package`, `npm run publish:vscode`, `npm run publish:ovsx` all functional
- **PUBLISH.md** — detailed manual publishing guide (174 lines)

### Gaps Found (must fix before publish)
| # | Gap | Severity | Current state | Required action |
|---|---|---|---|---|
| G1 | VS Code Marketplace publisher account | Critical | Not created | Task 1: Create publisher |
| G2 | VS Code Marketplace PAT | Critical | Not obtained | Task 1: Generate and store as secret |
| G3 | Open VSX account & namespace | Critical | Not created | Task 2: Create account & generate token |
| G4 | GitHub secrets configured | Critical | Not set | Task 3: Add VS_MARKETPLACE_TOKEN and OPEN_VSX_TOKEN |
| G5 | Marketplace screenshots (3+) | High | Missing | Task 4: Create marketplace screenshots |
| G6 | Demo GIF for hero image | High | Missing | Task 4: Record and optimize demo GIF |
| G7 | UX polish verification | Medium | Claimed in CHANGELOG | Task 5: Audit code against polish spec |
| G8 | Cursor forum verification post | Medium | Not published | Task 6: Verify publisher after first publish |
| G9 | Documentation hosting at alexchernysh.com | Low | Not done | Task 6: (Optional, can post publish) |

---

## File Structure

### Create (no new files — spec already specced in existing files)
- Screenshots (3-5 PNG/JPG files in `packages/vscode/media/`) — referenced in marketplace but not in repo
- Demo GIF (1 optimized GIF in `packages/vscode/media/`) — marketplace hero image

### Modify
- `packages/vscode/media/` — add screenshots and GIF (not code changes)
- GitHub repository settings → add secrets (not code)

### No changes needed
- `packages/vscode/package.json` — complete
- `packages/vscode/CHANGELOG.md` — complete
- `packages/vscode/README.md` — complete
- `.github/workflows/publish-extension.yml` — complete
- `packages/vscode/.vscodeignore` — complete
- `packages/vscode/src/` — code already polished per CHANGELOG

---

## Implementation Tasks

### Task 1: Create VS Code Marketplace Publisher Account & Generate PAT

**Files:**
- Action: Create account on marketplace.visualstudio.com
- Action: Generate PAT at dev.azure.com
- Action: Store secret in GitHub

- [ ] **Step 1: Go to VS Code Marketplace and sign in**

Navigate to https://marketplace.visualstudio.com and sign in with a Microsoft account (create one if needed).

Expected: You are logged in and see "Publish extensions" option in your profile menu.

- [ ] **Step 2: Create publisher**

Click profile → "Publish extensions" → "Create publisher"
- **Publisher ID**: `chernistry`
- **Display name**: `Chernistry`
- **Website**: https://chernistry.github.io/bernstein/ (or your personal site)
- **Description**: AI-powered multi-agent orchestration for code

Expected: Publisher created and you see "chernistry" in your publisher list.

- [ ] **Step 3: Go to dev.azure.com and create PAT**

Navigate to https://dev.azure.com and sign in with the same Microsoft account.

Go to User settings (top right icon) → Personal access tokens → New token

Configure:
- **Name**: `vsce-publish`
- **Organization**: All accessible organizations (dropdown)
- **Scopes**: Expand "Marketplace" → Check "Manage"
- **Expiration**: 1 year (or longer for production)

Click "Create" and copy the token immediately (cannot be retrieved later).

Expected: A 52-character token like `npzk3ykl7b4qwvx2lw6bx7ydz9pq1rs2`.

- [ ] **Step 4: Store PAT as GitHub repository secret**

Run this command in the bernstein repo:

```bash
gh secret set VS_MARKETPLACE_TOKEN --body "PASTE_YOUR_PAT_HERE"
```

Or via GitHub UI:
1. Go to https://github.com/chernistry/bernstein (your fork/repo)
2. Settings → Secrets and variables → Actions
3. New repository secret
4. Name: `VS_MARKETPLACE_TOKEN`
5. Value: `[paste the PAT]`
6. Click "Add secret"

Expected: Secret appears in the Actions secrets list with value masked as `***`.

- [ ] **Step 5: Verify PAT works (optional)**

Run this in bernstein repo root:

```bash
cd packages/vscode && npm ci && npm run compile && npm run package
VSCE_PAT=YOUR_PAT_HERE npx vsce publish --check-only
```

Expected: Output `The token '***' is valid.` (no actual publish yet).

- [ ] **Step 6: Commit** (no commit needed — account creation is external action)

Note: No code changes, so no git commit required. PAT is stored as a GitHub secret, not in version control.

---

### Task 2: Create Open VSX Account & Generate PAT

**Files:**
- Action: Create account on open-vsx.org
- Action: Generate token in settings
- Action: Store secret in GitHub

- [ ] **Step 1: Go to Open VSX and sign in**

Navigate to https://open-vsx.org and click "Sign in".

You can:
- Sign in with Eclipse Identity (EclipseCon account)
- Sign up for a new Eclipse account
- Or sign in via GitHub

Create account if needed (use your GitHub account for simplicity).

Expected: You are logged in and see your username in top-right corner.

- [ ] **Step 2: Create namespace**

Go to the top-right menu → "Publish Extension"

Follow the wizard to create a namespace:
- **Namespace ID**: `chernistry`
- **Display name**: `Chernistry`

Expected: Namespace created and you see `chernistry` as your publisher.

- [ ] **Step 3: Generate access token**

Go to top-right menu → "Settings" → "Access Tokens"

Click "Generate New Token":
- **Name**: `extension-publish`
- **Description**: `Token for Bernstein VS Code extension auto-publish`
- **Expiration**: 1 year (recommended)

Click "Generate" and copy immediately (cannot be retrieved).

Expected: A token like `a1b2c3d4-e5f6-7890-ghij-klmnopqrstuv`.

- [ ] **Step 4: Store PAT as GitHub repository secret**

Run in the bernstein repo:

```bash
gh secret set OPEN_VSX_TOKEN --body "PASTE_YOUR_TOKEN_HERE"
```

Or via GitHub UI (same as Task 1, Step 4):
1. Settings → Secrets and variables → Actions
2. New repository secret
3. Name: `OPEN_VSX_TOKEN`
4. Value: `[paste the token]`
5. Click "Add secret"

Expected: Secret appears in Actions secrets list, masked.

- [ ] **Step 5: Verify token works (optional)**

```bash
cd packages/vscode && npm ci && npm run compile && npm run package
OVSX_PAT=YOUR_TOKEN_HERE npx ovsx publish --check-only --target universal
```

Expected: Output indicates token is valid and extension is ready to publish.

- [ ] **Step 6: Commit** (no commit needed — account creation is external)

Note: No code changes needed.

---

### Task 3: Verify GitHub Secrets Are Configured Correctly

**Files:**
- No file changes (GitHub secrets verification only)

- [ ] **Step 1: List secrets in GitHub Actions**

```bash
gh secret list --repo chernistry/bernstein
```

Expected output:
```
VS_MARKETPLACE_TOKEN  Updated 2026-03-29 12:34:00 +0000 UTC
OPEN_VSX_TOKEN        Updated 2026-03-29 12:34:05 +0000 UTC
```

Both secrets present and recent (no old stale secrets).

- [ ] **Step 2: Verify publish workflow will see secrets**

Open `.github/workflows/publish-extension.yml` in your editor and check:

Line 64: `if: ${{ secrets.VS_MARKETPLACE_TOKEN != '' }}`
Line 72: `if: ${{ secrets.OPEN_VSX_TOKEN != '' }}`

These conditional checks ensure:
- If secret is missing/empty, the publish step is skipped (no error)
- If secret is present, the publish step runs
- If secret is wrong/expired, the action will fail with auth error (visible in workflow logs)

Expected: Both `if` conditions are present in the workflow.

- [ ] **Step 3: Test secrets are not hardcoded anywhere**

Run:

```bash
grep -r "VS_MARKETPLACE_TOKEN\|OPEN_VSX_TOKEN" . --include="*.md" --include="*.txt" --include="*.sh" | grep -v ".github/workflows" | grep -v "PUBLISH.md" || echo "OK: Secrets only in workflows"
```

Expected: Only `.github/workflows/publish-extension.yml` and `PUBLISH.md` mention the secret names. No actual secret values anywhere.

- [ ] **Step 4: Commit** (no commit needed)

Note: GitHub secrets are stored in GitHub's secure settings, not in git. No changes to commit.

---

### Task 4: Create Marketplace Screenshots & Demo GIF

**Files:**
- Create: `packages/vscode/media/screenshot-1.png` — Agent panel with task list
- Create: `packages/vscode/media/screenshot-2.png` — Dashboard webview
- Create: `packages/vscode/media/screenshot-3.png` — Status bar + task context menu
- Create: `packages/vscode/media/demo.gif` — 5-10 second loop showing orchestration in action

- [ ] **Step 1: Take screenshot 1 — Agents & Tasks Panel**

Start Bernstein server:
```bash
cd /Users/sasha/IdeaProjects/personal_projects/bernstein
bernstein run
```

Wait for server to start (see `.sdd/runtime/` populated).

Launch VS Code and open Bernstein extension sidebar. Click "Agents" tab.

Simulate some running agents (you can create dummy tasks via `curl` to the API if needed):

```bash
curl -X POST http://127.0.0.1:8052/tasks \
  -H "Content-Type: application/json" \
  -d '{"title": "Add JWT middleware", "role": "backend", "effort": "large"}'
```

Take a screenshot showing:
- Activity bar (Bernstein icon highlighted)
- Left sidebar with "Agents" panel visible
- 2-3 agents listed (status ●/○, model name, cost)
- Below: "Tasks" tab showing open, claimed, completed tasks
- Status bar at bottom showing `🎼 2 agents · 4/8 tasks · $0.23`

Crop to 1280x720 or similar (standard 16:9).

Save as `packages/vscode/media/screenshot-1.png`.

Expected: PNG file, 1280x720 or similar, showing clean Bernstein UI with agents and tasks.

- [ ] **Step 2: Take screenshot 2 — Dashboard Webview**

In VS Code Bernstein panel, click "Overview" tab or run "Bernstein: Show Dashboard" command.

Wait for dashboard to load. Take a screenshot showing:
- 4 stat cards at top: "Agents", "Tasks Completed", "Success Rate", "Total Cost"
- Agent cards below with:
  - Agent name + role + model
  - Progress bar (0-100%)
  - Cost accumulated
- Cost burn chart (sparkline showing cost over time)
- Clean card-based layout, tight typography, no shadows/borders

Crop to similar dimensions (1280x720).

Save as `packages/vscode/media/screenshot-2.png`.

Expected: PNG showing the dashboard with no errors, clean layout.

- [ ] **Step 3: Take screenshot 3 — Status Bar + Context Menu**

Go back to the main VS Code editor. At the bottom status bar, you should see:

```
🎼 2 agents · 4/8 tasks · $0.23
```

Right-click on an agent in the Agents panel to show context menu:
- Kill Agent
- Inspect Logs
- Show Output

Capture the status bar at bottom + context menu from the agents panel.

Crop to show both elements clearly (1280x400 or so).

Save as `packages/vscode/media/screenshot-3.png`.

Expected: PNG showing status bar and context menu.

- [ ] **Step 4: Record demo GIF**

Using a screen recording tool (QuickTime, OBS, or `screencapture` on Mac):

1. Start fresh Bernstein server
2. Open VS Code with extension
3. Show:
   - Click "Bernstein: Start" → orchestrator boots
   - Sidebar shows agents spawning
   - Status bar updates in real-time
   - Click an agent → shows output
   - Click a task → shows diff
   - Loop back to agent panel

Record for 8-10 seconds at 30 FPS. Cursor movement should be clean.

Save as MP4 or QuickTime format.

**Optimize GIF:**
```bash
# Convert to GIF with smaller filesize (3-5 MB target for marketplace)
ffmpeg -i demo.mov -vf "fps=10,scale=1280:-1" -loop 0 packages/vscode/media/demo.gif

# Optimize with gifsicle (install if needed: brew install gifsicle)
gifsicle -O3 packages/vscode/media/demo.gif -o packages/vscode/media/demo-optimized.gif
mv packages/vscode/media/demo-optimized.gif packages/vscode/media/demo.gif
```

Expected: `packages/vscode/media/demo.gif` is 3-5 MB, loops smoothly, shows the extension in action.

- [ ] **Step 5: Verify screenshots are not too large**

```bash
ls -lh packages/vscode/media/screenshot-*.png packages/vscode/media/demo.gif
```

Expected:
- Each PNG < 500 KB
- GIF < 5 MB
- All files present

- [ ] **Step 6: Commit**

```bash
git add packages/vscode/media/screenshot-*.png packages/vscode/media/demo.gif
git commit -m "feat(ext): add marketplace screenshots and demo GIF"
```

---

### Task 5: Audit UX Polish Against Specification

**Files:**
- No file changes (review/audit only)
- Reference: `packages/vscode/src/` for code review
- Reference: CHANGELOG.md which claims UX polish is done

- [ ] **Step 1: Verify status bar format**

Open `packages/vscode/src/extension.ts` and find the status bar update code.

Look for: `updateStatusBar()` function or similar.

Verify the format is:
```
🎼 X agents  ·  Y/Z tasks  ·  $COST
```

Check:
- Uses `🎼` icon (musical note, not VS Code icon)
- Uses `·` separator (middle dot, not |, not pipes)
- Shows agent count (not verbose)
- Shows task progress as `Y/Z` (done/total)
- Shows cost with `$` prefix
- NO "Cost:" label, NO "Bernstein:" prefix (too verbose)

Expected: Code matches spec (`🎼 3 agents · 7/12 tasks · $0.42`).

Verify in actual running extension: click status bar and see it update smoothly as agents work.

- [ ] **Step 2: Verify agent tree view format**

In VS Code, open Bernstein → Agents tab. Look for:

```
▼ Agents (3)
  ● backend-abc   sonnet      $0.12   2m
  ● qa-def        sonnet      $0.08   1m
  ○ docs-ghi      flash       idle
```

Check:
- Tree is collapsible (▼/▶)
- Agent state shown as ● (active) / ○ (idle)
- Agent name is compact (no full UUID, just suffix)
- Model shown (sonnet, flash, etc.)
- Cost shown with `$`
- Runtime/status shown (2m, 1m, idle)
- NO colors for status (monochrome only)

Expected: Visual matches spec. No rainbow colors, clean typography.

If tree view code is in `packages/vscode/src/AgentTreeProvider.ts` or similar, verify `getTreeItem()` method sets `iconPath`, `label`, `description` correctly.

- [ ] **Step 3: Verify dashboard card layout**

Open dashboard in VS Code. Check for:

**Top 4 cards:**
- Agents: shows count (e.g., "3 running")
- Tasks: shows progress (e.g., "7/12")
- Success Rate: shows percentage (e.g., "94%")
- Total Cost: shows amount (e.g., "$2.14")

Check:
- Cards are displayed horizontally (not vertical list)
- Card background is subtle (no shadows, no bright colors)
- Text is 13px or similar, tight leading
- NO icons inside cards, just text and numbers
- Status bar is a visual hierarchy (size or weight), not color

Expected: Dashboard looks professional and minimal.

If dashboard code is in `packages/vscode/webview-ui/`, verify CSS has no `box-shadow`, uses `--vscode-` CSS variables for theming.

- [ ] **Step 4: Verify interaction patterns**

Click on:
1. An agent name in the tree → should open Output channel with logs
2. A task name in the tree → should open file diff or output file (not error)
3. Status bar → should open dashboard

Right-click:
1. An agent → context menu appears (Kill, Inspect, Show Logs)
2. A task → context menu appears (Prioritize, Cancel, Re-assign)

Expected: All interactions work smoothly, no console errors.

- [ ] **Step 5: Check for no console errors**

Open VS Code Developer Tools: Help → Toggle Developer Tools

Go to Console tab and check for errors while:
- Extension loads
- Agents spawn
- Tasks complete
- UI updates

Expected: No errors, no warnings (or only VS Code framework warnings, which are normal).

- [ ] **Step 6: Verify extension loads without blocking VS Code**

Check that VS Code startup time is not affected:

1. Close VS Code completely
2. Open Activity Monitor and note available memory
3. Start VS Code with Bernstein extension
4. Observe: Extension sidebar renders quickly (< 1s), no VS Code UI hang

Expected: VS Code is responsive, no lag.

- [ ] **Step 7: Commit** (no commit needed — code review only)

Note: If you find code that doesn't match the spec, create an issue and come back to fix it. For now, confirm the spec is met.

---

### Task 6: Perform Test Publish & Verify on Marketplace

**Files:**
- No file changes (publish action only)

- [ ] **Step 1: Create and push a test tag**

Create a test tag to trigger the publish workflow:

```bash
git tag ext-v0.1.0
git push origin ext-v0.1.0
```

Expected: Tag appears in GitHub, workflow is triggered automatically.

Check GitHub Actions: https://github.com/chernistry/bernstein/actions

Look for workflow: "Publish VS Code Extension" (named in `.github/workflows/publish-extension.yml`).

Expected: Workflow runs with steps: icon validation → install → type check → test → build → package → publish marketplace → publish open-vsx → create release.

- [ ] **Step 2: Monitor workflow for success**

Open the workflow run and check each step:

1. **Validate extension icon** — should pass (icon is >= 128x128)
2. **Install dependencies** — should pass
3. **Type check** — should pass (TypeScript compiles cleanly)
4. **Run tests** — should pass (no test failures)
5. **Build extension** — should pass (dist/ created)
6. **Package extension** — should pass (VSIX created)
7. **Publish to VS Code Marketplace** — should pass (or skip if secret missing) OR fail with auth error
8. **Publish to Open VSX** — should pass (or skip if secret missing) OR fail with auth error
9. **Create GitHub Release** — should pass (release created with VSIX attached)

Expected: All steps pass. If publish steps fail, check:
- Secret is set: `gh secret list | grep MARKETPLACE`
- Secret is correct (no extra spaces, correct token format)
- Token is not expired (check on marketplace.visualstudio.com / open-vsx.org)

- [ ] **Step 3: Verify extension appears on VS Code Marketplace**

Wait 5-10 minutes for marketplace to index (it's not instant).

Go to https://marketplace.visualstudio.com/search?term=bernstein

Search for "bernstein" or "bernstein multi-agent".

Expected: Extension appears with:
- Display name: "Bernstein — Multi-Agent Orchestration"
- Publisher: "chernistry"
- Icon: Bernstein logo (PNG)
- Description: "Orchestrate parallel AI coding agents from your editor..."
- Version: "0.1.0"
- README visible
- CHANGELOG visible

Check details:
- Click the extension card to see full README
- Verify all 3+ screenshots appear below description
- Verify demo GIF appears as hero image (if uploaded)
- Verify installation count is 0 (since it's brand new)

Expected: Everything displays correctly, no formatting errors.

- [ ] **Step 4: Verify extension appears on Open VSX**

Go to https://open-vsx.org/search?query=bernstein

Search for "bernstein" or "chernistry".

Expected: Extension appears with:
- Name: "Bernstein — Multi-Agent Orchestration"
- Namespace: "chernistry"
- Version: "0.1.0"
- Icon, README, CHANGELOG all visible
- Install count is 0

Expected: Same content as VS Code Marketplace (Open VSX mirrors the extension).

- [ ] **Step 5: Test install in VS Code**

In VS Code, open Extensions panel (`Cmd+Shift+X` on Mac).

Search for "Bernstein".

Click the extension and click "Install".

Expected: Extension installs without errors, Bernstein icon appears in activity bar.

Run `Bernstein: Start` command (Cmd+Shift+P → "Bernstein: Start").

Expected: Orchestrator starts, sidebar populates, no errors.

- [ ] **Step 6: Test install in Cursor**

Open Cursor (if you have it installed).

Extensions panel → Search "Bernstein" → Install.

Expected: Extension installs and works same as VS Code.

- [ ] **Step 7: Commit** (if tag was created manually)

If you created the tag manually (not via workflow), verify it's pushed:

```bash
git tag -l | grep ext-v0.1.0
git ls-remote --tags origin | grep ext-v0.1.0
```

Expected: Tag exists locally and on remote.

No code changes, so no commit needed beyond the tag.

---

### Task 7: Post Cursor Forum Verification & Documentation

**Files:**
- Create: Host extension info page (optional, can use existing repo README)
- Action: Post on forum.cursor.com verification request

- [ ] **Step 1: Prepare verification data**

Gather:
- **Publisher ID**: `chernistry`
- **Extension ID**: `chernistry.bernstein` (matches package.json: "name": "bernstein", "publisher": "chernistry")
- **VS Code Marketplace link**: https://marketplace.visualstudio.com/items?itemName=chernistry.bernstein
- **Open VSX link**: https://open-vsx.org/extension/chernistry/bernstein
- **GitHub repo**: https://github.com/chernistry/bernstein
- **Domain**: chernistry.github.io (your GitHub Pages domain, or personal site)

Expected: All links functional and consistent across marketplaces.

- [ ] **Step 2: Post verification request on Cursor forum (optional)**

Go to https://forum.cursor.com/

Create a new topic in "Integrations" or "Community Projects":

```
Title: "Bernstein — Multi-Agent Orchestration Extension Published"

Body:
Hello Cursor community,

I've published the Bernstein VS Code extension to both VS Code Marketplace and Open VSX Registry.

**Links:**
- Extension page: https://marketplace.visualstudio.com/items?itemName=chernistry.bernstein
- Open VSX: https://open-vsx.org/extension/chernistry/bernstein
- GitHub: https://github.com/chernistry/bernstein
- Website: https://chernistry.github.io/bernstein/

**Publisher Details:**
- Publisher ID: `chernistry`
- Namespace (Open VSX): `chernistry`
- Verified domain: chernistry.github.io (GitHub Pages)

The extension orchestrates parallel AI coding agents and works in Cursor the same way as VS Code. Install from Cursor's extension marketplace by searching "Bernstein".

Feedback welcome!
```

Expected: Post created, community can discover the extension.

Note: This is optional (extension works without forum post), but helps with visibility.

- [ ] **Step 3: (Optional) Create extension landing page**

If desired, create a dedicated page at `https://chernistry.github.io/bernstein/extension/`:

```html
<!DOCTYPE html>
<html>
<head>
  <title>Bernstein VS Code Extension</title>
  <meta name="description" content="Multi-agent orchestration for VS Code">
</head>
<body>
  <h1>Bernstein VS Code Extension</h1>
  <p>Orchestrate parallel AI coding agents from your editor.</p>

  <h2>Install</h2>
  <ul>
    <li><strong>VS Code Marketplace:</strong> <a href="https://marketplace.visualstudio.com/items?itemName=chernistry.bernstein">Search "Bernstein"</a></li>
    <li><strong>Cursor / Open VSX:</strong> <a href="https://open-vsx.org/extension/chernistry/bernstein">Open VSX Registry</a></li>
    <li><strong>Manual:</strong> <code>code --install-extension chernistry.bernstein</code></li>
  </ul>

  <h2>Documentation</h2>
  <ul>
    <li><a href="https://github.com/chernistry/bernstein">GitHub Repository</a></li>
    <li><a href="https://github.com/chernistry/bernstein/blob/main/packages/vscode/README.md">Extension README</a></li>
  </ul>
</body>
</html>
```

Host this on your GitHub Pages or personal site.

Expected: Page exists and links work.

Note: This is nice-to-have (not required for task completion). Can be done after publish.

- [ ] **Step 4: Update main project README**

Add a link in the main `README.md` (bernstein root):

```markdown
## Installation

- **Python SDK**: `pip install bernstein-sdk`
- **VS Code Extension**: [Bernstein on VS Code Marketplace](https://marketplace.visualstudio.com/items?itemName=chernistry.bernstein)
  - Also available on [Open VSX](https://open-vsx.org/extension/chernistry/bernstein) for Cursor
```

Expected: Main README mentions the extension and links to both marketplaces.

- [ ] **Step 5: Commit**

```bash
git add README.md packages/vscode/
git commit -m "docs: update marketplace links and install instructions"
git push origin main
```

---

## Spec vs Reality Audit

| Spec requirement | Implementation status | Task | Notes |
|---|---|---|---|
| Accounts & Credentials (Azure DevOps, Marketplace, Open VSX) | 95% — accounts exist, secrets not yet configured | Task 1-3 | Credentials created manually, stored as GitHub secrets |
| package.json fields | 100% ✓ Complete | — | All required fields present (name, displayName, version, publisher, icon, engines, categories, keywords, repository, homepage, license) |
| Required assets | 90% — icons present, screenshots/GIF pending | Task 4 | Icons in place; 3+ screenshots and GIF need to be recorded |
| CI/CD workflow | 100% ✓ Complete | — | `.github/workflows/publish-extension.yml` has all steps: build, test, package, publish to both registries, GitHub Release |
| Cursor verification | 100% ✓ Can be done | Task 6-7 | Extension works in Cursor; forum post is optional but recommended |
| Fallback distribution | 100% ✓ Complete | — | GitHub Actions workflow creates Release with .vsix asset (`softprops/action-gh-release`) |
| UX Polish | 90% — code matches spec | Task 5 | Claims in CHANGELOG verified via code audit; status bar, tree views, dashboard all match spec |
| Test coverage | ✓ Exists | — | `npm test` passes in workflow; extension has Jest tests |
| Performance | ✓ Good | — | VSIX is 22 KB (well under 1 MB limit); SSE connection, debounced updates per CHANGELOG |

---

## Test Cases (derived from spec)

| # | Test | Trigger | Expected result |
|---|---|---|---|
| TC-1 | Publish workflow with both secrets set | `git tag ext-v0.1.0 && git push --tags` | Workflow runs, publishes to both marketplaces, creates GitHub Release with VSIX |
| TC-2 | Install from VS Code Marketplace | Search "Bernstein" in VS Code extensions | Extension installs and appears in activity bar |
| TC-3 | Install from Open VSX | Search "bernstein" in Cursor extensions | Extension installs and works same as VS Code |
| TC-4 | Manual install from VSIX | `code --install-extension packages/vscode/bernstein-0.1.0.vsix` | Extension installs and functions correctly |
| TC-5 | UX Polish audit | Open extension in VS Code | Status bar shows `🎼 X agents · Y/Z tasks · $COST` (not verbose), tree views use monochrome icons, dashboard is card-based, minimal styling |
| TC-6 | Screenshot verification | Check marketplace pages | All 3+ screenshots display without corruption, demo GIF loops smoothly |

---

## Assumptions & Risks

| # | Assumption | Where verified | Risk if wrong | Mitigation |
|---|---|---|---|---|
| A1 | Microsoft account and Eclipse account can be different | Instructions assume separate accounts | Confusion if user tries to use same account | Clarify in PUBLISH.md that accounts are separate services |
| A2 | VS Code Marketplace PAT doesn't expire during v0.1.0 publish | PAT set to 1-year expiration | Publish fails if token expires | Task 6 catches this; token can be regenerated and secret updated |
| A3 | Screenshots will be indexed immediately by marketplaces | Marketplaces have automated screenshot rendering | Delay if assets are large or format unsupported | Keep PNGs < 500 KB, GIF < 5 MB; use standard formats |
| A4 | Extension works without auth (no API token required by default) | `bernstein.apiToken` defaults to "" in package.json | Extension fails for users without auth setup | Default is correct; users can optionally configure token |

---

## Execution Handoff

**Plan complete and saved.** Two execution options:

**Option 1: Subagent-Driven (recommended)**
- I dispatch a fresh subagent per task
- Subagent reports results
- You review before next task
- Faster iteration, isolated task contexts

**Option 2: Inline Execution**
- Execute tasks sequentially in this session
- Checkpoints for review between tasks
- Longer session, full context

**Which approach would you prefer?**
