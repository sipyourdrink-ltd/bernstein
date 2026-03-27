# Extension Publishing & UX Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the Bernstein VS Code extension to VS Code Marketplace and Open VSX, verify it works in Cursor, and ensure all UX polish meets "decision-grade quiet command" standards.

**Architecture:** The extension publishing pipeline uses three distribution channels:
1. **VS Code Marketplace** (primary) — via GitHub Actions + vsce publish + VS_MARKETPLACE_TOKEN
2. **Open VSX** (Cursor/VSCodium/Gitpod) — via GitHub Actions + ovsx publish + OPEN_VSX_TOKEN
3. **GitHub Releases** (fallback) — direct VSIX download and manual install

All channels trigger automatically on `git tag ext-v*` push. The plan focuses on: (1) creating marketplace-ready visual assets (screenshots, icon variants), (2) configuring GitHub secrets, (3) performing initial release and verification, (4) ensuring extension UX meets specification.

**Tech Stack:** Node 20, TypeScript, VS Code Extension API, GitHub Actions, vsce, ovsx, Cursor IDE

---

## File Structure

**Existing structure:**
```
packages/vscode/
├── src/
│   ├── extension.ts        ✓ Already complete
│   ├── BernsteinClient.ts  ✓ Already complete
│   ├── DashboardProvider.ts ✓ Already polished
│   ├── StatusBarManager.ts ✓ Already minimal/clean
│   ├── AgentTreeProvider.ts ✓ Already complete
│   ├── TaskTreeProvider.ts  ✓ Already complete
│   ├── commands.ts         ✓ Already complete
│   └── ...                 ✓ All implementation done
├── media/
│   ├── bernstein-icon.png  ✓ Exists (128x128+)
│   ├── bernstein-icon.svg  ✓ Exists
│   ├── screenshot-1.png    ✗ MISSING — Agents panel in action
│   ├── screenshot-2.png    ✗ MISSING — Tasks panel + dashboard
│   ├── screenshot-3.png    ✗ MISSING — Status bar + real-time monitoring
│   └── demo.gif            ✗ MISSING — Extension workflow demo
├── package.json            ✓ Already correct
├── README.md               ✓ Already polished
├── CHANGELOG.md            ✓ Already detailed
├── tsconfig.json           ✓ Already correct
├── esbuild.mjs             ✓ Already correct
├── jest.config.js          ✓ Already correct
└── .vscodeignore           ✓ Already correct

.github/
└── workflows/
    └── publish-extension.yml  ✓ Already complete (triggers on ext-v* tag)
```

**Changes needed:**
1. Create 4 screenshot PNG files in `packages/vscode/media/`
2. Add screenshot descriptions to extension `package.json` (optional but recommended)
3. Configure GitHub secrets in repo settings
4. Create initial release tag `ext-v0.1.0` to trigger CI/CD
5. Verify publication on both marketplaces
6. Create website landing page (outside extension repo)

---

## Task Breakdown

### Task 1: Create Agent Panel Screenshot

**Files:**
- Create: `packages/vscode/media/screenshot-1.png`

**Context:** This screenshot shows the Agents tree view — the primary way users monitor their agent team. Must show:
- Multiple agents in different states (running/idle)
- Status indicator (● for active, ○ for idle)
- Model name (sonnet, flash, etc.)
- Estimated cost per agent
- Time elapsed
- Clean, monochrome visual hierarchy

**Steps:**

- [ ] **Step 1: Set up test Bernstein instance locally**

Run in the main bernstein directory:
```bash
uv run python scripts/run_tests.py -x  # ensure tests pass
# Then in another terminal:
python -m bernstein.cli.run_cmd --goal "test goal" &
```

This starts the orchestrator on localhost:8052.

- [ ] **Step 2: Launch VS Code with extension in development mode**

In `packages/vscode/`:
```bash
npm install          # if not done
npm run build        # compile TypeScript
code --extensionDevelopmentPath=$(pwd) ..
```

VS Code opens with the extension loaded in dev mode.

- [ ] **Step 3: Trigger agent spawning**

In the Bernstein instance (running in background), create some tasks:
```bash
curl -X POST http://127.0.0.1:8052/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Write a test function",
    "model": "sonnet",
    "role": "backend"
  }'
```

Repeat 2-3 times with different roles (backend, qa, docs) to get multiple agents.

- [ ] **Step 4: Wait for agents to appear in tree view**

