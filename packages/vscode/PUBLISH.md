# Publishing the Bernstein VS Code Extension

## Prerequisites

### 1. Marketplace Accounts

#### VS Code Marketplace
1. Go to [marketplace.visualstudio.com](https://marketplace.visualstudio.com)
2. Sign in with a Microsoft account
3. Click on your profile → Publish extensions
4. Create a publisher (use: `chernistry`)

#### Open VSX Registry
1. Go to [open-vsx.org](https://open-vsx.org)
2. Sign in with Eclipse identity or create new account
3. Click "Publish Extension"
4. Create a namespace (use: `chernistry`)

### 2. Personal Access Tokens (PATs)

#### VS Code Marketplace PAT
1. Go to [dev.azure.com](https://dev.azure.com)
2. Create an organization or use existing
3. Go to Personal access tokens → New token
4. Configure:
   - **Name**: `vsce-publish`
   - **Organization**: All accessible organizations
   - **Scopes**: Marketplace → Manage
5. Copy the token and save securely

#### Open VSX PAT
1. Log in to [open-vsx.org](https://open-vsx.org)
2. Go to Settings → Access Tokens
3. Create new token with name `extension-publish`
4. Copy and save securely

### 3. GitHub Secrets

Add the following secrets to your GitHub repository:

```bash
gh secret set VS_MARKETPLACE_TOKEN --body "YOUR_VS_CODE_MARKETPLACE_PAT"
gh secret set OPEN_VSX_TOKEN --body "YOUR_OPEN_VSX_PAT"
```

Or via GitHub UI:
1. Go to Settings → Secrets and variables → Actions
2. Create `VS_MARKETPLACE_TOKEN` with VS Code Marketplace token
3. Create `OPEN_VSX_TOKEN` with Open VSX token

## Publishing a Release

### Step 1: Update Version
Edit `packages/vscode/package.json`:
```json
{
  "version": "0.2.0"
}
```

### Step 2: Update CHANGELOG
Add entry to `packages/vscode/CHANGELOG.md`:
```markdown
## [0.2.0] - 2026-MM-DD

### Added
- Feature description

### Fixed
- Bug fix description
```

### Step 3: Commit & Tag
```bash
git add packages/vscode/
git commit -m "chore(ext): bump to 0.2.0"
git tag ext-v0.2.0
git push origin main --tags
```

**Important**: Use tag prefix `ext-v*` to trigger the publish workflow.

### Step 4: Verify Publication

The GitHub Actions workflow will automatically:
1. Install dependencies
2. Run type check and tests
3. Build the extension
4. Package as VSIX
5. Publish to VS Code Marketplace (if `VSCE_PAT` is set)
6. Publish to Open VSX (if `OVSX_PAT` is set)
7. Upload VSIX as release artifact

Monitor the workflow in GitHub Actions tab.

## Manual Publishing (if needed)

If the automated workflow fails or you need to publish manually:

```bash
cd packages/vscode

# Build
npm run compile

# Package
npm run package

# Publish to VS Code Marketplace
VSCE_PAT=YOUR_TOKEN npm run publish:vscode

# Publish to Open VSX
OVSX_PAT=YOUR_TOKEN npm run publish:ovsx
```

## Verification

### VS Code Marketplace
- Search for "Bernstein" on [marketplace.visualstudio.com](https://marketplace.visualstudio.com)
- Check that:
  - Display name is "Bernstein — Multi-Agent Orchestration"
  - Description shows "Orchestrate parallel AI coding agents..."
  - Icon appears correctly
  - README displays properly
  - CHANGELOG is visible

### Open VSX
- Search for "bernstein" on [open-vsx.org](https://open-vsx.org)
- Verify same content as VS Code Marketplace

### Cursor
- Launch Cursor
- Open Extensions panel (`Cmd+Shift+X` on Mac)
- Search for "Bernstein"
- Install from marketplace
- Verify extension loads without errors

## Troubleshooting

### Authentication Failed
- Verify tokens are correct and not expired
- Check that secrets are set in GitHub repository settings
- Try token again with `vsce publish --check-only`

### Version Already Published
- Each version can only be published once
- Increment the version number in `package.json`
- Use a new tag: `ext-v0.2.1`

### VSIX Too Large
- Current size: ~19 KB (well under 1 MB limit)
- If exceeds 1 MB, review what's being included
- Update `.vscodeignore` to exclude unnecessary files

### Extension Not Appearing on Marketplace
- Wait 5-10 minutes for marketplace to index
- Verify it appears in "Recent Extensions" section
- Check for any moderation flags or approval requirements

## Marketplace Policies

- Extension must have a valid license (Apache 2.0 ✓)
- README must describe what the extension does (✓)
- Icon must be provided (✓ 128x128)
- No malicious code or telemetry
- Respect VS Code extension guidelines

## See Also

- [VS Code Publishing Documentation](https://code.visualstudio.com/api/working-with-extensions/publishing-extension)
- [Open VSX Publishing Guide](https://github.com/EclipseFoundation/open-vsx/wiki/Publishing-Extensions)
- [vsce CLI Reference](https://github.com/microsoft/vscode-vsce)
- [ovsx CLI Reference](https://github.com/eclipse/publish-extensions)