In VS Code, navigate to the Bernstein panel (activity bar icon). The Agents tree should populate with the spawned agents.

- [ ] **Step 5: Capture high-quality screenshot**

- Maximize the Bernstein panel to fill most of the sidebar
- Ensure light text on dark background (use "Welcome" tab to show VS Code dark theme)
- Capture the Agents tree with 3-4 agents visible, showing:
  - ● agent-abc sonnet $0.12 2m (running)
  - ● agent-def flash $0.08 1m (running)
  - ○ agent-ghi flash idle (waiting for task)

Screenshot dimensions: 1920x1080 or higher
Format: PNG
Save as: `packages/vscode/media/screenshot-1.png`

Tool recommendation: Use `screencapture -i` on macOS, `gnome-screenshot` on Linux, or Snip & Sketch on Windows.

- [ ] **Step 6: Commit**

```bash
git add packages/vscode/media/screenshot-1.png
git commit -m "feat: add agents panel screenshot for marketplace"
```

---

### Task 2: Create Tasks Panel + Dashboard Screenshot

**Files:**
- Create: `packages/vscode/media/screenshot-2.png`

**Context:** This screenshot shows the Tasks tree view and the Dashboard overview together. Demonstrates:
- Task states (open, running, done, failed) with visual grouping
- Task assignment to agents
- Dashboard stats: agent count, task completion %, success rate, total cost
- Clean stat card layout
- Real-time data reflection

**Steps:**

- [ ] **Step 1: Keep Bernstein instance running with agents active**

(Continue from Task 1 — agents should still be running in the background.)

- [ ] **Step 2: Create more tasks to show task states**

```bash
for i in {1..5}; do
  curl -X POST http://127.0.0.1:8052/tasks \
    -H "Content-Type: application/json" \
    -d "{\"goal\": \"Task $i\", \"model\": \"sonnet\", \"role\": \"backend\"}"
  sleep 1
done
```

- [ ] **Step 3: Open the Dashboard webview**

In VS Code, right-click on the Bernstein panel → "Bernstein: Show Dashboard" or use command palette.

- [ ] **Step 4: Arrange for good dashboard screenshot**

The dashboard should show:
- 4 stat cards (top row):
  - Active Agents: 3
  - Tasks Complete: 2/7
  - Success Rate: 100%
  - Total Cost: $0.42

- Alerts section (if any): empty or minimal

- [ ] **Step 5: Capture side-by-side view**

Ideally, split the VS Code window:
- Left: Tasks tree (showing grouped tasks: open, running, done)
- Right: Dashboard webview (showing stats)

If not possible, take two separate screenshots and select the cleaner one.

Screenshot dimensions: 1920x1080 or higher
Format: PNG
Save as: `packages/vscode/media/screenshot-2.png`

- [ ] **Step 6: Commit**

```bash
git add packages/vscode/media/screenshot-2.png
git commit -m "feat: add tasks & dashboard screenshot for marketplace"
```

---

### Task 3: Create Status Bar + Real-Time Monitoring Screenshot

**Files:**
- Create: `packages/vscode/media/screenshot-3.png`

**Context:** This screenshot emphasizes:
- Clean status bar at bottom showing "● 3 agents · 7/12 tasks · $0.42"
- Real-time responsiveness (all panels showing live data)
- Status dots (● for active, ○ for idle) — no emoji
- VS Code dark theme integration

**Steps:**

- [ ] **Step 1: Ensure agents and tasks are running**

Keep the previous setup from Tasks 1 & 2 running.

- [ ] **Step 2: Open VS Code to show full interface**

Show:
- Bernstein activity bar icon (active)
- Agents tree on the left (populated)
- Main editor area (with Welcome tab)
- Status bar at the bottom showing the Bernstein indicator

- [ ] **Step 3: Arrange VS Code layout**

Full screen, no split panes, just the sidebar and main editor. The focus should be on the status bar at the bottom-left: `● 3 agents · 7/12 tasks · $0.42`

- [ ] **Step 4: Capture screenshot**

Screenshot dimensions: 1920x1080 or higher
Format: PNG
Save as: `packages/vscode/media/screenshot-3.png`

Key detail: The status bar should be clearly visible and readable.

- [ ] **Step 5: Commit**

```bash
git add packages/vscode/media/screenshot-3.png
git commit -m "feat: add status bar screenshot for marketplace"
```

---

### Task 4: Create Demo GIF

**Files:**
- Create: `packages/vscode/media/demo.gif`

**Context:** A short animated GIF (5-10 seconds) showing the extension in action:
1. Opening Bernstein panel → shows agents spawning
2. Tasks appearing in real-time
3. Dashboard updating with live cost/stats
4. Status bar updating
5. (Optional) clicking a task to show output

Tone: Professional, quiet, no dramatic effects — just clear, minimal interaction.

**Steps:**

- [ ] **Step 1: Clear tasks and restart Bernstein clean**

Kill the background Bernstein instance:
```bash
pkill -f "bernstein.*run_cmd"
```

Restart fresh:
```bash
python -m bernstein.cli.run_cmd --goal "test workflow" &
sleep 5
```

- [ ] **Step 2: Set up recording tool**

Recommendation: Use **ScreenFlow** (macOS), **OBS Studio** (cross-platform), or **SimpleScreenRecorder** (Linux).

For macOS with ScreenFlow:
```bash
# Record the next 30 seconds of screen activity
```

- [ ] **Step 3: Perform demo sequence**

Timing: Record a 30-second sequence:
- **T=0-5s**: Show VS Code with extension loaded, Bernstein panel empty ("Not connected")
- **T=5-10s**: Start Bernstein server, watch Agents tree populate
- **T=10-15s**: Create 2-3 tasks via curl
- **T=15-25s**: Watch Tasks tree and Dashboard update in real-time
- **T=25-30s**: Click an agent to show output channel

- [ ] **Step 4: Export as GIF**

Use ffmpeg or built-in export:
```bash
# With ffmpeg:
ffmpeg -i demo.mp4 -vf "fps=10,scale=960:-1:flags=lanczos" demo.gif

# Or use online tool: ezgif.com
```

File size: Keep GIF under 5MB (marketplace requirement).
Dimensions: 1920x1080 or 1280x720
Format: GIF (animated)
Save as: `packages/vscode/media/demo.gif`

- [ ] **Step 5: Commit**

```bash
git add packages/vscode/media/demo.gif
git commit -m "feat: add demo GIF for marketplace hero"
```

---

### Task 5: Verify Icon Assets and Add Marketplace Metadata

**Files:**
- Modify: `packages/vscode/package.json` (optional enhancements)
- Verify: `packages/vscode/media/bernstein-icon.png`

**Context:** VS Code Marketplace requires:
- Icon (128x128, PNG or SVG) ✓ Already exists
- Screenshots (at least 3) ✓ Just created
- README ✓ Already complete
- CHANGELOG ✓ Already complete

Optional: Add "galleryBanner" metadata for a custom marketplace banner.

**Steps:**

- [ ] **Step 1: Verify icon dimensions**

```bash
file packages/vscode/media/bernstein-icon.png
# Expected output: ... PNG image data ... 128 x 128 pixels (or larger)
```

If smaller than 128x128, resize:
```bash
# macOS:
sips -z 256 256 packages/vscode/media/bernstein-icon.png

# Or use ImageMagick:
convert bernstein-icon.png -resize 256x256 bernstein-icon-256.png
```

- [ ] **Step 2: Check that package.json has icon reference**

Verify that `packages/vscode/package.json` line 10 reads:
```json
"icon": "media/bernstein-icon.png",
```

✓ Already present.

- [ ] **Step 3: (Optional) Add marketplace banner**

In `packages/vscode/package.json`, add after the icon line:
```json
"galleryBanner": {
  "color": "#ffffff",
  "theme": "light"
}
```

But this is optional. Skip if you prefer the default look.

- [ ] **Step 4: Verify all metadata is complete**

Check package.json has:
- `name` ✓
- `displayName` ✓
- `description` ✓
- `version` = "0.1.0" ✓
- `publisher` = "chernistry" ✓
- `icon` ✓
- `license` = "Apache-2.0" ✓
- `repository` ✓
- `homepage` ✓
- `keywords` ✓
- `categories` ✓
- `engines.vscode` ✓

All present.

- [ ] **Step 5: Verify screenshots are referenced correctly**

While marketplace auto-discovers `.png` files in media/, best practice is to document them. In the README or a SCREENSHOTS section, note:
```markdown
## Marketplace Screenshots
- `screenshot-1.png` — Agents panel showing team composition and cost
- `screenshot-2.png` — Tasks panel and dashboard with real-time stats
- `screenshot-3.png` — Status bar integration in VS Code
- `demo.gif` — Live monitoring in action
```

(Optional — marketplace will find them automatically.)

- [ ] **Step 6: Commit if you made changes**

```bash
git add packages/vscode/package.json
git commit -m "chore: verify marketplace metadata and icon"
```

---

### Task 6: Configure GitHub Secrets for Publishing

**Files:**
- GitHub repo settings (not a code file, but critical infrastructure)

**Context:** The CI/CD workflow at `.github/workflows/publish-extension.yml` reads two secrets:
1. `VS_MARKETPLACE_TOKEN` — PAT from Azure DevOps for VS Code Marketplace
2. `OPEN_VSX_TOKEN` — access token from Eclipse Open VSX

Without these secrets, the publish steps will be skipped (no error, just not published).

**Steps:**

- [ ] **Step 1: Create Azure DevOps Personal Access Token**

1. Go to https://dev.azure.com
2. Sign in or create account
3. Click **User settings** (bottom-left icon)
4. Select **Personal access tokens**
5. Click **+ New Token**
6. Fill in:
   - **Name**: "Bernstein VS Code Marketplace"
   - **Organization**: "All accessible organizations"
   - **Scopes**: Expand "Marketplace" → check "Manage"
7. Click **Create**
8. Copy the token (it will never be shown again)

Expected format: Long alphanumeric string like `dsfsdfsdfsdfsdfsdfsdfsdfsdf123456`

- [ ] **Step 2: Create Open VSX Access Token**

1. Go to https://open-vsx.org
2. Sign in (create Eclipse account if needed)
3. Click your profile → **Settings**
4. Find "Access Tokens" section
5. Click **Generate**
6. Copy the token

Expected format: Similar alphanumeric string.

- [ ] **Step 3: Add secrets to GitHub repository**

1. Go to your GitHub repo: https://github.com/chernistry/bernstein
2. Settings → **Secrets and variables** → **Actions**
3. Click **New repository secret**
4. Name: `VS_MARKETPLACE_TOKEN`
5. Value: Paste the Azure DevOps PAT from Step 1
6. Click **Add secret**

Repeat for second secret:
7. Click **New repository secret**
8. Name: `OPEN_VSX_TOKEN`
9. Value: Paste the Open VSX token from Step 2
10. Click **Add secret**

- [ ] **Step 4: Verify secrets are listed**

On the **Secrets** page, you should see both secrets listed (values hidden for security):
```
VS_MARKETPLACE_TOKEN — Last used: never
OPEN_VSX_TOKEN — Last used: never
```

(They will say "Last used: never" until the first publish.)

- [ ] **Step 5: No commit needed**

GitHub secrets are stored in GitHub's secure vault, not in the repo. No files to commit.

**Note:** Do NOT commit PATs or tokens to the repo. The CI/CD workflow retrieves them from secrets at runtime.

---

### Task 7: Create Initial Release Tag and Publish

**Files:**
- No new files (CI/CD is automated)
- Git tag: `ext-v0.1.0`

**Context:** The CI/CD workflow is triggered by git tags matching `ext-v*`. Creating the tag automatically:
1. Builds the extension (TypeScript → JavaScript)
2. Runs tests
3. Packages to VSIX
4. Publishes to VS Code Marketplace (if token is set)
5. Publishes to Open VSX (if token is set)
6. Creates GitHub Release with VSIX attached

**Steps:**

- [ ] **Step 1: Ensure all changes are committed**

```bash
git status
# Expected: "nothing to commit, working tree clean"
```

If there are uncommitted changes, commit them:
```bash
git add -A
git commit -m "chore: extension publishing preparation"
```

- [ ] **Step 2: Create the release tag**

```bash
git tag -a ext-v0.1.0 -m "Initial VS Code extension release"
```

The `-a` flag creates an annotated tag (recommended for releases).

Expected output: None (silent success).

- [ ] **Step 3: Verify tag was created**

```bash
git tag -l | grep ext-v0
# Expected output: ext-v0.1.0
```

- [ ] **Step 4: Push the tag to GitHub**

```bash
git push origin ext-v0.1.0
```

This triggers the GitHub Actions workflow.

- [ ] **Step 5: Monitor the GitHub Actions workflow**

1. Go to https://github.com/chernistry/bernstein/actions
2. You should see a new workflow run: **Publish VS Code Extension**
3. Wait for it to complete (usually 2-5 minutes)

Workflow steps should show:
```
✓ Checkout
✓ Setup Node.js
✓ Install dependencies
✓ Type check
✓ Run tests
✓ Build extension
✓ Package extension
✓ Publish to VS Code Marketplace (if VS_MARKETPLACE_TOKEN set)
✓ Publish to Open VSX (if OPEN_VSX_TOKEN set)
✓ Create GitHub Release with VSIX
```

If any step fails, check the logs:
- Type/test failures: Fix in code, commit, re-tag with `ext-v0.1.1`
- Secret missing: Add secret to GitHub Settings, re-tag with `ext-v0.1.1`

- [ ] **Step 6: Verify publication on VS Code Marketplace**

1. Go to https://marketplace.visualstudio.com/search?term=bernstein
2. Look for: "Bernstein — Multi-Agent Orchestration" by publisher "chernistry"
3. Verify the icon, description, screenshots, and CHANGELOG are displayed correctly

If not visible: Marketplace syncs within 5-15 minutes. Wait and refresh.

- [ ] **Step 7: Verify publication on Open VSX**

1. Go to https://open-vsx.org/extension/chernistry/bernstein
2. Verify the extension page exists
3. Check that README, version, and publisher are correct

- [ ] **Step 8: Verify GitHub Release**

1. Go to https://github.com/chernistry/bernstein/releases
2. Find the release "Initial VS Code extension release" (or the tag name)
3. Verify VSIX file is attached and downloadable

**Troubleshooting:**

If publish failed:
- **"Registry authentication failed"** → PAT/token wrong or expired → regenerate and re-add to GitHub secrets → re-tag with next version
- **"Extension validation failed"** → package.json has invalid field → fix → commit → re-tag
- **"File not found: bernstein-*.vsix"** → build step failed → check test/compile logs → fix → re-tag

---

### Task 8: Verify Installation in Cursor and Post to Forum

**Files:**
- No code changes (verification only)
- Forum post: https://forum.cursor.com (external, not tracked here)

**Context:** Cursor users install VS Code extensions from the Open VSX registry. Before claiming success, verify:
1. Extension installs and activates in Cursor
2. Extension connects to local Bernstein server
3. All UI elements appear correctly
4. Forum verification post is accepted

**Steps:**

- [ ] **Step 1: Install extension in Cursor**

1. Open Cursor
2. Go to Extensions (Cmd+Shift+X on macOS, Ctrl+Shift+X on Linux/Windows)
3. Search for "bernstein"
4. Click the result: "Bernstein — Multi-Agent Orchestration" by chernistry
5. Click **Install**

Expected: Extension installs and activates automatically.

- [ ] **Step 2: Verify extension activates**

In Cursor:
1. Open the Extensions panel and find Bernstein
2. Status should show "Installed" (not "Install" button)
3. You may see a note: "Extension contributed code completions" or similar (optional)

- [ ] **Step 3: Test extension connectivity**

1. Open a terminal in Cursor
2. Start Bernstein server: `python -m bernstein.cli.run_cmd --goal "test"`
3. In Cursor, look for the Bernstein icon in the activity bar (left sidebar)
4. Click it → the Agents tree should appear
5. After a few seconds, agents should populate as tasks are assigned

If agents don't appear:
- Check that `http://127.0.0.1:8052` is running: `curl http://127.0.0.1:8052/status`
- Verify Bernstein server logs for errors
- Check Cursor's Output panel (View → Output → select "Bernstein") for extension logs

- [ ] **Step 4: Verify dashboard and status bar**

In Cursor:
1. Click **Bernstein: Show Dashboard** from command palette
2. Dashboard webview should appear showing stats, agents, tasks
3. Look at the bottom status bar → should show: `● N agents · M/K tasks · $X.XX`
4. Click status bar → dashboard should open (or focus if already open)

If dashboard doesn't work:
- Check that `python -m bernstein.cli.run_cmd` is still running
- The webview should auto-reconnect after Bernstein comes back online

- [ ] **Step 5: Verify in both light and dark themes**

1. In Cursor: Settings → search "theme"
2. Try "Cursor Light" and "Cursor Dark"
3. Verify that:
   - Icons and text are readable in both themes
   - Status bar indicator is visible (● or ○)
   - Dashboard colors adapt correctly

Expected: Extension respects VS Code theme variables.

- [ ] **Step 6: Post verification to Cursor forum**

1. Go to https://forum.cursor.com
2. Find or create a post in the appropriate category (likely "Extensions" or "Integrations")
3. Create a post titled: **"Bernstein Multi-Agent Orchestration Extension Published"**
4. Body:
   ```markdown
   ## Bernstein VS Code Extension

   The **Bernstein multi-agent orchestration extension** is now published and available in Open VSX.

   **Install:** Search for "bernstein" in Cursor extensions, or click [here](https://open-vsx.org/extension/chernistry/bernstein)

   **Publisher ID:** chernistry
   **Extension ID:** bernstein
   **Repository:** https://github.com/chernistry/bernstein

   Features:
   - Real-time agent monitoring (Agents panel)
   - Task tracking and assignment (Tasks panel)
   - Live dashboard with cost tracking
   - Status bar integration
   - Output channel for agent logs

   **Verification:** This post serves as publisher verification for the Cursor community.
   ```

5. Click **Post**
6. Note the post URL for reference

- [ ] **Step 7: No commit needed**

The forum post is external documentation, not tracked in the repo.

---

### Task 9: Create Website Landing Page

**Files:**
- Create: `docs/extension-landing.md` (in repo, for documentation)
- External: `alexchernysh.com/bernstein/extension` (out of scope for this plan, but documented here)

**Context:** The task requirements mention hosting an extension page at `alexchernysh.com/bernstein/extension`. This is documentation/marketing material, not extension code. It should:
- Explain why use the extension
- Show installation instructions
- Link to Open VSX and VS Code Marketplace
- Provide Discord/forum links for support

This can be a simple static HTML page or markdown that your website builder converts to HTML.

**Steps:**

- [ ] **Step 1: Create minimal landing page markdown**

In the bernstein repo, create `docs/extension-landing.md`:

```markdown
# Bernstein VS Code Extension

Deploy and monitor AI coding agents from your editor.

## Install

- **VS Code / Cursor:** Search "bernstein" in extensions, or [visit marketplace](https://open-vsx.org/extension/chernistry/bernstein)
- **Manual:** Download the [VSIX](https://github.com/chernistry/bernstein/releases) and install via Extensions panel

## What it does

- **Real-time monitoring** — see all agents and tasks in a tree view
- **Dashboard** — cost, progress, and success metrics at a glance
- **Status bar** — live stats in the bottom status bar
- **Agent control** — kill agents, inspect logs, view output
- **Auto-connect** — automatically finds the Bernstein server

## Requirements

- VS Code 1.100+ (or Cursor, VSCodium, etc.)
- Bernstein orchestrator running locally (`bernstein run`)

## Links

- [GitHub repository](https://github.com/chernistry/bernstein)
- [VS Code Marketplace](https://marketplace.visualstudio.com/items?itemName=chernistry.bernstein)
- [Open VSX Registry](https://open-vsx.org/extension/chernistry/bernstein)
- [Documentation](https://chernistry.github.io/bernstein/)
```

Save this as reference documentation in the repo.

- [ ] **Step 2: Deploy to your website**

On your website builder (assuming you have one at alexchernysh.com):
1. Create a new page or section at `/bernstein/extension`
2. Copy the content from the markdown above
3. Add the Bernstein icon or a screenshot
4. Publish

If you don't have a website builder set up:
- Option A: Create a GitHub Pages site in the bernstein repo: `docs/extension/index.html`
- Option B: Skip this step (not critical for marketplace publication; it's optional marketing)

- [ ] **Step 3: (Optional) Add to main docs navigation**

If you have a bernstein docs site at chernistry.github.io/bernstein/:
1. Add a link to the extension landing page in the main navigation
2. Cross-reference from the main README

- [ ] **Step 4: Commit docs**

```bash
git add docs/extension-landing.md
git commit -m "docs: add extension landing page template"
```

---

## Validation Checklist

Before marking the task complete, verify:

- [ ] **Screenshots created and committed**
  - [ ] `packages/vscode/media/screenshot-1.png` (Agents panel)
  - [ ] `packages/vscode/media/screenshot-2.png` (Tasks + Dashboard)
  - [ ] `packages/vscode/media/screenshot-3.png` (Status bar)
  - [ ] `packages/vscode/media/demo.gif` (demo animation)

- [ ] **GitHub secrets configured**
  - [ ] `VS_MARKETPLACE_TOKEN` added to GitHub repo secrets
  - [ ] `OPEN_VSX_TOKEN` added to GitHub repo secrets

- [ ] **Initial release published**
  - [ ] Tag `ext-v0.1.0` created and pushed
  - [ ] GitHub Actions workflow completed successfully
  - [ ] Extension visible on VS Code Marketplace
  - [ ] Extension visible on Open VSX
  - [ ] GitHub Release created with VSIX asset

- [ ] **Verification complete**
  - [ ] Extension installs in Cursor without errors
  - [ ] Extension connects to local Bernstein server
  - [ ] All UI elements (Agents, Tasks, Dashboard, Status bar) function correctly
  - [ ] Both light and dark themes display correctly
  - [ ] Forum post created for verification

- [ ] **Documentation**
  - [ ] README.md in extension is complete and accurate
  - [ ] CHANGELOG.md documents the 0.1.0 release
  - [ ] Extension landing page created (optional but recommended)

---

## Spec vs Reality Audit

**Initial audit (2026-03-29):**

| # | Finding | Severity | Status | Resolution |
|---|---------|----------|--------|------------|
| 1 | Screenshots missing from media/ | High | Found during planning | Create 4 visual assets per Tasks 1-4 |
| 2 | GitHub secrets not configured | High | Confirmed needed | Add VS_MARKETPLACE_TOKEN and OPEN_VSX_TOKEN per Task 6 |
| 3 | No initial release tag created | Critical | Confirmed needed | Create ext-v0.1.0 tag per Task 7 |
| 4 | Extension code already polished | Low | Verified | No code changes needed; UX standards already met |
| 5 | CI/CD workflow already in place | Low | Verified | No changes to publish-extension.yml needed |
| 6 | Website landing page optional | Low | Decision deferred | Create if time permits; not critical for marketplace |

---

## Assumptions

| # | Assumption | Verification | Risk |
|---|-----------|--------------|------|
| A1 | GitHub repo exists at chernistry/bernstein | Manual check: https://github.com/chernistry/bernstein | Low |
| A2 | Azure DevOps account available for PAT generation | You have a Microsoft/Azure account | Medium — if not, create one first |
| A3 | Eclipse account available for Open VSX token | You have an Eclipse account or can create one | Medium — if not, create one first |
| A4 | Bernstein server can be run locally for screenshot testing | Verified in CLAUDE.md and earlier tasks | Low |
| A5 | Cursor IDE is installed for verification step | Not automatically assumed | Medium — install from https://cursor.sh if needed |
| A6 | Publisher ID "chernistry" is already registered on both marketplaces | Assumed from package.json | High — if not, register before Task 6 |

---

## Test Cases Derived from Plan

| Test | Trigger | Expected Behavior | Owner |
|------|---------|------------------|-------|
| TC-01: Screenshots in media | Verify files exist | 4 PNG/GIF files in `media/` | Task 1-4 |
| TC-02: GitHub Actions trigger | Push tag `ext-v0.1.0` | Workflow runs, publishes to both marketplaces | Task 7 |
| TC-03: VS Code Marketplace listing | Search "bernstein" on marketplace | Extension visible with icon, screenshots, README | Task 7 |
| TC-04: Open VSX listing | Visit open-vsx.org link | Extension visible with correct metadata | Task 7 |
| TC-05: Cursor installation | Search "bernstein" in Cursor extensions | Install succeeds, extension activates | Task 8 |
| TC-06: Local connection test | Run Bernstein server, open Cursor | Extension auto-connects, agents/tasks populate | Task 8 |
| TC-07: UI theme support | Toggle light/dark theme in Cursor | Status bar and dashboard adapt correctly | Task 8 |
| TC-08: GitHub Release | Visit Releases page | VSIX file downloadable, release notes present | Task 7 |

---

## Open Questions

- Should the website landing page be at alexchernysh.com or on GitHub Pages? (Task 9 defers decision)
- Do you want a Discord invite in the extension description? (Not currently included; add if desired)
- Should there be a changelog entry for 0.1.1 or beyond? (Start with 0.1.0; increment as needed)

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-03-29-extension-publish.md`.

**Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, with review checkpoints between tasks. Good for catching issues early and iterating quickly.

**2. Inline Execution** — Execute all tasks in this session using executing-plans skill, with checkpoint reviews. Faster if you're confident, but harder to pause and iterate.

**Which approach would you prefer?**

(Once you choose, I'll use the appropriate skill to begin implementation.)
